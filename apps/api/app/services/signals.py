"""Post-enrichment signal pass: loitering, unusual activity and smart
notification priority.

Runs once per event, after the AI pipeline has settled ``row.type`` and
``row.zone`` but *before* the event is broadcast, so SSE clients and the
incident router see exactly the same tags and priority the REST API will
later return. Every signal is individually defensive: a failure adds no
tag and never breaks ingestion.

Results are written to ``Event.tags`` (``loitering``, ``unusual_activity``)
and to ``Event.event_metadata['signals']``. The computed notification
priority lives in metadata as ``notification_priority`` rather than
overwriting the existing ``Event.priority`` column, which carries the
source/ingestion-level priority and is asserted on by existing behaviour.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models.db import CameraZone, Event
from . import activity_baseline, loitering, priority, suspicious

logger = logging.getLogger(__name__)

LOITERING_TAG = "loitering"
UNUSUAL_TAG = "unusual_activity"
PACKAGE_REMOVED_TAG = "package_removed"


@dataclass(frozen=True)
class SignalResult:
    priority: str
    loitering: bool
    unusual: bool
    tags: tuple[str, ...]
    details: dict


async def _zone_kind(session: AsyncSession, camera_id: str, zone: str | None) -> str | None:
    if not zone:
        return None
    result = await session.execute(
        select(CameraZone.kind).where(CameraZone.camera_id == camera_id, CameraZone.name == zone)
    )
    return result.scalar_one_or_none()


def _confidence(metadata: dict) -> float | None:
    value = metadata.get("confidence")
    if value is None:
        detections = metadata.get("detections") or []
        confidences = [d.get("confidence") for d in detections if isinstance(d, dict)]
        confidences = [c for c in confidences if isinstance(c, (int, float))]
        value = max(confidences) if confidences else None
    return float(value) if isinstance(value, (int, float)) else None


async def apply_signals(session: AsyncSession, row: Event, *, mode: str | None = None) -> SignalResult:
    """Evaluate every signal for ``row`` and persist the outcome on it."""
    metadata = dict(row.event_metadata or {})
    tags = list(row.tags or [])
    details: dict = {}

    loitering_hit = False
    if row.type == "person":
        try:
            result = await loitering.record_sighting(
                session,
                camera_id=row.camera_id,
                zone=row.zone,
                label="person",
                seen_at=row.start_time or datetime.now(timezone.utc),
            )
        except Exception:  # noqa: BLE001 - a signal must never break ingestion
            logger.exception("loitering evaluation failed for %s", row.id)
            result = None
        if result is not None:
            details["dwell_seconds"] = round(result.dwell_seconds, 1)
            details["dwell_threshold"] = result.threshold
            loitering_hit = result.loitering
            if loitering_hit and LOITERING_TAG not in tags:
                tags.append(LOITERING_TAG)

    unusual_hit = False
    try:
        unusual = await activity_baseline.evaluate(
            session,
            camera_id=row.camera_id,
            occurred_at=row.start_time or datetime.now(timezone.utc),
            exclude_event_id=row.id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("unusual-activity evaluation failed for %s", row.id)
        unusual = None
    if unusual is not None:
        details["activity_baseline"] = {
            "slot_count": unusual.slot_count,
            "mean": round(unusual.mean, 2),
            "z_score": round(unusual.z_score, 2),
            "samples": unusual.samples,
        }
        unusual_hit = unusual.unusual
        if unusual_hit and UNUSUAL_TAG not in tags:
            tags.append(UNUSUAL_TAG)

    if settings.suspicious_enabled and (row.type in {"person", "suspicious_activity"}
                                        or "mailbox_visit" in tags):
        at = row.start_time or datetime.now(timezone.utc)
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        hour = at.astimezone(ZoneInfo(settings.home_timezone)).hour
        night = mode == "night" or hour >= 22 or hour < 6
        evidence = dict(metadata.get("suspicious_signals") or {})
        evidence["behaviours"] = metadata.get("behaviours") or []
        if row.type == "person":
            evidence.update(await suspicious.returning_visits(session, row, at, night))
        appearance = metadata.get("appearance") or {}
        verdict = suspicious.score(
            evidence, clothing=appearance.get("clothing"), mode=mode,
            night=night, unusual=unusual_hit,
        )
        if verdict["level"]:
            metadata["suspicious"] = verdict
            if verdict["level"] not in tags:
                tags.append(verdict["level"])
            details["suspicious"] = verdict
            if row.type == "suspicious_activity":
                row.description = "; ".join(verdict["reasons"])[:500]

    scored = priority.score_event(
        event_type=row.type,
        mode=mode,
        zone_kind=await _zone_kind(session, row.camera_id, row.zone),
        confidence=_confidence(metadata),
        tags=tags,
        loitering=loitering_hit,
        unusual=unusual_hit,
        package_theft=PACKAGE_REMOVED_TAG in tags,
    )

    if settings.notification_priority_enabled:
        metadata["notification_priority"] = scored.priority
        metadata["priority_reasons"] = list(scored.reasons)
    if details:
        metadata["signals"] = {**(metadata.get("signals") or {}), **details}

    row.tags = tags
    row.event_metadata = metadata
    await session.flush()
    return SignalResult(scored.priority, loitering_hit, unusual_hit, tuple(tags), details)
