"""Append-only audit trail (SPEC follow-up: audit trail).

Records who did what security-relevant action, to what, and when. Never
records camera imagery/content. Every write here is best-effort in the sense
that a failure to audit-log must never block or fail the action it is
recording — callers should log-and-continue, not audit-then-act.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import AuditLog

# Ordering guard. ``audit_logs`` has no sequence column, so entries written in
# the same clock tick (three rapid mode changes on a fast host) sort
# arbitrarily and "most recent first" stops being true. Timestamps issued by
# this process are therefore forced to be strictly increasing. Two replicas
# writing inside the same microsecond is a genuine tie with no real ordering,
# so this deliberately only guarantees monotonicity per process.
_last_timestamp: datetime | None = None
_TICK = timedelta(microseconds=1)


def _next_timestamp() -> datetime:
    global _last_timestamp
    now = datetime.now(timezone.utc)
    if _last_timestamp is not None and now <= _last_timestamp:
        now = _last_timestamp + _TICK
    _last_timestamp = now
    return now


def _entry(
    action: str,
    *,
    actor_user_id: str | None = None,
    actor_label: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict | None = None,
) -> AuditLog:
    return AuditLog(
        id=str(uuid.uuid4()),
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details or {},
        created_at=_next_timestamp(),
    )


def stage(
    session: AsyncSession,
    action: str,
    *,
    actor_user_id: str | None = None,
    actor_label: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict | None = None,
) -> AuditLog:
    """Add an audit entry without committing.

    For actions whose own write is already part of an open transaction (the
    scheduler's atomic mode claim, for instance), committing the audit entry
    separately would leave a window where the action is durable but its audit
    record is not — and the claim can never be retried. Such callers stage the
    entry and let the single commit cover both.
    """
    entry = _entry(
        action,
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        target_type=target_type,
        target_id=target_id,
        details=details,
    )
    session.add(entry)
    return entry


async def record(
    session: AsyncSession,
    action: str,
    *,
    actor_user_id: str | None = None,
    actor_label: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict | None = None,
) -> AuditLog:
    entry = _entry(
        action,
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        target_type=target_type,
        target_id=target_id,
        details=details,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


async def list_entries(
    session: AsyncSession,
    *,
    action: str | None = None,
    limit: int = 200,
) -> list[AuditLog]:
    statement = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(max(limit, 1), 1000))
    if action is not None:
        statement = statement.where(AuditLog.action == action)
    result = await session.execute(statement)
    return list(result.scalars().all())


def to_dict(entry: AuditLog) -> dict:
    return {
        "id": entry.id,
        "actor_user_id": entry.actor_user_id,
        "actor_label": entry.actor_label,
        "action": entry.action,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "details": entry.details,
        "created_at": entry.created_at.isoformat(),
    }
