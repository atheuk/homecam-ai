"""Per-camera ingestion leader lease: with two replicas only one ingests a
camera, and a standby takes over once the holder stops renewing."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal, init_db
from app.models.db import IngestionLease
from app.services import ingestion, ingestion_lease, scene_state
from app.services.ingestion_lease import LeaseKeeper

TTL = 30.0
CAMERA = "mock-driveway"


@pytest.fixture(autouse=True)
async def _setup(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "ingestion_lease_enabled", True)
    monkeypatch.setattr(settings, "ingestion_lease_ttl_seconds", TTL)
    ingestion.reset_cooldowns()
    yield
    ingestion.reset_cooldowns()


async def _ensure(keeper: LeaseKeeper, now: float, camera: str = CAMERA):
    async with SessionLocal() as session:
        return await keeper.ensure(session, camera, now=now)


async def _holder(camera: str = CAMERA) -> str | None:
    async with SessionLocal() as session:
        row = await session.get(IngestionLease, camera)
        return row.holder if row else None


async def test_only_one_of_two_replicas_holds_a_camera():
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
    assert (await _ensure(a, 1000.0)).held
    assert not (await _ensure(b, 1000.5)).held
    assert not (await _ensure(b, 1000.0 + TTL - 1)).held
    assert await _holder() == "replica-a"


async def test_a_renewing_holder_keeps_the_camera():
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
    for now in (1000.0, 1010.0, 1020.0, 1030.0, 1040.0, 1050.0):
        assert (await _ensure(a, now)).held
        assert not (await _ensure(b, now + 0.5)).held
    assert await _holder() == "replica-a"


async def test_standby_takes_over_after_the_ttl():
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
    assert (await _ensure(a, 1000.0)).held
    # Replica A dies: no more renewals.
    assert not (await _ensure(b, 1000.0 + TTL - 0.1)).held
    taken = await _ensure(b, 1000.0 + TTL + 0.1)
    assert taken.held and taken.changed
    assert await _holder() == "replica-b"
    # A comes back and finds it has lost the camera.
    back = await _ensure(a, 1000.0 + TTL + 1)
    assert not back.held and back.changed


async def test_a_clean_shutdown_hands_over_immediately():
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
    assert (await _ensure(a, 1000.0)).held
    async with SessionLocal() as session:
        await a.release_all(session)
    assert (await _ensure(b, 1001.0)).held


async def test_two_ingesting_replicas_have_one_active_ingester_per_camera(monkeypatch):
    """Drive ``poll_once`` as two replicas sharing one database. Each records
    the cameras it actually samples; every camera has exactly one ingester,
    until that ingester stops and the TTL passes."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(ingestion_lease, "_now", lambda: clock["now"])
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
        clock["now"] += 5
        assert await tick(b) == set()
        assert await tick(a) == first
    async with SessionLocal() as session:
        holders = set((await session.execute(select(IngestionLease.holder))).scalars())
    assert holders == {"replica-a"}

    # Replica A stops renewing. B stays on standby until the TTL lapses.
    clock["now"] += TTL - 1
    assert await tick(b) == set()
    clock["now"] += 2
    assert await tick(b) == first
    assert await tick(a) == set()


async def test_gaining_the_lease_reloads_scene_state_from_the_database(monkeypatch):
    released: list[str] = []
    monkeypatch.setattr(ingestion.stream_hub, "release", released.append)
    a, b = LeaseKeeper("replica-a"), LeaseKeeper("replica-b")
    clock = {"now": 1000.0}
    monkeypatch.setattr(ingestion_lease, "_now", lambda: clock["now"])

    monkeypatch.setattr(ingestion_lease, "keeper", b)
    async with SessionLocal() as session:
        await b.ensure(session, CAMERA)
    monkeypatch.setattr(ingestion_lease, "keeper", a)
    stale = scene_state.CameraScene(camera_id=CAMERA)
    scene_state._scenes[CAMERA] = stale
    assert not await ingestion._lead(SessionLocal, CAMERA)
    assert scene_state._scenes.get(CAMERA) is stale  # was never held: nothing to drop

    clock["now"] += TTL + 1
    assert await ingestion._lead(SessionLocal, CAMERA)
    assert CAMERA not in scene_state._scenes  # reloaded from the DB on next frame

    monkeypatch.setattr(ingestion_lease, "keeper", b)
    clock["now"] += TTL + 1
    assert await ingestion._lead(SessionLocal, CAMERA)  # b takes over from a
    monkeypatch.setattr(ingestion_lease, "keeper", a)
    scene_state._scenes[CAMERA] = stale
    assert not await ingestion._lead(SessionLocal, CAMERA)
    assert CAMERA not in scene_state._scenes
    assert released == [CAMERA]


async def test_disabled_lease_ingests_everywhere(monkeypatch):
    monkeypatch.setattr(settings, "ingestion_lease_enabled", False)
    async with SessionLocal() as session:
        await LeaseKeeper("other").ensure(session, CAMERA)
    assert await ingestion._lead(SessionLocal, CAMERA)
