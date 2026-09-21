"""Temporal dwell tracking used to tell "passing" from "parked/lingering".

Deliberately tiny and dependency-free: a track is keyed by camera + object
class + zone, and stays alive while the bounding-box centre barely moves.
The tracker never invents observations — callers pass real event timestamps,
so a "car parked" conclusion is always backed by two or more real frames
separated by real time (SPEC section 18: never fabricate intermediate
actions).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .detector import BoundingBox

# A car that shifts its box centre by more than this fraction of the frame
# between observations is treated as moving, not parked.
DEFAULT_MOVEMENT_THRESHOLD = 0.05


@dataclass
class Track:
    key: tuple[str, str, str]
    first_seen: datetime
    last_seen: datetime
    bbox: BoundingBox
    stationary_since: datetime
    observations: int = 1

    @property
    def stationary_seconds(self) -> float:
        return max((self.last_seen - self.stationary_since).total_seconds(), 0.0)


@dataclass
class DwellTracker:
    """In-process dwell tracker.

    State is intentionally in-memory: it is an optimisation over the durable
    event log, and losing it on restart only means the next parked-car
    conclusion takes one more dwell window.
    """

    movement_threshold: float = DEFAULT_MOVEMENT_THRESHOLD
    ttl_seconds: float = 900.0
    _tracks: dict[tuple[str, str, str], Track] = field(default_factory=dict)

    def observe(
        self, camera_id: str, label: str, zone_name: str | None, bbox: BoundingBox, at: datetime
    ) -> Track:
        self._expire(at)
        key = (camera_id, label, zone_name or "")
        existing = self._tracks.get(key)
        if existing is None:
            track = Track(key=key, first_seen=at, last_seen=at, bbox=bbox, stationary_since=at)
            self._tracks[key] = track
            return track
        old_cx, old_cy = existing.bbox.center
        new_cx, new_cy = bbox.center
        moved = max(abs(new_cx - old_cx), abs(new_cy - old_cy))
        if moved > self.movement_threshold:
            existing.stationary_since = at
        existing.bbox = bbox
        existing.last_seen = at
        existing.observations += 1
        return existing

    def stationary_seconds(self, camera_id: str, label: str, zone_name: str | None) -> float:
        track = self._tracks.get((camera_id, label, zone_name or ""))
        return track.stationary_seconds if track else 0.0

    def _expire(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.ttl_seconds)
        for key, track in list(self._tracks.items()):
            if track.last_seen < cutoff:
                del self._tracks[key]

    def reset(self) -> None:
        self._tracks.clear()


dwell_tracker = DwellTracker()
