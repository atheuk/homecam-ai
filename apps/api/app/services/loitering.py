"""Loitering detection: "someone has been standing in the driveway for a
minute", not "someone walked past".

Deliberately built on a database row per (camera, zone, label) rather than
on :mod:`app.ai.dwell`'s in-process frame tracker. ``app.ai.dwell`` answers
a per-stream question inside one worker; loitering has to survive the API
running as two Container Apps replicas, where consecutive sightings of the
same subject can land on different replicas. The ``zone_presence`` row is
the single point of serialization for that tuple, and the check-then-act
around it is guarded by the same transactional advisory lock the incident
router uses.

A gap longer than ``loitering_gap_seconds`` between sightings ends the
visit and restarts the clock: "came back three times" is not "stayed".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models.db import CameraZone, ZonePresence

logger = logging.getLogger(__name__)

#: Only subjects that can meaningfully "linger" are tracked. A parked car
#: is not loitering, and a package certainly is not.
LOITERING_LABELS = frozenset({"person"})


@dataclass(frozen=True)
class LoiteringResult:
    """Outcome of recording one sighting."""

    loitering: bool
    dwell_seconds: float
    threshold: float
    zone: str


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _key(camera_id: str, zone: str, label: str) -> str:
    return f"{camera_id}|{zone}|{label}"


async def _acquire_presence_lock(session: AsyncSession, key: str) -> None:
    """Serialize one presence key across replicas.

    Same rationale and same mechanism as
    ``app.services.incidents._acquire_route_lock``: on PostgreSQL take a
    transactional advisory lock that any other replica touching the same
    key blocks on until this transaction ends; on SQLite (dev/tests) the
    single-writer file already provides that serialization, so this is a
    no-op.
    """
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"presence|{key}"}
    )


async def dwell_threshold(session: AsyncSession, camera_id: str, zone: str) -> float:
    """The zone's configured dwell threshold, or the global default."""
    result = await session.execute(
        select(CameraZone.dwell_seconds).where(
            CameraZone.camera_id == camera_id, CameraZone.name == zone
        )
    )
    value = result.scalar_one_or_none()
    if value is None or value <= 0:
        return settings.zone_default_dwell_seconds
    return float(value)


async def record_sighting(
    session: AsyncSession,
    *,
    camera_id: str,
    zone: str | None,
    label: str = "person",
    seen_at: datetime | None = None,
) -> LoiteringResult | None:
    """Record that ``label`` was seen in ``zone`` and report whether that
    now counts as loitering.

    Returns ``None`` when loitering cannot apply at all (feature disabled,
    no zone, or a label that cannot linger), which lets callers treat
    "not applicable" and "not loitering yet" differently.
    """
    if not settings.loitering_detection_enabled or not zone or label not in LOITERING_LABELS:
        return None

    now = seen_at or datetime.now(timezone.utc)
    key = _key(camera_id, zone, label)
    await _acquire_presence_lock(session, key)

    row = await session.get(ZonePresence, key)
    if row is None:
        session.add(
            ZonePresence(
                id=key,
                camera_id=camera_id,
                zone=zone,
                label=label,
                first_seen_at=now,
                last_seen_at=now,
                last_alert_at=None,
                updated_at=now,
            )
        )
        await session.flush()
        return LoiteringResult(False, 0.0, await dwell_threshold(session, camera_id, zone), zone)

    last_seen = _as_aware(row.last_seen_at) or now
    gap = (now - last_seen).total_seconds()
    if gap > settings.loitering_gap_seconds or gap < 0:
        # The subject left and came back (or the clock jumped). A new visit
        # starts now, and the previous visit's alert state goes with it.
        row.first_seen_at = now
        row.last_alert_at = None

    row.last_seen_at = now
    row.updated_at = now
    threshold = await dwell_threshold(session, camera_id, zone)
    dwell = max(0.0, (now - (_as_aware(row.first_seen_at) or now)).total_seconds())

    if dwell < threshold:
        await session.flush()
        return LoiteringResult(False, dwell, threshold, zone)

    last_alert = _as_aware(row.last_alert_at)
    if last_alert is not None and now - last_alert < timedelta(seconds=settings.loitering_repeat_seconds):
        # Already flagged recently - keep tracking, but do not re-alert.
        await session.flush()
        return LoiteringResult(False, dwell, threshold, zone)

    row.last_alert_at = now
    await session.flush()
    logger.info("loitering detected camera=%s zone=%s dwell=%.0fs", camera_id, zone, dwell)
    return LoiteringResult(True, dwell, threshold, zone)


async def clear_presence(session: AsyncSession, camera_id: str, zone: str | None = None) -> int:
    """Drop tracked presence for a camera (all zones, or one). Used when a
    camera is removed so stale rows cannot resurrect a dwell clock."""
    statement = select(ZonePresence).where(ZonePresence.camera_id == camera_id)
    if zone:
        statement = statement.where(ZonePresence.zone == zone)
    rows = list((await session.execute(statement)).scalars())
    for row in rows:
        await session.delete(row)
    return len(rows)
