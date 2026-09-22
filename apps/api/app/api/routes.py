"""Versioned public API routes (SPEC section 33).

Camera and provider data always flows through the provider registry so
provider failures are isolated; camera/event state is persisted via the
services layer so it survives beyond the in-process provider objects.
"""
from __future__ import annotations

import base64
import binascii
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models.db import AIAnalysis, Event
from ..providers.base import CameraNotFoundError, CameraOfflineError, ProviderUnavailableError
from ..providers.capabilities import AUDIO_DETECTION
from ..schemas import (
    AudioAnalysisOut,
    CameraBatteryIn,
    CameraStatusIn,
    MockAudioIn,
    MockEventIn,
    ProviderOutageIn,
)
from ..ai.audio import analyze_pcm
from ..services import activities as activity_service
from ..services import cameras as camera_service
from ..services import events as event_service
from ..services.provider_registry import (
    discover_all_cameras,
    find_provider_for_camera,
    get_all_provider_health,
    mock_providers,
)

router = APIRouter(prefix="/api/v1")


def find_mock_provider_for_camera(camera_id: str):
    return next((provider for provider in mock_providers() if provider.has_camera(camera_id)), None)


def _classify_stream_url(stream_url: str) -> tuple[str, bool]:
    """Classify a provider-returned stream URL for safe browser rendering.

    Never used to sanitize credentials out of a URL: providers must not
    return credentialed URLs to this endpoint in the first place (direct
    Dahua mode returns a plain ``rtsp://host/...`` with no embedded auth;
    edge mode never returns raw RTSP at all). This only tells the frontend
    which kind of player (if any) is safe/possible to use.
    """
    lowered = stream_url.lower()
    if lowered.startswith("rtsp://"):
        return "rtsp", False
    if ".m3u8" in lowered or lowered.startswith("hls:"):
        return "hls", True
    if "webrtc" in lowered or lowered.startswith("whep:") or lowered.startswith("whip:"):
        return "webrtc", True
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return "link", True
    return "unknown", False


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
    provider = await find_provider_for_camera(camera_id)
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
    provider = await find_provider_for_camera(camera_id)
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
    provider = await find_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        stream_url = await provider.get_live_stream(camera_id)
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    except CameraOfflineError as exc:
        raise HTTPException(503, f"Camera '{camera_id}' is currently offline") from exc
    except ProviderUnavailableError as exc:
        raise HTTPException(503, f"Provider '{provider.id}' is currently unavailable") from exc
    kind, browser_playable = _classify_stream_url(stream_url)
    return {
        "camera_id": camera_id,
        "hls_url": stream_url,
        "stream_url": stream_url,
        "mode": provider.id,
        "provider_id": provider.id,
        "kind": kind,
        "browser_playable": browser_playable,
    }


@router.get("/events")
async def events(limit: int = 50, session: AsyncSession = Depends(get_db)):
    rows = await event_service.list_events(session, limit)
    return [event_service.to_dict(row) for row in rows]


@router.get("/events/{event_id}")
async def event_detail(event_id: str, session: AsyncSession = Depends(get_db)):
    row = await session.get(Event, event_id)
    if row is None:
        raise HTTPException(404, "Event not found")
    payload = event_service.to_dict(row)
    if row.ai_analysis_id:
        analysis = await session.get(AIAnalysis, row.ai_analysis_id)
        if analysis is not None:
            payload["ai_analysis"] = {
                "id": analysis.id,
                "provider": analysis.provider,
                "model": analysis.model,
                "summary": analysis.summary,
                "objects": analysis.objects,
                "actions": analysis.actions,
                "category": analysis.category,
                "confidence": analysis.confidence,
                "embedding_dimensions": analysis.embedding_dimensions,
                "detections": analysis.detections,
            }
    return payload


