"""Regression: a camera with two identically named zones must not break
enrichment, and a failed enrichment stage must not poison the stages after it.

Production incident: a household ended up with two zones called the same
thing on one Dahua channel. Every zone lookup used ``scalar_one_or_none()``,
so each live event raised ``MultipleResultsFound`` inside
``signals.apply_signals`` and ``incidents.route_event``. Both stages are
wrapped in "must never break ingestion" handlers, so the failures were only
logged - suspicious-behaviour signals, notification priority and incident
creation silently stopped happening on real events.

The ``rollback()`` in those handlers then expired the event row, so the
scene-ingestion caller's next plain attribute read (``row.id``) tried to
lazy-load from a sync context and died with ``MissingGreenlet``, losing the
scene transition too.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.config import settings
from app.db import SessionLocal, init_db
from app.models.db import CameraZone, Event, Incident, ZonePresence
from app.services import events as event_service
from app.services import incidents as incident_service
from app.services import loitering, signals, zones

CAMERA = "mock-front-door"


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(ZonePresence))
            await session.execute(delete(CameraZone))
            await session.execute(delete(Incident))
            await session.execute(delete(Event))
            await session.commit()

    await _clear()
    yield
    await _clear()


async def _add_zone(zone_id: str, name: str, kind: str, dwell: float | None, created_at: datetime):
    async with SessionLocal() as session:
        session.add(
            CameraZone(
                id=zone_id,
                camera_id=CAMERA,
                name=name,
                kind=kind,
                x1=0.0,
                y1=0.0,
                x2=1.0,
                y2=1.0,
                dwell_seconds=dwell,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        await session.commit()


async def _duplicate_zones():
    """Two zones named "driveway" on one camera, oldest first."""
    now = datetime.now(timezone.utc)
    await _add_zone("zone-old", "driveway", "driveway", 30.0, now - timedelta(days=2))
    await _add_zone("zone-new", "driveway", "entry", 90.0, now)


def _event_payload(event_id: str) -> dict:
    return {
        "id": event_id,
        "camera_id": CAMERA,
        "type": "person",
        "priority": "normal",
        "source": "local-ai",
        "start_time": datetime.now(timezone.utc).isoformat(),
        "description": "person in the driveway",
        "zone": "driveway",
        "tags": [],
        "metadata": {"confidence": 0.9},
    }


@pytest.mark.asyncio
async def test_duplicate_zone_names_resolve_to_the_oldest_zone():
    await _duplicate_zones()
    async with SessionLocal() as session:
        kind = await zones.zone_attribute(session, CAMERA, "driveway", CameraZone.kind)
        dwell = await loitering.dwell_threshold(session, CAMERA, "driveway")
    assert kind == "driveway"
    assert dwell == 30.0


@pytest.mark.asyncio
async def test_unknown_zone_still_resolves_to_none():
    await _duplicate_zones()
    async with SessionLocal() as session:
        assert await zones.zone_attribute(session, CAMERA, "nowhere", CameraZone.kind) is None
        assert await zones.zone_attribute(session, CAMERA, None, CameraZone.kind) is None
        # No row at all must fall back to the global default, not to 0.
        assert await loitering.dwell_threshold(session, CAMERA, "nowhere") == (
            settings.zone_default_dwell_seconds
        )


@pytest.mark.asyncio
async def test_signals_and_routing_survive_duplicate_zone_names():
    await _duplicate_zones()
    async with SessionLocal() as session:
        row = await event_service.persist_event(session, _event_payload("evt-dupe-zone"))
        # Both of these raised MultipleResultsFound in production.
        result = await signals.apply_signals(session, row, mode="away")
        await session.commit()
        assert result.priority
        assert (row.event_metadata or {}).get("notification_priority")

        incident = await incident_service.route_event(session, row, mode="away")
    assert incident is not None, "an armed person-in-zone event must open an incident"


@pytest.mark.asyncio
async def test_event_row_stays_usable_after_incident_routing_fails(monkeypatch):
    """``create_and_broadcast_event`` rolls back on a routing failure, which
    expires the row. Callers keep using it afterwards, so it must come back
    loaded rather than raising MissingGreenlet on the next attribute read."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("routing exploded")

    monkeypatch.setattr(incident_service, "route_event", _boom)

    async with SessionLocal() as session:
        row = await event_service.create_and_broadcast_event(session, _event_payload("evt-expired"))
        # A plain attribute read: this is exactly what ingestion does next.
        assert row.id == "evt-expired"
        assert row.camera_id == CAMERA
        assert row.start_time is not None


@pytest.mark.asyncio
async def test_zone_names_must_be_unique_per_camera(client):
    base = f"/api/v1/admin/cameras/{CAMERA}/zones"
    payload = {"name": "driveway", "kind": "driveway", "x1": 0.1, "y1": 0.1, "x2": 0.9, "y2": 0.9}
    created = await client.post(base, json=payload)
    assert created.status_code == 201, created.text

    duplicate = await client.post(base, json=payload)
    assert duplicate.status_code == 409, duplicate.text

    other = await client.post(base, json={**payload, "name": "porch"})
    assert other.status_code == 201, other.text
    renamed = await client.put(f"{base}/{other.json()['id']}", json={"name": "driveway"})
    assert renamed.status_code == 409, renamed.text

    # Renaming a zone to the name it already has is not a conflict.
    unchanged = await client.put(f"{base}/{other.json()['id']}", json={"name": "porch"})
    assert unchanged.status_code == 200, unchanged.text
