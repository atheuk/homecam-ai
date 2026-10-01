"""Incident model & lifecycle (SPEC follow-up: incident grouping/timeline,
acknowledgment/escalation, evidence export, detection dedup at the
actionable layer).

Two distinct incident families, both persisted in the same ``Incident``
table but routed by different, fully deterministic rules:

* ``intrusion`` - a subject event (person/vehicle) that is alert-worthy for
  the *current* arming mode and zone (see ``app.services.security_modes``).
  Never raised while disarmed; never gated by AI confidence beyond the
  detector's own existing thresholds.
* ``camera_offline`` / ``camera_obstruction`` / ``camera_frozen`` - camera
  integrity problems raised by :mod:`app.ai.camera_health`. Always raised
  regardless of arming mode: whether the camera can be trusted right now is
  not the same question as whether the household is armed.

Every incident always gets a deterministic, template-built ``summary``.
An optional ``ai_summary`` (see :mod:`app.ai.incident_summary`) may be
attached afterwards on a best-effort basis and is always additional, never
authoritative or required.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai import incident_summary
from ..config import settings
from ..models.db import Camera, CameraZone, Event, Incident
from . import audit as audit_service
from . import security_modes

logger = logging.getLogger(__name__)

# Event types that can ever become an "intrusion" incident. Deliberately a
# small, explicit allowlist: package/animal events are informational, not
# an intrusion signal, and are never incident-worthy on their own.
INTRUSION_EVENT_TYPES = frozenset({"person", "vehicle"})
CAMERA_HEALTH_KINDS = ("camera_offline", "camera_obstruction", "camera_frozen")

_SEVERITY_BY_EVENT_TYPE = {"person": "high", "vehicle": "medium"}
_CAMERA_HEALTH_SEVERITY = "high"

# Grouping/dedup ("one incident, not N alerts") reads whether an eligible
# open incident already exists, then either updates it or inserts a new
# one - a classic check-then-act. ``_lock_for`` gives an in-process
# ``asyncio.Lock`` keyed by the same (camera_id, zone, kind) tuple the
# dedup query groups on, which fully serializes that check-then-act
# section for two concurrently-routed events handled by the *same*
# process (e.g. a webhook and the poll loop, or two rapid webhooks).
#
# That alone is not enough in production: the API can run as up to two
# Container Apps replicas sharing only the database (see
# infra/modules/api.bicep's ``scale.maxReplicas: 2``), and an in-process
# lock cannot serialize anything across processes. ``_acquire_route_lock``
# below closes that gap with a real, cross-replica database lock.
_incident_locks: dict[tuple[str | None, str | None, str], asyncio.Lock] = {}


def _lock_for(camera_id: str | None, zone: str | None, kind: str) -> asyncio.Lock:
    key = (camera_id, zone, kind)
    lock = _incident_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _incident_locks[key] = lock
    return lock


async def _acquire_route_lock(session: AsyncSession, camera_id: str | None, zone: str | None, kind: str) -> None:
    """Serialize the incident dedup check-then-act for one (camera_id,
    zone, kind) key across concurrent *replicas* of this API, not just
    within one process (see the module-level comment above ``_lock_for``).

    On Postgres (the real deployment target) this takes a transactional
    advisory lock keyed by the same tuple ``_lock_for`` uses: any other
    transaction - on this replica or the other one - requesting the same
    key blocks here until this transaction commits or rolls back, at which
    point Postgres releases the lock automatically. No lock table, no
    cleanup, and it composes correctly with the existing time-window merge
    logic below (it is a pure mutual-exclusion lock, not a uniqueness
    constraint, so multiple *sequential* open incidents for the same key
    are still allowed once the merge window has passed).

    SQLite (dev/tests only) has no equivalent and does not need one: a
    single SQLite file already serializes every writer globally, so
    ``_lock_for``'s in-process lock is already sufficient there and this
    is a no-op.
    """
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    key = f"{camera_id or ''}|{zone or ''}|{kind}"
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


async def _camera_name(session: AsyncSession, camera_id: str) -> str:
    camera = await session.get(Camera, camera_id)
    return camera.name if camera is not None else camera_id


async def _zone_kind(session: AsyncSession, camera_id: str, zone_name: str | None) -> str | None:
    if not zone_name:
        return None
    result = await session.execute(
        select(CameraZone.kind).where(CameraZone.camera_id == camera_id, CameraZone.name == zone_name)
    )
    return result.scalar_one_or_none()


def _new_id() -> str:
    return str(uuid.uuid4())


def _as_aware(value: datetime) -> datetime:
    """SQLite loses tzinfo on round-trip; every timestamp this module reads
    back from the DB is always UTC, so re-attach it before doing arithmetic
    against a timezone-aware ``now``."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _open_incident_for(
    session: AsyncSession, camera_id: str, zone: str | None, kind: str, now: datetime
) -> Incident | None:
    """The most recent open/acknowledged incident of this kind for this
    camera+zone still inside the merge window, if any."""
    result = await session.execute(
        select(Incident)
        .where(
            Incident.camera_id == camera_id,
            Incident.zone == zone,
            Incident.kind == kind,
            Incident.status.in_(("open", "acknowledged")),
        )
        .order_by(Incident.last_seen_at.desc())
        .limit(1)
    )
    incident = result.scalars().first()
    if incident is None:
        return None
    window = timedelta(seconds=settings.incident_merge_window_seconds)
    last_seen_at = _as_aware(incident.last_seen_at)
    if now - last_seen_at > window:
        return None
    return incident


