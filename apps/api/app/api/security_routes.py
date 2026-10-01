"""Security API: arming modes, incidents, and the audit trail.

Every write here is a human action and is always audit-logged (mode changes
inside ``security_modes.set_mode`` itself; incident actions inside
``incidents.py``). Nothing in this router can be triggered by AI/automation.

Auth note: same single-user-is-admin scope limitation as
``app/api/admin_routes.py`` - see that module's docstring. All of these
routes require a valid session like every other authenticated route, with
one deliberate exception: ``POST /mode/integration`` is authenticated by a
shared secret instead of a user session, so an external presence automation
(Home Assistant) can arm the house without holding a login. It fails closed
when no secret is configured and is audit-logged on every accepted call.
``tests/test_route_auth.py`` pins that exception explicitly.
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..config import settings
from ..db import get_db
from ..models.db import ArmingSchedule, Incident, IncidentClip, User
from ..schemas import (
    ArmingScheduleIn,
    ArmingScheduleOut,
    ArmingScheduleUpdate,
    AuditLogOut,
    DeterrenceActionIn,
    DeterrenceActionOut,
    DeterrenceCapabilitiesOut,
    IncidentExportOut,
    IncidentOut,
    IntegrationModeIn,
    RetentionHoldIn,
    SecurityModeIn,
    SecurityModeOut,
)
from ..services import arming_schedules as schedule_service
from ..services import audit as audit_service
from ..services import deterrence as deterrence_service
from ..services import incidents as incident_service
from ..services import security_modes

router = APIRouter(prefix="/api/v1/security", tags=["security"])


async def require_integration_token(
    x_homecam_token: str | None = Header(default=None, alias="X-HomeCam-Token"),
) -> str:
    """Shared-secret auth for external presence automations.

    Fails closed: with no ``security_integration_token`` configured the
    endpoint is disabled rather than open, so a deployment that never set
    the secret cannot be armed/disarmed by anyone who finds the URL. The
    comparison is constant-time, and the caller is always audit-logged by
    the route itself.
    """
    expected = settings.security_integration_token
    if not expected:
        raise HTTPException(status_code=503, detail="Integration endpoint is not configured")
    if not x_homecam_token or not hmac.compare_digest(x_homecam_token, expected):
        raise HTTPException(status_code=401, detail="Invalid integration token")
    return "integration"


@router.get("/mode", response_model=SecurityModeOut)
async def get_mode(session: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)):
    state = await security_modes.get_state(session)
    payload = security_modes.to_dict(state)
    payload["schedule"] = await schedule_service.status(session)
    return payload


@router.put("/mode", response_model=SecurityModeOut)
async def set_mode(
    payload: SecurityModeIn,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Set the mode by hand.

    Holds until the next scheduled transition - see
    ``app.services.arming_schedules`` for why that needs no expiry field.
    """
    state = await security_modes.set_mode(session, payload.mode, changed_by=user.id)
    body = security_modes.to_dict(state)
    body["schedule"] = await schedule_service.status(session)
    return body


@router.post("/mode/integration", response_model=SecurityModeOut)
async def set_mode_from_integration(
    payload: IntegrationModeIn,
    session: AsyncSession = Depends(get_db),
    _token: str = Depends(require_integration_token),
):
    """Token-authenticated mode change for Home Assistant presence automations.

    Treated exactly like a manual change: audited, and overriding the
    schedule only until the next scheduled transition.
    """
    state = await security_modes.set_mode(
        session,
        payload.mode,
        changed_by=None,
        source="integration",
        actor_label=payload.source,
    )
    body = security_modes.to_dict(state)
    body["schedule"] = await schedule_service.status(session)
    return body


# --- arming schedules --------------------------------------------------------------


@router.get("/schedules", response_model=list[ArmingScheduleOut])
async def list_schedules(session: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)):
    rows = await schedule_service.list_schedules(session)
    return [schedule_service.to_dict(row) for row in rows]


@router.post("/schedules", response_model=ArmingScheduleOut, status_code=201)
async def create_schedule(
    payload: ArmingScheduleIn,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        row = await schedule_service.create_schedule(
            session,
            name=payload.name,
            mode=payload.mode,
            days_of_week=payload.days_of_week,
            start_time=payload.start_time,
            end_time=payload.end_time,
            enabled=payload.enabled,
            priority=payload.priority,
            actor_user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return schedule_service.to_dict(row)


async def _get_schedule_or_404(session: AsyncSession, schedule_id: str) -> ArmingSchedule:
    row = await session.get(ArmingSchedule, schedule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return row


@router.put("/schedules/{schedule_id}", response_model=ArmingScheduleOut)
async def update_schedule(
    schedule_id: str,
    payload: ArmingScheduleUpdate,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = await _get_schedule_or_404(session, schedule_id)
    try:
        row = await schedule_service.update_schedule(
            session,
            row,
            name=payload.name,
            mode=payload.mode,
            days_of_week=payload.days_of_week,
            start_time=payload.start_time,
            end_time=payload.end_time,
            enabled=payload.enabled,
            priority=payload.priority,
            actor_user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return schedule_service.to_dict(row)


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: str,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = await _get_schedule_or_404(session, schedule_id)
    await schedule_service.delete_schedule(session, row, actor_user_id=user.id)
    return None


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


@router.get("/incidents/{incident_id}/clip")
async def incident_clip(
    incident_id: str,
    download: bool = False,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    incident = await _get_incident_or_404(session, incident_id)
    if incident.clip_status != "ready":
        raise HTTPException(status_code=404, detail="No clip available for this incident")
    clip = await session.get(IncidentClip, incident_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="No clip available for this incident")
    return Response(
        content=clip.video,
        media_type="video/mp4",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{incident_id}.mp4"',
        },
    )


@router.put("/incidents/{incident_id}/clip/hold", response_model=IncidentOut)
async def hold_incident_clip(
    incident_id: str,
    payload: RetentionHoldIn,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    incident = await _get_incident_or_404(session, incident_id)
    if incident.clip_status != "ready":
        raise HTTPException(status_code=409, detail="No completed clip to keep")
    incident.clip_hold = payload.hold
    await audit_service.record(
        session,
        "incident.clip_hold_set" if payload.hold else "incident.clip_hold_cleared",
        actor_user_id=user.id,
        target_type="incident",
        target_id=incident_id,
        details={"hold": payload.hold},
    )
    await session.commit()
    await session.refresh(incident)
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