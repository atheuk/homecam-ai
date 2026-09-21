"""Event analysis pipeline (SPEC section 12), applied to every provider.

Pipeline order mirrors the spec exactly::

    normalize -> persist -> snapshot -> local detector -> AI analysis
    -> embedding -> notification rules -> activity correlation -> realtime

This module owns the stages between *persist* and *realtime*. It is provider
independent by construction: it only ever receives a camera id, a normalized
event dict and raw snapshot bytes obtained through the generic provider
contract, so Dahua channels, the Eufy T8210 doorbell and mock cameras are
analyzed by exactly the same code.

Failure policy (SPEC 43): every stage is defensive. A detector, AI provider
or snapshot failure is logged and the event survives unenriched — analysis
must never break ingestion.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from ..ai import best_photo as best_photo_module
from ..ai.detector import ANIMAL_CLASSES, VEHICLE_CLASSES, Detection, DetectionContext, get_detector
from ..ai.dwell import dwell_tracker
from ..ai.provider import AnalysisContext, get_ai_provider
from ..ai.schemas import GroundingError
from ..ai.semantics import derive_semantics
from ..config import settings
from ..models.db import AIAnalysis, Event
from ..providers.base import CameraOfflineError, CameraNotFoundError, ProviderUnavailableError
from . import zones as zone_service

logger = logging.getLogger(__name__)

# Classes worth keeping a good representative photo of.
BEST_PHOTO_TARGETS = frozenset({"person", "package"}) | VEHICLE_CLASSES | ANIMAL_CLASSES

# Events that describe device state rather than something in front of the
# lens. Running detection on them would attach unrelated objects to a
# battery warning, so they are skipped entirely.
NON_VISUAL_EVENT_TYPES = frozenset({"battery_low"})


async def _sample_frames(provider, camera_id: str, count: int) -> list[bytes]:
    """Sample a few snapshots around the trigger via the generic contract.

    Uses only ``get_snapshot`` so no provider needs new streaming infra.
    """
    frames: list[bytes] = []
    for _ in range(max(1, count)):
        try:
            frames.append(await provider.get_snapshot(camera_id))
        except (CameraOfflineError, CameraNotFoundError, ProviderUnavailableError) as exc:
            logger.info("snapshot unavailable for %s: %s", camera_id, exc)
            break
        except Exception as exc:  # noqa: BLE001 - analysis must not break ingest
            logger.warning("snapshot failed for %s: %s", camera_id, exc)
            break
    return frames


def _store_best_photo(event_id: str, image: bytes) -> str | None:
    try:
        root = Path(settings.media_root) / "best-photos"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{event_id}.img"
        path.write_bytes(image)
        return str(path)
    except OSError as exc:
        logger.warning("could not persist best photo for %s: %s", event_id, exc)
        return None


async def enrich_event(session: AsyncSession, row: Event, event: dict) -> dict:
    """Run detection/zone/AI stages for a persisted event row.

    Returns the normalized event dict augmented with everything that was
    derived, and mutates ``row`` in place (caller commits).
    """
    if not settings.ai_analysis_enabled:
        return event
    if row.type in NON_VISUAL_EVENT_TYPES:
        return event

    from .provider_registry import find_provider_for_camera

    camera_name = str(event.get("camera_name") or row.camera_id)
    now = row.start_time if row.start_time.tzinfo else row.start_time.replace(tzinfo=timezone.utc)

    provider = None
    try:
        provider = await find_provider_for_camera(row.camera_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider lookup failed for %s: %s", row.camera_id, exc)

    frames: list[bytes] = []
    if provider is not None:
        frames = await _sample_frames(provider, row.camera_id, settings.best_photo_frames)

    detector = get_detector()
    context = DetectionContext(
        camera_id=row.camera_id, camera_name=camera_name, event_type=row.type
    )
    detections: list[Detection] = []
    if frames:
        try:
            detections = detector.detect(frames[0], context)
        except Exception as exc:  # noqa: BLE001
            logger.warning("local detector failed for %s: %s", row.camera_id, exc)

    zones = await zone_service.zones_for_camera(session, row.camera_id)
    semantics = derive_semantics(
        camera_id=row.camera_id,
        camera_name=camera_name,
        base_event_type=row.type,
        detections=detections,
        zones=zones,
        tracker=dwell_tracker,
        at=now,
        parked_after_seconds=settings.parked_vehicle_seconds,
    )

    row.type = semantics.type
    row.zone = semantics.zone
    row.tags = list(semantics.tags)
    if semantics.description:
        row.description = semantics.description[:500]
    row.source = "local-ai" if detections else row.source

    if frames and settings.best_photo_enabled and detections:
        try:
            photo = best_photo_module.select_best_photo(
                frames, detector, context, BEST_PHOTO_TARGETS
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("best-photo selection failed for %s: %s", row.camera_id, exc)
            photo = None
        if photo is not None:
            stored = _store_best_photo(row.id, photo.image)
            row.best_photo_path = stored
            row.thumbnail_path = stored
            metadata = dict(row.event_metadata or {})
            metadata["best_photo"] = photo.as_dict()
            row.event_metadata = metadata

    analysis_row = await _persist_ai_analysis(session, row, camera_name, detections, semantics)
    if analysis_row is not None:
        row.ai_analysis_id = analysis_row.id

    enriched = dict(event)
    enriched.update(
        {
            "type": row.type,
            "zone": row.zone,
            "tags": row.tags,
            "description": row.description,
            "source": row.source,
            "thumbnail_path": row.thumbnail_path,
            "best_photo_path": row.best_photo_path,
            "ai_analysis_id": row.ai_analysis_id,
            "detections": [detection.as_dict() for detection in detections],
        }
    )
    return enriched


async def _persist_ai_analysis(
    session: AsyncSession, row: Event, camera_name: str, detections: list[Detection], semantics
) -> AIAnalysis | None:
    """AI analysis + embedding persistence (SPEC section 32)."""
    ai_provider = get_ai_provider()
    context = AnalysisContext(
        camera_id=row.camera_id,
        camera_name=camera_name,
        event_type=row.type,
        detections=detections,
        zone=row.zone,
        tags=list(row.tags or []),
    )
    try:
        analysis = await ai_provider.analyze_image(b"", context)
    except GroundingError as exc:
        logger.error("discarding ungrounded AI analysis for %s: %s", row.id, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI analysis failed for %s: %s", row.id, exc)
        return None

    try:
        embedding = await ai_provider.create_embedding(analysis.summary)
    except Exception as exc:  # noqa: BLE001
        logger.warning("embedding failed for %s: %s", row.id, exc)
        embedding = []

    analysis_row = AIAnalysis(
        id=str(uuid.uuid4()),
        event_id=row.id,
        provider=ai_provider.name,
        model=ai_provider.model,
        summary=analysis.summary[:500],
        objects=list(analysis.objects),
        actions=list(analysis.actions),
        category=analysis.event_category,
        confidence=analysis.confidence,
        embedding=embedding,
        embedding_dimensions=len(embedding),
        detections=[detection.as_dict() for detection in detections],
        created_at=datetime.now(timezone.utc),
    )
    session.add(analysis_row)
    return analysis_row
