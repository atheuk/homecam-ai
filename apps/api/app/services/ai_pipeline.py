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
from ..ai.vision import caption_confirms_person, get_image_captioner, get_image_embedder
from ..config import settings
from ..models.db import AIAnalysis, Event, EventPhoto
from ..providers.base import CameraOfflineError, CameraNotFoundError, ProviderUnavailableError
from . import persons as person_service
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
    """Best-effort mirror of the photo onto ``media_root``.

    The database row written by :func:`_persist_event_photo` is the durable
    copy; this local file is kept only as a debugging/observability aid on
    hosts with a real filesystem and is never what serves the API.
    """
    try:
        root = Path(settings.media_root) / "best-photos"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{event_id}.img"
        path.write_bytes(image)
        return str(path)
    except OSError as exc:
        logger.warning("could not persist best photo for %s: %s", event_id, exc)
        return None


async def _persist_event_photo(
    session: AsyncSession, event_id: str, photo, caption: str | None
) -> None:
    """Upsert the durable copy of an event's photo."""
    existing = await session.get(EventPhoto, event_id)
    if existing is None:
        session.add(
            EventPhoto(
                event_id=event_id,
                image=photo.image,
                content_type=photo.content_type,
                width=photo.width,
                height=photo.height,
                caption=caption,
                created_at=datetime.now(timezone.utc),
            )
        )
        return
    existing.image = photo.image
    existing.content_type = photo.content_type
    existing.width = photo.width
    existing.height = photo.height
    if caption:
        existing.caption = caption


async def _caption_photo(photo) -> str | None:
    captioner = get_image_captioner()
    if captioner is None:
        return None
    try:
        return await captioner.caption_image(photo.image, photo.content_type)
    except Exception as exc:  # noqa: BLE001 - a caption is never worth an event
        logger.warning("photo captioning failed: %s", exc)
        return None


async def _embed_photo(photo) -> list[float]:
    if not settings.person_recognition_enabled:
        return []
    embedder = get_image_embedder()
    try:
        return await embedder.embed_image(photo.image)
    except Exception as exc:  # noqa: BLE001 - recognition degrades, ingest survives
        logger.warning("person embedding failed: %s", exc)
        return []


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

    person_match = None
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

            # Only bother captioning/embedding when a person is actually the
            # subject: a caption of a passing car costs a model call and
            # teaches the identity matcher nothing.
            is_person_photo = photo.detection is not None and photo.detection.label == "person"
            caption = await _caption_photo(photo) if is_person_photo else None
            await _persist_event_photo(session, row.id, photo, caption)

            metadata = dict(row.event_metadata or {})
            metadata["best_photo"] = photo.as_dict()
            if caption:
                metadata["photo_caption"] = caption
            row.event_metadata = metadata

            if is_person_photo and caption_confirms_person(caption):
                embedding = await _embed_photo(photo)
                if embedding:
                    try:
                        person_match = await person_service.record_sighting(
                            session, row, embedding, at=now
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("person matching failed for %s: %s", row.id, exc)

    analysis_row = await _persist_ai_analysis(session, row, camera_name, detections, semantics)
    if analysis_row is not None:
        row.ai_analysis_id = analysis_row.id

    # Once we know *who* it is, say so: "Sarah detected on Front Yard" is the
    # whole point of naming people, and it should be visible without opening
    # the event. Only applied to already-named identities, because
    # "Unknown person 4F2A detected" is noise, not information.
    if person_match is not None and person_match.person.name:
        row.description = f"{person_match.person.name} seen on {camera_name}"[:500]

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
            "person_id": row.person_id,
            "person_name": person_match.person.name if person_match else None,
            "person_display_name": (
                person_service.display_name(person_match.person) if person_match else None
            ),
            "person_confidence": row.person_confidence,
            "person_is_new": person_match.created if person_match else None,
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
