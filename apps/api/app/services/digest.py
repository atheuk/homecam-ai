"""Daily home digest: "here is what happened at home today".

The Nest/Ring "daily summary" idea, built to the same rule as every other
AI surface in HomeCam: the deterministic, template-built summary is the
product, and the AI provider only ever *rewrites* facts that were computed
in SQL. If no provider is configured (or it fails) the digest is still
produced, just in plainer language - it is never empty and never invented.

Idempotency across replicas is enforced by the database: ``daily_digests``
is keyed on the date string, so a second generator racing the first loses
the INSERT and re-reads the winner's row rather than writing a duplicate.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai import digest_summary
from ..config import settings
from ..models.db import DailyDigest, Event, Incident

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DigestResult:
    date: str
    summary: str
    source: str
    stats: dict

    def as_dict(self) -> dict:
        return {"date": self.date, "summary": self.summary, "source": self.source, "stats": self.stats}


def _day_bounds(day: date_type) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def collect_stats(session: AsyncSession, day: date_type) -> dict:
    """Aggregate one day's events and incidents. Pure counting - no AI."""
    start, end = _day_bounds(day)
    events = list(
        (
            await session.execute(
                select(Event).where(Event.start_time >= start, Event.start_time < end).order_by(Event.start_time)
            )
        ).scalars()
    )
    incidents = list(
        (
            await session.execute(
                select(Incident).where(Incident.first_seen_at >= start, Incident.first_seen_at < end)
            )
        ).scalars()
    )

    by_camera = Counter(event.camera_id for event in events)
    by_type = Counter(event.type for event in events)
    tags = Counter(tag for event in events for tag in (event.tags or []))
    priorities = Counter(
        (event.event_metadata or {}).get("notification_priority") or "normal" for event in events
    )
    bird_species = sorted({
        animal["common_name"] for event in events
        if (animal := (event.event_metadata or {}).get("animal"))
        and animal.get("taxonomic_group") == "bird" and animal.get("common_name")
    })

    notable = [
        {
            "event_id": event.id,
            "camera_id": event.camera_id,
            "type": event.type,
            "zone": event.zone,
            "at": _as_aware(event.start_time).isoformat(),
            "tags": list(event.tags or []),
            "priority": (event.event_metadata or {}).get("notification_priority"),
            "description": event.description,
        }
        for event in events
        if (event.event_metadata or {}).get("notification_priority") in {"high", "critical"}
        or {"loitering", "unusual_activity", "package_removed"} & set(event.tags or [])
    ][: settings.digest_max_notable_items]

    return {
        "event_count": len(events),
        "incident_count": len(incidents),
        "by_camera": dict(by_camera),
        "by_type": dict(by_type),
        "by_priority": dict(priorities),
        "tags": dict(tags),
        "bird_species": bird_species,
        "loitering_count": tags.get("loitering", 0),
        "unusual_count": tags.get("unusual_activity", 0),
        "package_removed_count": tags.get("package_removed", 0),
        "incidents": [
            {"id": incident.id, "kind": incident.kind, "severity": incident.severity, "summary": incident.summary}
            for incident in incidents
        ],
        "notable": notable,
    }


def build_summary(day: date_type, stats: dict) -> str:
    """Deterministic fallback summary. Always produced, always accurate."""
    if not stats["event_count"]:
        return f"No activity was recorded on {day.isoformat()}."
    parts = [f"{stats['event_count']} events were recorded on {day.isoformat()}"]
    cameras = sorted(stats["by_camera"].items(), key=lambda item: (-item[1], item[0]))
    if cameras:
        busiest = ", ".join(f"{name} ({count})" for name, count in cameras[:3])
        parts.append(f"across {len(cameras)} cameras - busiest: {busiest}")
    sentence = " ".join(parts) + "."
    extras = []
    if stats["incident_count"]:
        extras.append(f"{stats['incident_count']} incidents were opened")
    if stats["loitering_count"]:
        extras.append(f"{stats['loitering_count']} loitering detections")
    if stats["package_removed_count"]:
        extras.append(f"{stats['package_removed_count']} package removals")
    if stats["unusual_count"]:
        extras.append(f"{stats['unusual_count']} events at unusual times")
    if stats.get("bird_species"):
        extras.append(f"{len(stats['bird_species'])} bird species seen: {', '.join(stats['bird_species'])}")
    if extras:
        sentence += " " + ", ".join(extras).capitalize() + "."
    return sentence


async def _ai_summary(day: date_type, stats: dict, fallback: str) -> tuple[str, str]:
    facts = {
        "date": day.isoformat(),
        "events": stats["event_count"],
        "incidents": stats["incident_count"],
        "by_camera": stats["by_camera"],
        "by_type": stats["by_type"],
        "loitering": stats["loitering_count"],
        "unusual": stats["unusual_count"],
        "packages_removed": stats["package_removed_count"],
    }
    try:
        text = await digest_summary.summarize_day(facts)
    except Exception:  # noqa: BLE001 - the digest must never depend on the provider
        logger.warning("digest AI summary raised unexpectedly", exc_info=True)
        return fallback, "template"
    text = (text or "").strip()
    return (text, "ai") if text else (fallback, "template")


async def generate(session: AsyncSession, day: date_type, *, refresh: bool = False) -> DigestResult:
    """Generate (or fetch) the digest for ``day``.

    ``refresh`` re-runs the aggregate and overwrites the stored row; the
    default reuses whatever is already stored, which is what makes the
    scheduled generator safe to run repeatedly.
    """
    key = day.isoformat()
    existing = await session.get(DailyDigest, key)
    if existing is not None and not refresh:
        return DigestResult(key, existing.summary, existing.source, dict(existing.stats or {}))

    stats = await collect_stats(session, day)
    fallback = build_summary(day, stats)
    summary, source = await _ai_summary(day, stats, fallback)

    if existing is not None:
        existing.summary = summary
        existing.source = source
        existing.stats = stats
        await session.commit()
        return DigestResult(key, summary, source, stats)

    session.add(
        DailyDigest(
            date=key, summary=summary, source=source, stats=stats, created_at=datetime.now(timezone.utc)
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # Another replica generated the same day first. Its row is just as
        # valid as ours, so adopt it rather than fighting over the key.
        await session.rollback()
        winner = await session.get(DailyDigest, key)
        if winner is not None:
            return DigestResult(key, winner.summary, winner.source, dict(winner.stats or {}))
    return DigestResult(key, summary, source, stats)
