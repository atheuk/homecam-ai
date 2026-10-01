"""Loitering detection: dwell threshold, visit reset, repeat suppression."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.config import settings
from app.db import SessionLocal, init_db
from app.models.db import CameraZone, ZonePresence
from app.services import loitering


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(ZonePresence))
            await session.execute(delete(CameraZone))
            await session.commit()

    await _clear()
    yield
    await _clear()


async def _add_zone(name: str, dwell: float | None):
    async with SessionLocal() as session:
        session.add(
            CameraZone(
                id=f"zone-{name}",
                camera_id="mock-front-door",
                name=name,
                kind="entry",
                points=[[0, 0], [1, 0], [1, 1]],
                x1=0.0,
                y1=0.0,
                x2=1.0,
                y2=1.0,
                dwell_seconds=dwell,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_brief_visit_is_not_loitering():
    start = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        first = await loitering.record_sighting(
            session, camera_id="mock-front-door", zone="driveway", seen_at=start
        )
        second = await loitering.record_sighting(
            session, camera_id="mock-front-door", zone="driveway", seen_at=start + timedelta(seconds=10)
        )
        await session.commit()
    assert first is not None and first.loitering is False
    assert second is not None and second.loitering is False
    assert second.dwell_seconds == pytest.approx(10.0, abs=0.5)


@pytest.mark.asyncio
async def test_dwelling_past_threshold_flags_loitering():
    start = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        await loitering.record_sighting(
            session, camera_id="mock-front-door", zone="driveway", seen_at=start
        )
        # Sightings every 30s (under loitering_gap_seconds). The alert
        # fires on the sighting that crosses the 60s threshold.
        results = []
        for step in (30, 60, 90):
            results.append(
                await loitering.record_sighting(
                    session,
                    camera_id="mock-front-door",
                    zone="driveway",
                    seen_at=start + timedelta(seconds=step),
                )
            )
        await session.commit()
    assert [r.loitering for r in results] == [False, True, False]
    result = results[1]
    assert result is not None
    assert result.loitering is True
    assert result.dwell_seconds >= settings.zone_default_dwell_seconds


@pytest.mark.asyncio
async def test_repeat_alert_is_suppressed_within_window():
    start = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        for step in (0, 30, 70):
            result = await loitering.record_sighting(
                session, camera_id="mock-front-door", zone="driveway", seen_at=start + timedelta(seconds=step)
            )
        assert result.loitering is True
        again = await loitering.record_sighting(
            session, camera_id="mock-front-door", zone="driveway", seen_at=start + timedelta(seconds=100)
        )
        await session.commit()
    assert again.loitering is False


@pytest.mark.asyncio
async def test_gap_restarts_the_visit():
    start = datetime.now(timezone.utc)
    gap = settings.loitering_gap_seconds + 60
    async with SessionLocal() as session:
        await loitering.record_sighting(
            session, camera_id="mock-front-door", zone="driveway", seen_at=start
        )
        result = await loitering.record_sighting(
            session, camera_id="mock-front-door", zone="driveway", seen_at=start + timedelta(seconds=gap)
        )
        await session.commit()
    # Coming back after a long absence is a new visit, not a long dwell.
    assert result.loitering is False
    assert result.dwell_seconds == pytest.approx(0.0, abs=0.5)


@pytest.mark.asyncio
async def test_zone_dwell_seconds_overrides_the_default():
    await _add_zone("porch", 10.0)
    start = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        assert await loitering.dwell_threshold(session, "mock-front-door", "porch") == 10.0
        await loitering.record_sighting(session, camera_id="mock-front-door", zone="porch", seen_at=start)
        result = await loitering.record_sighting(
            session, camera_id="mock-front-door", zone="porch", seen_at=start + timedelta(seconds=15)
        )
        await session.commit()
    assert result.loitering is True
    assert result.threshold == 10.0


@pytest.mark.asyncio
async def test_zone_without_dwell_uses_global_default():
    await _add_zone("lawn", None)
    async with SessionLocal() as session:
        assert (
            await loitering.dwell_threshold(session, "mock-front-door", "lawn")
            == settings.zone_default_dwell_seconds
        )


@pytest.mark.asyncio
async def test_non_person_labels_and_zoneless_events_are_ignored():
    async with SessionLocal() as session:
        assert await loitering.record_sighting(
            session, camera_id="mock-front-door", zone="driveway", label="vehicle"
        ) is None
        assert await loitering.record_sighting(session, camera_id="mock-front-door", zone=None) is None


@pytest.mark.asyncio
async def test_clear_presence_removes_tracked_rows():
    async with SessionLocal() as session:
        await loitering.record_sighting(session, camera_id="mock-front-door", zone="driveway")
        await loitering.record_sighting(session, camera_id="mock-front-door", zone="porch")
        await session.commit()
        assert await loitering.clear_presence(session, "mock-front-door", zone="porch") == 1
        assert await loitering.clear_presence(session, "mock-front-door") == 1
        await session.commit()


@pytest.mark.asyncio
async def test_presence_lock_is_a_noop_on_sqlite():
    """The cross-replica lock must not break the SQLite dev/test path."""
    async with SessionLocal() as session:
        assert session.get_bind().dialect.name == "sqlite"
        await loitering._acquire_presence_lock(session, "mock-front-door|driveway|person")


@pytest.mark.asyncio
async def test_presence_lock_uses_advisory_lock_on_postgres():
    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    executed: list[tuple[str, dict]] = []

    class _Session:
        def get_bind(self):
            return _Bind()

        async def execute(self, statement, params=None):
            executed.append((str(statement), params or {}))

    await loitering._acquire_presence_lock(_Session(), "cam|zone|person")
    assert "pg_advisory_xact_lock" in executed[0][0]
    assert executed[0][1]["key"] == "presence|cam|zone|person"