@router.get("/activities")
async def activities(limit: int = 50, session: AsyncSession = Depends(get_db)):
    """Cross-camera correlated activities (SPEC sections 18/19/33)."""
    rows = await activity_service.list_activities(session, limit)
    return [activity_service.to_dict(row) for row in rows]


@router.get("/activities/{activity_id}")
async def activity_detail(activity_id: str, session: AsyncSession = Depends(get_db)):
    row = await activity_service.get_activity(session, activity_id)
    if row is None:
        raise HTTPException(404, "Activity not found")
    payload = activity_service.to_dict(row)
    events_result = await event_service.list_events(session, 200)
    members = [event_service.to_dict(e) for e in events_result if e.id in set(payload["event_ids"])]
    payload["events"] = sorted(members, key=lambda item: item["start_time"])
    return payload


@router.post("/mock/events")
async def create_event(payload: MockEventIn, session: AsyncSession = Depends(get_db)):
    provider = find_mock_provider_for_camera(payload.camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    event = provider.event(payload.camera_id, payload.type)
    await event_service.create_and_broadcast_event(session, event)
    return event


@router.post("/mock/cameras/{camera_id}/status")
async def set_camera_status(camera_id: str, payload: CameraStatusIn, session: AsyncSession = Depends(get_db)):
    """Development control to simulate a camera going offline/degraded
    (SPEC section 40/43)."""
    provider = find_mock_provider_for_camera(camera_id)
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
    provider = find_mock_provider_for_camera(camera_id)
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


@router.post("/cameras/{camera_id}/audio/analyze", response_model=AudioAnalysisOut)
async def analyze_audio(
    camera_id: str, payload: MockAudioIn, session: AsyncSession = Depends(get_db)
):
    """Analyze a supplied audio buffer for speech-like activity.

    Gated by the ``audioDetection`` capability: a camera whose provider does
    not expose an audio buffer (or that has the feature disabled) returns
    ``503`` instead of HomeCam inventing audio. This detects *speech-like
    audio activity* only — never transcription or speaker identity.
    """
    provider = await find_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        capabilities = await provider.get_capabilities(camera_id)
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    except ProviderUnavailableError as exc:
        raise HTTPException(503, f"Provider '{provider.id}' is currently unavailable") from exc
    if capabilities.get(AUDIO_DETECTION) != "SUPPORTED":
        raise HTTPException(
            503,
            f"Audio detection is {capabilities.get(AUDIO_DETECTION, 'UNKNOWN')} for camera '{camera_id}'",
        )
    try:
        pcm = base64.b64decode(payload.pcm_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(422, "pcm_base64 must be valid base64") from exc

    analysis = analyze_pcm(pcm, settings.audio_energy_threshold)
    event_id: str | None = None
    if analysis.speech_like:
        mock = find_mock_provider_for_camera(camera_id)
        if mock is not None:
            event = mock.event(camera_id, "motion")
            event["description"] = (
                f"Speech-like audio activity detected on {event.get('camera_name', camera_id)} "
                f"(confidence {analysis.confidence:.2f}); no transcription performed."
            )
            event["tags"] = ["audio", "speech-like"]
            row = await event_service.create_and_broadcast_event(session, event)
            event_id = row.id
    return AudioAnalysisOut(camera_id=camera_id, event_id=event_id, **analysis.as_dict())


@router.post("/mock/providers/{provider_id}/outage")
async def set_provider_outage(provider_id: str, payload: ProviderOutageIn):
    """Development control to simulate an entire provider (e.g. the Eufy
    HomeBase) becoming unreachable, to verify provider failure isolation."""
    provider = next((p for p in mock_providers() if p.id == provider_id), None)
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
        "ai_detector_backend": settings.ai_detector_backend,
        "ai_analysis_enabled": settings.ai_analysis_enabled,
        "audio_detection_enabled": settings.audio_detection_enabled,
        "embedding_dimensions": settings.embedding_dimensions,
        "parked_vehicle_seconds": settings.parked_vehicle_seconds,
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
