"""PR #22 review fixes: endpoint auth, retrievable package evidence, and
cross-replica package-removal dedup."""
import time
import uuid

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal, init_db
from app.models.db import Event, EventEvidence, Incident, SceneDedupClaim
from app.services import ingestion, scene_dedup, scene_state, security_modes
from app.services.scene_state import SceneTransition
from _auth import auth_headers

BEFORE_JPEG = b"\xff\xd8\xff\xe0before-crop\xff\xd9"
AFTER_JPEG = b"\xff\xd8\xff\xe0after-crop\xff\xd9"


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(Incident))
            await session.execute(delete(EventEvidence))
            await session.execute(delete(Event))
            await session.execute(delete(SceneDedupClaim))
            await session.commit()
            await security_modes.set_mode(session, "disarmed", None)

    await _clear()
    yield
    await _clear()


# --- 1. every new endpoint requires auth -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/search?q=package"),
        ("GET", "/api/v1/digest"),
        ("GET", "/api/v1/events/evt-x/evidence/before"),
        ("GET", "/api/v1/security/deterrence/capabilities"),
        ("GET", "/api/v1/security/deterrence/actions"),
        ("POST", "/api/v1/security/deterrence/actions"),
        ("POST", "/api/v1/security/deterrence/actions/act-x/confirm"),
        ("POST", "/api/v1/security/deterrence/actions/act-x/cancel"),
    ],
)
async def test_new_endpoints_reject_unauthenticated_requests(client, method, path):
    body = {"camera_id": "mock-front-door", "action": "light"} if method == "POST" else None
    client.cookies.clear()
    r = await client.request(method, path, json=body)
    assert r.status_code == 401, (path, r.status_code, r.text)


# --- 4. package theft evidence is retrievable ----------------------------------------


def _removal(camera_id: str = "mock-front-door", zone_id: str = "zone-porch") -> SceneTransition:
    return SceneTransition(
        camera_id=camera_id,
        kind="mailbox",
        transition="mailbox_package_removed",
        event_type="package",
        priority="high",
        description="A package was taken from the porch at Front Door.",
        tags=["mailbox", "package_removed"],
        metadata={
            "mailbox": {
                "visit_id": "visit-1",
                "zone": "porch",
                "item_removed": "yes",
                "before": {"package_detected": True, "image": True},
                "after": {"package_detected": False, "image": True},
            },
            "scene": {"kind": "mailbox", "transition": "mailbox_package_removed", "zone": "porch"},
        },
        zone="porch",
        evidence_images={"before": BEFORE_JPEG, "after": AFTER_JPEG},
        dedup_key=f"package_removed:{camera_id}:{zone_id}",
        dedup_window_seconds=float(settings.mailbox_dedupe_seconds),
        observed_at=time.time(),
    )


def _replica_emitting(monkeypatch, transition: SceneTransition):
    """Make ``process_frame`` report ``transition``, as a replica's own
    in-process scene cache would after seeing the removal."""

    async def fake_process_frame(session, camera_id, camera_name, image, detections):
        return [transition]

    async def fake_note_event(session, transition, event_id):
        return None

    monkeypatch.setattr(scene_state, "process_frame", fake_process_frame)
    monkeypatch.setattr(scene_state, "note_event", fake_note_event)


async def _run_ingestion() -> int:
    return await ingestion._emit_scene_transitions(
        SessionLocal, "mock-front-door", "Front Door", b"frame", [], None, "snapshot"
    )


@pytest.mark.asyncio
async def test_package_theft_evidence_is_stored_and_retrievable(client, monkeypatch):
    headers = await auth_headers(client)
    async with SessionLocal() as session:
        await security_modes.set_mode(session, "away", None)
    _replica_emitting(monkeypatch, _removal())
    assert await _run_ingestion() == 1

    r = await client.get("/api/v1/security/incidents", headers=headers)
    assert r.status_code == 200, r.text
    theft = [i for i in r.json() if i["kind"] == "package_theft"]
    assert len(theft) == 1
    evidence = theft[0]["evidence"]
    for label, expected in (("before", BEFORE_JPEG), ("after", AFTER_JPEG)):
        url = evidence[label]["image_url"]
        assert url.endswith(f"/evidence/{label}")
        assert evidence[label]["image"] is True
        image = await client.get(url, headers=headers)
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/jpeg")
        assert image.content == expected
        # Evidence is incident material: never public.
        saved = dict(client.cookies)
        client.cookies.clear()
        assert (await client.get(url)).status_code == 401
        client.cookies.update(saved)


@pytest.mark.asyncio
async def test_missing_evidence_is_404(client):
    headers = await auth_headers(client)
    r = await client.get("/api/v1/events/evt-nope/evidence/before", headers=headers)
    assert r.status_code == 404


# --- 3. package removal dedup is DB-atomic across replicas ---------------------------


@pytest.mark.asyncio
async def test_two_replicas_emit_one_removal_and_one_incident_count(monkeypatch):
    """Each replica has its own in-process scene cache, so both see the
    removal. Only the replica that wins the DB claim may emit it."""
    async with SessionLocal() as session:
        await security_modes.set_mode(session, "away", None)

    _replica_emitting(monkeypatch, _removal())
    first = await _run_ingestion()
    _replica_emitting(monkeypatch, _removal())
    second = await _run_ingestion()

    assert (first, second) == (1, 0)
    async with SessionLocal() as session:
        events = list((await session.execute(select(Event).where(Event.type == "package"))).scalars())
        incidents = list((await session.execute(select(Incident).where(Incident.kind == "package_theft"))).scalars())
    assert len(events) == 1
    assert len(incidents) == 1
    assert incidents[0].event_count == 1


@pytest.mark.asyncio
async def test_dedup_claim_is_exclusive_within_the_window_and_reopens_after():
    now = time.time()
    key = f"package_removed:cam:{uuid.uuid4().hex}"
    async with SessionLocal() as a, SessionLocal() as b:
        assert await scene_dedup.claim(a, key, now, 60.0, "evt-1") is True
        assert await scene_dedup.claim(b, key, now + 1, 60.0, "evt-2") is False
        assert await scene_dedup.claim(b, key, now + 59, 60.0, "evt-3") is False
        assert await scene_dedup.claim(a, key, now + 61, 60.0, "evt-4") is True
    async with SessionLocal() as session:
        row = await session.get(SceneDedupClaim, key)
    assert row.event_id == "evt-4"


@pytest.mark.asyncio
async def test_different_zones_are_deduplicated_independently(monkeypatch):
    _replica_emitting(monkeypatch, _removal(zone_id="zone-porch"))
    assert await _run_ingestion() == 1
    _replica_emitting(monkeypatch, _removal(zone_id="zone-mailbox"))
    assert await _run_ingestion() == 1