async def route_event(session: AsyncSession, row: Event, *, mode: str | None = None) -> Incident | None:
    """Called once per newly created/enriched event. Returns the incident
    the event was routed into, or ``None`` if the event was not
    alert-worthy (disarmed, off-mode zone, or not an intrusion-eligible
    event type) - the event itself is unaffected either way.

    ``mode`` may be pre-captured by the caller and passed through so a
    mode change that happens during a slow enrichment step between event
    creation and this call cannot retroactively change whether an event
    that occurred while armed (or disarmed) is treated as alert-worthy -
    see ``events.py``'s ``create_and_broadcast_event``. When omitted, the
    current mode is looked up fresh (used by direct/legacy callers with no
    such gap)."""
    if row.type not in INTRUSION_EVENT_TYPES:
        return None
    if mode is None:
        mode = await security_modes.get_mode(session)
    zone_kind = await _zone_kind(session, row.camera_id, row.zone)
    if not security_modes.is_alert_armed(mode, zone_kind):
        return None

    now = row.start_time if row.start_time.tzinfo else row.start_time.replace(tzinfo=timezone.utc)
    camera_name = await _camera_name(session, row.camera_id)
    where = f"the {row.zone} zone" if row.zone else "an unzoned area"
    severity = _SEVERITY_BY_EVENT_TYPE.get(row.type, "low")

    async with _lock_for(row.camera_id, row.zone, "intrusion"):
        await _acquire_route_lock(session, row.camera_id, row.zone, "intrusion")
        existing = await _open_incident_for(session, row.camera_id, row.zone, "intrusion", now)
        if existing is not None:
            existing.event_ids = [*existing.event_ids, row.id]
            existing.event_count += 1
            existing.last_seen_at = now
            if _severity_rank(severity) > _severity_rank(existing.severity):
                existing.severity = severity
            existing.summary = (
                f"{existing.event_count} {row.type} detections in {where} on {camera_name} "
                f"since {existing.first_seen_at.strftime('%H:%M')}."
            )
            existing.updated_at = now
            await session.commit()
            await session.refresh(existing)
            incident = existing
            created = False
        else:
            incident = Incident(
                id=_new_id(),
                kind="intrusion",
                status="open",
                severity=severity,
                camera_id=row.camera_id,
                zone=row.zone,
                mode_at_creation=mode,
                event_ids=[row.id],
                event_count=1,
                first_seen_at=now,
                last_seen_at=now,
                summary=f"{row.type.capitalize()} detected in {where} on {camera_name} while {mode}.",
                created_at=now,
                updated_at=now,
            )
            session.add(incident)
            await session.commit()
            await session.refresh(incident)
            created = True

    await audit_service.record(
        session,
        "incident.created" if created else "incident.updated",
        target_type="incident",
        target_id=incident.id,
        details={"kind": incident.kind, "camera_id": incident.camera_id, "event_id": row.id},
    )
    await _maybe_attach_ai_summary(session, incident)
    await _broadcast(incident, "incident.created" if created else "incident.updated")
    return incident


