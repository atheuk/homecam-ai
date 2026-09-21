"""Event persistence and real-time broadcast (SPEC sections 12, 31, 34).

Events are written to the database as the durable source of truth, and
simultaneously fanned out to any subscribed SSE clients through an
in-process ``EventBus``. Persistence and broadcast are deliberately kept
independent: a slow/disconnected subscriber must never block a write, and a
restart must never lose already-persisted events.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import Event


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
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def create_and_broadcast_event(session: AsyncSession, event: dict) -> Event:
    row = await persist_event(session, event)
    await event_bus.publish(event)
    return row


async def list_events(session: AsyncSession, limit: int = 50) -> list[Event]:
    result = await session.execute(
        select(Event).order_by(Event.start_time.desc()).limit(limit)
    )
    return list(result.scalars().all())
