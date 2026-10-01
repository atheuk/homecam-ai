"""Security API: arming modes, incidents, and the audit trail.

Every write here is a human action and is always audit-logged (mode changes
inside ``security_modes.set_mode`` itself; incident actions inside
``incidents.py``). Nothing in this router can be triggered by AI/automation.

Auth note: same single-user-is-admin scope limitation as
``app/api/admin_routes.py`` - see that module's docstring. All of these
routes require a valid session like every other authenticated route.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..db import get_db
from ..models.db import Incident, User
from ..schemas import (
    AuditLogOut,
    DeterrenceActionIn,
    DeterrenceActionOut,
    DeterrenceCapabilitiesOut,
    IncidentExportOut,
    IncidentOut,
    SecurityModeIn,
    SecurityModeOut,
)
from ..services import audit as audit_service
from ..services import deterrence as deterrence_service
from ..services import incidents as incident_service
from ..services import security_modes

router = APIRouter(prefix="/api/v1/security", tags=["security"])


@router.get("/mode", response_model=SecurityModeOut)
async def get_mode(session: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)):
    state = await security_modes.get_state(session)
    return security_modes.to_dict(state)


@router.put("/mode", response_model=SecurityModeOut)
async def set_mode(
    payload: SecurityModeIn,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    state = await security_modes.set_mode(session, payload.mode, changed_by=user.id)
    return security_modes.to_dict(state)


@router.get("/incidents", response_model=list[IncidentOut])
async def list_incidents(
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    rows = await incident_service.list_incidents(session, status=status, kind=kind, limit=limit)
    return [incident_service.to_dict(row) for row in rows]


async def _get_incident_or_404(session: AsyncSession, incident_id: str) -> Incident:
    incident = await incident_service.get_incident(session, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@router.get("/incidents/{incident_id}", response_model=IncidentOut)
async def get_incident(
    incident_id: str, session: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)
):
    incident = await _get_incident_or_404(session, incident_id)
    return incident_service.to_dict(incident)


@router.post("/incidents/{incident_id}/acknowledge", response_model=IncidentOut)
async def acknowledge_incident(
    incident_id: str, session: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
):
    incident = await _get_incident_or_404(session, incident_id)
    incident = await incident_service.acknowledge(session, incident, user.id)
    return incident_service.to_dict(incident)


@router.post("/incidents/{incident_id}/resolve", response_model=IncidentOut)
async def resolve_incident(
    incident_id: str, session: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
):
    incident = await _get_incident_or_404(session, incident_id)
    incident = await incident_service.resolve(session, incident, user.id)
    return incident_service.to_dict(incident)


@router.get("/incidents/{incident_id}/export", response_model=IncidentExportOut)
async def export_incident(
    incident_id: str, session: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
):
    """Full evidence export for one incident: the incident plus every
    grouped event's existing photo/metadata. Never includes camera
    credentials/secrets or a live camera feed."""
    incident = await _get_incident_or_404(session, incident_id)
    export = await incident_service.export_incident(session, incident)
    await audit_service.record(
        session, "incident.exported", actor_user_id=user.id, target_type="incident", target_id=incident.id,
    )
    await session.commit()
    return export


@router.get("/audit-log", response_model=list[AuditLogOut])
async def audit_log(
    action: str | None = Query(default=None),
    limit: int = Query(default=200, le=1000),
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    entries = await audit_service.list_entries(session, action=action, limit=limit)
    return [audit_service.to_dict(entry) for entry in entries]


# --- deterrence -------------------------------------------------------------------
#
# Siren/light/voice. Every one of these routes requires an authenticated
# user, and *execution* additionally requires a separate, explicit
# confirmation call naming the exact pending action. There is deliberately
# no automation-reachable path to execution, and no action that contacts
# emergency services. See app/services/deterrence.py and docs/ai-features.md.


@router.get("/deterrence/capabilities", response_model=DeterrenceCapabilitiesOut)
async def deterrence_capabilities(_user: User = Depends(get_current_user)):
    return deterrence_service.capabilities()


@router.get("/deterrence/actions", response_model=list[DeterrenceActionOut])
async def list_deterrence_actions(
    camera_id: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    rows = await deterrence_service.list_actions(session, camera_id=camera_id, limit=limit)
    return [deterrence_service.to_dict(row) for row in rows]


@router.post("/deterrence/actions", response_model=DeterrenceActionOut, status_code=201)
async def request_deterrence_action(
    payload: DeterrenceActionIn,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Propose a deterrent. Always lands in ``pending``; never executes."""
    try:
        row = await deterrence_service.request_action(
            session,
            camera_id=payload.camera_id,
            action=payload.action,
            reason=payload.reason or "",
            incident_id=payload.incident_id,
            requested_by=user.id,
        )
    except deterrence_service.DeterrenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return deterrence_service.to_dict(row)


@router.post("/deterrence/actions/{action_id}/confirm", response_model=DeterrenceActionOut)
async def confirm_deterrence_action(
    action_id: str,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The only path to execution, and it is a human pressing a button."""
    try:
        row = await deterrence_service.confirm_action(session, action_id, confirmed_by=user.id)
    except deterrence_service.DeterrenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return deterrence_service.to_dict(row)


@router.post("/deterrence/actions/{action_id}/cancel", response_model=DeterrenceActionOut)
async def cancel_deterrence_action(
    action_id: str,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        row = await deterrence_service.cancel_action(session, action_id, actor=user.id)
    except deterrence_service.DeterrenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return deterrence_service.to_dict(row)