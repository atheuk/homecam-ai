"""Zero-detection watchdog (incident 2026-09-30).

Production ingested frames at 100% success for three hours while producing
no events at all, because a stale ``AI_DETECTOR_BACKEND`` had silently
replaced the real detector with the mock one. Every existing signal stayed
green: ``/health`` and ``/ready`` returned 200, the provider was ONLINE 2/2,
and each ingestion stats line read ``events=0`` — which is also what a quiet
driveway looks like.

This module watches the one quantity that separates those two cases:

* **Not** "no events were emitted". Events are legitimately suppressed for a
  parked car, so a working system can emit zero events all afternoon.
* **But** "frames were successfully detected on, and the detector returned
  literally no box at all, for a sustained window". A working detector on a
  real scene produces boxes constantly — a parked car, a tree, a passing
  vehicle — even when none of them become events.

State is kept **per camera**. A single global window would let one busy
camera hide a blind one: any detection anywhere restarts the evidence
window, so a driveway camera seeing traffic all day would indefinitely mask
a back-garden camera whose stream had died. Each camera therefore carries
its own window, and the surfaces aggregate with *any camera blind* rather
than *all cameras blind*.

The window is deliberately long (default 45 minutes) and gated on a minimum
frame count, so a genuinely quiet period on a camera that is barely
delivering frames never raises a false alarm.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..config import settings

logger = logging.getLogger(__name__)


@dataclass
class _CameraWindow:
    """Evidence window for a single camera."""

    camera_id: str
    window_started_at: float | None = None
    frames_detected: int = 0
    detections_seen: int = 0
    last_detection_at: float | None = None
    blackout: bool = False
    blackout_since: float | None = None

    def as_dict(self, moment: float) -> dict:
        window = 0.0 if self.window_started_at is None else moment - self.window_started_at
        return {
            "camera_id": self.camera_id,
            "blackout": self.blackout,
            "window_seconds": round(window, 1),
            "frames_detected_in_window": self.frames_detected,
            "detections_in_window": self.detections_seen,
            "seconds_since_last_detection": (
                None
                if self.last_detection_at is None
                else round(moment - self.last_detection_at, 1)
            ),
            "blackout_seconds": (
                None if self.blackout_since is None else round(moment - self.blackout_since, 1)
            ),
        }


_cameras: dict[str, _CameraWindow] = {}


def record_frame(camera_id: str, detections_count: int, *, now: float | None = None) -> None:
    """Record one frame the detector actually ran on, for one camera.

    ``detections_count`` is the number of boxes the detector returned, before
    any event-level suppression — suppression is exactly what this watchdog
    must see through.
    """
    if not settings.detector_watchdog_enabled:
        return
    moment = time.monotonic() if now is None else now
    key = str(camera_id)
    camera = _cameras.get(key)
    if camera is None:
        camera = _CameraWindow(camera_id=key)
        _cameras[key] = camera

    if camera.window_started_at is None:
        camera.window_started_at = moment
    camera.frames_detected += 1

    if detections_count > 0:
        camera.detections_seen += detections_count
        camera.last_detection_at = moment
        if camera.blackout:
            logger.warning(
                "DETECTOR BLACKOUT CLEARED for camera %s: detections resumed after %.0fs of "
                "silence",
                key,
                moment - (camera.blackout_since if camera.blackout_since is not None else moment),
            )
        camera.blackout = False
        camera.blackout_since = None
        # A detection proves the detector works for this camera; restart its
        # evidence window so the next alarm needs a fresh, full window of
        # silence.
        camera.window_started_at = moment
        camera.frames_detected = 0
        camera.detections_seen = 0
        return

    _evaluate(camera, moment)


def _evaluate(camera: _CameraWindow, now: float) -> None:
    started = camera.window_started_at
    window = now - (started if started is not None else now)
    if window < settings.detector_blackout_window_seconds:
        return
    if camera.frames_detected < settings.detector_blackout_min_frames:
        return
    if camera.detections_seen > 0:  # pragma: no cover - defensive; reset on detection
        return
    if camera.blackout:
        return
    camera.blackout = True
    camera.blackout_since = now
    logger.error(
        "DETECTOR BLACKOUT on camera %s: %d frames were detected on over %.0fs and the detector "
        "returned zero detections in total. This camera is very likely blind (check "
        "AI_DETECTOR_BACKEND / AI_DETECTOR_MODEL_PATH and GET /api/v1/system/status).",
        camera.camera_id,
        camera.frames_detected,
        window,
    )


def blackout_cameras() -> list[str]:
    """Camera ids currently in blackout, sorted for a stable surface."""
    return sorted(key for key, camera in _cameras.items() if camera.blackout)


def status(*, now: float | None = None) -> dict:
    """Watchdog state for the status/readiness surface.

    ``blackout`` aggregates as *any camera blind*: one working camera must
    never be able to vouch for a blind one.
    """
    moment = time.monotonic() if now is None else now
    blind = blackout_cameras()
    per_camera = [_cameras[key].as_dict(moment) for key in sorted(_cameras)]
    return {
        "enabled": settings.detector_watchdog_enabled,
        "blackout": bool(blind),
        "blackout_cameras": blind,
        "cameras_tracked": len(_cameras),
        "window_limit_seconds": settings.detector_blackout_window_seconds,
        "min_frames": settings.detector_blackout_min_frames,
        "cameras": per_camera,
    }


def camera_status(camera_id: str, *, now: float | None = None) -> dict | None:
    camera = _cameras.get(str(camera_id))
    if camera is None:
        return None
    return camera.as_dict(time.monotonic() if now is None else now)


def in_blackout() -> bool:
    """True when *any* tracked camera is in blackout."""
    return any(camera.blackout for camera in _cameras.values())


def reset() -> None:
    """Forget all watchdog state (startup and tests)."""
    _cameras.clear()
