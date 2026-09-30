"""Current-still capture for the admin zone editor.

Drawing a zone needs one *current* picture of what the camera sees. This
reuses the exact acquisition path the pipeline already uses — a frame the
ingestion stream reader has already decoded if there is one, otherwise the
provider's snapshot — so the editor adds no new load on the NVR beyond one
snapshot per explicit user request.

Cameras that are known-offline are never asked at all, and a camera whose
capture just failed is put on a short cooldown, so repeatedly clicking
"retry" on a disconnected channel cannot turn into a request storm.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from ..providers.base import CameraNotFoundError, CameraOfflineError, ProviderUnavailableError
from . import cameras as camera_service
from .provider_registry import find_provider_for_camera, hidden_provider_ids
from .stream_frames import stream_hub

#: How long a camera is left alone after a failed capture.
FAILURE_COOLDOWN_SECONDS = 10.0

_last_failure: dict[str, float] = {}


class CameraStillUnavailable(Exception):
    """No current still could be produced for this camera."""


class CameraStillNotFound(Exception):
    """No such camera (or its provider is hidden)."""


@dataclass(frozen=True)
class CameraStill:
    image: bytes
    source: str
    width: int | None
    height: int | None


def reset_cooldowns() -> None:
    _last_failure.clear()


def _note_failure(camera_id: str) -> None:
    _last_failure[camera_id] = time.monotonic()


def _cooling_down(camera_id: str) -> bool:
    failed_at = _last_failure.get(camera_id)
    return failed_at is not None and time.monotonic() - failed_at < FAILURE_COOLDOWN_SECONDS


def _dimensions(image: bytes) -> tuple[int | None, int | None]:
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(image)) as opened:
            return opened.size
    except Exception:  # noqa: BLE001 - an unreadable still is still drawable
        return (None, None)


async def capture_still(session: AsyncSession, camera_id: str) -> CameraStill:
    row = await camera_service.get_camera(session, camera_id)
    if row is None or row.provider_id in await hidden_provider_ids():
        raise CameraStillNotFound(camera_id)

    cached = stream_hub.latest(camera_id)
    if cached is not None:
        _last_failure.pop(camera_id, None)
        return _still(cached.frame, "stream")

    if not row.online:
        raise CameraStillUnavailable(
            f"Camera '{camera_id}' is offline, so it has no current picture to draw on."
        )
    if _cooling_down(camera_id):
        raise CameraStillUnavailable(
            f"Camera '{camera_id}' could not be reached a moment ago; try again shortly."
        )

    provider = await find_provider_for_camera(camera_id)
    if provider is None:
        raise CameraStillNotFound(camera_id)
    try:
        image = await provider.get_snapshot(camera_id)
    except CameraNotFoundError as exc:
        raise CameraStillNotFound(camera_id) from exc
    except (CameraOfflineError, ProviderUnavailableError) as exc:
        _note_failure(camera_id)
        raise CameraStillUnavailable(
            f"Camera '{camera_id}' did not return a picture: {type(exc).__name__}."
        ) from exc
    if not image:
        _note_failure(camera_id)
        raise CameraStillUnavailable(f"Camera '{camera_id}' returned an empty picture.")

    _last_failure.pop(camera_id, None)
    stream_hub.note_snapshot(camera_id, image)
    return _still(image, "snapshot")


def _still(image: bytes, source: str) -> CameraStill:
    width, height = _dimensions(image)
    return CameraStill(image=image, source=source, width=width, height=height)


__all__ = [
    "CameraStill",
    "CameraStillNotFound",
    "CameraStillUnavailable",
    "FAILURE_COOLDOWN_SECONDS",
    "capture_still",
    "reset_cooldowns",
]
