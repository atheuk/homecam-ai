"""Short, bounded video clips for ordinary events.

Two sources, both strictly bounded:

``stream``
    The API's own short fMP4 buffer of a camera's HLS relay (see
    :mod:`stream_frames`). It gives real pre-roll for always-on cameras
    such as the Dahua NVR channels. One capture per camera is shared by
    every event that overlaps it, so a burst of detections stores one clip,
    not N copies of the same footage.

``edge``
    A camera-side recording fetched from the provider (the Eufy doorbell
    edge recorder). A sleeping battery doorbell never streamed before the
    trigger, so these clips have *no* pre-roll; that is reported as such and
    never padded with older, unrelated footage.

Clip state lives in ``Event.event_metadata["clip"]`` and is always one of
``pending``, ``ready``, ``skipped`` (budget), ``unavailable`` (with a
reason), ``expired`` or ``unsupported``. Admission is serialized with the
incident clip budget lock and counts towards the same aggregate storage cap,
so ordinary event clips can never crowd out (or evict) held incident
evidence.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as datetime_time, timedelta, timezone

from sqlalchemy import delete, func, select, text

from ..config import settings
from ..db import SessionLocal
from ..models.db import Event, EventClip, IncidentClip
from . import mp4info

logger = logging.getLogger(__name__)

_BUDGET_LOCK_KEY = 873284610  # shared with incident_clips
_MAX_EDGE_WAITERS = 8
_EDGE_POLL_SECONDS = 5.0
_EDGE_MATCH_BEFORE_SECONDS = 20
_SWEEP_INTERVAL_SECONDS = 3600

_tasks: set[asyncio.Task] = set()
_edge_waiters = 0
_sweeper: asyncio.Task | None = None
# In-process half of budget admission (the Postgres advisory lock covers
# other replicas); shared with incident clips so both see each other's inserts.
admission_lock = asyncio.Lock()


@dataclass
class _SharedCapture:
    camera_id: str
    task: asyncio.Task
    started: float
    offsets: dict[str, float] = field(default_factory=dict)


_shared: dict[str, _SharedCapture] = {}

_REASONS = {
    "gap in clip segments": "Video stream dropped segments during the event",
    "clip exceeds configured size cap": "Clip exceeded the per-clip size cap",
    "stream reader stopped": "Live video stream stopped during the event",
    "stream reader changed": "Live video stream restarted during the event",
    "stream stopped or unsupported": "Live video stream stopped during the event",
    "insufficient post-roll": "Stream ended before the after-event footage was complete",
}


def friendly_reason(reason: str) -> str:
    return _REASONS.get(reason, reason or "Clip capture failed")


def wanted_types() -> frozenset[str]:
    return frozenset(t.strip() for t in settings.event_clip_types.split(",") if t.strip())


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clip_facts(video: bytes, offset: float = 0.0) -> dict:
    """Timing/format facts recorded on each event for display."""
    facts: dict = {}
    try:
        info = mp4info.inspect(video)
        facts.update(codec=info.codec, duration_seconds=round(info.duration_seconds, 1),
                     width=info.width, height=info.height)
    except mp4info.InvalidClipError:
        duration = getattr(video, "duration_seconds", 0.0)
        if duration:
            facts["duration_seconds"] = duration
    pre_roll = getattr(video, "pre_roll_seconds", None)
    if pre_roll is not None:
        facts["pre_roll_seconds"] = round(pre_roll + offset, 1)
    facts["size_bytes"] = len(video)
    return facts


# --------------------------------------------------------------- metadata


async def update_events(event_ids: list[str], state: dict, only_pending: bool = True) -> None:
    """Merge ``state`` into each event's clip metadata and broadcast it."""
    if not event_ids:
        return
    from . import events as event_service

    async with SessionLocal() as session:
        for event_id in event_ids:
            row = await session.get(Event, event_id)
            if row is None:
                continue
            metadata = dict(row.event_metadata or {})
            current = dict(metadata.get("clip") or {})
            if only_pending and current and current.get("status") != "pending":
                continue
            current.update(state)
            current["updated_at"] = _now().isoformat()
            metadata["clip"] = current
            row.event_metadata = metadata
            await session.commit()
            await session.refresh(row)
            await event_service.event_bus.publish({**event_service.to_dict(row), "_sse_event": "event.updated"})


async def link_incident(event_id: str, incident_id: str) -> None:
    await update_events([event_id], {
        "status": "pending", "source": "incident", "incident_id": incident_id,
        "started_at": _now().isoformat(),
    }, only_pending=False)


