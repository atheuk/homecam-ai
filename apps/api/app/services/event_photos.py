"""Subject-independent event evidence, with bounded asynchronous backfill."""
from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import datetime, timedelta, timezone

from PIL import Image
from sqlalchemy import select

from ..ai.best_photo import BestPhoto, sharpness_score
from ..ai.imaging import configure_pillow
from ..config import settings
from ..db import SessionLocal
from ..models.db import Event, EventPhoto
from .provider_registry import find_provider_for_camera
from .stream_frames import stream_hub

logger = logging.getLogger(__name__)
CAPTURE_DEADLINE = 15.0
ATTEMPT_TIMEOUT = 3.0
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PENDING = 128
_tasks: dict[str, asyncio.Task] = {}
_camera_tasks: dict[str, asyncio.Task] = {}
_recent: dict[str, tuple[float, bytes]] = {}
_slots = asyncio.Semaphore(2)


def fallback_photo(frames: list[bytes]) -> BestPhoto | None:
    """Keep readable bounded evidence, never invent a subject or its boxes."""
    candidates = []
    configure_pillow()
    for index, image in enumerate(frames):
        if not image or len(image) > MAX_IMAGE_BYTES:
            continue
        try:
            with Image.open(io.BytesIO(image)) as opened:
                opened.load()
                opened.thumbnail((1920, 1920))
                output = io.BytesIO()
                opened.convert("RGB").save(output, "JPEG", quality=90)
                data, width, height = output.getvalue(), opened.width, opened.height
        except Exception:  # noqa: BLE001 - a corrupt camera frame is not evidence
            if image.startswith(b"HOME_CAM_MOCK_SNAPSHOT:") and settings.app_env != "production":
                data, width, height = image, None, None
            else:
                logger.warning("camera returned an unreadable event photo")
                continue
        candidates.append(BestPhoto(
            frame_index=index, score=sharpness_score(data), sharpness=sharpness_score(data),
            detection=None, image=data, cropped=False, width=width, height=height,
        ))
    return max(candidates, key=lambda photo: photo.score) if candidates else None


async def store_fallback(session, row: Event, frames: list[bytes], source: str) -> bool:
    from .ai_pipeline import _persist_event_photo

    if await session.get(EventPhoto, row.id) is not None:
        return True
    photo = await asyncio.to_thread(fallback_photo, frames)
    if photo is None:
        return False
    await _persist_event_photo(session, row.id, photo, None)
    row.event_metadata = {
        **(row.event_metadata or {}),
        "best_photo": photo.as_dict(), "photo_verified": False,
        "photo_capture": {"status": "captured", "source": source, "fallback": True},
    }
    return True


def capture_state(row: Event) -> dict:
    state = dict((row.event_metadata or {}).get("photo_capture") or {})
    at = row.start_time if row.start_time.tzinfo else row.start_time.replace(tzinfo=timezone.utc)
    if state.get("status") == "pending" and (datetime.now(timezone.utc) - at).total_seconds() > 180:
        return {"status": "failed", "reason": "Photo capture was interrupted"}
    return state


async def _acquire(camera_id: str) -> tuple[bytes, str]:
    # Share acquisition across simultaneous events from one camera. The
    # provider/edge's own session caps still apply to every snapshot call.
    async with _slots:
        for delay in (0, 1, 2, 4):
            await asyncio.sleep(delay)
            cached = stream_hub.latest(camera_id)
            if cached is not None and await asyncio.to_thread(fallback_photo, [cached.frame]) is not None:
                return cached.frame, "stream"
            recent = _recent.get(camera_id)
            if recent is not None and time.monotonic() - recent[0] <= 10:
                return recent[1], "snapshot"
            try:
                provider = await asyncio.wait_for(find_provider_for_camera(camera_id), ATTEMPT_TIMEOUT)
                if provider is None:
                    continue
                capture = getattr(provider, "get_event_snapshot", None)
                if capture is not None:
                    try:
                        image = await asyncio.wait_for(capture(camera_id), ATTEMPT_TIMEOUT)
                        if image and await asyncio.to_thread(fallback_photo, [image]) is not None:
                            return image, "provider_event"
                    except Exception:  # noqa: BLE001 - fresh snapshot remains available
                        logger.warning("provider event image unavailable for %s", camera_id)
                # Respect ingestion's per-camera CGI rate limit as well.
                from . import ingestion

                if not ingestion._snapshot_due(camera_id):
                    continue
                ingestion._last_snapshot_at[camera_id] = time.monotonic()
                image = await asyncio.wait_for(provider.get_snapshot(camera_id), ATTEMPT_TIMEOUT)
                if await asyncio.to_thread(fallback_photo, [image]) is not None:
                    if len(_recent) >= 256:
                        _recent.pop(next(iter(_recent)))
                    _recent[camera_id] = (time.monotonic(), image)
                    return image, "snapshot"
            except Exception:  # noqa: BLE001 - bounded retry, never suppress the event
                logger.warning("event photo attempt failed for camera %s", camera_id)
        raise TimeoutError("Camera did not return an image")


