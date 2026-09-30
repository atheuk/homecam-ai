"""Unusual-activity baseline: "this camera is never busy at 3am, and it
just was".

A cheap, explainable, per-camera hour-of-week model. For each camera the
last ``unusual_activity_window_days`` of events are bucketed into the 168
(weekday, hour) slots; an event lands in a *historically quiet* slot when
that slot's historical count is far below the camera's own mean. That is
the whole model - no clustering, no learned embeddings, no identity, and
nothing that could not be explained to a household member in one sentence.

Aggregation happens in Python over a bounded sample rather than in SQL
because hour-of-week extraction is not portable (``strftime('%w')`` on
SQLite vs ``extract(dow ...)`` on PostgreSQL) and the sample is small.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models.db import Event

logger = logging.getLogger(__name__)

SLOTS = 168
#: How many distinct busy hours a camera needs before "this hour is never
#: busy" is treated as a signal rather than as thin data.
MIN_ACTIVE_SLOTS = 3


@dataclass(frozen=True)
class UnusualResult:
    unusual: bool
    #: Historical event count in this event's own hour-of-week slot.
    slot_count: int
    mean: float
    z_score: float
    samples: int


def slot_for(value: datetime) -> int:
    """Hour-of-week bucket, 0 = Monday 00:00 .. 167 = Sunday 23:00."""
    return value.weekday() * 24 + value.hour


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def evaluate(
    session: AsyncSession,
    *,
    camera_id: str,
    occurred_at: datetime,
    exclude_event_id: str | None = None,
) -> UnusualResult | None:
    """Decide whether an event at ``occurred_at`` is unusually timed.

    Returns ``None`` when the feature is off or there is not yet enough
    history to have an opinion - deliberately distinct from "checked, and
    it is normal", so a brand-new camera never floods the feed.
    """
    if not settings.unusual_activity_enabled:
        return None

    occurred_at = _as_aware(occurred_at)
    since = occurred_at - timedelta(days=settings.unusual_activity_window_days)
    statement = (
        select(Event.id, Event.start_time)
        .where(Event.camera_id == camera_id, Event.start_time >= since, Event.start_time <= occurred_at)
        .order_by(Event.start_time.desc())
        .limit(settings.unusual_activity_max_samples)
    )
    rows = [
        (event_id, start_time)
        for event_id, start_time in (await session.execute(statement)).all()
        if event_id != exclude_event_id
    ]
    if len(rows) < settings.unusual_activity_min_history:
        return None

    counts = [0] * SLOTS
    for _event_id, start_time in rows:
        counts[slot_for(_as_aware(start_time))] += 1

    # Statistics are taken over the slots the camera is *ever* active in,
    # not over all 168. Most homes only generate activity in a handful of
    # hours, so including the ~140 permanently-empty slots would drag the
    # mean towards zero and make 3am look perfectly normal.
    active = [count for count in counts if count > 0]
    slot = slot_for(occurred_at)
    slot_count = counts[slot]
    mean = statistics.fmean(active) if active else 0.0
    stdev = statistics.pstdev(active) if len(active) > 1 else 0.0
    z = 0.0 if stdev <= 0 else (slot_count - mean) / stdev

    # Two ways to be unusual: the camera has a clear routine and this hour
    # of the week is simply not part of it, or the slot is measurably
    # below the camera's own norm.
    quiet = (slot_count == 0 and len(active) >= MIN_ACTIVE_SLOTS) or z <= -settings.unusual_activity_z_threshold
    if quiet:
        logger.info(
            "unusual activity camera=%s slot=%d count=%d mean=%.2f z=%.2f",
            camera_id,
            slot,
            slot_count,
            mean,
            z,
        )
    return UnusualResult(bool(quiet), slot_count, mean, z, len(rows))