def _set_state(row: Event, state: dict) -> None:
    metadata = dict(row.event_metadata or {})
    metadata["clip"] = {**state, "started_at": _now().isoformat()}
    row.event_metadata = metadata


def status(row: Event) -> dict:
    state = dict((row.event_metadata or {}).get("clip") or {})
    if not state:
        return {"status": "none", "url": None}
    current = state.get("status") or "unavailable"
    if current == "pending":
        started = state.get("started_at")
        try:
            at = datetime.fromisoformat(started) if started else row.start_time
        except ValueError:
            at = row.start_time
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        limit = settings.incident_clip_post_seconds + settings.event_clip_edge_wait_seconds + 180
        if (_now() - at).total_seconds() > limit:
            current = "unavailable"
            state["reason"] = "Clip capture was interrupted"
    result = {
        "status": current,
        "reason": state.get("reason"),
        "source": state.get("source"),
        "incident_id": state.get("incident_id"),
        "duration_seconds": state.get("duration_seconds"),
        "pre_roll_seconds": state.get("pre_roll_seconds"),
        "codec": state.get("codec"),
        "width": state.get("width"),
        "height": state.get("height"),
        "size_bytes": state.get("size_bytes"),
        "url": f"/api/v1/events/{row.id}/clip" if current == "ready" else None,
    }
    return result


# ------------------------------------------------------------------ attach


async def attach(session, row: Event, capture: asyncio.Task | None) -> tuple[asyncio.Task | None, bool]:
    """Give ``row`` a clip. Returns (capture the caller must discard, changed)."""
    if not settings.event_clips_enabled or row.type not in wanted_types():
        return capture, False
    shared = _shared.get(row.camera_id)
    now = time.monotonic()
    if (
        shared is not None
        and not shared.task.done()
        and now - shared.started <= settings.incident_clip_post_seconds
    ):
        # Already recording this camera: one clip covers both events.
        shared.offsets[row.id] = now - shared.started
        _set_state(row, {"status": "pending", "source": "stream"})
        await session.commit()
        await session.refresh(row)
        return capture, True
    if capture is not None:
        shared = _SharedCapture(row.camera_id, capture, now, {row.id: 0.0})
        _shared[row.camera_id] = shared
        _spawn(_finish_stream(shared))
        _set_state(row, {"status": "pending", "source": "stream"})
        await session.commit()
        await session.refresh(row)
        return None, True
    global _edge_waiters
    if not await _edge_capable(row.camera_id):
        _set_state(row, {
            "status": "unavailable",
            "reason": "No buffered live video for this camera at the event time",
        })
    elif _edge_waiters >= _MAX_EDGE_WAITERS:
        _set_state(row, {"status": "unavailable", "reason": "Too many clips are being fetched right now"})
    else:
        _edge_waiters += 1
        _set_state(row, {"status": "pending", "source": "edge"})
        _spawn(_fetch_edge(row.id, row.camera_id, row.start_time))
    await session.commit()
    await session.refresh(row)
    return None, True


async def _edge_capable(camera_id: str) -> bool:
    """Whether the camera's provider records its own event clips."""
    from .provider_registry import find_provider_for_camera

    try:
        provider = await asyncio.wait_for(find_provider_for_camera(camera_id), 5)
        if not hasattr(provider, "list_event_clips") or not hasattr(provider, "get_event_clip"):
            return False
        capabilities = await asyncio.wait_for(provider.get_capabilities(camera_id), 5)
    except Exception:  # noqa: BLE001 - treat as no edge recorder
        return False
    return capabilities.get("eventClips") == "SUPPORTED"


async def _finish_stream(shared: _SharedCapture) -> None:
    try:
        try:
            video = await shared.task
        except asyncio.CancelledError:
            if shared.task.cancelled():
                await update_events(list(shared.offsets), {
                    "status": "unavailable", "reason": "Clip capture was cancelled"
                })
                return
            raise
        except (ValueError, OSError) as exc:
            logger.info("event clip for camera %s unavailable: %s", shared.camera_id, exc)
            await update_events(list(shared.offsets), {"status": "unavailable", "reason": friendly_reason(str(exc))})
            return
        except Exception:  # noqa: BLE001 - never leave events pending
            logger.exception("event clip capture failed for camera %s", shared.camera_id)
            await update_events(list(shared.offsets), {"status": "unavailable", "reason": "Clip capture failed"})
            return
        finally:
            if _shared.get(shared.camera_id) is shared:
                _shared.pop(shared.camera_id, None)
        await store(shared.camera_id, video, "stream", None, dict(shared.offsets))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("event clip storage failed for camera %s", shared.camera_id)
        await update_events(list(shared.offsets), {"status": "unavailable", "reason": "Clip storage failed"})


