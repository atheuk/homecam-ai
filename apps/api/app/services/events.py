"""Event persistence and real-time broadcast (SPEC sections 12, 31, 34).

Events are written to the database as the durable source of truth, and
simultaneously fanned out to any subscribed SSE clients through an
in-process ``EventBus``. Persistence and broadcast are deliberately kept
independent: a slow/disconnected subscriber must never block a write, and a
restart must never lose already-persisted events.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import Event
from . import activities as activity_service
from . import ai_pipeline

logger = logging.getLogger(__name__)


class EventBus:
    """Simple in-process pub/sub used to fan out newly created events to
    connected SSE clients. Not persisted; persistence is handled separately
    by ``persist_event``."""

    def __init__(self) -> None:
        self.subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self.subscribers:
            self.subscribers.remove(queue)

    async def publish(self, event: dict) -> None:
        for queue in list(self.subscribers):
            await queue.put(event)


event_bus = EventBus()


async def persist_event(session: AsyncSession, event: dict) -> Event:
    row = Event(
        id=event["id"],
        camera_id=event["camera_id"],
        type=event["type"],
        priority=event["priority"],
        source=event["source"],
        start_time=datetime.fromisoformat(event["start_time"]),
        description=event["description"],
        event_metadata=event.get("metadata", {}),
        zone=event.get("zone"),
        tags=list(event.get("tags", [])),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def create_and_broadcast_event(session: AsyncSession, event: dict) -> Event:
    """Run the SPEC section 12 ingestion pipeline for one normalized event.

    ``normalize -> persist -> snapshot -> detector -> AI -> embedding ->
    correlation -> realtime``. Every analysis stage is optional and defensive:
    if analysis or correlation fails the event is still persisted and still
    broadcast, just without enrichment.
    """
    row = await persist_event(session, event)
    enriched = event
    try:
        enriched = await ai_pipeline.enrich_event(session, row, event)
        await activity_service.correlate_event(session, row)
        await session.commit()
        await session.refresh(row)
    except Exception:  # noqa: BLE001 - analysis must never break ingestion
        logger.exception("event analysis failed for %s", row.id)
        await session.rollback()
    enriched = {**enriched, "activity_id": row.activity_id}
    await event_bus.publish(enriched)
    return row


async def list_events(session: AsyncSession, limit: int = 50) -> list[Event]:
    result = await session.execute(
        select(Event).order_by(Event.start_time.desc()).limit(limit)
    )
    return list(result.scalars().all())


def to_dict(row: Event) -> dict:
    metadata = row.event_metadata or {}
    return {
        "id": row.id,
        "camera_id": row.camera_id,
        "type": row.type,
        "priority": row.priority,
        "source": row.source,
        "start_time": row.start_time.isoformat(),
        "description": row.description,
        "zone": row.zone,
        "tags": list(row.tags or []),
        "thumbnail_path": row.thumbnail_path,
        "best_photo_path": row.best_photo_path,
        "ai_analysis_id": row.ai_analysis_id,
        "activity_id": row.activity_id,
        "person_id": row.person_id,
        "person_confidence": row.person_confidence,
        "person_confirmed": bool(row.person_confirmed),
        "photo_rating": row.photo_rating,
        "photo_caption": metadata.get("photo_caption"),
        "metadata": metadata,
    }
