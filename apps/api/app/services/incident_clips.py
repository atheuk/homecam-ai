"""Bounded, best-effort incident video from the ingestion owner's HLS reader."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, time as datetime_time, timedelta, timezone

from sqlalchemy import func, select, text

from ..config import settings
from ..db import SessionLocal
from ..models.db import Incident, IncidentClip
from . import incidents as incident_service
from .stream_frames import ClipSegment, clips_enabled, stream_hub

logger = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()
_captures: dict[str, set[asyncio.Task]] = {}
_MAX_CAPTURES_PER_CAMERA = 2
_BUDGET_LOCK_KEY = 873284610


class CapturedClip(bytes):
    """Clip bytes plus the real (never padded) timing around the trigger."""

    pre_roll_seconds: float = 0.0
    duration_seconds: float = 0.0


def seed(camera_id: str) -> tuple[ClipSegment, ...]:
    """Freeze pre-roll at detection time, before optional AI enrichment."""
    if not clips_enabled():
        return ()
    segments = stream_hub.clip_segments(camera_id)
    now = time.monotonic()
    return tuple(s for s in segments if s.captured_at >= now - settings.incident_clip_pre_seconds)


def capture(camera_id: str, segments: tuple[ClipSegment, ...], triggered_at: float) -> asyncio.Task | None:
    if not segments or len(_captures.get(camera_id, ())) >= _MAX_CAPTURES_PER_CAMERA:
        return None
    task = asyncio.create_task(_collect(camera_id, segments, triggered_at))
    _captures.setdefault(camera_id, set()).add(task)

    def forget(done: asyncio.Task) -> None:
        active = _captures.get(camera_id)
        if active is not None:
            active.discard(done)
            if not active:
                _captures.pop(camera_id, None)

    task.add_done_callback(forget)
    return task


async def discard(capture_task: asyncio.Task | None) -> None:
    if capture_task is not None:
        capture_task.cancel()
        await asyncio.gather(capture_task, return_exceptions=True)


async def start(
    incident: Incident, capture_task: asyncio.Task | None, event_id: str | None = None
) -> bool:
    """Called only for the first event of a newly created incident.

    Returns whether the incident took ownership of ``capture_task``. When it
    did, the triggering event (``event_id``) links to the incident clip
    instead of storing a second copy of the same footage.
    """
    if not settings.incident_clips_enabled:
        return False
    async with SessionLocal() as session:
        row = await session.get(Incident, incident.id)
        if row is None or row.clip_status is not None:
            return False
        row.clip_status = "pending" if capture_task is not None else "unavailable"
        await session.commit()
        await incident_service._broadcast(row, "incident.updated")
    if capture_task is None:
        return False
    if event_id is not None:
        from . import event_clips

        await event_clips.link_incident(event_id, incident.id)
    task = asyncio.create_task(_finish(incident.id, capture_task, event_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def _collect(camera_id: str, initial: tuple[ClipSegment, ...], triggered_at: float) -> CapturedClip:
    deadline = triggered_at + settings.incident_clip_post_seconds
    segments = list(initial)
    total_bytes = len(segments[0].init) + sum(len(segment.data) for segment in segments)
    if total_bytes > settings.incident_clip_max_bytes:
        raise ValueError("clip exceeds configured size cap")
    reader = stream_hub._readers.get(camera_id)
    if reader is None:
        raise ValueError("stream reader stopped")
    grace_until = deadline + settings.incident_clip_post_grace_seconds
    while True:
        # Refresh before deciding, so a covering segment that is already
        # buffered counts even if this task was scheduled late.
        if stream_hub._readers.get(camera_id) is not reader:
            raise ValueError("stream reader changed")
        current = stream_hub.clip_segments(camera_id)
        if not current:
            raise ValueError("stream stopped or unsupported")
        for segment in current:
            if segment.seq > segments[-1].seq:
                if segment.seq != segments[-1].seq + 1 or segment.init != segments[0].init:
                    raise ValueError("gap in clip segments")
                total_bytes += len(segment.data)
                if total_bytes > settings.incident_clip_max_bytes:
                    raise ValueError("clip exceeds configured size cap")
                segments.append(segment)
        now = time.monotonic()
        if now >= deadline and (segments[-1].captured_at >= deadline - 3 or now >= grace_until):
            break
        await asyncio.sleep(min(0.5, max(0.01, (deadline if now < deadline else grace_until) - now)))
    if segments[-1].captured_at < deadline - 3:
        logger.info(
            "clip post-roll for %s short: newest segment %.1fs before deadline after %.1fs grace (%d segments)",
            camera_id, deadline - segments[-1].captured_at, settings.incident_clip_post_grace_seconds, len(segments),
        )
        raise ValueError("insufficient post-roll")
    video = CapturedClip(segments[0].init + b"".join(segment.data for segment in segments))
    first = segments[0]
    first_start = first.captured_at - first.duration if first.duration else first.captured_at
    video.pre_roll_seconds = round(max(0.0, triggered_at - first_start), 1)
    video.duration_seconds = round(sum(segment.duration for segment in segments), 1)
    return video


async def _finish(incident_id: str, capture_task: asyncio.Task, event_id: str | None = None) -> None:
    from . import event_clips

    try:
        video = await capture_task
        async with event_clips.admission_lock, SessionLocal() as session:
            if session.get_bind().dialect.name == "postgresql":
                # Serialize admission across API replicas; a lock held through
                # commit ensures both aggregate reads see the previous insert.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BUDGET_LOCK_KEY}
                )
            row = await session.get(Incident, incident_id)
            if row is None or row.clip_status != "pending":
                return
            now = datetime.now(timezone.utc)
            today = datetime.combine(now.date(), datetime_time.min, tzinfo=timezone.utc)
            daily_count = (await session.execute(
                select(func.count()).select_from(IncidentClip).where(
                    IncidentClip.created_at >= today,
                    IncidentClip.created_at < today + timedelta(days=1),
                )
            )).scalar_one()
            stored_bytes = await event_clips.stored_clip_bytes(session)
            if (
                daily_count >= settings.incident_clip_daily_limit
                or stored_bytes + len(video) > settings.incident_clip_storage_limit_bytes
            ):
                row.clip_status = "skipped"
                await session.commit()
                await incident_service._broadcast(row, "incident.updated")
                logger.info("incident clip %s skipped: daily or storage budget reached", incident_id)
                await event_clips.update_events(
                    [event_id] if event_id else [],
                    {"status": "skipped", "reason": "Daily or storage clip budget reached"},
                )
                return
            session.add(IncidentClip(
                incident_id=incident_id, video=video, size_bytes=len(video), created_at=now
            ))
            row.clip_status = "ready"
            await session.commit()
            await incident_service._broadcast(row, "incident.updated")
        await event_clips.update_events(
            [event_id] if event_id else [], {"status": "ready", **event_clips.clip_facts(video)}
        )
    except asyncio.CancelledError:
        raise
    except (ValueError, OSError) as exc:
        logger.info("incident clip %s unavailable: %s", incident_id, exc)
        await _unavailable(incident_id, event_id, str(exc))
    except Exception:
        logger.exception("incident clip %s failed", incident_id)
        await _unavailable(incident_id, event_id, "Clip capture failed")


async def _unavailable(incident_id: str, event_id: str | None = None, reason: str = "") -> None:
    async with SessionLocal() as session:
        row = await session.get(Incident, incident_id)
        if row is not None and row.clip_status == "pending":
            row.clip_status = "unavailable"
            await session.commit()
            await incident_service._broadcast(row, "incident.updated")
    if event_id:
        from . import event_clips

        await event_clips.update_events(
            [event_id], {"status": "unavailable", "reason": event_clips.friendly_reason(reason)}
        )


def status(incident: Incident) -> dict:
    state = incident.clip_status or "unavailable"
    # A worker can disappear mid-capture; never leave "pending" forever.
    created = incident.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if (
        state == "pending"
        and (datetime.now(timezone.utc) - created).total_seconds() > settings.incident_clip_post_seconds + 120
    ):
        state = "unavailable"
    return {
        "status": state,
        "url": f"/api/v1/security/incidents/{incident.id}/clip" if state == "ready" else None,
    }


async def stop() -> None:
    tasks = list(_tasks) + [task for pending in _captures.values() for task in pending]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
    _captures.clear()
