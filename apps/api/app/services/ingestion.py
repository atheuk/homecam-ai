"""Continuous camera event ingestion (SPEC section 12).

Nothing in this codebase used to *trigger* the event pipeline from real
camera activity: events only ever appeared via the ``/mock/events`` test
endpoint. :class:`IngestionService` closes that gap by periodically
snapshotting every online, snapshot-capable camera across every provider,
running it through the configured local detector, and creating a real event
whenever the detector actually sees something.

This deliberately reuses the exact same pipeline as every other event
source (:func:`app.services.events.create_and_broadcast_event`) rather than
re-implementing persistence/enrichment/correlation/broadcast — the initial
event this module creates only needs to be "close enough" (a bare
``motion``/best-guess label); :func:`app.services.ai_pipeline.enrich_event`
re-samples fresh frames and re-runs the detector itself, then reclassifies
the event's final type/zone/tags from what it actually sees.

Because :class:`app.ai.detector.MockDetector` cannot see pixels and
intentionally returns no detections for a bare/unlabeled poll (SPEC 15), this
loop is a safe no-op under the default ``mock`` backend: it will run, find
nothing, and never spam an event. Only an opt-in pixel-aware backend
(``opencv``/``onnx``) makes it actually produce events, which is the
intended gate for "genuine, not invented, activity" (SPEC 43).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from ..ai.detector import DetectionContext, get_detector
from ..config import settings
from ..db import SessionLocal
from ..providers.base import CameraNotFoundError, CameraOfflineError, ProviderUnavailableError
from . import events as event_service
from .provider_registry import discover_all_cameras, find_provider_for_camera

logger = logging.getLogger(__name__)


def _event_type_for(labels: set[str]) -> str:
    # "start with people": if a person is among what was detected, that is
    # always the headline event type, even alongside a vehicle/animal/etc.
    if "person" in labels:
        return "person"
    return sorted(labels)[0]


async def poll_once(session_factory=SessionLocal) -> int:
    """Run a single ingestion pass over every discovered camera.

    Returns the number of events created, mainly so tests/logging can
    observe progress without depending on internal state.
    """
    detector = get_detector()
    created = 0
    for camera in await discover_all_cameras(settings.camera_discovery_cache_seconds):
        camera_id = camera.get("id")
        if not camera_id or not camera.get("online"):
            continue
        if camera.get("capabilities", {}).get("snapshot") != "SUPPORTED":
            continue
        if not _cooldown_elapsed(camera_id):
            continue
        provider = await find_provider_for_camera(camera_id)
        if provider is None:
            continue
        try:
            image = await provider.get_snapshot(camera_id)
        except (CameraOfflineError, CameraNotFoundError, ProviderUnavailableError) as exc:
            logger.debug("ingestion snapshot unavailable for %s: %s", camera_id, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - one bad camera must not stop the loop
            logger.warning("ingestion snapshot failed for %s: %s", camera_id, exc)
            continue

        camera_name = str(camera.get("name") or camera_id)
        try:
            detections = detector.detect(image, DetectionContext(camera_id=camera_id, camera_name=camera_name))
        except Exception as exc:  # noqa: BLE001 - detector must not break ingestion
            logger.warning("ingestion detection failed for %s: %s", camera_id, exc)
            continue
        if not detections:
            continue

        labels = {detection.label for detection in detections}
        event_type = _event_type_for(labels)
        now = datetime.now(timezone.utc)
        event = {
            "id": "evt-" + uuid.uuid4().hex[:16],
            "camera_id": camera_id,
            "camera_name": camera_name,
            "type": event_type,
            "priority": "high" if event_type == "person" else "normal",
            "source": "local-ai",
            "start_time": now.isoformat(),
            "description": f"{event_type.replace('_', ' ').title()} detected on {camera_name}",
        }
        async with session_factory() as session:
            await event_service.create_and_broadcast_event(session, event)
        _mark_created(camera_id)
        created += 1
    return created


# Per-camera cooldown so continued presence doesn't create a new event every
# poll interval. Process-wide and deliberately simple (a dict, not a DB
# table): losing it on restart just means the first post-restart detection
# creates one event immediately, which is harmless.
_last_event_at: dict[str, float] = {}


def _cooldown_elapsed(camera_id: str) -> bool:
    last = _last_event_at.get(camera_id)
    return last is None or (time.monotonic() - last) >= settings.event_cooldown_seconds


def _mark_created(camera_id: str) -> None:
    _last_event_at[camera_id] = time.monotonic()


def reset_cooldowns() -> None:
    """Test hook: forget every camera's cooldown timer."""
    _last_event_at.clear()


class IngestionService:
    """Owns the background asyncio task that periodically calls
    :func:`poll_once`. Started/stopped from the FastAPI lifespan so it shares
    the API process's provider registry, DB, and (for edge-mode Dahua) its
    already-authenticated Tailscale network path."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="event-ingestion")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
                logger.exception("event ingestion pass failed")
            try:
                await asyncio.sleep(settings.event_poll_interval_seconds)
            except asyncio.CancelledError:
                raise


ingestion_service = IngestionService()