async def stored_clip_bytes(session) -> int:
    incident = (await session.execute(select(func.coalesce(func.sum(IncidentClip.size_bytes), 0)))).scalar_one()
    event = (await session.execute(select(func.coalesce(func.sum(EventClip.size_bytes), 0)))).scalar_one()
    return int(incident) + int(event)


async def store(
    camera_id: str,
    video: bytes,
    source: str,
    source_ref: str | None,
    offsets: dict[str, float],
    pre_roll_seconds: float | None = None,
) -> str | None:
    """Validate and admit one clip under the shared budget; returns its id."""
    event_ids = list(offsets)
    try:
        info = mp4info.inspect(video)
    except mp4info.InvalidClipError as exc:
        logger.info("event clip for camera %s rejected: %s", camera_id, exc)
        await update_events(event_ids, {"status": "unavailable", "reason": "Recorded video was not a playable MP4"})
        return None
    if len(video) > settings.incident_clip_max_bytes:
        await update_events(event_ids, {"status": "unavailable", "reason": "Clip exceeded the per-clip size cap"})
        return None
    pre_roll = getattr(video, "pre_roll_seconds", pre_roll_seconds)
    async with admission_lock, SessionLocal() as session:
        if session.get_bind().dialect.name == "postgresql":
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BUDGET_LOCK_KEY})
        if source_ref is not None:
            existing = (await session.execute(
                select(EventClip).where(EventClip.source_ref == source_ref)
            )).scalar_one_or_none()
            if existing is not None:
                # The same camera recording already backs another event.
                existing.event_ids = list(dict.fromkeys([*existing.event_ids, *event_ids]))
                clip_id = existing.id
                await session.commit()
                await update_events(event_ids, _ready_state(clip_id, info, len(video), pre_roll, 0.0, source))
                return clip_id
        now = _now()
        today = datetime.combine(now.date(), datetime_time.min, tzinfo=timezone.utc)
        daily = (await session.execute(
            select(func.count()).select_from(EventClip).where(
                EventClip.created_at >= today, EventClip.created_at < today + timedelta(days=1)
            )
        )).scalar_one()
        event_bytes = (await session.execute(
            select(func.coalesce(func.sum(EventClip.size_bytes), 0))
        )).scalar_one()
        total = await stored_clip_bytes(session)
        if (
            daily >= settings.event_clip_daily_limit
            or event_bytes + len(video) > settings.event_clip_storage_limit_bytes
            or total + len(video) > settings.incident_clip_storage_limit_bytes
        ):
            await session.rollback()
            logger.info("event clip for camera %s skipped: daily or storage budget reached", camera_id)
            await update_events(event_ids, {"status": "skipped", "reason": "Daily or storage clip budget reached"})
            return None
        clip_id = uuid.uuid4().hex
        session.add(EventClip(
            id=clip_id, camera_id=camera_id, video=bytes(video), content_type="video/mp4",
            size_bytes=len(video), duration_ms=int(info.duration_seconds * 1000),
            pre_roll_ms=int((pre_roll or 0) * 1000), source=source, source_ref=source_ref,
            event_ids=event_ids, created_at=now,
        ))
        await session.commit()
    for event_id, offset in offsets.items():
        await update_events([event_id], _ready_state(clip_id, info, len(video), pre_roll, offset, source))
    logger.info(
        "event clip stored camera=%s source=%s duration=%.1fs pre_roll=%s bytes=%d events=%d",
        camera_id, source, info.duration_seconds, pre_roll, len(video), len(event_ids),
    )
    return clip_id


def _ready_state(clip_id, info, size, pre_roll, offset, source) -> dict:
    return {
        "status": "ready", "reason": None, "clip_id": clip_id, "source": source,
        "codec": info.codec, "duration_seconds": round(info.duration_seconds, 1),
        "width": info.width, "height": info.height, "size_bytes": size,
        "pre_roll_seconds": None if pre_roll is None else round(pre_roll + offset, 1),
    }


# -------------------------------------------------------------------- edge


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