def _severity_rank(severity: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(severity, 0)


async def _maybe_attach_ai_summary(session: AsyncSession, incident: Incident) -> None:
    facts = {
        "kind": incident.kind,
        "camera_id": incident.camera_id,
        "zone": incident.zone,
        "event_count": incident.event_count,
        "severity": incident.severity,
        "mode": incident.mode_at_creation,
        "first_seen_at": incident.first_seen_at.isoformat(),
    }
    try:
        summary = await incident_summary.summarize_incident(facts)
    except Exception:  # noqa: BLE001 - AI summary is best-effort, never fatal
        logger.warning("incident AI summary raised unexpectedly", exc_info=True)
        summary = None
    if summary:
        incident.ai_summary = summary
        await session.commit()
        await session.refresh(incident)


async def raise_camera_health(
    session_factory, camera_id: str, camera_name: str, subtype: str, message: str
) -> Incident | None:
    """Open (or reuse) a camera-health incident. ``subtype`` is one of
    ``offline``/``obstruction``/``frozen``; always raised regardless of
    arming mode (see module docstring)."""
    kind = f"camera_{subtype}"
    now = datetime.now(timezone.utc)
    async with _lock_for(camera_id, None, kind):
        async with session_factory() as session:
            await _acquire_route_lock(session, camera_id, None, kind)
            recent = await session.execute(
                select(Incident)
                .where(Incident.camera_id == camera_id, Incident.kind == kind)
                .order_by(Incident.created_at.desc())
                .limit(1)
            )
            last = recent.scalars().first()
            if last is not None and last.status in ("open", "acknowledged"):
                return last
            if (
                last is not None
                and last.resolved_at is not None
                and now - last.resolved_at < timedelta(seconds=settings.camera_health_dedupe_seconds)
            ):
                return None

            incident = Incident(
                id=_new_id(),
                kind=kind,
                status="open",
                severity=_CAMERA_HEALTH_SEVERITY,
                camera_id=camera_id,
                zone=None,
                mode_at_creation=await security_modes.get_mode(session),
                event_ids=[],
                event_count=0,
                first_seen_at=now,
                last_seen_at=now,
                summary=f"{message} ({camera_name})",
                created_at=now,
                updated_at=now,
            )
            session.add(incident)
            await audit_service.record(
                session,
                "incident.created",
                target_type="incident",
                target_id=incident.id,
                details={"kind": kind, "camera_id": camera_id},
            )
            await session.commit()
            await session.refresh(incident)
    await _broadcast(incident, "incident.created")
    logger.warning("camera health incident raised: %s (%s)", camera_id, kind)
    return incident


async def resolve_camera_health(session_factory, camera_id: str, subtype: str) -> Incident | None:
    kind = f"camera_{subtype}"
    now = datetime.now(timezone.utc)
    async with _lock_for(camera_id, None, kind):
        async with session_factory() as session:
            await _acquire_route_lock(session, camera_id, None, kind)
            result = await session.execute(
                select(Incident)
                .where(
                    Incident.camera_id == camera_id,
                    Incident.kind == kind,
                    Incident.status.in_(("open", "acknowledged")),
                )
                .order_by(Incident.created_at.desc())
                .limit(1)
            )
            incident = result.scalars().first()
            if incident is None:
                return None
            incident.status = "resolved"
            incident.resolved_at = now
            incident.resolved_by = None  # system-resolved (condition cleared), not a human action
            incident.updated_at = now
            await audit_service.record(
                session,
                "incident.auto_resolved",
                target_type="incident",
                target_id=incident.id,
                details={"kind": kind, "camera_id": camera_id},
            )
            await session.commit()
            await session.refresh(incident)
    await _broadcast(incident, "incident.updated")
    return incident


async def escalate_due_incidents(session_factory) -> int:
    """Bump ``escalation_level`` for open, unacknowledged incidents that
    have sat unattended past ``incident_escalation_seconds``. Purely a
    severity/urgency signal for the UI and audit trail - never pages,
    emails, or dispatches anyone."""
    now = datetime.now(timezone.utc)
    threshold = timedelta(seconds=settings.incident_escalation_seconds)
    escalated = 0
    async with session_factory() as session:
        result = await session.execute(
            select(Incident).where(
                Incident.status == "open",
                Incident.escalation_level < settings.incident_max_escalation_level,
            )
        )
        for incident in result.scalars().all():
            baseline = _as_aware(incident.last_escalated_at or incident.created_at)
            if now - baseline < threshold:
                continue
            incident.escalation_level += 1
            incident.last_escalated_at = now
            incident.updated_at = now
            await audit_service.record(
                session,
                "incident.escalated",
                target_type="incident",
                target_id=incident.id,
                details={"escalation_level": incident.escalation_level},
            )
            escalated += 1
            await session.commit()
            await session.refresh(incident)
            await _broadcast(incident, "incident.escalated")
    return escalated


async def acknowledge(session: AsyncSession, incident: Incident, user_id: str | None) -> Incident:
    now = datetime.now(timezone.utc)
    incident.status = "acknowledged"
    incident.acknowledged_by = user_id
    incident.acknowledged_at = now
    incident.updated_at = now
    await audit_service.record(
        session, "incident.acknowledged", actor_user_id=user_id, target_type="incident", target_id=incident.id,
    )
    await session.commit()
    await session.refresh(incident)
    await _broadcast(incident, "incident.updated")
    return incident


async def resolve(session: AsyncSession, incident: Incident, user_id: str | None) -> Incident:
    now = datetime.now(timezone.utc)
    incident.status = "resolved"
    incident.resolved_by = user_id
    incident.resolved_at = now
    incident.updated_at = now
    await audit_service.record(
        session, "incident.resolved", actor_user_id=user_id, target_type="incident", target_id=incident.id,
    )
    await session.commit()
    await session.refresh(incident)
    await _broadcast(incident, "incident.updated")
    return incident


async def get_incident(session: AsyncSession, incident_id: str) -> Incident | None:
    return await session.get(Incident, incident_id)


async def list_incidents(
    session: AsyncSession, *, status: str | None = None, kind: str | None = None, limit: int = 100
) -> list[Incident]:
    statement = select(Incident).order_by(Incident.last_seen_at.desc()).limit(min(max(limit, 1), 500))
    if status is not None:
        statement = statement.where(Incident.status == status)
    if kind is not None:
        statement = statement.where(Incident.kind == kind)
    result = await session.execute(statement)
    return list(result.scalars().all())


async def timeline(session: AsyncSession, incident: Incident) -> list[Event]:
    """The events grouped into this incident, oldest first."""
    if not incident.event_ids:
        return []
    result = await session.execute(
        select(Event).where(Event.id.in_(incident.event_ids)).order_by(Event.start_time.asc())
    )
    return list(result.scalars().all())


async def export_incident(session: AsyncSession, incident: Incident) -> dict:
    """Full evidence export for one incident: the incident record plus every
    grouped event's already-existing (non-secret, non-raw-feed) detail. Never
    includes camera credentials/secrets or a live camera feed - only the
    same event photos/metadata already served by the existing events API."""
    from . import events as event_service

    events = await timeline(session, incident)
    return {
        "incident": to_dict(incident),
        "events": [event_service.to_dict(event) for event in events],
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }


async def _broadcast(incident: Incident, sse_event: str) -> None:
    from . import events as event_service

    payload = to_dict(incident)
    payload["_sse_event"] = sse_event
    await event_service.event_bus.publish(payload)


def to_dict(incident: Incident) -> dict:
    return {
        "id": incident.id,
        "kind": incident.kind,
        "status": incident.status,
        "severity": incident.severity,
        "camera_id": incident.camera_id,
        "zone": incident.zone,
        "mode_at_creation": incident.mode_at_creation,
        "event_ids": list(incident.event_ids or []),
        "event_count": incident.event_count,
        "first_seen_at": incident.first_seen_at.isoformat(),
        "last_seen_at": incident.last_seen_at.isoformat(),
        "acknowledged_by": incident.acknowledged_by,
        "acknowledged_at": incident.acknowledged_at.isoformat() if incident.acknowledged_at else None,
        "resolved_by": incident.resolved_by,
        "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
        "escalation_level": incident.escalation_level,
        "last_escalated_at": incident.last_escalated_at.isoformat() if incident.last_escalated_at else None,
        "summary": incident.summary,
        "ai_summary": incident.ai_summary,
        "created_at": incident.created_at.isoformat(),
        "updated_at": incident.updated_at.isoformat(),
    }
