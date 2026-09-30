"""Deterministic camera health watchdog (SPEC follow-up: tamper/obstruction/
offline detection).

Coarse pixel-level heuristics only - explicitly **not** a learned model -
watching for three integrity failures a burglar (or a loose cable, or a
spider on the lens) can cause: the lens being obstructed, a frozen/replayed
feed, and the camera going offline entirely. These are questions about
whether the camera can currently be *trusted at all*, which is a different
question from "is there an intruder", so unlike intrusion incidents these
are always raised regardless of arming mode (see ``app.services.incidents``)
and never depend on any AI/ML model.

State is kept in-process only (module-level dict), the same convention
:mod:`app.services.ingestion` already uses for cooldown bookkeeping: losing
a few minutes of "how long has this looked frozen" state on a restart is
harmless, since the heuristics re-accumulate evidence within seconds to
minutes of the next poll.

Under the default ``mock`` provider/detector backend, mock cameras emit
non-image placeholder bytes; :func:`app.ai.imaging.open_frame` returns
``None`` for those, so frame-based checks (obstruction/frozen) safely no-op
- consistent with the rest of the AI pipeline's "mock cannot see pixels"
design (SPEC 15). Offline detection is independent of frame decoding and
still works under mock cameras, since it is driven directly by the
provider-reported ``online`` flag.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from ..config import settings
from ..models.db import Incident
from ..services import incidents as incident_service
from .imaging import open_frame

logger = logging.getLogger(__name__)


@dataclass
class _CameraHealth:
    last_online: bool | None = None
    offline_incident_open: bool = False
    consecutive_obstructed: int = 0
    obstruction_incident_open: bool = False
    last_frame_stats: tuple[float, float] | None = None
    frozen_since: float | None = None
    frozen_incident_open: bool = False


_state: dict[str, _CameraHealth] = {}


def reset() -> None:
    """Test hook: clear all in-process camera health state."""
    _state.clear()


async def seed_from_open_incidents(session_factory) -> None:
    """Restore in-process watchdog state from currently open/acknowledged
    camera-health incidents. Call once at process startup.

    State is deliberately in-memory only (see module docstring), but that
    means a restart while a camera is genuinely offline/obstructed/frozen
    would otherwise reset ``last_online``/``*_incident_open`` to unknown -
    and the very next "camera looks fine again" observation would be
    swallowed by the first-observation guard in
    :func:`evaluate_online_state`/:func:`evaluate_frame` (there is nothing
    to compare against yet), leaving the already-open incident stuck open
    forever even after the camera actually recovers. Seeding state from the
    DB's existing open incidents at startup closes that gap without
    changing the deliberate "don't alarm on a brand-new camera's first
    reading" behavior for cameras that have no open incident.

    Raises no new incidents; it only seeds state so future transitions are
    detected correctly.
    """
    async with session_factory() as session:
        result = await session.execute(
            select(Incident).where(
                Incident.kind.in_(("camera_offline", "camera_obstruction", "camera_frozen")),
                Incident.status.in_(("open", "acknowledged")),
            )
        )
        for incident in result.scalars().all():
            if not incident.camera_id:
                continue
            health = _get(incident.camera_id)
            if incident.kind == "camera_offline":
                health.last_online = False
                health.offline_incident_open = True
            elif incident.kind == "camera_obstruction":
                health.obstruction_incident_open = True
            elif incident.kind == "camera_frozen":
                health.frozen_incident_open = True


def _get(camera_id: str) -> _CameraHealth:
    return _state.setdefault(camera_id, _CameraHealth())


def _frame_stats(image: bytes) -> tuple[float, float] | None:
    """``(mean, std)`` luminance of ``image``, or ``None`` if it does not
    decode as an image at all (mock placeholder payloads)."""
    frame = open_frame(image)
    if frame is None:
        return None
    from PIL import ImageStat

    gray = frame.convert("L")
    gray.thumbnail((160, 160))  # coarse heuristic; exact resolution does not matter
    stat = ImageStat.Stat(gray)
    return stat.mean[0], stat.stddev[0]


async def evaluate_online_state(session_factory, camera_id: str, camera_name: str, online: bool) -> None:
    """Raise/resolve a ``camera_offline`` incident on online<->offline
    transitions. Must run for every discovered camera every ingestion tick,
    including ones the rest of the loop skips for being offline."""
    health = _get(camera_id)
    was_online = health.last_online
    health.last_online = online
    if was_online is None or online == was_online:
        return
    if not online:
        await incident_service.raise_camera_health(
            session_factory, camera_id, camera_name, "offline",
            "Camera went offline and stopped responding.",
        )
        health.offline_incident_open = True
    elif health.offline_incident_open:
        await incident_service.resolve_camera_health(session_factory, camera_id, "offline")
        health.offline_incident_open = False


async def evaluate_frame(session_factory, camera_id: str, camera_name: str, image: bytes) -> None:
    """Obstruction/frozen-feed checks on one freshly-acquired frame. Only
    ever called for cameras that are online and yielded a real frame this
    tick; a ``None`` frame decode (mock placeholder) safely no-ops."""
    stats = _frame_stats(image)
    if stats is None:
        return
    mean, std = stats
    health = _get(camera_id)
    now = time.monotonic()

    await _evaluate_obstruction(session_factory, camera_id, camera_name, health, std)
    await _evaluate_frozen(session_factory, camera_id, camera_name, health, mean, std, now)
    health.last_frame_stats = (mean, std)


async def _evaluate_obstruction(session_factory, camera_id, camera_name, health: _CameraHealth, std: float) -> None:
    if std < settings.camera_obstruction_std_threshold:
        health.consecutive_obstructed += 1
    else:
        health.consecutive_obstructed = 0
        if health.obstruction_incident_open:
            await incident_service.resolve_camera_health(session_factory, camera_id, "obstruction")
            health.obstruction_incident_open = False
        return
    if (
        health.consecutive_obstructed >= settings.camera_obstruction_confirm_samples
        and not health.obstruction_incident_open
    ):
        await incident_service.raise_camera_health(
            session_factory, camera_id, camera_name, "obstruction",
            "Camera view looks blocked or covered (a flat, low-detail image for several samples in a row).",
        )
        health.obstruction_incident_open = True


async def _evaluate_frozen(session_factory, camera_id, camera_name, health: _CameraHealth, mean, std, now) -> None:
    previous = health.last_frame_stats
    if previous is None:
        return
    prev_mean, prev_std = previous
    unchanged = (
        abs(mean - prev_mean) < settings.camera_frozen_diff_threshold
        and abs(std - prev_std) < settings.camera_frozen_diff_threshold
    )
    if not unchanged:
        health.frozen_since = None
        if health.frozen_incident_open:
            await incident_service.resolve_camera_health(session_factory, camera_id, "frozen")
            health.frozen_incident_open = False
        return
    if health.frozen_since is None:
        health.frozen_since = now
    if (
        now - health.frozen_since >= settings.camera_frozen_seconds
        and not health.frozen_incident_open
    ):
        await incident_service.raise_camera_health(
            session_factory, camera_id, camera_name, "frozen",
            "Camera feed has not changed in a long time and may be frozen or tampered with.",
        )
        health.frozen_incident_open = True