async def _fetch_edge(event_id: str, camera_id: str, start_time: datetime) -> None:
    global _edge_waiters
    try:
        from .provider_registry import find_provider_for_camera

        try:
            provider = await asyncio.wait_for(find_provider_for_camera(camera_id), 15)
        except Exception:  # noqa: BLE001
            provider = None
        lister = getattr(provider, "list_event_clips", None)
        fetcher = getattr(provider, "get_event_clip", None)
        if lister is None or fetcher is None:
            await update_events([event_id], {
                "status": "unavailable",
                "reason": "No buffered live video for this camera at the event time",
            })
            return
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        deadline = time.monotonic() + settings.event_clip_edge_wait_seconds
        unsupported = False
        while time.monotonic() < deadline:
            try:
                clips = await asyncio.wait_for(lister(camera_id), 15)
            except NotImplementedError:
                unsupported = True
                break
            except Exception:  # noqa: BLE001 - keep polling until the deadline
                logger.info("edge clip listing failed for camera %s", camera_id)
                clips = []
            if clips is None:
                unsupported = True
                break
            match = _match(clips, start_time)
            if match is not None and match.get("complete", True):
                video = await asyncio.wait_for(fetcher(camera_id, str(match["id"])), 30)
                pre_roll = float(match.get("pre_roll_seconds") or 0.0)
                await store(camera_id, video, "edge", f"{camera_id}:{match['id']}", {event_id: 0.0}, pre_roll)
                return
            await asyncio.sleep(_EDGE_POLL_SECONDS)
        if unsupported:
            await update_events([event_id], {
                "status": "unsupported", "reason": "This camera's bridge does not record event clips",
            })
        else:
            await update_events([event_id], {
                "status": "unavailable", "reason": "The camera did not record a clip for this event",
            })
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("edge clip fetch failed for camera %s", camera_id)
        await update_events([event_id], {"status": "unavailable", "reason": "Could not fetch the camera's clip"})
    finally:
        _edge_waiters -= 1


def _match(clips, start_time: datetime) -> dict | None:
    best = None
    best_delta = None
    for clip in clips or []:
        if not isinstance(clip, dict) or not clip.get("id"):
            continue
        at = _parse_time(clip.get("started_at"))
        if at is None:
            continue
        delta = (at - start_time).total_seconds()
        if -_EDGE_MATCH_BEFORE_SECONDS <= delta <= settings.event_clip_edge_wait_seconds:
            if best_delta is None or abs(delta) < best_delta:
                best, best_delta = clip, abs(delta)
    return best


# ------------------------------------------------------------- playback


async def clip_bytes(session, row: Event) -> tuple[bytes, str] | None:
    """Return (video, content type) for a ready event clip, else ``None``."""
    state = (row.event_metadata or {}).get("clip") or {}
    if state.get("status") != "ready":
        return None
    if state.get("source") == "incident" and state.get("incident_id"):
        clip = await session.get(IncidentClip, state["incident_id"])
        return (clip.video, "video/mp4") if clip is not None else None
    clip_id = state.get("clip_id")
    if not clip_id:
        return None
    clip = await session.get(EventClip, clip_id)
    return (clip.video, clip.content_type or "video/mp4") if clip is not None else None


# ---------------------------------------------------------------- expiry


async def expire(session, now: datetime | None = None) -> int:
    """Delete event clips past retention unless an event using them is held."""
    now = now or _now()
    cutoff = now - timedelta(days=settings.event_clip_retention_days)
    rows = (await session.execute(
        select(EventClip.id, EventClip.event_ids).where(EventClip.created_at < cutoff)
    )).all()
    removed = 0
    for clip_id, event_ids in rows:
        events = []
        if event_ids:
            events = list((await session.execute(select(Event).where(Event.id.in_(list(event_ids))))).scalars())
        if any(event.retention_hold for event in events):
            continue
        await session.execute(delete(EventClip).where(EventClip.id == clip_id))
        for event in events:
            metadata = dict(event.event_metadata or {})
            state = dict(metadata.get("clip") or {})
            if state.get("clip_id") == clip_id:
                state.update(status="expired", reason="Clip passed its retention period")
                metadata["clip"] = state
                event.event_metadata = metadata
        removed += 1
    await session.commit()
    if removed:
        logger.info("expired %d event clip(s)", removed)
    return removed


async def _sweep_loop() -> None:
    while True:
        try:
            async with SessionLocal() as session:
                await expire(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - retry next hour
            logger.exception("event clip expiry failed")
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)


def start_sweeper() -> None:
    global _sweeper
    if _sweeper is None or _sweeper.done():
        _sweeper = asyncio.create_task(_sweep_loop())


async def stop() -> None:
    global _sweeper, _edge_waiters
    tasks = list(_tasks)
    if _sweeper is not None:
        tasks.append(_sweeper)
        _sweeper = None
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
    _shared.clear()
    _edge_waiters = 0
