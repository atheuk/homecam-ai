"""Admin API for data retention.

Same single-user-is-admin scope limitation as ``app/api/admin_routes.py``.

The purge endpoint defaults to a dry run and must be asked explicitly to
delete, because the one thing a retention feature must never do is surprise
somebody by removing footage they still wanted.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..db import get_db
from ..models.db import User
from ..schemas import RetentionPolicyOut, RetentionPurgeIn
from ..services import audit as audit_service
from ..services import retention as retention_service

router = APIRouter(prefix="/api/v1/admin/retention", tags=["admin"])


@router.get("", response_model=RetentionPolicyOut)
async def get_retention(
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """The configured policy plus a dry-run count of what it would delete."""
    report = await retention_service.run(session, dry_run=True)
    return {"policy": retention_service.policy(), "report": report.to_dict()}


@router.post("/purge", response_model=RetentionPolicyOut)
async def purge(
    payload: RetentionPurgeIn | None = None,
    dry_run: bool | None = Query(default=None),
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Run one retention pass now. Dry run unless explicitly told otherwise."""
    requested = payload.dry_run if payload is not None else True
    if dry_run is not None:
        requested = dry_run
    report = await retention_service.run(session, dry_run=requested)
    await audit_service.record(
        session,
        "retention.purge_dry_run" if requested else "retention.purged",
        actor_user_id=user.id,
        target_type="retention",
        target_id="policy",
        details=report.to_dict(),
    )
    return {"policy": retention_service.policy(), "report": report.to_dict()}
