import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models.db import Event


@pytest.mark.asyncio
async def test_event_is_persisted_to_database_not_only_memory(client):
    """SPEC 31: events must be written to the database, not kept only in an
    in-process structure that disappears on restart."""
    created = await client.post("/api/v1/mock/events", json={"camera_id": "mock-garden", "type": "motion"})
    event_id = created.json()["id"]

    async with SessionLocal() as session:
        row = await session.get(Event, event_id)
        assert row is not None
        assert row.camera_id == "mock-garden"
        assert row.type == "motion"


@pytest.mark.asyncio
async def test_events_endpoint_reads_from_database(client):
    created = await client.post("/api/v1/mock/events", json={"camera_id": "mock-backyard", "type": "person"})
    event_id = created.json()["id"]

    # A second, independent DB session must see the same row: proof the
    # first request's data is durable, not held in a per-request cache.
    async with SessionLocal() as session:
        result = await session.execute(select(Event).where(Event.id == event_id))
        assert result.scalars().first() is not None

    events = (await client.get("/api/v1/events")).json()
    assert any(e["id"] == event_id for e in events)
