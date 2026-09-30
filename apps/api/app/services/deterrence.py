"""Deterrence actions (siren / light / voice) - request, confirm, execute.

SimpliSafe-style "proactive deterrence", with the one property that makes
it acceptable in this codebase: **HomeCam never triggers a deterrent on its
own.** Every action follows the same three-step path:

1. *request* - anything (a UI button, an incident view) may create a
   ``pending`` row saying what is proposed and why;
2. *confirm* - an authenticated human explicitly confirms that exact
   action id, within a short TTL;
3. *execute* - only then does the provider run, and the whole thing is
   written to the append-only audit log.

There is deliberately no code path from detection to execution. There is
also deliberately no "emergency", "dispatch" or "call" action: HomeCam
does not contact emergency services, automatically or otherwise. See
docs/ai-features.md.

The default provider is a no-op that records what *would* have happened,
so the feature is safe to leave wired up on hardware that has no siren.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models.db import DeterrenceAction
from . import audit as audit_service

logger = logging.getLogger(__name__)

#: The complete set of permitted actions. Intentionally closed, and
#: intentionally contains nothing that contacts a third party.
ACTIONS = ("siren", "light", "voice")

STATUS_PENDING = "pending"
STATUS_EXECUTED = "executed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"


class DeterrenceError(RuntimeError):
    """Raised for every refusal, so callers can map one exception to 4xx."""


@dataclass(frozen=True)
class DeterrenceCapability:
    action: str
    supported: bool
    detail: str


class MockDeterrenceProvider:
    """No-op provider: reports capability, performs nothing physical."""

    name = "mock"

    def capabilities(self) -> list[DeterrenceCapability]:
        return [
            DeterrenceCapability(action, True, "simulated only - no hardware is driven")
            for action in ACTIONS
        ]

    async def execute(self, action: str, camera_id: str) -> str:
        logger.info("mock deterrence %s on %s (no-op)", action, camera_id)
        return f"simulated {action} on {camera_id}"


_provider = MockDeterrenceProvider()


def get_provider() -> MockDeterrenceProvider:
    return _provider


def capabilities() -> dict:
    provider = get_provider()
    return {
        "enabled": settings.deterrence_enabled,
        "provider": provider.name,
        "requires_human_confirmation": True,
        "actions": [
            {"action": cap.action, "supported": cap.supported, "detail": cap.detail}
            for cap in provider.capabilities()
        ],
    }


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def to_dict(row: DeterrenceAction) -> dict:
    return {
        "id": row.id,
        "camera_id": row.camera_id,
        "action": row.action,
        "status": row.status,
        "reason": row.reason,
        "incident_id": row.incident_id,
        "requested_by": row.requested_by,
        "confirmed_by": row.confirmed_by,
        "result": row.result,
        "created_at": _as_aware(row.created_at).isoformat(),
        "expires_at": _as_aware(row.expires_at).isoformat(),
        "resolved_at": _as_aware(row.resolved_at).isoformat() if row.resolved_at else None,
    }


async def _acquire_action_lock(session: AsyncSession, action_id: str) -> None:
    """Serialize confirm-then-execute for one action across replicas.

    Without this, two simultaneous confirmations of the same pending action
    could both read ``pending`` and both execute - a doubled siren. Same
    mechanism as ``app.services.incidents._acquire_route_lock``: a
    transactional advisory lock on PostgreSQL, and a no-op on SQLite where
    the single-writer file already serializes writers.
    """
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"deterrence|{action_id}"},
    )


async def request_action(
    session: AsyncSession,
    *,
    camera_id: str,
    action: str,
    reason: str = "",
    incident_id: str | None = None,
    requested_by: str | None = None,
) -> DeterrenceAction:
    """Create a ``pending`` action. Nothing is executed here, ever."""
    if not settings.deterrence_enabled:
        raise DeterrenceError("Deterrence is disabled. Enable it in settings first.")
    if action not in ACTIONS:
        raise DeterrenceError(f"Unsupported deterrence action '{action}'. Allowed: {', '.join(ACTIONS)}.")

    now = datetime.now(timezone.utc)
    row = DeterrenceAction(
        id=str(uuid.uuid4()),
        camera_id=camera_id,
        action=action,
        status=STATUS_PENDING,
        reason=reason[:300],
        incident_id=incident_id,
        requested_by=requested_by,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.deterrence_confirmation_ttl_seconds),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await audit_service.record(
        session,
        "deterrence.requested",
        actor_user_id=requested_by,
        target_type="deterrence",
        target_id=row.id,
        details={"camera_id": camera_id, "action": action, "reason": reason[:300]},
    )
    return row


async def confirm_action(
    session: AsyncSession, action_id: str, *, confirmed_by: str
) -> DeterrenceAction:
    """Execute a pending action after an explicit human confirmation.

    ``confirmed_by`` is the authenticated user id supplied by the route's
    auth dependency - there is no way to reach this function without one.
    """
    if not settings.deterrence_enabled:
        raise DeterrenceError("Deterrence is disabled. Enable it in settings first.")

    await _acquire_action_lock(session, action_id)
    row = await session.get(DeterrenceAction, action_id)
    if row is None:
        raise DeterrenceError("No such deterrence action.")
    if row.status != STATUS_PENDING:
        raise DeterrenceError(f"This action is already {row.status}.")

    now = datetime.now(timezone.utc)
    if now > (_as_aware(row.expires_at) or now):
        row.status = STATUS_EXPIRED
        row.resolved_at = now
        await session.commit()
        await session.refresh(row)
        await audit_service.record(
            session,
            "deterrence.expired",
            actor_user_id=confirmed_by,
            target_type="deterrence",
            target_id=row.id,
            details={"camera_id": row.camera_id, "action": row.action},
        )
        raise DeterrenceError("This confirmation expired. Request the action again.")

    try:
        result = await get_provider().execute(row.action, row.camera_id)
        row.status = STATUS_EXECUTED
        row.result = str(result)[:300]
    except Exception as exc:  # noqa: BLE001 - surface the failure, never retry silently
        logger.exception("deterrence execution failed for %s", row.id)
        row.status = STATUS_FAILED
        row.result = str(exc)[:300]
    row.confirmed_by = confirmed_by
    row.resolved_at = now
    await session.commit()
    await session.refresh(row)
    await audit_service.record(
        session,
        "deterrence.executed" if row.status == STATUS_EXECUTED else "deterrence.failed",
        actor_user_id=confirmed_by,
        target_type="deterrence",
        target_id=row.id,
        details={"camera_id": row.camera_id, "action": row.action, "result": row.result},
    )
    return row


async def cancel_action(session: AsyncSession, action_id: str, *, actor: str) -> DeterrenceAction:
    await _acquire_action_lock(session, action_id)
    row = await session.get(DeterrenceAction, action_id)
    if row is None:
        raise DeterrenceError("No such deterrence action.")
    if row.status != STATUS_PENDING:
        raise DeterrenceError(f"This action is already {row.status}.")
    row.status = STATUS_CANCELLED
    row.resolved_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    await audit_service.record(
        session,
        "deterrence.cancelled",
        actor_user_id=actor,
        target_type="deterrence",
        target_id=row.id,
        details={"camera_id": row.camera_id, "action": row.action},
    )
    return row


async def list_actions(
    session: AsyncSession, *, camera_id: str | None = None, limit: int = 50
) -> list[DeterrenceAction]:
    statement = select(DeterrenceAction).order_by(DeterrenceAction.created_at.desc()).limit(limit)
    if camera_id:
        statement = statement.where(DeterrenceAction.camera_id == camera_id)
    return list((await session.execute(statement)).scalars())
