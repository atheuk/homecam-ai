"""Versioned public API routes (SPEC section 33).

Camera and provider data always flows through the provider registry so
provider failures are isolated; camera/event state is persisted via the
services layer so it survives beyond the in-process provider objects.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..providers.base import CameraNotFoundError, CameraOfflineError, ProviderUnavailableError
from ..providers.mock import mock_eufy_provider, mock_provider
from ..schemas import CameraBatteryIn, CameraStatusIn, MockEventIn, ProviderOutageIn
from ..services import cameras as camera_service
from ..services import events as event_service
from ..services.provider_registry import (
    discover_all_cameras,
    find_provider_for_camera,
    get_all_provider_health,
)

router = APIRouter(prefix="/api/v1")


@router.get("/providers")
async def providers():
    return await get_all_provider_health()


@router.get("/providers/{provider_id}")
async def provider_detail(provider_id: str):
    for health in await get_all_provider_health():
        if health["provider_id"] == provider_id:
            return health
    raise HTTPException(404, "Provider not found")


@router.get("/cameras")
async def cameras(session: AsyncSession = Depends(get_db)):
    discovered = await discover_all_cameras()
    await camera_service.sync_cameras(session, discovered)
    rows = await camera_service.list_cameras(session)
    return [
        {
            "id": row.id, "provider_id": row.provider_id, "name": row.name, "type": row.type,
            "model": row.model, "online": row.online, "status": row.status,
            "battery_level": row.battery_level, "capabilities": row.capabilities,
        }
        for row in rows
    ]


@router.get("/cameras/{camera_id}")
async def camera(camera_id: str, session: AsyncSession = Depends(get_db)):
    row = await camera_service.get_camera(session, camera_id)
    if row is None:
        raise HTTPException(404, "Camera not found")
    return {
        "id": row.id, "provider_id": row.provider_id, "name": row.name, "type": row.type,
        "model": row.model, "online": row.online, "status": row.status,
        "battery_level": row.battery_level, "capabilities": row.capabilities,
    }


@router.get("/cameras/{camera_id}/capabilities")
async def camera_capabilities(camera_id: str):
    provider = find_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        return await provider.get_capabilities(camera_id)
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    except ProviderUnavailableError as exc:
        raise HTTPException(503, f"Provider '{provider.id}' is currently unavailable") from exc


@router.get("/cameras/{camera_id}/snapshot")
async def snapshot(camera_id: str):
    provider = find_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        return Response(await provider.get_snapshot(camera_id), media_type="text/plain")
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    except CameraOfflineError as exc:
        raise HTTPException(503, f"Camera '{camera_id}' is currently offline") from exc
    except ProviderUnavailableError as exc:
        raise HTTPException(503, f"Provider '{provider.id}' is currently unavailable") from exc


@router.get("/cameras/{camera_id}/live")
async def live(camera_id: str):
    provider = find_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        hls_url = await provider.get_live_stream(camera_id)
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    except CameraOfflineError as exc:
        raise HTTPException(503, f"Camera '{camera_id}' is currently offline") from exc
    except ProviderUnavailableError as exc:
        raise HTTPException(503, f"Provider '{provider.id}' is currently unavailable") from exc
    return {"camera_id": camera_id, "hls_url": hls_url, "mode": "mock"}


@router.get("/events")
async def events(limit: int = 50, session: AsyncSession = Depends(get_db)):
    rows = await event_service.list_events(session, limit)
    return [
        {
            "id": row.id, "camera_id": row.camera_id, "type": row.type, "priority": row.priority,
            "source": row.source, "start_time": row.start_time.isoformat(), "description": row.description,
            "metadata": row.event_metadata,
        }
        for row in rows
    ]


@router.post("/mock/events")
async def create_event(payload: MockEventIn, session: AsyncSession = Depends(get_db)):
    provider = find_provider_for_camera(payload.camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    event = provider.event(payload.camera_id, payload.type)
    await event_service.create_and_broadcast_event(session, event)
    return event


@router.post("/mock/cameras/{camera_id}/status")
async def set_camera_status(camera_id: str, payload: CameraStatusIn, session: AsyncSession = Depends(get_db)):
    """Development control to simulate a camera going offline/degraded
    (SPEC section 40/43)."""
    provider = find_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        updated = provider.simulate_status(camera_id, payload.status)
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    await camera_service.upsert_camera(session, updated)
    return updated


@router.post("/mock/cameras/{camera_id}/battery")
async def set_camera_battery(camera_id: str, payload: CameraBatteryIn, session: AsyncSession = Depends(get_db)):
    """Development control to simulate battery drain; automatically raises
    a high-priority ``battery_low`` event under the configurable threshold
    (SPEC section 8.3)."""
    provider = find_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        updated = provider.simulate_battery(camera_id, payload.battery_level)
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await camera_service.upsert_camera(session, updated)
    if updated["battery_level"] is not None and updated["battery_level"] < settings.low_battery_threshold:
        event = provider.event(camera_id, "battery_low")
        event["description"] = f"{updated['name']} battery is low ({updated['battery_level']}%)"
        await event_service.create_and_broadcast_event(session, event)
    return updated


@router.post("/mock/providers/{provider_id}/outage")
async def set_provider_outage(provider_id: str, payload: ProviderOutageIn):
    """Development control to simulate an entire provider (e.g. the Eufy
    HomeBase) becoming unreachable, to verify provider failure isolation."""
    provider = next((p for p in (mock_provider, mock_eufy_provider) if p.id == provider_id), None)
    if provider is None:
        raise HTTPException(404, "Provider not found")
    provider.simulate_outage(payload.unavailable)
    return await provider.get_health()


@router.get("/system/health")
async def health():
    provider_health = await get_all_provider_health()
    overall = "ok" if all(h["status"] != "OFFLINE" for h in provider_health) else "degraded"
    return {"status": overall, "service": "homecam-api", "providers": provider_health}


@router.get("/system/readiness")
async def readiness():
    return {"status": "ready", "database": "configured", "redis": "configured"}


@router.get("/settings")
async def get_settings():
    return {
        "privacy_mode": "LOCAL ONLY",
        "retention_days": 30,
        "ai_provider": settings.ai_provider,
        "low_battery_threshold": settings.low_battery_threshold,
    }


@router.get("/ws")
async def sse():
    async def stream():
        queue = event_service.event_bus.subscribe()
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                event = await queue.get()
                yield f"event: event.created\ndata: {json.dumps(event)}\n\n"
        finally:
            event_service.event_bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")

