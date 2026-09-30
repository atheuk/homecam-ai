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

The window is deliberately long (default 45 minutes) and gated on a minimum
frame count, so a genuinely quiet period on a camera that is barely
delivering frames never raises a false alarm.
"""
from __future__ import annotations

import logging
import time

from ..config import settings

logger = logging.getLogger(__name__)

_window_started_at: float | None = None
_frames_detected: int = 0
_detections_seen: int = 0
_last_detection_at: float | None = None
_blackout: bool = False
_blackout_since: float | None = None


def record_frame(detections_count: int, *, now: float | None = None) -> None:
    """Record one frame the detector actually ran on.

    ``detections_count`` is the number of boxes the detector returned, before
    any event-level suppression — suppression is exactly what this watchdog
    must see through.
    """
    global _window_started_at, _frames_detected, _detections_seen
    global _last_detection_at, _blackout, _blackout_since

    if not settings.detector_watchdog_enabled:
        return
    moment = time.monotonic() if now is None else now
    if _window_started_at is None:
        _window_started_at = moment
    _frames_detected += 1
    if detections_count > 0:
        _detections_seen += detections_count
        _last_detection_at = moment
        if _blackout:
            logger.warning(
                "DETECTOR BLACKOUT CLEARED: detections resumed after %.0fs of silence",
                moment - (_blackout_since if _blackout_since is not None else moment),
            )
        _blackout = False
        _blackout_since = None
        # A detection proves the detector works; restart the evidence window
        # so the next alarm needs a fresh, full window of silence.
        _window_started_at = moment
        _frames_detected = 0
        _detections_seen = 0
        return

    _evaluate(moment)


def _evaluate(now: float) -> None:
    global _blackout, _blackout_since

    window = now - (_window_started_at if _window_started_at is not None else now)
    if window < settings.detector_blackout_window_seconds:
        return
    if _frames_detected < settings.detector_blackout_min_frames:
        return
    if _detections_seen > 0:  # pragma: no cover - defensive; reset on detection
        return
    if _blackout:
        return
    _blackout = True
    _blackout_since = now
    logger.error(
        "DETECTOR BLACKOUT: %d frames were detected on over %.0fs and the detector returned zero "
        "detections in total. The system is very likely blind (check AI_DETECTOR_BACKEND / "
        "AI_DETECTOR_MODEL_PATH and GET /api/v1/system/status).",
        _frames_detected,
        window,
    )


def status(*, now: float | None = None) -> dict:
    """Watchdog state for the status/readiness surface."""
    moment = time.monotonic() if now is None else now
    window = 0.0 if _window_started_at is None else moment - _window_started_at
    return {
        "enabled": settings.detector_watchdog_enabled,
        "blackout": _blackout,
        "window_seconds": round(window, 1),
        "window_limit_seconds": settings.detector_blackout_window_seconds,
        "frames_detected_in_window": _frames_detected,
        "detections_in_window": _detections_seen,
        "min_frames": settings.detector_blackout_min_frames,
        "seconds_since_last_detection": (
            None if _last_detection_at is None else round(moment - _last_detection_at, 1)
        ),
    }


def in_blackout() -> bool:
    return _blackout


def reset() -> None:
    """Forget all watchdog state (startup and tests)."""
    global _window_started_at, _frames_detected, _detections_seen
    global _last_detection_at, _blackout, _blackout_since
    _window_started_at = None
    _frames_detected = 0
    _detections_seen = 0
    _last_detection_at = None
    _blackout = False
    _blackout_since = None
