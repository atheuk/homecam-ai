"""Per-camera ingestion leader lease: with two replicas only one ingests a
camera, a standby takes over once the holder stops renewing, every scene write
is fenced on the lease epoch, and expiry follows the database clock.

Time: each replica measures its renewal interval with ``_monotonic`` (a fake
clock here), and lease expiry is judged by the database clock. ``advance``
moves both: the fake clock forward and every stored ``expires_at`` back, which
to the database is the same as that much time passing.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from app.ai.detector import BoundingBox, Detection
from app.config import settings
from app.db import SessionLocal, init_db
from app.models.db import Event, IngestionLease, SceneState, VehicleTrack
from app.services import ingestion, ingestion_lease, scene_state
from app.services.ingestion_lease import LeaseKeeper, LeaseLost, LeaseToken
from app.services.scene_state import SceneTransition

TTL = 30.0
CAMERA = "mock-driveway"


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 1000.0}
    monkeypatch.setattr(ingestion_lease, "_monotonic", lambda: state["now"])
    return state


@pytest.fixture(autouse=True)
async def _setup(monkeypatch, clock):
    await init_db()
    monkeypatch.setattr(settings, "ingestion_lease_enabled", True)
    monkeypatch.setattr(settings, "ingestion_lease_ttl_seconds", TTL)
    ingestion.reset_cooldowns()
    yield
    ingestion.reset_cooldowns()


async def advance(clock, seconds: float) -> None:
    clock["now"] += seconds
    async with SessionLocal() as session:
        await session.execute(update(IngestionLease).values(expires_at=IngestionLease.expires_at - seconds))
        await session.commit()


async def _ensure(keeper: LeaseKeeper, camera: str = CAMERA):
    async with SessionLocal() as session:
        return await keeper.ensure(session, camera)


async def _row(camera: str = CAMERA) -> IngestionLease | None:
    async with SessionLocal() as session:
        return await session.get(IngestionLease, camera)


async def _fence(token: LeaseToken) -> bool:
    async with SessionLocal() as session:
        ok = await ingestion_lease.fence(session, token)
        await session.rollback()
        return ok


async def test_only_one_of_two_replicas_holds_a_camera(clock):
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
    assert (await _ensure(a)).held
    await advance(clock, 0.5)
    assert not (await _ensure(b)).held
    await advance(clock, TTL - 2)
    assert not (await _ensure(b)).held
    assert (await _row()).holder == "replica-a"


async def test_a_renewing_holder_keeps_the_camera(clock):
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
    first = await _ensure(a)
    for _ in range(6):
        await advance(clock, 10)
        held = await _ensure(a)
        assert held.held and not held.changed
        assert held.token == first.token  # renewals keep the epoch
        assert not (await _ensure(b)).held
    assert (await _row()).holder == "replica-a"


async def test_standby_takes_over_after_the_ttl(clock):
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
    assert (await _ensure(a)).held
    # Replica A dies: no more renewals.
    await advance(clock, TTL - 0.1)
    assert not (await _ensure(b)).held
    await advance(clock, 0.2)
    taken = await _ensure(b)
    assert taken.held and taken.changed
    assert (await _row()).holder == "replica-b"
    # A comes back and finds it has lost the camera.
    assert not a.held(CAMERA)
    back = await _ensure(a)
    assert not back.held and back.changed


async def test_a_clean_shutdown_hands_over_immediately(clock):
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
    assert (await _ensure(a)).held
    async with SessionLocal() as session:
        await a.release_all(session)
    await advance(clock, 1)
    assert (await _ensure(b)).held


async def test_every_acquisition_increments_the_epoch_and_stolen_epochs_are_fenced(clock):
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
    a1 = (await _ensure(a)).token
    assert a1.epoch == 1 and await _fence(a1)

    await advance(clock, TTL + 1)
    b2 = (await _ensure(b)).token
    assert b2.epoch == 2
    assert not await _fence(a1)  # holder changed
    assert await _fence(b2)

    await advance(clock, TTL + 1)
    a3 = (await _ensure(a)).token
    assert a3.epoch == 3
    # Same holder again, but the epoch-1 token belongs to a lost lease.
    assert not await _fence(a1)
    assert not await _fence(b2)
    assert await _fence(a3)
    # And an expired lease fences nothing, even before anyone takes it.
    await advance(clock, TTL + 1)
    assert not await _fence(a3)


async def test_expiry_follows_database_time_not_the_app_clock(monkeypatch):
    """Replica clocks are skewed by hours in both directions. The lease row
    still expires TTL seconds after the *database's* now, and a replica whose
    clock runs a day ahead cannot steal a fresh lease."""
    real = time.time()
    for camera, skew in (("skew-behind", -7200.0), ("skew-ahead", 7200.0)):
        monkeypatch.setattr(time, "time", lambda s=skew: real + s)
        a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
        assert (await _ensure(a, camera)).held
        row = await _row(camera)
        assert abs(row.expires_at - (real + TTL)) < 5, "expiry must be DB now + TTL"
        assert abs(row.acquired_at - real) < 5
        monkeypatch.setattr(time, "time", lambda: real + 86400.0)
        assert not (await _ensure(b, camera)).held
        assert (await _row(camera)).holder == "replica-a"


async def test_a_lease_stolen_mid_pipeline_cannot_save_scene_state(clock, monkeypatch):
    """Replica A starts a frame under epoch 1; while it is still processing
    (a slow detector or Foundry check) the lease expires and B takes it. A's
    save is rejected: no scene rows, no transitions, and its cache dropped."""
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
    token = (await _ensure(a)).token
    original_zones = scene_state._zones

    async def slow_zones(session, scene, now):
        await advance(clock, TTL + 1)
        assert (await _ensure(b)).held
        return await original_zones(session, scene, now)

    monkeypatch.setattr(scene_state, "_zones", slow_zones)
    async with SessionLocal() as session:
        before = len((await session.execute(select(SceneState))).scalars().all())
        out = await scene_state.process_frame(
            session, CAMERA, "Driveway", None,
            [Detection("car", 0.9, BoundingBox(0.55, 0.40, 0.95, 0.84))], lease=token,
        )
    assert out == []
    assert CAMERA not in scene_state._scenes
    async with SessionLocal() as session:
        assert (await session.execute(select(VehicleTrack).where(VehicleTrack.camera_id == CAMERA))).first() is None
        assert len((await session.execute(select(SceneState))).scalars().all()) == before

    # The same frame under the live lease does save.
    monkeypatch.setattr(scene_state, "_zones", original_zones)
    live = b.token(CAMERA)
    async with SessionLocal() as session:
        await scene_state.process_frame(
            session, CAMERA, "Driveway", None,
            [Detection("car", 0.9, BoundingBox(0.55, 0.40, 0.95, 0.84))], lease=live,
        )
    async with SessionLocal() as session:
        assert (await session.execute(select(VehicleTrack).where(VehicleTrack.camera_id == CAMERA))).first() is not None


async def test_a_stolen_epoch_cannot_overwrite_newer_zone_state(clock):
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
    stale = (await _ensure(a)).token
    await advance(clock, TTL + 1)
    assert (await _ensure(b)).held
    async with SessionLocal() as session:
        session.add(SceneState(id="ss-fence", camera_id=CAMERA, kind="mailbox", zone_id="z-fence", state="new", data={"by": "b"},
                                updated_at=datetime.now(timezone.utc)))
        await session.commit()

    scene = scene_state.CameraScene(camera_id=CAMERA, lease=stale)
    scene.zones["z-fence"] = scene_state.ZoneRecord(
        id="ss-fence", camera_id=CAMERA, kind="mailbox", zone_id="z-fence", state="old", data={"by": "a"}
    )
    scene.dirty_zones.add("z-fence")
    scene_state._scenes[CAMERA] = scene
    async with SessionLocal() as session:
        with pytest.raises(LeaseLost):
            await scene_state._save(session, scene)
    async with SessionLocal() as session:
        row = await session.get(SceneState, "ss-fence")
        assert (row.state, row.data) == ("new", {"by": "b"})
        await session.delete(row)
        await session.commit()
    assert CAMERA not in scene_state._scenes


async def test_events_from_a_lost_lease_are_dropped(clock, monkeypatch):
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
    token = (await _ensure(a)).token

    async def fake_process_frame(session, camera_id, camera_name, image, detections, **kwargs):
        # The scene step finished, then the lease was stolen before the
        # event transaction.
        await advance(clock, TTL + 1)
        assert (await _ensure(b)).held
        return [SceneTransition(
            camera_id=CAMERA, kind="mailbox", transition="opened", event_type="mailbox_opened",
            description="Mailbox opened", tags=["mailbox"], metadata={},
        )]

    monkeypatch.setattr(scene_state, "process_frame", fake_process_frame)
    created = await ingestion._emit_scene_transitions(
        SessionLocal, CAMERA, "Driveway", b"jpeg", [], None, "snapshot", lease=token
    )
    assert created == 0
    async with SessionLocal() as session:
        assert (await session.execute(select(Event).where(Event.type == "mailbox_opened"))).first() is None


async def test_two_ingesting_replicas_have_one_active_ingester_per_camera(monkeypatch, clock):
    """Drive ``poll_once`` as two replicas sharing one database. Each records
    the cameras it actually samples; every camera has exactly one ingester,
    until that ingester stops and the TTL passes."""
    sampled: list[tuple[str, str]] = []

    async def fake_acquire(camera_id, provider):
        sampled.append((ingestion_lease.keeper.holder, camera_id))
        return None

    monkeypatch.setattr(ingestion, "_acquire_frame", fake_acquire)
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")

    async def tick(keeper: LeaseKeeper) -> set[str]:
        monkeypatch.setattr(ingestion_lease, "keeper", keeper)
        sampled.clear()
        await ingestion.poll_once()
        assert {holder for holder, _ in sampled} <= {keeper.holder}
        return {camera for _, camera in sampled}

    first = await tick(a)
    assert first, "replica A should ingest the online cameras"
    for _ in range(3):
        await advance(clock, 5)
        assert await tick(b) == set()
        assert await tick(a) == first
    async with SessionLocal() as session:
        holders = set((await session.execute(select(IngestionLease.holder))).scalars())
    assert holders == {"replica-a"}

    # Replica A stops renewing. B stays on standby until the TTL lapses.
    await advance(clock, TTL - 1)
    assert await tick(b) == set()
    await advance(clock, 2)
    assert await tick(b) == first
    assert await tick(a) == set()


async def test_gaining_the_lease_reloads_scene_state_from_the_database(monkeypatch, clock):
    released: list[str] = []
    monkeypatch.setattr(ingestion.stream_hub, "release", released.append)
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")

    monkeypatch.setattr(ingestion_lease, "keeper", b)
    await _ensure(b)
    monkeypatch.setattr(ingestion_lease, "keeper", a)
    stale = scene_state.CameraScene(camera_id=CAMERA)
    scene_state._scenes[CAMERA] = stale
    assert await ingestion._lead(SessionLocal, CAMERA) is None
    assert scene_state._scenes.get(CAMERA) is stale  # was never held: nothing to drop

    await advance(clock, TTL + 1)
    token = await ingestion._lead(SessionLocal, CAMERA)
    assert token is not None and token.holder == "replica-a"
    assert CAMERA not in scene_state._scenes  # reloaded from the DB on next frame

    monkeypatch.setattr(ingestion_lease, "keeper", b)
    await advance(clock, TTL + 1)
    assert await ingestion._lead(SessionLocal, CAMERA) is not None  # b takes over from a
    monkeypatch.setattr(ingestion_lease, "keeper", a)
    scene_state._scenes[CAMERA] = stale
    assert await ingestion._lead(SessionLocal, CAMERA) is None
    assert CAMERA not in scene_state._scenes
    assert released == [CAMERA]


async def test_disabled_lease_ingests_everywhere(monkeypatch):
    monkeypatch.setattr(settings, "ingestion_lease_enabled", False)
    async with SessionLocal() as session:
        await LeaseKeeper("other").ensure(session, CAMERA)
    assert await ingestion._lead(SessionLocal, CAMERA) is ingestion._UNLEASED
