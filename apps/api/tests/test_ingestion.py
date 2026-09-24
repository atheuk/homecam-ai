"""Continuous event ingestion tests (SPEC section 12)."""
import pytest
from sqlalchemy import delete

from app.ai.detector import BoundingBox, Detection, mock_detector
from app.config import settings
from app.db import SessionLocal
from app.models.db import Activity, AIAnalysis, Event
from app.services import ingestion


@pytest.fixture(autouse=True)
async def _clean_events(client):
    ingestion.reset_cooldowns()

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(AIAnalysis))
            await session.execute(delete(Activity))
            await session.execute(delete(Event))
            await session.commit()

    await _clear()
    yield
    await _clear()
    ingestion.reset_cooldowns()


async def test_poll_once_is_a_no_op_under_the_default_mock_backend(client):
    """A mock detector cannot see pixels (SPEC 15): a bare poll with no
    event-type hint must find nothing and create no events, so enabling
    ingestion is always safe under the default configuration."""
    created = await ingestion.poll_once()
    assert created == 0
    async with SessionLocal() as session:
        rows = (await session.execute(Event.__table__.select())).fetchall()
    assert rows == []


async def test_poll_once_creates_a_real_event_when_the_detector_sees_a_person(client):
    mock_detector().set_script(
        "mock-front-door", [Detection("person", 0.91, BoundingBox(0.1, 0.1, 0.3, 0.6))]
    )
    created = await ingestion.poll_once()
    assert created >= 1

    async with SessionLocal() as session:
        rows = (await session.execute(Event.__table__.select())).fetchall()
    matching = [r for r in rows if r.camera_id == "mock-front-door"]
    assert matching
    assert matching[0].type == "person"
    assert matching[0].source == "local-ai"


async def test_poll_once_prefers_person_when_multiple_labels_are_seen(client):
    mock_detector().set_script(
        "mock-garden",
        [
            Detection("dog", 0.6, BoundingBox(0.05, 0.05, 0.2, 0.3)),
            Detection("person", 0.8, BoundingBox(0.4, 0.4, 0.6, 0.8)),
        ],
    )
    await ingestion.poll_once()
    async with SessionLocal() as session:
        rows = (await session.execute(Event.__table__.select())).fetchall()
    garden = [r for r in rows if r.camera_id == "mock-garden"]
    assert garden and garden[0].type == "person"


async def test_poll_once_respects_the_per_camera_cooldown(client, monkeypatch):
    monkeypatch.setattr(settings, "event_cooldown_seconds", 9999.0)
    mock_detector().set_script(
        "mock-backyard", [Detection("person", 0.7, BoundingBox(0.1, 0.1, 0.4, 0.5))]
    )
    first = await ingestion.poll_once()
    assert first >= 1
    second = await ingestion.poll_once()
    assert second == 0

    async with SessionLocal() as session:
        rows = (await session.execute(Event.__table__.select())).fetchall()
    assert len([r for r in rows if r.camera_id == "mock-backyard"]) == 1


async def test_poll_once_skips_offline_cameras(client):
    await client.post("/api/v1/mock/cameras/mock-driveway/status", json={"status": "offline"})
    mock_detector().set_script(
        "mock-driveway", [Detection("person", 0.7, BoundingBox(0.1, 0.1, 0.4, 0.5))]
    )
    created = await ingestion.poll_once()
    async with SessionLocal() as session:
        rows = (await session.execute(Event.__table__.select())).fetchall()
    assert not any(r.camera_id == "mock-driveway" for r in rows)
    assert created == 0
