"""Bounded, best-effort incident video from the ingestion owner's HLS reader."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from ..config import settings
from ..db import SessionLocal
from ..models.db import Incident, IncidentClip
from . import incidents as incident_service
from .stream_frames import ClipSegment, stream_hub

logger = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()
_captures: dict[str, set[asyncio.Task]] = {}
_MAX_CAPTURES_PER_CAMERA = 2


def seed(camera_id: str) -> tuple[ClipSegment, ...]:
    """Freeze pre-roll at detection time, before optional AI enrichment."""
    if not settings.incident_clips_enabled:
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


async def start(incident: Incident, capture_task: asyncio.Task | None) -> None:
    """Called only for the first event of a newly created incident."""
    if not settings.incident_clips_enabled:
        await discard(capture_task)
        return
    async with SessionLocal() as session:
        row = await session.get(Incident, incident.id)
        if row is None or row.clip_status is not None:
            await discard(capture_task)
            return
        row.clip_status = "pending" if capture_task is not None else "unavailable"
        await session.commit()
        await incident_service._broadcast(row, "incident.updated")
    if capture_task is None:
        return
    task = asyncio.create_task(_finish(incident.id, capture_task))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _collect(camera_id: str, initial: tuple[ClipSegment, ...], triggered_at: float) -> bytes:
    deadline = triggered_at + settings.incident_clip_post_seconds
    segments = list(initial)
    total_bytes = len(segments[0].init) + sum(len(segment.data) for segment in segments)
    if total_bytes > settings.incident_clip_max_bytes:
        raise ValueError("clip exceeds configured size cap")
    reader = stream_hub._readers.get(camera_id)
    if reader is None:
        raise ValueError("stream reader stopped")
    while time.monotonic() < deadline:
        await asyncio.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
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
    if segments[-1].captured_at < deadline - 3:
        raise ValueError("insufficient post-roll")
    video = segments[0].init + b"".join(segment.data for segment in segments)
    return video


async def _finish(incident_id: str, capture_task: asyncio.Task) -> None:
    try:
        video = await capture_task
        async with SessionLocal() as session:
            row = await session.get(Incident, incident_id)
            if row is None or row.clip_status != "pending":
                return
            session.add(IncidentClip(incident_id=incident_id, video=video, created_at=datetime.now(timezone.utc)))
            row.clip_status = "ready"
            await session.commit()
            await incident_service._broadcast(row, "incident.updated")
    except asyncio.CancelledError:
        raise
    except (ValueError, OSError) as exc:
        logger.info("incident clip %s unavailable: %s", incident_id, exc)
        await _unavailable(incident_id)
    except Exception:
        logger.exception("incident clip %s failed", incident_id)
        await _unavailable(incident_id)


async def _unavailable(incident_id: str) -> None:
    async with SessionLocal() as session:
        row = await session.get(Incident, incident_id)
        if row is not None and row.clip_status == "pending":
            row.clip_status = "unavailable"
            await session.commit()
            await incident_service._broadcast(row, "incident.updated")


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
