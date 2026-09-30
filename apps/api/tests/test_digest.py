"""Daily digest: deterministic aggregation, AI narration, idempotency."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.db import SessionLocal, init_db
from app.models.db import DailyDigest, Event, Incident
from app.services import digest


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(DailyDigest))
            await session.execute(delete(Event))
            await session.execute(delete(Incident))
            await session.commit()

    await _clear()
    yield
    await _clear()


DAY = date(2024, 4, 10)


def _at(hour: int) -> datetime:
    return datetime(2024, 4, 10, hour, 0, tzinfo=timezone.utc)


async def _seed(count: int = 3, *, tags=None, metadata=None, camera_id="mock-front-door"):
    async with SessionLocal() as session:
        for index in range(count):
            session.add(
                Event(
                    id=f"{camera_id}-{index}-{tags or 'plain'}",
                    camera_id=camera_id,
                    type="person",
                    priority="normal",
                    source="test",
                    start_time=_at(9 + index),
                    description="someone walked past",
                    tags=list(tags or []),
                    event_metadata=dict(metadata or {}),
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_empty_day_digest_is_still_produced():
    async with SessionLocal() as session:
        result = await digest.generate(session, DAY)
    assert result.date == "2024-04-10"
    assert "No activity" in result.summary
    assert result.stats["event_count"] == 0
    assert result.source == "template"


@pytest.mark.asyncio
async def test_digest_counts_events_by_camera_and_type():
    await _seed(2, camera_id="mock-front-door")
    await _seed(1, camera_id="mock-garden")
    async with SessionLocal() as session:
        result = await digest.generate(session, DAY)
    assert result.stats["event_count"] == 3
    assert result.stats["by_camera"]["mock-front-door"] == 2
    assert result.stats["by_camera"]["mock-garden"] == 1
    assert result.stats["by_type"]["person"] == 3
    assert "3 events" in result.summary


@pytest.mark.asyncio
async def test_digest_highlights_notable_signals():
    await _seed(1, tags=["loitering"], metadata={"notification_priority": "high"})
    await _seed(1, tags=["package_removed"], metadata={"notification_priority": "critical"})
    async with SessionLocal() as session:
        result = await digest.generate(session, DAY)
    assert result.stats["loitering_count"] == 1
    assert result.stats["package_removed_count"] == 1
    assert len(result.stats["notable"]) == 2
    assert "loitering" in result.summary


@pytest.mark.asyncio
async def test_digest_ignores_other_days():
    await _seed(2)
    async with SessionLocal() as session:
        result = await digest.generate(session, DAY - timedelta(days=1))
    assert result.stats["event_count"] == 0


@pytest.mark.asyncio
async def test_digest_is_cached_until_refreshed():
    await _seed(1)
    async with SessionLocal() as session:
        first = await digest.generate(session, DAY)
        assert first.stats["event_count"] == 1
    await _seed(2, camera_id="mock-garden")
    async with SessionLocal() as session:
        cached = await digest.generate(session, DAY)
        assert cached.stats["event_count"] == 1
        refreshed = await digest.generate(session, DAY, refresh=True)
        assert refreshed.stats["event_count"] == 3


@pytest.mark.asyncio
async def test_concurrent_generation_keeps_one_row():
    """Two replicas generating the same day must not create two rows."""
    await _seed(1)
    async with SessionLocal() as one, SessionLocal() as two:
        await digest.generate(one, DAY)
        await digest.generate(two, DAY)
    async with SessionLocal() as session:
        rows = (await session.execute(select(DailyDigest))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_ai_summary_is_used_when_available(monkeypatch):
    async def _fake(facts):
        return f"AI wrote this about {facts['date']}."

    monkeypatch.setattr(digest.digest_summary, "summarize_day", _fake)
    await _seed(1)
    async with SessionLocal() as session:
        result = await digest.generate(session, DAY)
    assert result.source == "ai"
    assert result.summary == "AI wrote this about 2024-04-10."


@pytest.mark.asyncio
async def test_provider_failure_falls_back_to_template(monkeypatch):
    async def _boom(facts):
        raise RuntimeError("provider down")

    monkeypatch.setattr(digest.digest_summary, "summarize_day", _boom)
    await _seed(1)
    async with SessionLocal() as session:
        result = await digest.generate(session, DAY)
    assert result.source == "template"
    assert "1 events" in result.summary or "recorded" in result.summary


@pytest.mark.asyncio
async def test_digest_endpoint(client):
    await client.post("/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": "person"})
    today = datetime.now(timezone.utc).date().isoformat()
    r = await client.get("/api/v1/digest", params={"date": today, "refresh": True})
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == today
    assert body["stats"]["event_count"] >= 1
    assert body["summary"]


@pytest.mark.asyncio
async def test_digest_endpoint_rejects_bad_date(client):
    r = await client.get("/api/v1/digest", params={"date": "not-a-date"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_scheduler_tick_generates_today_and_yesterday(monkeypatch):
    from app.services.digest_scheduler import digest_scheduler

    now = datetime.now(timezone.utc)
    await digest_scheduler.tick(now)
    async with SessionLocal() as session:
        assert await session.get(DailyDigest, now.date().isoformat()) is not None
        assert await session.get(DailyDigest, (now - timedelta(days=1)).date().isoformat()) is not None


@pytest.mark.asyncio
async def test_scheduler_does_not_start_when_disabled():
    from app.services.digest_scheduler import DigestScheduler

    scheduler = DigestScheduler()
    scheduler.start()
    assert scheduler._task is None
    await scheduler.stop()
