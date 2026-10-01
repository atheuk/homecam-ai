"""Continuous camera event ingestion (SPEC section 12).

Nothing in this codebase used to *trigger* the event pipeline from real
camera activity: events only ever appeared via the ``/mock/events`` test
endpoint. :class:`IngestionService` closes that gap by periodically
snapshotting every online, snapshot-capable camera across every provider,
running it through the configured local detector, and creating a real event
whenever the detector actually sees something.

Cameras that relay a sub-stream (Dahua edge) are sampled from it instead of
``snapshot.cgi`` (see :mod:`app.services.stream_frames`); snapshots remain
the rate-limited fallback and the only source for other providers.

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

from ..ai.detector import (
    SUBJECT_LABELS,
    BoundingBox,
    Detection,
    DetectionContext,
    get_detector,
    subject_for_label,
)
from ..ai import camera_health
from ..config import settings
from ..db import SessionLocal
from ..providers.base import CameraNotFoundError, CameraOfflineError, ProviderUnavailableError
from . import ai_pipeline, detector_watchdog, scene_state
from . import events as event_service
from . import incidents as incident_service
from .provider_registry import discover_all_cameras, find_provider_for_camera
from .stream_frames import stream_hub

logger = logging.getLogger(__name__)

# Order in which simultaneous subjects are raised: "start with people".
_SUBJECT_ORDER: tuple[str, ...] = tuple(SUBJECT_LABELS)


def _subjects_in(labels: set[str]) -> list[str]:
    """Every event subject present among ``labels``, people first.

    Each subject gets its own event. Collapsing a frame to one headline
    type meant a parked car and a passing person shared one event (and one
    photo), and a car in view could stand in for the person entirely.
    """
    present = {subject_for_label(label) for label in labels} - {None}
    return [subject for subject in _SUBJECT_ORDER if subject in present]


async def _acquire_frame(camera_id: str, provider) -> tuple[bytes, list[bytes] | None, str] | None:
    """The frame to run detection on: ``(frame, shared_frames, source)``.

    Cameras with a relayed sub-stream use its newest decoded frame, which
    costs the NVR nothing. A CGI snapshot is only the fallback when that
    frame is missing or stale, and - like every snapshot-only camera - is
    taken at most once per ``event_poll_interval_seconds`` so a stream outage
    cannot turn into more load on a session-limited NVR than before.
    """
    stats = _stats_for(camera_id)
    streamed = settings.stream_frames_enabled and getattr(provider, "supports_stream_frames", False)
    if streamed:
        stream_hub.ensure(camera_id)
        sample = stream_hub.latest(camera_id)
        if sample is not None:
            if _last_stream_seq.get(camera_id) == sample.seq:
                stats["stream_repeat"] += 1
                return None
            _last_stream_seq[camera_id] = sample.seq
            stats["stream"] += 1
            return sample.frame, list(sample.frames), "stream"
        stats["stream_missing"] += 1

    if not _snapshot_due(camera_id):
        if streamed:
            # A stream camera wanted a frame this tick and got none.
            stats["no_frame"] += 1
        return None
    _last_snapshot_at[camera_id] = time.monotonic()
    try:
        image = await provider.get_snapshot(camera_id)
    except (CameraOfflineError, CameraNotFoundError, ProviderUnavailableError) as exc:
        stats["snapshot_failed"] += 1
        stats["no_frame"] += 1
        logger.debug("ingestion snapshot unavailable for %s: %s", camera_id, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - one bad camera must not stop the loop
        stats["snapshot_failed"] += 1
        stats["no_frame"] += 1
        logger.warning("ingestion snapshot failed for %s: %s", camera_id, exc)
        return None
    stats["snapshot"] += 1
    stream_hub.note_snapshot(camera_id, image)
    return image, None, "snapshot"


async def poll_once(session_factory=SessionLocal) -> int:
    """Run a single ingestion pass over every discovered camera.

    Returns the number of events created, mainly so tests/logging can
    observe progress without depending on internal state.
    """
    detector = get_detector()
    created = 0
    for camera in await discover_all_cameras(settings.camera_discovery_cache_seconds):
        camera_id = camera.get("id")
        if not camera_id:
            continue
        camera_name = str(camera.get("name") or camera_id)
        # Camera-health (offline transition) must be evaluated for *every*
        # discovered camera, online or not - this is why it runs before the
        # offline-skip below, unlike everything else in this loop.
        try:
            await camera_health.evaluate_online_state(
                session_factory, camera_id, camera_name, bool(camera.get("online"))
            )
        except Exception as exc:  # noqa: BLE001 - health checks must never break ingestion
            logger.warning("camera health (online) check failed for %s: %s", camera_id, exc)
        if not camera.get("online"):
            continue
        if camera.get("capabilities", {}).get("snapshot") != "SUPPORTED":
            continue
        provider = await find_provider_for_camera(camera_id)
        if provider is None:
            continue
        acquired = await _acquire_frame(camera_id, provider)
        _maybe_log_stats(camera_id)
        if acquired is None:
            continue
        image, stream_frames, frame_source = acquired
        try:
            await camera_health.evaluate_frame(session_factory, camera_id, camera_name, image)
        except Exception as exc:  # noqa: BLE001 - health checks must never break ingestion
            logger.warning("camera health (frame) check failed for %s: %s", camera_id, exc)

        try:
            # Off the event loop: RT-DETR takes ~225ms of CPU per frame, and
            # with frames every few seconds the API must stay responsive.
            detections = await asyncio.to_thread(
                detector.detect, image, DetectionContext(camera_id=camera_id, camera_name=camera_name)
            )
        except Exception as exc:  # noqa: BLE001 - detector must not break ingestion
            logger.warning("ingestion detection failed for %s: %s", camera_id, exc)
            continue
        # A successfully-detected frame that yielded nothing is the exact
        # signal the 2026-09-30 blackout produced for three hours.
        detector_watchdog.record_frame(camera_id, len(detections))

        # Subject events first: a slow scene check (a Foundry mailbox/bin
        # verification) must never delay the person/animal event from the
        # same frame.
        created += await _emit_subject_events(
            session_factory, provider, camera_id, camera_name, image, detections,
            stream_frames, frame_source,
        )
        # Every real frame advances the persistent scene state, including
        # frames with nothing in them: that is how a vehicle departs or a
        # mailbox visit ends. Its transitions are events of their own.
        created += await _emit_scene_transitions(
            session_factory, camera_id, camera_name, image, detections, stream_frames, frame_source
        )
    try:
        # Once per poll tick (not per camera): sweep every open incident for
        # overdue escalation. Cheap (one query) and independent of any
        # particular camera's frame.
        await incident_service.escalate_due_incidents(session_factory)
    except Exception as exc:  # noqa: BLE001 - escalation must never break ingestion
        logger.warning("incident escalation sweep failed: %s", exc)
    return created


async def _emit_subject_events(
    session_factory,
    provider,
    camera_id: str,
    camera_name: str,
    image: bytes,
    detections: list[Detection],
    stream_frames: list[bytes] | None,
    frame_source: str,
) -> int:
    created = 0
    if detections:
        # Cooldown is per subject: a car parked in view all day must not
        # hold the camera's only cooldown slot and so silence the person or
        # animal that walks past it.
        due: list[str] = []
        boxes_by_subject: dict[str, list[BoundingBox]] = {}
        for subject in _subjects_in({detection.label for detection in detections}):
            if subject in _scene_tracked_subjects():
                continue
            if not _cooldown_elapsed(camera_id, subject):
                continue
            subject_detections = [d for d in detections if subject_for_label(d.label) == subject]
            if _is_stationary_repeat(camera_id, subject, subject_detections):
                _stats_for(camera_id)["stationary_suppressed"] += 1
                continue
            due.append(subject)
            boxes_by_subject[subject] = [d.bbox for d in subject_detections]
        if not due:
            return 0

        # One frame sample shared by every event from this moment. Stream
        # frames already come as a burst from one segment; snapshot-only
        # cameras spend the NVR's tiny session budget once, not per event.
        # The frame we just detected on leads, since it is the correct
        # moment to photograph.
        frames: list[bytes] | None = stream_frames
        if frames is None and len(due) > 1 and settings.ai_analysis_enabled:
            frames = await ai_pipeline._sample_frames(
                provider, camera_id, settings.best_photo_frames, seed=image
            )

        for subject in due:
            now = datetime.now(timezone.utc)
            event = {
                "id": "evt-" + uuid.uuid4().hex[:16],
                "camera_id": camera_id,
                "camera_name": camera_name,
                "type": subject,
                "priority": "high" if subject == "person" else "normal",
                "source": "local-ai",
                "start_time": now.isoformat(),
                "description": f"{subject.title()} detected on {camera_name}",
                "metadata": {"frame_source": frame_source},
            }
            async with session_factory() as session:
                await event_service.create_and_broadcast_event(
                    session, event, trigger_frame=image, frames=frames
                )
            _mark_created(camera_id, subject, boxes_by_subject[subject])
            _stats_for(camera_id)["events"] += 1
            created += 1
    return created


def _scene_tracked_subjects() -> set[str]:
    """Subjects whose events come from :mod:`scene_state`, not cooldowns."""
    return {"vehicle"} if settings.vehicle_tracking_enabled else set()


async def _emit_scene_transitions(
    session_factory,
    camera_id: str,
    camera_name: str,
    image: bytes,
    detections: list[Detection],
    stream_frames: list[bytes] | None,
    frame_source: str,
) -> int:
    created = 0
    try:
        async with session_factory() as session:
            transitions = await scene_state.process_frame(
                session, camera_id, camera_name, image, detections
            )
    except Exception:  # noqa: BLE001 - scene state must not break ingestion
        logger.exception("scene state update failed for %s", camera_id)
        return 0
    for transition in transitions:
        frames = transition.frames or stream_frames
        event = {
            "id": "evt-" + uuid.uuid4().hex[:16],
            "camera_id": camera_id,
            "camera_name": camera_name,
            "type": transition.event_type,
            "priority": transition.priority,
            "source": "local-ai",
            "start_time": datetime.now(timezone.utc).isoformat(),
            "description": transition.description,
            "zone": transition.zone,
            "tags": list(transition.tags),
            "metadata": {"frame_source": frame_source, **transition.metadata},
        }
        try:
            async with session_factory() as session:
                row = await event_service.create_and_broadcast_event(
                    session, event, trigger_frame=(frames or [image])[0], frames=frames
                )
                await scene_state.note_event(session, transition, row.id)
        except Exception:  # noqa: BLE001
            logger.exception("scene event failed for %s", camera_id)
            continue
        logger.info(
            "scene transition %s: %s %s -> %s", camera_id, transition.kind, transition.transition, row.id
        )
        stats = _stats_for(camera_id)
        stats["events"] += 1
        stats["scene_events"] += 1
        created += 1
    return created


# Per-(camera, subject) cooldown so continued presence doesn't create a new
# event every poll interval. Process-wide and deliberately simple (a dict,
# not a DB table): losing it on restart just means the first post-restart
# detection creates one event immediately, which is harmless.
_last_event_at: dict[tuple[str, str], float] = {}
# Objects already reported per (camera, subject), with when each was first
# reported, for stationary suppression.
_last_event_boxes: dict[tuple[str, str], list[tuple[float, BoundingBox]]] = {}
_last_snapshot_at: dict[str, float] = {}
_last_stream_seq: dict[str, int] = {}
_frame_stats: dict[str, dict[str, int]] = {}
_stats_since: dict[str, float] = {}

_STAT_KEYS = (
    "stream",
    "stream_repeat",
    "stream_missing",
    "snapshot",
    "snapshot_failed",
    "no_frame",
    "stationary_suppressed",
    "scene_events",
    "events",
)


def _cooldown_elapsed(camera_id: str, subject: str) -> bool:
    last = _last_event_at.get((camera_id, subject))
    return last is None or (time.monotonic() - last) >= settings.event_cooldown_seconds


def _snapshot_due(camera_id: str) -> bool:
    last = _last_snapshot_at.get(camera_id)
    return last is None or (time.monotonic() - last) >= settings.event_poll_interval_seconds


def _mark_created(camera_id: str, subject: str, boxes: list[BoundingBox] | None = None) -> None:
    now = time.monotonic()
    _last_event_at[(camera_id, subject)] = now
    if not boxes:
        return
    # Remember every object this event reported, each on its own clock.
    # Keeping only the latest event's boxes let a low-confidence static box
    # that flickers in and out (a car half out of frame) count as "new" on
    # each appearance and re-emit the parked car beside it every cooldown.
    # Already-known objects keep their original time, so a parked car still
    # re-emits once per window rather than never.
    key = (camera_id, subject)
    known = _live_known_boxes(key, now)
    threshold = settings.stationary_iou_threshold
    for box in boxes:
        if not any(_same_object(box, prior, threshold) for _, prior in known):
            known.append((now, box))
    _last_event_boxes[key] = known[-_MAX_KNOWN_BOXES:]


_MAX_KNOWN_BOXES = 32


def _live_known_boxes(key: tuple[str, str], now: float) -> list[tuple[float, BoundingBox]]:
    window = settings.stationary_suppress_seconds
    return [(at, box) for at, box in _last_event_boxes.get(key, []) if now - at < window]


def _stationary_subjects() -> set[str]:
    return {s.strip() for s in settings.stationary_subjects.split(",") if s.strip()} - {"person"}


def _same_object(box: BoundingBox, previous: BoundingBox, threshold: float) -> bool:
    """IoU, or containment of ``box`` in ``previous``, at or above ``threshold``.

    Containment covers the detector sometimes adding a second, partial box
    on the same parked car, which is not a new object.
    """
    ix1, iy1 = max(box.x1, previous.x1), max(box.y1, previous.y1)
    ix2, iy2 = min(box.x2, previous.x2), min(box.y2, previous.y2)
    if ix2 <= ix1 or iy2 <= iy1:
        return False
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = box.area + previous.area - inter
    return (union > 0 and inter / union >= threshold) or (box.area > 0 and inter / box.area >= threshold)


def _is_stationary_repeat(camera_id: str, subject: str, detections: list[Detection]) -> bool:
    """Whether this frame shows only objects already reported, not moved.

    A parked car otherwise re-emits a vehicle event every cooldown, all day.
    At least one known object must still be in view. Any other box must
    either match a known object or be too weak to be a new one (below
    ``stationary_new_object_min_confidence``). A new or moved object that
    is detected confidently still emits. People are never suppressed this
    way.
    """
    if not detections or subject not in _stationary_subjects() or settings.stationary_suppress_seconds <= 0:
        return False
    known = _live_known_boxes((camera_id, subject), time.monotonic())
    if not known:
        return False
    threshold = settings.stationary_iou_threshold
    matched = [any(_same_object(d.bbox, prior, threshold) for _, prior in known) for d in detections]
    if not any(matched):
        return False
    floor = settings.stationary_new_object_min_confidence
    return all(m or d.confidence < floor for m, d in zip(matched, detections))


def _stats_for(camera_id: str) -> dict[str, int]:
    stats = _frame_stats.get(camera_id)
    if stats is None:
        stats = _frame_stats[camera_id] = dict.fromkeys(_STAT_KEYS, 0)
        _stats_since[camera_id] = time.monotonic()
    return stats


def frame_stats() -> dict[str, dict[str, int]]:
    return {camera_id: dict(stats) for camera_id, stats in _frame_stats.items()}


def _maybe_log_stats(camera_id: str) -> None:
    """Periodic per-camera line: where frames came from and how often.

    ``frames`` counts ticks that got a new frame to detect on (stream or
    snapshot); ``success`` is its share of the ticks that sought one. A
    stream repeat (no new segment yet) is neither, nor is a snapshot-only
    camera's tick between its rate-limited snapshots.
    """
    now = time.monotonic()
    since = _stats_since.get(camera_id, now)
    window = now - since
    if window < settings.ingestion_stats_log_seconds:
        return
    stats = _frame_stats.get(camera_id) or dict.fromkeys(_STAT_KEYS, 0)
    frames = stats["stream"] + stats["snapshot"]
    attempts = frames + stats["no_frame"]
    logger.info(
        "ingestion frames %s: window=%.0fs frames=%d success=%s%% cadence=%s stream=%d "
        "stream_repeat=%d stream_missing=%d snapshot=%d snapshot_failed=%d no_frame=%d "
        "stationary_suppressed=%d scene_events=%d events=%d",
        camera_id,
        window,
        frames,
        f"{100.0 * frames / attempts:.1f}" if attempts else "n/a",
        f"{window / frames:.1f}s" if frames else "n/a",
        stats["stream"],
        stats["stream_repeat"],
        stats["stream_missing"],
        stats["snapshot"],
        stats["snapshot_failed"],
        stats["no_frame"],
        stats["stationary_suppressed"],
        stats["scene_events"],
        stats["events"],
    )
    _frame_stats[camera_id] = dict.fromkeys(_STAT_KEYS, 0)
    _stats_since[camera_id] = now


def reset_cooldowns() -> None:
    """Test hook: forget every camera's cooldown timer and frame state."""
    _last_event_at.clear()
    _last_event_boxes.clear()
    _last_snapshot_at.clear()
    _last_stream_seq.clear()
    _frame_stats.clear()
    _stats_since.clear()
    detector_watchdog.reset()
    scene_state.reset_memory()


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
            await stream_hub.stop()
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        await stream_hub.stop()

    async def _run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
                logger.exception("event ingestion pass failed")
            # Detection time counts toward the tick, so the sampling cadence
            # stays near the configured interval rather than interval+work.
            elapsed = time.monotonic() - started
            try:
                await asyncio.sleep(max(0.5, tick_seconds() - elapsed))
            except asyncio.CancelledError:
                raise


def tick_seconds() -> float:
    """Ingestion loop period.

    With stream frames the loop runs at the stream sampling interval;
    snapshot-only cameras are still rate limited per camera to
    ``event_poll_interval_seconds`` inside :func:`_acquire_frame`.
    """
    if settings.stream_frames_enabled:
        return min(settings.stream_sample_interval_seconds, settings.event_poll_interval_seconds)
    return settings.event_poll_interval_seconds


ingestion_service = IngestionService()
