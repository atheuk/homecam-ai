"""Enforced data retention.

HomeCam reported ``retention_days: 30`` from the day the settings endpoint
was written, while nothing ever deleted anything. This module is the actual
policy, and ``GET /api/v1/settings`` now reports *it*.

Design notes that matter more than the SQL:

**Nothing is deleted that a human still needs.** Two protections outrank
every age cutoff. An event referenced by *any* incident is never purged -
open and acknowledged incidents are the obvious case, and resolved
incidents keep their timeline intact until the incident itself ages out,
after which its events become purgeable on a later run. An event with
``retention_hold`` set is never purged at all, at any age, by anything.

**Bounded work.** Every pass deletes at most ``retention_batch_size`` rows
per statement and at most ``retention_max_batches_per_run`` batches per
category, committing between batches. A large backlog is worked off over
several runs instead of one transaction long enough to block ingestion.

**Dry run first.** ``retention_dry_run`` (the default) computes and reports
exactly what *would* be deleted without deleting it, which is how a
deployment is expected to start: nobody should discover the retention
policy by losing data.

Deletion order follows the foreign keys - photos, evidence, sightings and
analyses before their event - because ``events.id`` is referenced by all
four and Postgres will (correctly) refuse to orphan them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models.db import (
    AIAnalysis,
    AuditLog,
    Event,
    EventEvidence,
    EventPhoto,
    Incident,
    IncidentClip,
    Person,
    PersonSighting,
)

logger = logging.getLogger(__name__)

# Incident states that pin their events in place. Anything not resolved is
# still being worked on, so its evidence must survive the purge.
UNRESOLVED_INCIDENT_STATUSES = ("open", "acknowledged")

CATEGORIES = (
    "incidents",
    "media",
    "embeddings",
    "ai_analyses",
    "events",
    "audit_logs",
)


@dataclass
class RetentionReport:
    """What a pass did (``dry_run=False``) or would do (``dry_run=True``)."""

    dry_run: bool
    started_at: datetime
    cutoffs: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    protected: dict[str, int] = field(default_factory=dict)
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "started_at": self.started_at.isoformat(),
            "cutoffs": self.cutoffs,
            "counts": self.counts,
            "protected": self.protected,
            # True when a category hit ``retention_max_batches_per_run``;
            # the remainder is picked up by the next run.
            "truncated": self.truncated,
        }


def policy() -> dict:
    """The configured retention policy, as reported by the API."""
    return {
        "enabled": settings.retention_enabled,
        "dry_run": settings.retention_dry_run,
        "event_days": settings.retention_event_days,
        "media_days": settings.retention_media_days,
        "ai_analysis_days": settings.retention_ai_analysis_days,
        "embedding_days": settings.retention_embedding_days,
        "incident_days": settings.retention_incident_days,
        "audit_days": settings.retention_audit_days,
        "batch_size": settings.retention_batch_size,
        "max_batches_per_run": settings.retention_max_batches_per_run,
        "interval_seconds": settings.retention_interval_seconds,
    }


def reported_retention_days() -> int:
    """The single "how long do you keep my footage" number for the UI.

    Imagery is what people mean by retention, so this reports the shorter
    of the event and media cutoffs rather than, say, the audit trail.
    """
    return min(settings.retention_event_days, settings.retention_media_days)


def _cutoffs(now: datetime) -> dict[str, datetime]:
    return {
        "events": now - timedelta(days=settings.retention_event_days),
        "media": now - timedelta(days=settings.retention_media_days),
        "ai_analyses": now - timedelta(days=settings.retention_ai_analysis_days),
        "embeddings": now - timedelta(days=settings.retention_embedding_days),
        "incidents": now - timedelta(days=settings.retention_incident_days),
        "audit_logs": now - timedelta(days=settings.retention_audit_days),
    }


async def _incident_event_ids(session: AsyncSession) -> set[str]:
    """Every event id referenced by an incident that still exists.

    ``incidents.event_ids`` is a JSON list, which no portable join can
    reach into, so the protected set is materialized here. Incident rows
    are few (one per actionable grouping, not per event) and resolved ones
    age out, so this stays small by construction.
    """
    result = await session.execute(select(Incident.event_ids))
    protected: set[str] = set()
    for row in result.scalars().all():
        for event_id in row or []:
            if isinstance(event_id, str):
                protected.add(event_id)
    return protected


async def _purgeable_event_ids(
    session: AsyncSession,
    cutoff: datetime,
    protected: set[str],
    limit: int,
) -> list[str]:
    """Oldest events past ``cutoff`` that no protection applies to."""
    stmt = (
        select(Event.id)
        .where(Event.start_time < cutoff, Event.retention_hold.is_(False))
        .order_by(Event.start_time)
        # Over-fetch so incident-protected ids inside the batch do not
        # starve the batch down to nothing.
        .limit(limit + len(protected) if protected else limit)
    )
    result = await session.execute(stmt)
    out = [event_id for event_id in result.scalars().all() if event_id not in protected]
    return out[:limit]


async def _count_purgeable_events(session: AsyncSession, cutoff: datetime, protected: set[str]) -> int:
    stmt = select(func.count()).select_from(Event).where(
        Event.start_time < cutoff, Event.retention_hold.is_(False)
    )
    total = int((await session.execute(stmt)).scalar_one())
    if not protected:
        return total
    protected_stmt = select(func.count()).select_from(Event).where(
        Event.start_time < cutoff,
        Event.retention_hold.is_(False),
        Event.id.in_(protected),
    )
    return total - int((await session.execute(protected_stmt)).scalar_one())


async def _delete_event_children(session: AsyncSession, event_ids: list[str]) -> None:
    """Drop everything that points at these events, FK order first."""
    await session.execute(delete(EventPhoto).where(EventPhoto.event_id.in_(event_ids)))
    await session.execute(delete(EventEvidence).where(EventEvidence.event_id.in_(event_ids)))
    await session.execute(delete(PersonSighting).where(PersonSighting.event_id.in_(event_ids)))
    await session.execute(delete(AIAnalysis).where(AIAnalysis.event_id.in_(event_ids)))
    # ``persons.cover_event_id`` has no FK but would otherwise point at a
    # deleted event and render a broken thumbnail.
    await session.execute(
        update(Person).where(Person.cover_event_id.in_(event_ids)).values(cover_event_id=None)
    )


async def _count(session: AsyncSession, statement) -> int:
    """Row count for a candidate query.

    Dry runs must not materialise every matching id: the whole point of the
    dry run is to be safe to call against a long-neglected database, and a
    backlog of millions of rows would then be loaded into the API process.
    """
    return int((await session.execute(select(func.count()).select_from(statement.subquery()))).scalar_one())


async def _lock_unheld(session: AsyncSession, event_ids: list[str]) -> list[str]:
    """Re-check ``retention_hold`` for ``event_ids`` while holding a row lock.

    Candidates are selected in one statement and deleted in another, so under
    PostgreSQL's READ COMMITTED a hold placed in between would otherwise be
    missed and the held event deleted anyway. Locking the rows here makes a
    concurrent ``PUT /retention-hold`` wait for this transaction, after which
    it either finds the event gone (404) or has safely excluded it. SQLite
    ignores ``FOR UPDATE`` and serialises writers anyway.
    """
    if not event_ids:
        return []
    statement = (
        select(Event.id)
        .where(Event.id.in_(event_ids), Event.retention_hold.is_(False))
        .with_for_update()
    )
    return list((await session.execute(statement)).scalars().all())


async def run(session: AsyncSession, *, dry_run: bool | None = None, now: datetime | None = None) -> RetentionReport:
    """Execute (or simulate) one retention pass.

    Safe to run concurrently on several replicas: every statement is an
    age-bounded DELETE, so a row another replica already removed simply is
    not there, and the batch limits keep any single pass short.
    """
    now = now or datetime.now(timezone.utc)
    dry_run = settings.retention_dry_run if dry_run is None else dry_run
    cutoffs = _cutoffs(now)
    batch = settings.retention_batch_size
    max_batches = settings.retention_max_batches_per_run
    report = RetentionReport(
        dry_run=dry_run,
        started_at=now,
        cutoffs={name: value.isoformat() for name, value in cutoffs.items()},
        counts={name: 0 for name in CATEGORIES},
    )

    # Resolved incidents first: that releases their events for a later run
    # while an unresolved incident keeps pinning its own.
    incident_stmt = select(Incident.id).where(
        Incident.status == "resolved",
        Incident.clip_hold.is_(False),
        Incident.last_seen_at < cutoffs["incidents"],
    )
    if dry_run:
        report.counts["incidents"] = await _count(session, incident_stmt)
    else:
        for _ in range(max_batches):
            ids = list((await session.execute(incident_stmt.limit(batch))).scalars().all())
            if not ids:
                break
            rows = list((await session.execute(
                select(Incident).where(Incident.id.in_(ids)).with_for_update()
            )).scalars().all())
            deletable = [
                row.id for row in rows
                if row.status == "resolved" and not row.clip_hold
                and (
                    row.last_seen_at.replace(tzinfo=timezone.utc)
                    if row.last_seen_at.tzinfo is None else row.last_seen_at
                ) < cutoffs["incidents"]
            ]
            if deletable:
                await session.execute(delete(IncidentClip).where(IncidentClip.incident_id.in_(deletable)))
                await session.execute(delete(Incident).where(Incident.id.in_(deletable)))
            await session.commit()
            report.counts["incidents"] += len(deletable)
            if len(ids) < batch:
                break
        else:
            report.truncated = True

    protected = await _incident_event_ids(session)
    report.protected["events_in_incidents"] = len(protected)
    held = int(
        (
            await session.execute(
                select(func.count()).select_from(Event).where(Event.retention_hold.is_(True))
            )
        ).scalar_one()
    )
    report.protected["events_on_hold"] = held

    clip_stmt = select(IncidentClip.incident_id).join(
        Incident, Incident.id == IncidentClip.incident_id
    ).where(
        Incident.status == "resolved",
        Incident.clip_hold.is_(False),
        Incident.last_seen_at < cutoffs["media"],
    )
    if dry_run:
        report.counts["media"] += await _count(session, clip_stmt)
    else:
        for _ in range(max_batches):
            ids = list((await session.execute(clip_stmt.limit(batch))).scalars().all())
            if not ids:
                break
            # Serialize the human hold toggle against deletion on PostgreSQL.
            rows = list((await session.execute(
                select(Incident).where(Incident.id.in_(ids)).with_for_update()
            )).scalars().all())
            deletable = [
                row.id for row in rows
                if row.status == "resolved" and not row.clip_hold
                and (
                    row.last_seen_at.replace(tzinfo=timezone.utc)
                    if row.last_seen_at.tzinfo is None else row.last_seen_at
                ) < cutoffs["media"]
            ]
            if deletable:
                await session.execute(delete(IncidentClip).where(IncidentClip.incident_id.in_(deletable)))
                await session.execute(
                    update(Incident).where(Incident.id.in_(deletable)).values(clip_status="unavailable")
                )
            await session.commit()
            report.counts["media"] += len(deletable)
            if len(ids) < batch:
                break
        else:
            report.truncated = True

    # Media blobs age out before their events: they dominate storage, and
    # the event's text stays searchable without them.
    photo_stmt = (
        select(EventPhoto.event_id)
        .join(Event, Event.id == EventPhoto.event_id)
        .where(Event.start_time < cutoffs["media"], Event.retention_hold.is_(False))
    )
    evidence_stmt = (
        select(EventEvidence.event_id)
        .join(Event, Event.id == EventEvidence.event_id)
        .where(Event.start_time < cutoffs["media"], Event.retention_hold.is_(False))
    )
    if protected:
        photo_stmt = photo_stmt.where(Event.id.notin_(protected))
        evidence_stmt = evidence_stmt.where(Event.id.notin_(protected))
    if dry_run:
        report.counts["media"] += await _count(session, photo_stmt) + await _count(session, evidence_stmt)
    else:
        for stmt, model, column in (
            (photo_stmt, EventPhoto, EventPhoto.event_id),
            (evidence_stmt, EventEvidence, EventEvidence.event_id),
        ):
            for _ in range(max_batches):
                ids = list((await session.execute(stmt.limit(batch))).scalars().all())
                if not ids:
                    break
                deletable = await _lock_unheld(session, ids)
                if deletable:
                    await session.execute(delete(model).where(column.in_(deletable)))
                await session.commit()
                report.counts["media"] += len(deletable)
                if len(ids) < batch:
                    break
            else:
                report.truncated = True

    # Embeddings are cleared in place: the analysis text stays useful, and
    # search degrades to keyword matching exactly as it does for events
    # that were never embedded.
    embedding_stmt = (
        select(AIAnalysis.id, AIAnalysis.event_id)
        .join(Event, Event.id == AIAnalysis.event_id)
        .where(
            AIAnalysis.created_at < cutoffs["embeddings"],
            AIAnalysis.embedding_dimensions > 0,
            Event.retention_hold.is_(False),
        )
    )
    if protected:
        embedding_stmt = embedding_stmt.where(Event.id.notin_(protected))
    if dry_run:
        report.counts["embeddings"] = await _count(session, embedding_stmt)
    else:
        for _ in range(max_batches):
            rows = list((await session.execute(embedding_stmt.limit(batch))).all())
            if not rows:
                break
            unheld = set(await _lock_unheld(session, [event_id for _, event_id in rows]))
            ids = [analysis_id for analysis_id, event_id in rows if event_id in unheld]
            if ids:
                await session.execute(
                    update(AIAnalysis)
                    .where(AIAnalysis.id.in_(ids))
                    .values(embedding=[], embedding_dimensions=0)
                )
            await session.commit()
            report.counts["embeddings"] += len(ids)
            if len(rows) < batch:
                break
        else:
            report.truncated = True

    analysis_stmt = (
        select(AIAnalysis.id, AIAnalysis.event_id)
        .join(Event, Event.id == AIAnalysis.event_id)
        .where(AIAnalysis.created_at < cutoffs["ai_analyses"], Event.retention_hold.is_(False))
    )
    if protected:
        analysis_stmt = analysis_stmt.where(Event.id.notin_(protected))
    if dry_run:
        report.counts["ai_analyses"] = await _count(session, analysis_stmt)
    else:
        for _ in range(max_batches):
            rows = list((await session.execute(analysis_stmt.limit(batch))).all())
            if not rows:
                break
            unheld = set(await _lock_unheld(session, [event_id for _, event_id in rows]))
            ids = [analysis_id for analysis_id, event_id in rows if event_id in unheld]
            if ids:
                await session.execute(delete(AIAnalysis).where(AIAnalysis.id.in_(ids)))
            await session.commit()
            report.counts["ai_analyses"] += len(ids)
            if len(rows) < batch:
                break
        else:
            report.truncated = True

    if dry_run:
        report.counts["events"] = await _count_purgeable_events(session, cutoffs["events"], protected)
    else:
        for _ in range(max_batches):
            ids = await _purgeable_event_ids(session, cutoffs["events"], protected, batch)
            if not ids:
                break
            deletable = await _lock_unheld(session, ids)
            if deletable:
                await _delete_event_children(session, deletable)
                await session.execute(delete(Event).where(Event.id.in_(deletable)))
            await session.commit()
            report.counts["events"] += len(deletable)
            if len(ids) < batch:
                break
        else:
            report.truncated = True

    # The audit trail is kept deliberately longer than the imagery it
    # describes, and contains no camera content.
    audit_stmt = select(AuditLog.id).where(AuditLog.created_at < cutoffs["audit_logs"])
    if dry_run:
        report.counts["audit_logs"] = await _count(session, audit_stmt)
    else:
        for _ in range(max_batches):
            ids = list((await session.execute(audit_stmt.limit(batch))).scalars().all())
            if not ids:
                break
            await session.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
            await session.commit()
            report.counts["audit_logs"] += len(ids)
            if len(ids) < batch:
                break
        else:
            report.truncated = True

    return report
