"""Cross-camera activity correlation (SPEC sections 18 and 19).

Grounding rules taken directly from the spec:

- correlation uses only *real* persisted events and their real timestamps;
- no intermediate action is ever invented — the summary lists only the
  events that actually exist;
- only temporal proximity, camera sequence, detected objects and event
  categories are used. Visual re-identification is explicitly out of scope.

An event joins the most recent open activity when it lands within
``ACTIVITY_CORRELATION_WINDOW_SECONDS`` of that activity's last event,
regardless of which camera or provider produced it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models.db import Activity, Event

# Highest-priority category wins when several signals are present.
_CATEGORY_PRIORITY = ("delivery", "visitor", "arrival", "departure", "vehicle", "unknown")


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def categorize(events: list[Event]) -> str:
    types = {event.type for event in events}
    tags = {tag for event in events for tag in (event.tags or [])}
    if "package" in types or "mailbox" in tags:
        return "delivery"
    if "doorbell" in types:
        return "visitor"
    if "vehicle" in types:
        # A vehicle that parks and is followed by a person reads as an arrival.
        return "arrival" if "person" in types else "vehicle"
    if "person" in types:
        return "visitor" if "driveway-access" in tags else "unknown"
    return "unknown"


def summarize(events: list[Event], camera_names: dict[str, str] | None = None) -> str:
    """Describe only the events that actually exist, in order."""
    names = camera_names or {}
    ordered = sorted(events, key=lambda event: _as_utc(event.start_time))
    parts = []
    for event in ordered:
        camera = names.get(event.camera_id, event.camera_id)
        zone = f" ({event.zone})" if event.zone else ""
        parts.append(f"{_as_utc(event.start_time).strftime('%H:%M:%S')} {camera}{zone}: {event.type}")
    return " | ".join(parts)[:500]


async def _open_activity(session: AsyncSession, at: datetime, window: float) -> Activity | None:
    cutoff = at - timedelta(seconds=window)
    result = await session.execute(
        select(Activity).order_by(Activity.end_time.desc()).limit(5)
    )
    for activity in result.scalars().all():
        if _as_utc(activity.end_time) >= cutoff and _as_utc(activity.start_time) <= at + timedelta(seconds=window):
            return activity
    return None


async def _events_for(session: AsyncSession, event_ids: list[str]) -> list[Event]:
    if not event_ids:
        return []
    result = await session.execute(select(Event).where(Event.id.in_(event_ids)))
    return list(result.scalars().all())


async def correlate_event(session: AsyncSession, row: Event) -> Activity | None:
    """Attach ``row`` to a temporally-close activity, or open a new one."""
    if not settings.activity_correlation_enabled:
        return None
    at = _as_utc(row.start_time)
    window = settings.activity_correlation_window_seconds
    now = datetime.now(timezone.utc)

    activity = await _open_activity(session, at, window)
    if activity is None:
        activity = Activity(
            id=str(uuid.uuid4()),
            start_time=at,
            end_time=at,
            category="unknown",
            summary="",
            confidence=None,
            event_ids=[],
            cameras=[],
            created_at=now,
            updated_at=now,
        )
        session.add(activity)

    event_ids = list(activity.event_ids or [])
    if row.id not in event_ids:
        event_ids.append(row.id)
    cameras = list(activity.cameras or [])
    if row.camera_id not in cameras:
        cameras.append(row.camera_id)

    members = await _events_for(session, [eid for eid in event_ids if eid != row.id])
    members.append(row)

    activity.event_ids = event_ids
    activity.cameras = cameras
    activity.start_time = min(_as_utc(event.start_time) for event in members)
    activity.end_time = max(_as_utc(event.start_time) for event in members)
    activity.category = categorize(members)
    activity.summary = summarize(members)
    # More corroborating events across more cameras -> higher confidence,
    # capped so a correlation is never presented as certain.
    activity.confidence = round(min(0.9, 0.4 + 0.1 * len(members) + 0.1 * (len(cameras) - 1)), 4)
    activity.updated_at = now
    row.activity_id = activity.id
    return activity


async def list_activities(session: AsyncSession, limit: int = 50) -> list[Activity]:
    result = await session.execute(
        select(Activity).order_by(Activity.start_time.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def get_activity(session: AsyncSession, activity_id: str) -> Activity | None:
    return await session.get(Activity, activity_id)


def to_dict(activity: Activity) -> dict:
    return {
        "id": activity.id,
        "start_time": _as_utc(activity.start_time).isoformat(),
        "end_time": _as_utc(activity.end_time).isoformat(),
        "category": activity.category,
        "summary": activity.summary,
        "confidence": activity.confidence,
        "event_ids": list(activity.event_ids or []),
        "cameras": list(activity.cameras or []),
    }


def category_priority(category: str) -> int:
    return _CATEGORY_PRIORITY.index(category) if category in _CATEGORY_PRIORITY else len(_CATEGORY_PRIORITY)
