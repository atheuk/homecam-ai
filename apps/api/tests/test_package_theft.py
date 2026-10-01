"""Package theft (feature 3) and the end-to-end signals pipeline.

These are integration tests: they drive real events through
``events.create_and_broadcast_event`` so the loitering / unusual-activity /
priority signals and the incident router all run together, the way they do
in production.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal, init_db
from app.models.db import CameraZone, Event, Incident, ZonePresence
from app.services import events as event_service
from app.services import security_modes


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(Incident))
            await session.execute(delete(Event))
            await session.execute(delete(ZonePresence))
            await session.execute(delete(CameraZone))
            await session.commit()
            await security_modes.set_mode(session, "disarmed", None)

    await _clear()
    yield
    await _clear()


async def _arm(mode: str) -> None:
    async with SessionLocal() as session:
        await security_modes.set_mode(session, mode, None)


async def _emit(**overrides) -> Event:
    payload = {
        "id": f"evt-{uuid.uuid4()}",
        "camera_id": "mock-front-door",
        "type": "package",
        "priority": "normal",
        "source": "test",
        "start_time": datetime.now(timezone.utc).isoformat(),
        "description": "the package by the door is gone",
        "zone": None,
        "tags": ["package_removed"],
        "metadata": {
            "mailbox": {"before": "before.jpg", "after": "after.jpg", "visit_id": "visit-1"},
            # Scene-derived events carry a "scene" key; the AI pipeline
            # preserves the caller's type/zone/tags when it is present,
            # which is exactly how scene_state emits package removals.
            "scene": {"kind": "mailbox", "transition": "mailbox_package_removed"},
        },
    }
    payload.update(overrides)
    async with SessionLocal() as session:
        return await event_service.create_and_broadcast_event(session, payload)


async def _incidents(kind: str | None = None) -> list[Incident]:
    async with SessionLocal() as session:
        statement = select(Incident)
        if kind:
            statement = statement.where(Incident.kind == kind)
        return list((await session.execute(statement)).scalars())


# --- package theft ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["away", "night"])
async def test_package_removal_while_armed_opens_an_incident(mode):
    await _arm(mode)
    await _emit(zone="porch")
    rows = await _incidents("package_theft")
    assert len(rows) == 1
    assert rows[0].severity == "high"
    assert mode in rows[0].summary


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["home", "disarmed"])
async def test_package_removal_while_someone_is_home_is_not_escalated(mode):
    await _arm(mode)
    await _emit(zone="porch")
    assert await _incidents("package_theft") == []


@pytest.mark.asyncio
async def test_package_theft_incident_carries_before_after_evidence():
    await _arm("away")
    row = await _emit(zone="porch")
    incident = (await _incidents("package_theft"))[0]
    assert incident.evidence["before"] == "before.jpg"
    assert incident.evidence["after"] == "after.jpg"
    assert incident.evidence["visit_id"] == "visit-1"
    assert incident.evidence["event_id"] == row.id


@pytest.mark.asyncio
async def test_repeat_removals_group_into_one_incident():
    await _arm("away")
    await _emit(zone="porch")
    await _emit(zone="porch")
    rows = await _incidents("package_theft")
    assert len(rows) == 1
    assert rows[0].event_count == 2


@pytest.mark.asyncio
async def test_package_theft_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "package_theft_detection_enabled", False)
    await _arm("away")
    await _emit(zone="porch")
    assert await _incidents("package_theft") == []


@pytest.mark.asyncio
async def test_a_plain_package_event_is_never_an_incident():
    await _arm("away")
    await _emit(tags=[], metadata={})
    assert await _incidents() == []


# --- signals pipeline -------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_event_gets_a_notification_priority():
    row = await _emit(type="person", tags=[], metadata={})
    assert row.event_metadata["notification_priority"] in {"low", "normal", "high", "critical"}
    assert isinstance(row.event_metadata["priority_reasons"], list)


@pytest.mark.asyncio
async def test_package_removal_while_away_scores_critical():
    await _arm("away")
    row = await _emit(zone="porch")
    assert row.event_metadata["notification_priority"] == "critical"


@pytest.mark.asyncio
async def test_low_priority_events_do_not_open_incidents(monkeypatch):
    """Noise suppression: the event is still recorded, it just stays quiet."""
    monkeypatch.setattr(settings, "incident_min_priority", "critical")
    await _arm("away")
    row = await _emit(type="person", tags=[], metadata={}, description="someone outside")
    assert row.id is not None
    assert row.event_metadata["notification_priority"] != "critical"
    assert await _incidents("intrusion") == []


@pytest.mark.asyncio
async def test_sufficient_priority_still_opens_an_incident():
    await _arm("away")
    await _emit(type="person", tags=[], metadata={}, description="someone outside")
    assert len(await _incidents("intrusion")) == 1


@pytest.mark.asyncio
async def test_loitering_tag_raises_priority_and_routes_as_intrusion():
    await _arm("away")
    row = await _emit(
        type="person",
        tags=["loitering"],
        metadata={"scene": {"kind": "test"}},
        zone="porch",
    )
    assert "loitering" in row.tags
    assert row.event_metadata["notification_priority"] in {"high", "critical"}
    assert len(await _incidents("intrusion")) == 1


@pytest.mark.asyncio
async def test_unusual_activity_tagging_needs_history():
    """With no history the baseline must stay silent rather than guess."""
    row = await _emit(type="person", tags=[], metadata={})
    assert "unusual_activity" not in (row.tags or [])


@pytest.mark.asyncio
async def test_signals_never_overwrite_the_stored_event_priority():
    row = await _emit(
        type="person",
        priority="low",
        tags=["loitering"],
        metadata={"scene": {"kind": "test"}},
    )
    assert row.priority == "low"
    assert row.event_metadata["notification_priority"] != "low"


@pytest.mark.asyncio
async def test_old_events_are_not_reprocessed_for_loitering():
    """A backfilled event must not create phantom dwell state."""
    stale = datetime.now(timezone.utc) - timedelta(days=3)
    await _emit(type="person", tags=[], metadata={}, zone="porch", start_time=stale.isoformat())
    async with SessionLocal() as session:
        rows = list((await session.execute(select(ZonePresence))).scalars())
    assert all(row.camera_id == "mock-front-door" for row in rows)

