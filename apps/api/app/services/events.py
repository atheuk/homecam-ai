"""Event persistence and real-time broadcast (SPEC sections 12, 31, 34).

Events are written to the database as the durable source of truth, and
simultaneously fanned out to any subscribed SSE clients through an
in-process ``EventBus``. Persistence and broadcast are deliberately kept
independent: a slow/disconnected subscriber must never block a write, and a
restart must never lose already-persisted events.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from ..models.db import Event, EventEvidence
from . import activities as activity_service
from . import ai_pipeline
from . import incidents as incident_service
from . import incident_clips
from . import security_modes
from . import signals as signal_service
from . import scene_dedup
from ..config import settings

logger = logging.getLogger(__name__)


class EventBus:
    """Simple in-process pub/sub used to fan out newly created events to
    connected SSE clients. Not persisted; persistence is handled separately
    by ``persist_event``."""

    def __init__(self) -> None:
        self.subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self.subscribers:
            self.subscribers.remove(queue)

    async def publish(self, event: dict) -> None:
        for queue in list(self.subscribers):
            await queue.put(event)


event_bus = EventBus()


async def persist_event(session: AsyncSession, event: dict) -> Event:
    row = Event(
        id=event["id"],
        camera_id=event["camera_id"],
        type=event["type"],
        priority=event["priority"],
        source=event["source"],
        start_time=datetime.fromisoformat(event["start_time"]),
        description=event["description"],
        event_metadata=event.get("metadata", {}),
        zone=event.get("zone"),
        tags=list(event.get("tags", [])),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _attach_evidence(session: AsyncSession, row: Event, images: dict[str, bytes]) -> None:
    """Store evidence rows and advertise their URLs in one transaction.

    The ``image_url`` metadata and the :class:`EventEvidence` rows commit
    together, so an event never advertises evidence URLs that would 404. On
    failure the caller rolls back and the event keeps its URL-free metadata.
    """
    now = datetime.now(timezone.utc)
    for label, image in images.items():
        session.add(EventEvidence(event_id=row.id, label=label, image=image, content_type="image/jpeg", created_at=now))
    row.event_metadata = _with_evidence_urls(row.id, dict(row.event_metadata or {}), images)
    await session.commit()
    await session.refresh(row)


def evidence_url(event_id: str, label: str) -> str:
    return f"/api/v1/events/{event_id}/evidence/{label}"


def _with_evidence_urls(event_id: str, metadata: dict, images: dict[str, bytes]) -> dict:
    metadata = dict(metadata)
    mailbox = dict(metadata.get("mailbox") or {})
    for label in images:
        entry = dict(mailbox.get(label) or {})
        entry["image"] = True
        entry["image_url"] = evidence_url(event_id, label)
        mailbox[label] = entry
    metadata["mailbox"] = mailbox
    return metadata


async def _recover(session: AsyncSession, row: Event, event_id: str) -> None:
    """Roll a failed enrichment stage back and leave ``row`` usable.

    ``rollback()`` expires every attribute on ``row``, so without an
    explicit reload the next *plain* attribute access - even ``row.id`` -
    lazy-loads from a sync context and raises ``MissingGreenlet``. That
    turned one failed stage into a failure of everything downstream,
    including the caller's own logging: a failed incident routing took the
    scene-transition caller down with it.

    If even the reload fails the row stays expired, so restore its identity
    from the id captured before enrichment. Callers are promised ``row.id``
    (scene ingestion links the event to its track with it); that promise has
    to hold without touching the database, however badly the stage failed.
    """
    await session.rollback()
    try:
        await session.refresh(row)
    except Exception:  # noqa: BLE001 - recovery must never raise
        logger.exception("could not reload event %s after a failed stage", event_id)
        set_committed_value(row, "id", event_id)


async def create_and_broadcast_event(
    session: AsyncSession,
    event: dict,
    trigger_frame: bytes | None = None,
    frames: list[bytes] | None = None,
    evidence_images: dict[str, bytes] | None = None,
) -> Event:
    """Run the SPEC section 12 ingestion pipeline for one normalized event.

    ``normalize -> persist -> snapshot -> detector -> AI -> embedding ->
    correlation -> realtime``. Every analysis stage is optional and defensive:
    if analysis or correlation fails the event is still persisted and still
    broadcast, just without enrichment.

    ``trigger_frame`` is the snapshot that caused this event, when the caller
    already has one. Passing it through means analysis does not have to win a
    second race for a scarce NVR session just to look at the same moment.
    ``frames`` is an already-sampled set shared by several events raised from
    the same moment (see :func:`ai_pipeline.enrich_event`).

    ``evidence_images`` are labelled JPEGs (e.g. package ``before``/``after``
    crops) stored as :class:`EventEvidence` rows. Only once those rows are
    committed does each matching ``metadata["mailbox"][label]`` entry get an
    ``image_url`` pointing at ``GET /api/v1/events/{id}/evidence/{label}``
    (same transaction), which incident routing copies into
    ``Incident.evidence``.
    """
    clip_triggered_at = time.monotonic()
    clip_seed = incident_clips.seed(event["camera_id"])
    row = await persist_event(session, event)
    clip_capture = incident_clips.capture(event["camera_id"], clip_seed, clip_triggered_at)
    event_id = row.id
    if evidence_images:
        try:
            await _attach_evidence(session, row, evidence_images)
        except Exception:  # noqa: BLE001 - evidence must never break ingestion
            logger.exception("evidence storage failed for %s", event["id"])
            await _recover(session, row, event_id)
        event = {**event, "metadata": dict(row.event_metadata or {})}
    # Snapshot the arming mode immediately, before the (potentially slow,
    # AI-backed) enrichment awaits below. Incident routing runs after
    # enrichment completes; without this snapshot, a mode change that
    # happens *during* enrichment (e.g. the household disarms mid-analysis)
    # would be applied retroactively to an event that was actually detected
    # under a different mode - silently dropping an incident that should
    # have been raised, or (symmetrically) raising one that shouldn't have
    # been. This is unrelated to ``row.type``/``row.zone``, which the AI
    # pipeline is still allowed - and expected - to reclassify below; only
    # the point-in-time human arming decision needs to be frozen.
    mode_at_detection = await security_modes.get_mode(session)
    enriched = event
    signal_result = None
    try:
        enriched = await ai_pipeline.enrich_event(session, row, event, trigger_frame, frames)
        await activity_service.correlate_event(session, row)
        await session.commit()
        await session.refresh(row)
    except Exception:  # noqa: BLE001 - analysis must never break ingestion
        logger.exception("event analysis failed for %s", event_id)
        # The rollback expires the already-committed row; reload it here so
        # callers never trigger an implicit (sync) load outside the greenlet.
        await _recover(session, row, event_id)
    try:
        # Loitering / unusual-activity / notification-priority run after the
        # AI pipeline has settled row.type and row.zone, but strictly before
        # the broadcast below, so SSE clients and the incident router see the
        # same tags and priority the REST API will later return.
        signal_result = await signal_service.apply_signals(session, row, mode=mode_at_detection)
        await session.commit()
        await session.refresh(row)
    except Exception:  # noqa: BLE001 - signals must never break ingestion
        logger.exception("signal evaluation failed for %s", event_id)
        await _recover(session, row, event_id)
    if row.type == "person" and (row.event_metadata or {}).get("suspicious", {}).get("level"):
        claim_key = f"suspicious-person:{row.camera_id}:{row.person_id or row.id}"
        try:
            won = await scene_dedup.claim(
                session, claim_key, row.start_time.timestamp(),
                settings.suspicious_dedupe_seconds, row.id,
            )
            if not won:
                # A lost claim rolls the session back, expiring the row. The
                # de-duplicated verdict below is written from the row's own
                # metadata, so reload it first - otherwise the read raises and
                # the repeat appearance keeps its full-priority alert.
                await session.refresh(row)
                metadata = dict(row.event_metadata or {})
                verdict = dict(metadata["suspicious"])
                verdict["deduplicated"] = True
                verdict["level"] = None
                metadata["suspicious"] = verdict
                metadata["notification_priority"] = "low"
                metadata["priority_reasons"] = ["repeat of a recent appearance alert"]
                row.event_metadata = metadata
                row.tags = [tag for tag in row.tags or [] if tag not in {"elevated", "suspicious"}]
                await session.commit()
                await session.refresh(row)
        except Exception:  # noqa: BLE001 - alert dedup cannot discard an event
            logger.exception("suspicious alert claim failed for %s", event_id)
            await _recover(session, row, event_id)
    enriched = {**enriched, "activity_id": row.activity_id}
    if signal_result is not None:
        enriched = {
            **enriched,
            "tags": list(row.tags or []),
            "notification_priority": signal_result.priority,
            "metadata": dict(row.event_metadata or {}),
        }
    await event_bus.publish(enriched)
    try:
        # Incident routing happens after the event is fully enriched/final
        # (row.type/zone reflect the AI pipeline's final classification) and
        # strictly after the event itself is persisted/broadcast, so an
        # incident-routing failure can never suppress or delay the event.
        # ``mode`` is the mode captured above, before enrichment, not
        # whatever is active now (see comment above).
        incident = await incident_service.route_event(session, row, mode=mode_at_detection)
        if incident is not None and incident.event_ids[0] == row.id:
            await incident_clips.start(incident, clip_capture)
            clip_capture = None
    except Exception:  # noqa: BLE001 - incident routing must never break ingestion
        logger.exception("incident routing failed for %s", event_id)
        # Callers keep using ``row`` after this returns (scene ingestion
        # links the event to its track and logs ``row.id``), so the row has
        # to survive the rollback as a usable object, not an expired one.
        await _recover(session, row, event_id)
    await incident_clips.discard(clip_capture)
    return row


async def list_events(session: AsyncSession, limit: int = 50) -> list[Event]:
    result = await session.execute(
        select(Event).order_by(Event.start_time.desc()).limit(limit)
    )
    return list(result.scalars().all())


def to_dict(row: Event) -> dict:
    metadata = row.event_metadata or {}
    return {
        "id": row.id,
        "camera_id": row.camera_id,
        "type": row.type,
        "priority": row.priority,
        # Smart notification priority (low/normal/high/critical) computed by
        # ``app.services.priority`` from type, zone, arming mode and signals.
        # Distinct from ``priority`` above, which is the source's own level.
        "notification_priority": metadata.get("notification_priority"),
        "priority_reasons": list(metadata.get("priority_reasons") or []),
        # Loitering dwell / activity-baseline details behind the
        # ``loitering`` and ``unusual_activity`` tags.
        "signals": metadata.get("signals"),
        "source": row.source,
        "start_time": row.start_time.isoformat(),
        "description": row.description,
        "zone": row.zone,
        "tags": list(row.tags or []),
        "thumbnail_path": row.thumbnail_path,
        "best_photo_path": row.best_photo_path,
        "ai_analysis_id": row.ai_analysis_id,
        "activity_id": row.activity_id,
        "person_id": row.person_id,
        "person_confidence": row.person_confidence,
        "person_confirmed": bool(row.person_confirmed),
        "photo_rating": row.photo_rating,
        # Human "keep this" marker; exempts the event from retention purges.
        "retention_hold": bool(row.retention_hold),
        "photo_caption": metadata.get("photo_caption"),
        # Detection borders, already expressed in the stored photo's own
        # coordinates by the AI pipeline, so the UI can draw them directly
        # without knowing anything about how the photo was cropped.
        "photo_boxes": list((metadata.get("best_photo") or {}).get("boxes") or []),
        "full_photo_boxes": list((metadata.get("best_photo") or {}).get("frame_boxes") or []),
        "photo_width": (metadata.get("best_photo") or {}).get("width"),
        "photo_height": (metadata.get("best_photo") or {}).get("height"),
        "full_photo_width": (metadata.get("best_photo") or {}).get("full_width"),
        "full_photo_height": (metadata.get("best_photo") or {}).get("full_height"),
        # Whether a vision model agreed with the local detector. ``None``
        # means nobody checked (no Foundry configured, or a non-person
        # subject), which the UI must show as neither confirmation nor doubt.
        "photo_verified": metadata.get("photo_verified"),
        # Observable, non-protected description: apparent age band, build,
        # clothing, carried items, whether the face is visible.
        "appearance": metadata.get("appearance"),
        "animal": metadata.get("animal"),
        "suspicious": metadata.get("suspicious"),
        "vehicle": metadata.get("vehicle"),
        # What changed, for scene transitions (vehicle arrived/parked/moved/
        # departed/returned, mailbox delivery, bin put out/emptied). The UI
        # renders this instead of the raw tracker state.
        "scene": metadata.get("scene"),
        "metadata": metadata,
    }
