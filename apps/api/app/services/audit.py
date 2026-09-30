"""Append-only audit trail (SPEC follow-up: audit trail).

Records who did what security-relevant action, to what, and when. Never
records camera imagery/content. Every write here is best-effort in the sense
that a failure to audit-log must never block or fail the action it is
recording — callers should log-and-continue, not audit-then-act.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import AuditLog


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
    entry = AuditLog(
        id=str(uuid.uuid4()),
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details or {},
        created_at=datetime.now(timezone.utc),
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
