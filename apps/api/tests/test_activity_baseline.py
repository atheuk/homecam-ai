"""Unusual-activity baseline: quiet-hour detection from the camera's own history."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.config import settings
from app.db import SessionLocal, init_db
from app.models.db import Event
from app.services import activity_baseline


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(Event))
            await session.commit()

    await _clear()
    yield
    await _clear()


async def _seed_history(camera_id: str, moments: list[datetime]):
    async with SessionLocal() as session:
        for index, moment in enumerate(moments):
            session.add(
                Event(
                    id=f"{camera_id}-hist-{index}",
                    camera_id=camera_id,
                    type="person",
                    priority="normal",
                    source="test",
                    start_time=moment,
                    description="history",
                    tags=[],
                    event_metadata={},
                )
            )
        await session.commit()


def _busy_afternoon(anchor: datetime, days: int = 14, per_day: int = 6) -> list[datetime]:
    """A camera that is only ever busy between 13:00 and 18:00."""
    moments = []
    for day in range(1, days + 1):
        base = anchor - timedelta(days=day)
        for hour in range(13, 13 + per_day):
            moments.append(base.replace(hour=hour, minute=5, second=0, microsecond=0))
    return moments


def test_slot_for_is_hour_of_week():
    monday = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)  # a Monday
    assert activity_baseline.slot_for(monday) == 0
    assert activity_baseline.slot_for(monday.replace(hour=3)) == 3
    assert activity_baseline.slot_for(monday + timedelta(days=6, hours=23)) == 167


@pytest.mark.asyncio
async def test_no_verdict_without_enough_history():
    now = datetime.now(timezone.utc)
    await _seed_history("mock-garden", [now - timedelta(hours=index) for index in range(1, 5)])
    async with SessionLocal() as session:
        assert (
            await activity_baseline.evaluate(session, camera_id="mock-garden", occurred_at=now)
            is None
        )


@pytest.mark.asyncio
async def test_event_in_a_historically_quiet_slot_is_unusual():
    # Anchor on a Wednesday so the "quiet" 3am slot is not the same
    # hour-of-week as any seeded afternoon slot.
    anchor = datetime(2024, 5, 15, 3, 0, tzinfo=timezone.utc)
    await _seed_history("mock-garden", _busy_afternoon(anchor))
    async with SessionLocal() as session:
        result = await activity_baseline.evaluate(
            session, camera_id="mock-garden", occurred_at=anchor
        )
    assert result is not None
    assert result.samples >= settings.unusual_activity_min_history
    assert result.slot_count == 0
    assert result.unusual is True


@pytest.mark.asyncio
async def test_event_in_a_busy_slot_is_not_unusual():
    anchor = datetime(2024, 5, 15, 3, 0, tzinfo=timezone.utc)
    history = _busy_afternoon(anchor)
    await _seed_history("mock-garden", history)
    # Same weekday+hour as a well-populated historical slot.
    busy_moment = anchor.replace(hour=14)
    async with SessionLocal() as session:
        result = await activity_baseline.evaluate(
            session, camera_id="mock-garden", occurred_at=busy_moment + timedelta(days=7)
        )
    assert result is not None
    assert result.unusual is False


@pytest.mark.asyncio
async def test_baseline_is_per_camera():
    anchor = datetime(2024, 5, 15, 3, 0, tzinfo=timezone.utc)
    await _seed_history("mock-garden", _busy_afternoon(anchor))
    async with SessionLocal() as session:
        # A different camera has no history of its own, so no verdict.
        assert (
            await activity_baseline.evaluate(
                session, camera_id="mock-backyard", occurred_at=anchor
            )
            is None
        )


@pytest.mark.asyncio
async def test_disabled_flag_short_circuits(monkeypatch):
    monkeypatch.setattr(settings, "unusual_activity_enabled", False)
    async with SessionLocal() as session:
        assert (
            await activity_baseline.evaluate(
                session, camera_id="mock-garden", occurred_at=datetime.now(timezone.utc)
            )
            is None
        )