async def _bounded_acquire(camera_id: str) -> tuple[bytes, str]:
    async with asyncio.timeout(CAPTURE_DEADLINE):
        return await _acquire(camera_id)


async def backfill(event_id: str, camera_id: str) -> None:
    image = None
    source = ""
    reason = "Camera did not return an image"
    try:
        task = _camera_tasks.get(camera_id)
        if task is None:
            task = asyncio.create_task(_bounded_acquire(camera_id))
            _camera_tasks[camera_id] = task
            def forget(done: asyncio.Task) -> None:
                if _camera_tasks.get(camera_id) is done:
                    _camera_tasks.pop(camera_id, None)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(forget)
        try:
            image, source = await asyncio.wait_for(asyncio.shield(task), CAPTURE_DEADLINE)
        finally:
            if task.done() and _camera_tasks.get(camera_id) is task:
                _camera_tasks.pop(camera_id, None)
    except Exception:  # noqa: BLE001 - persist a visible terminal state
        logger.warning("event photo capture exhausted for %s (%s)", event_id, camera_id)
    try:
        from .events import event_bus, to_dict

        async with SessionLocal() as session:
            row = (await session.execute(
                select(Event).where(Event.id == event_id).with_for_update()
            )).scalar_one_or_none()
            # Retention may have removed the event while acquisition ran.
            if row is None or await session.get(EventPhoto, event_id) is not None:
                return
            if image is not None and await store_fallback(session, row, [image], source):
                logger.info("event photo backfilled for %s camera=%s source=%s", event_id, camera_id, source)
            else:
                row.event_metadata = {
                    **(row.event_metadata or {}),
                    "photo_capture": {"status": "failed", "reason": reason},
                }
            await session.commit()
            payload = to_dict(row)
            has_photo = capture_state(row).get("status") == "captured"
            payload.update(
                _sse_event="event.updated", has_photo=has_photo,
                photo_url=f"/api/v1/events/{event_id}/photo" if has_photo else None,
            )
            await event_bus.publish(payload)
    except Exception:  # noqa: BLE001 - log durable storage failures explicitly
        logger.exception("event photo backfill storage failed for %s", event_id)


def schedule(row: Event) -> bool:
    if capture_state(row).get("status") != "pending" or row.id in _tasks:
        return True
    if len(_tasks) >= MAX_PENDING:
        logger.error("event photo capture queue full for %s", row.id)
        return False
    event_id = row.id
    task = asyncio.create_task(backfill(row.id, row.camera_id))
    _tasks[event_id] = task
    task.add_done_callback(lambda _: _tasks.pop(event_id, None))
    return True


async def recover_pending() -> None:
    """A restart must not leave persisted pending cards spinning forever."""
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Event).where(Event.start_time >= datetime.now(timezone.utc) - timedelta(minutes=5))
            .order_by(Event.start_time.desc()).limit(MAX_PENDING)
        )).scalars().all()
        for row in rows:
            schedule(row)


async def stop() -> None:
    tasks = [*_tasks.values(), *_camera_tasks.values()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
    _camera_tasks.clear()
    _recent.clear()
