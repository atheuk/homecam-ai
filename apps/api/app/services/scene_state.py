"""Temporal scene engine: what changed over time, not just what is in a frame.

The ingestion loop (:mod:`app.services.ingestion`) detects objects frame by
frame. Some things the owner cares about are only visible across frames:

* **Parked vehicles.** Each vehicle is a *track* matched frame to frame by
  box overlap. Observations are counted on every processed frame,
  independently of event cooldowns. After ``vehicle_parked_observations``
  matching, unmoved observations the track is *parked* and stays silent: no
  event and no model call. It speaks again only when evidence shows a
  transition: it moved materially, it was missing long enough to have left
  (a camera outage never counts), or a person is at it. Several vehicles
  are tracked separately.
* **Mailbox deliveries** (zone kind ``mailbox``). A person at the mailbox
  over several frames is an episode with a BEFORE, DURING and AFTER frame.
  A single-frame walk-by is dropped. A parcel left in the zone is a local
  positive. Anything else ambiguous gets one closed-question vision check
  (:mod:`app.ai.temporal_vision`).
* **Garbage bins** (zone kind ``bins``). The zone's appearance is compared
  with a slowly adapting baseline. A change that persists while nothing
  occludes the zone is a candidate. The vision check then counts the bins
  before and after. ``bin_emptied`` also needs collection evidence;
  disappearance alone does not count.

The engine is part of the existing pipeline, not a second one. It only
decides *that* an event happened and why. Events still go through
:func:`app.services.events.create_and_broadcast_event`. They keep the SPEC
event types and express the rest through ``tags``, ``zone`` and
``metadata.temporal``, the same extension rule as :mod:`app.ai.semantics`.

State is persisted per camera in ``scene_states`` after every transition
(and periodically), so a redeploy neither re-announces a parked car nor
mistakes bins already out for bins just placed out.
"""
from __future__ import annotations

import io
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..ai.detector import VEHICLE_CLASSES, BoundingBox, Detection
from ..ai.temporal_vision import BinVerdict, MailboxVerdict, get_temporal_verifier
from ..ai.zones import Zone, intersection_area
from ..config import settings

logger = logging.getLogger(__name__)

STATE_VERSION = 1

# Vehicle transitions, as event tags.
ARRIVED = "vehicle_arrived"
PARKED = "vehicle_parked"
MOVED = "vehicle_moved"
DEPARTED = "vehicle_departed"
RETURNED = "vehicle_returned"
INTERACTION = "vehicle_interaction"

# Non-parked tracks unseen for this many processed frames are dropped. A
# passing car is gone; it has not "departed" from anywhere.
_TRANSIENT_MISSES = 3
# Frames a person must be at a parked vehicle before it counts as an
# interaction rather than someone walking past in front of it.
_INTERACTION_FRAMES = 2
# A moving (not yet parked) track may continue by centre distance when the
# box changed too much to overlap, e.g. a car turning in.
_MOVING_MATCH_DISTANCE = 0.2
# Someone just got in: the car may pull away a long way between frames.
_ATTENDED_MATCH_DISTANCE = 0.35
_RETURN_IOU = 0.5
_MAX_DEPARTED = 16
_SAVE_EVERY_SECONDS = 60.0


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def iou(a: BoundingBox, b: BoundingBox) -> float:
    inter = intersection_area(a, b)
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def containment(a: BoundingBox, b: BoundingBox) -> float:
    """Share of the smaller box that lies inside the larger one."""
    smaller = min(a.area, b.area)
    return intersection_area(a, b) / smaller if smaller > 0 else 0.0


def centre_distance(a: BoundingBox, b: BoundingBox) -> float:
    (ax, ay), (bx, by) = a.center, b.center
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def _box(raw) -> BoundingBox:
    return BoundingBox.from_dict(raw)


# --- vehicles -----------------------------------------------------------------


@dataclass
class VehicleTrack:
    id: str
    label: str
    box: BoundingBox
    anchor: BoundingBox
    anchor_at: float
    first_seen: float
    last_seen: float
    confidence: float
    count: int = 1  # matching observations since ``anchor``
    observations: int = 1  # every matching observation of this track
    state: str = "new"  # new | moving | parked | attended
    missed: int = 0
    interaction_frames: int = 0
    bootstrap: bool = False
    arrival: bool = False  # it drove into view, so "arrived" is supported
    returned: bool = False
    announced: bool = False
    event_id: str | None = None
    pending: list[str] = field(default_factory=list)
    last_transition: str | None = None

    @property
    def settled(self) -> bool:
        return self.state in ("parked", "attended")

    @property
    def stationary_since(self) -> float | None:
        return self.anchor_at if self.state == "parked" else None

    def describe(self) -> dict:
        return {
            "track_id": self.id,
            "label": self.label,
            "state": self.state,
            "observation_count": self.observations,
            "stationary_since": _iso(self.stationary_since),
            "first_seen": _iso(self.first_seen),
            "last_seen": _iso(self.last_seen),
            "transition": self.last_transition,
            "box": self.box.as_dict(),
        }

    def to_state(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "box": self.box.as_dict(),
            "anchor": self.anchor.as_dict(),
            "anchor_at": self.anchor_at,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "confidence": self.confidence,
            "count": self.count,
            "observations": self.observations,
            "state": self.state,
            "arrival": self.arrival,
            "returned": self.returned,
            "announced": self.announced,
            "event_id": self.event_id,
            "last_transition": self.last_transition,
        }

    @classmethod
    def from_state(cls, raw: dict) -> "VehicleTrack":
        return cls(
            id=str(raw["id"]),
            label=str(raw.get("label") or "car"),
            box=_box(raw["box"]),
            anchor=_box(raw["anchor"]),
            anchor_at=float(raw["anchor_at"]),
            first_seen=float(raw["first_seen"]),
            last_seen=float(raw["last_seen"]),
            confidence=float(raw.get("confidence") or 0.0),
            count=int(raw.get("count") or 1),
            observations=int(raw.get("observations") or 1),
            # An in-progress interaction does not survive a restart: the
            # person is not known to be there any more.
            state="parked" if raw.get("state") == "attended" else str(raw.get("state") or "new"),
            arrival=bool(raw.get("arrival")),
            returned=bool(raw.get("returned")),
            announced=bool(raw.get("announced")),
            event_id=raw.get("event_id"),
            last_transition=raw.get("last_transition"),
        )


@dataclass
class VehicleEmission:
    """One vehicle event covering every transition due at this moment."""

    tracks: list[VehicleTrack]
    departures: list[dict]
    tags: list[str]
    detections: list[Detection]
    description: str
    photo: str  # "subject" or "scene"
    temporal: bool  # False for a plain first sighting


@dataclass
class ParkedUpdate:
    """An already-emitted event whose vehicle has now settled."""

    event_id: str
    tags: list[str]
    track: dict


class VehicleTracker:
    def __init__(self) -> None:
        self.tracks: list[VehicleTrack] = []
        self.departed: list[dict] = []
        self.pending_departures: list[dict] = []
        self.observed_frames = 0
        self.restored = False
        self.dirty = False

    # -- persistence

    def to_state(self) -> dict:
        return {
            "tracks": [track.to_state() for track in self.tracks],
            "departed": self.departed[-_MAX_DEPARTED:],
        }

    def load_state(self, raw: dict) -> None:
        if not isinstance(raw, dict):
            return
        tracks = []
        for item in raw.get("tracks") or []:
            try:
                tracks.append(VehicleTrack.from_state(item))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                logger.debug("dropping unreadable vehicle track: %s", exc)
        self.tracks = tracks
        departed = []
        for item in raw.get("departed") or []:
            try:
                _box(item["box"])
                float(item.get("at") or 0.0)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            departed.append(item)
        self.departed = departed
        self.restored = True

    # -- observation

    def observe(
        self, vehicles: list[Detection], people: list[Detection], now: float
    ) -> tuple[dict[int, VehicleTrack], list[ParkedUpdate]]:
        """Advance every track by one processed frame.

        Returns ``(interactions, parked_updates)``. ``interactions`` maps the
        index of a person in ``people`` to the parked vehicle they are at.
        """
        bootstrap = self.observed_frames == 0 and not self.restored
        self.observed_frames += 1
        matched = self._match(vehicles)
        parked_updates: list[ParkedUpdate] = []

        matched_boxes = [vehicles[i].bbox for i in matched.values()]
        created: set[str] = set()
        for index, detection in enumerate(vehicles):
            if index in matched.values():
                continue
            # A second, partial box on a car that is already tracked (the
            # detector often adds one) is not another vehicle.
            if any(containment(detection.bbox, box) >= settings.vehicle_track_containment for box in matched_boxes):
                continue
            track = self._new_track(detection, now, bootstrap)
            self.tracks.append(track)
            created.add(track.id)
            matched_boxes.append(detection.bbox)

        survivors: list[VehicleTrack] = []
        for track in self.tracks:
            index = matched.get(track.id)
            if index is None:
                if track.id in created:
                    survivors.append(track)
                    continue
                track.missed += 1
                if self._absent(track, now):
                    continue
                survivors.append(track)
                continue
            update = self._update(track, vehicles[index], now)
            if update is not None:
                parked_updates.append(update)
            survivors.append(track)
        self.tracks = survivors

        interactions = self._interactions(people)
        return interactions, parked_updates

    def _match(self, vehicles: list[Detection]) -> dict[str, int]:
        pairs: list[tuple[float, str, int]] = []
        for track in self.tracks:
            for index, detection in enumerate(vehicles):
                overlap = iou(track.box, detection.bbox)
                contained = containment(track.box, detection.bbox)
                if overlap >= settings.vehicle_track_match_iou or contained >= settings.vehicle_track_containment:
                    pairs.append((max(overlap, contained * 0.99), track.id, index))
                elif (
                    not track.settled
                    and track.missed <= 2
                    and centre_distance(track.box, detection.bbox) <= _MOVING_MATCH_DISTANCE
                ):
                    pairs.append((0.01, track.id, index))
        matched: dict[str, int] = {}
        used: set[int] = set()
        for _, track_id, index in sorted(pairs, reverse=True):
            if track_id in matched or index in used:
                continue
            matched[track_id] = index
            used.add(index)
        # A parked car that pulls out can move far enough between frames that
        # its boxes no longer overlap. If its spot is now empty and a
        # similar-sized vehicle is right beside it, that is the same car
        # moving, not a new car plus a departure.
        for track in self.tracks:
            if track.id in matched or not track.settled:
                continue
            best: tuple[float, int] | None = None
            for index, detection in enumerate(vehicles):
                if index in used:
                    continue
                ratio = detection.bbox.area / track.box.area if track.box.area > 0 else 0.0
                distance = centre_distance(track.box, detection.bbox)
                reach = _ATTENDED_MATCH_DISTANCE if track.state == "attended" else _MOVING_MATCH_DISTANCE
                if 0.5 <= ratio <= 2.0 and distance <= reach:
                    if best is None or distance < best[0]:
                        best = (distance, index)
            if best is not None:
                matched[track.id] = best[1]
                used.add(best[1])
        return matched

    def _new_track(self, detection: Detection, now: float, bootstrap: bool) -> VehicleTrack:
        track = VehicleTrack(
            id="veh-" + uuid.uuid4().hex[:8],
            label=detection.label,
            box=detection.bbox,
            anchor=detection.bbox,
            anchor_at=now,
            first_seen=now,
            last_seen=now,
            confidence=detection.confidence,
            bootstrap=bootstrap,
            arrival=not bootstrap,
        )
        cutoff = now - settings.vehicle_return_window_seconds
        for departed in list(self.departed):
            if float(departed.get("at") or 0) < cutoff:
                continue
            if iou(_box(departed["box"]), detection.bbox) >= _RETURN_IOU:
                track.returned = True
                self.departed.remove(departed)
                break
        self.dirty = True
        return track

    def _moved(self, track: VehicleTrack, box: BoundingBox) -> bool:
        return (
            centre_distance(box, track.anchor) > settings.vehicle_move_threshold
            and iou(box, track.anchor) < settings.vehicle_moved_iou
        )

    def _update(self, track: VehicleTrack, detection: Detection, now: float) -> ParkedUpdate | None:
        track.missed = 0
        track.last_seen = now
        track.box = detection.bbox
        track.confidence = max(track.confidence, detection.confidence)
        track.observations += 1
        if self._moved(track, detection.bbox):
            if track.settled:
                track.pending.append(MOVED)
                track.last_transition = MOVED
                # Parking again after this is not an arrival.
                track.arrival = False
                self.dirty = True
            track.state = "moving"
            track.anchor = detection.bbox
            track.anchor_at = now
            track.count = 1
            return None
        track.count += 1
        if track.state in ("new", "moving", "attended") and track.count >= settings.vehicle_parked_observations:
            was_attended = track.state == "attended"
            track.state = "parked"
            self.dirty = True
            if was_attended or not track.announced or not track.event_id:
                return None
            tags = [PARKED]
            if track.arrival:
                tags.insert(0, ARRIVED)
            track.last_transition = PARKED
            return ParkedUpdate(event_id=track.event_id, tags=tags, track=track.describe())
        return None

    def _absent(self, track: VehicleTrack, now: float) -> bool:
        """Whether an unseen track is gone (and so is dropped)."""
        if not track.settled:
            return track.missed > _TRANSIENT_MISSES
        if track.missed < settings.vehicle_absence_frames:
            return False
        if now - track.last_seen < settings.vehicle_absence_seconds:
            return False
        if not track.announced:
            # Never reported (too small, or held back): its leaving is not news either.
            self.dirty = True
            return True
        track.last_transition = DEPARTED
        departure = {**track.describe(), "state": "departed", "departed_at": _iso(now)}
        self.pending_departures.append(departure)
        self.departed.append({"box": track.anchor.as_dict(), "at": now, "track_id": track.id})
        self.departed = self.departed[-_MAX_DEPARTED:]
        self.dirty = True
        return True

    def _interactions(self, people: list[Detection]) -> dict[int, VehicleTrack]:
        found: dict[int, VehicleTrack] = {}
        at_track: set[str] = set()
        for index, person in enumerate(people):
            if person.bbox.area <= 0:
                continue
            for track in self.tracks:
                if not track.settled:
                    continue
                if intersection_area(person.bbox, track.box) / person.bbox.area >= settings.vehicle_interaction_overlap:
                    at_track.add(track.id)
                    if track.interaction_frames + 1 >= _INTERACTION_FRAMES:
                        found[index] = track
                    break
        for track in self.tracks:
            if track.id in at_track:
                track.interaction_frames += 1
                if track.interaction_frames >= _INTERACTION_FRAMES and track.state == "parked":
                    # Someone is at the car: it may be about to leave, so
                    # count afresh and let any move or departure speak.
                    track.state = "attended"
                    track.count = 0
                    track.anchor = track.box
                    track.last_transition = INTERACTION
                    self.dirty = True
            else:
                track.interaction_frames = 0
        return found

    # -- emission

    def emission(self, current: list[Detection], camera_name: str) -> VehicleEmission | None:
        """The vehicle event due now, if any (caller applies the cooldown)."""
        announce: list[VehicleTrack] = []
        for track in self.tracks:
            if track.missed:
                continue
            if track.pending:
                announce.append(track)
            elif not track.announced and self._announceable(track):
                announce.append(track)
        departures = list(self.pending_departures)
        if not announce and not departures:
            return None

        tags: list[str] = []
        for track in announce:
            for tag in track.pending:
                if tag not in tags:
                    tags.append(tag)
            if not track.announced:
                if track.returned and RETURNED not in tags:
                    tags.append(RETURNED)
                if track.state == "parked":
                    # It settled while a cooldown held its first event back.
                    for tag in ((ARRIVED, PARKED) if track.arrival else (PARKED,)):
                        if tag not in tags:
                            tags.append(tag)
        if departures:
            tags.append(DEPARTED)

        boxes = {id(track): track.box for track in announce}
        detections = [d for d in current if any(containment(d.bbox, box) >= 0.8 for box in boxes.values())]
        if MOVED in tags:
            description = f"A parked vehicle moved on {camera_name}."
        elif RETURNED in tags:
            description = f"A vehicle returned to its spot on {camera_name}."
        elif ARRIVED in tags:
            description = f"A vehicle arrived and parked on {camera_name}."
        elif departures and not announce:
            description = (
                f"A parked vehicle left on {camera_name}."
                if len(departures) == 1
                else f"{len(departures)} parked vehicles left on {camera_name}."
            )
        else:
            description = f"Vehicle detected on {camera_name}"
        if departures and announce:
            description += " A parked vehicle also left."
        return VehicleEmission(
            tracks=announce,
            departures=departures,
            tags=tags,
            detections=detections,
            description=description,
            photo="subject" if announce else "scene",
            temporal=bool(tags),
        )

    def _announceable(self, track: VehicleTrack) -> bool:
        if track.box.area < settings.vehicle_min_area:
            return False
        return track.observations >= 2 or track.confidence >= settings.vehicle_confirm_confidence

    def commit(self, emission: VehicleEmission, event_id: str) -> None:
        for track in emission.tracks:
            track.announced = True
            track.event_id = event_id
            if track.pending:
                track.last_transition = track.pending[-1]
            track.pending = []
        for departure in emission.departures:
            if departure in self.pending_departures:
                self.pending_departures.remove(departure)
        self.dirty = True

    def summary(self) -> list[dict]:
        return [track.describe() for track in self.tracks]


# --- frames and crops ---------------------------------------------------------


def _open(image: bytes):
    from ..ai.imaging import open_frame

    frame = open_frame(image)
    if frame is None:
        return None
    try:
        return frame.convert("RGB")
    except Exception:  # noqa: BLE001 - a corrupt frame is just "no frame"
        return None


def crop_zone(image: bytes | None, box: BoundingBox, context: float = 0.5, max_side: int = 640) -> bytes | None:
    """JPEG of ``box`` plus ``context`` of its size on every side."""
    if image is None:
        return None
    frame = _open(image)
    if frame is None:
        return None
    width, height = frame.size
    pad_x = (box.x2 - box.x1) * context
    pad_y = (box.y2 - box.y1) * context
    left = max(0, int((box.x1 - pad_x) * width))
    top = max(0, int((box.y1 - pad_y) * height))
    right = min(width, int(round((box.x2 + pad_x) * width)))
    bottom = min(height, int(round((box.y2 + pad_y) * height)))
    if right - left < 2 or bottom - top < 2:
        return None
    crop = frame.crop((left, top, right, bottom))
    crop.thumbnail((max_side, max_side))
    out = io.BytesIO()
    crop.save(out, format="JPEG", quality=85)
    return out.getvalue()


def zone_signature(image: bytes, box: BoundingBox) -> list[float] | None:
    """A 32x32 contrast-normalised grayscale fingerprint of a zone.

    Normalising removes overall brightness and contrast, so a cloud or a
    gradual change of daylight is not a change in *what is there*. Returns
    ``None`` for an unreadable or featureless (blank, black) crop.
    """
    frame = _open(image)
    if frame is None:
        return None
    width, height = frame.size
    crop = frame.crop(
        (int(box.x1 * width), int(box.y1 * height), max(int(box.x1 * width) + 1, int(box.x2 * width)),
         max(int(box.y1 * height) + 1, int(box.y2 * height)))
    )
    gray = crop.convert("L").resize((32, 32))
    values = [float(v) for v in gray.getdata()]
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    std = variance ** 0.5
    if std < 2.0:
        return None
    return [(v - mean) / std for v in values]


def signature_distance(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


# --- temporal events -----------------------------------------------------------


@dataclass
class TemporalEmission:
    """A mailbox or bin event, fully decided."""

    kind: str  # "mailbox" or "bins"
    zone: str
    type: str
    description: str
    tags: list[str]
    temporal: dict
    evidence: dict[str, bytes]
    trigger_frame: bytes
    detections: list[Detection]


def _overlaps(detection: Detection, zone_box: BoundingBox, share_of_zone: float) -> bool:
    return zone_box.area > 0 and intersection_area(detection.bbox, zone_box) / zone_box.area >= share_of_zone


def _expanded(box: BoundingBox, margin: float) -> BoundingBox:
    return BoundingBox(
        max(0.0, box.x1 - margin), max(0.0, box.y1 - margin), min(1.0, box.x2 + margin), min(1.0, box.y2 + margin)
    )


@dataclass
class _MailboxEpisode:
    started: float
    last_seen: float
    before: bytes | None
    during: list[bytes] = field(default_factory=list)
    person_frames: int = 0
    package_during: bool = False
    package_before: bool = False


class MailboxWatcher:
    def __init__(self, zone: Zone) -> None:
        self.zone = zone
        self.clear_frame: bytes | None = None
        self.clear_at: float = 0.0
        self.clear_package = False
        self.episode: _MailboxEpisode | None = None
        self.cooldown_until = 0.0
        self.discarded_walkbys = 0

    def to_state(self) -> dict:
        return {"cooldown_until": self.cooldown_until}

    def load_state(self, raw: dict) -> None:
        self.cooldown_until = float(raw.get("cooldown_until") or 0.0)

    def observe(self, image: bytes, detections: list[Detection], now: float) -> dict | None:
        """Track one frame; returns a finished episode worth judging."""
        near = _expanded(self.zone.bbox, 0.05)
        present = any(
            d.label == "person" and _overlaps(d, near, settings.mailbox_zone_cover) for d in detections
        )
        package = any(d.label == "package" and intersection_area(d.bbox, near) > 0 for d in detections)
        episode = self.episode
        if present:
            if episode is None:
                fresh = now - self.clear_at <= settings.mailbox_before_max_age_seconds
                episode = self.episode = _MailboxEpisode(
                    started=now,
                    last_seen=now,
                    before=self.clear_frame if fresh else None,
                    package_before=self.clear_package if fresh else False,
                )
            episode.last_seen = now
            episode.person_frames += 1
            episode.package_during = episode.package_during or package
            if len(episode.during) < 3:
                episode.during.append(image)
            else:
                episode.during[-1] = image
            if now - episode.started >= settings.mailbox_max_episode_seconds:
                return self._finish(None, False, now)
            return None
        if episode is not None:
            return self._finish(image, package, now)
        self.clear_frame, self.clear_at, self.clear_package = image, now, package
        return None

    def _finish(self, after: bytes | None, package_after: bool, now: float) -> dict | None:
        episode = self.episode
        self.episode = None
        if after is not None:
            self.clear_frame, self.clear_at, self.clear_package = after, now, package_after
        if episode is None:
            return None
        if episode.person_frames < settings.mailbox_min_frames and not episode.package_during:
            # Someone passed the mailbox in a single frame: walking by.
            self.discarded_walkbys += 1
            return None
        if now < self.cooldown_until:
            return None
        return {
            "before": episode.before,
            "during": list(episode.during),
            "after": after,
            "person_frames": episode.person_frames,
            "package_before": episode.package_before,
            "package_during": episode.package_during,
            "package_after": package_after,
            "started": episode.started,
            "ended": now,
        }


@dataclass
class BinWatcher:
    zone: Zone
    state: str = "unknown"  # unknown | present | absent
    baseline: list[float] | None = None
    stable_frame: bytes | None = None
    settle: int = 0
    collection_at: float | None = None
    collection_check: bool = False
    emptied_reported: bool = False
    cooldown_until: float = 0.0
    last_transition: str | None = None
    bins_count: int | None = None
    assessed_at: float | None = None

    def to_state(self) -> dict:
        return {
            "state": self.state,
            "baseline": [round(v, 3) for v in self.baseline] if self.baseline else None,
            "collection_at": self.collection_at,
            "emptied_reported": self.emptied_reported,
            "cooldown_until": self.cooldown_until,
            "last_transition": self.last_transition,
            "bins_count": self.bins_count,
            "assessed_at": self.assessed_at,
        }

    def load_state(self, raw: dict) -> None:
        self.state = str(raw.get("state") or "unknown")
        baseline = raw.get("baseline")
        self.baseline = [float(v) for v in baseline] if isinstance(baseline, list) and len(baseline) == 1024 else None
        self.collection_at = float(raw["collection_at"]) if raw.get("collection_at") is not None else None
        self.emptied_reported = bool(raw.get("emptied_reported"))
        self.cooldown_until = float(raw.get("cooldown_until") or 0.0)
        self.last_transition = raw.get("last_transition")
        self.bins_count = int(raw["bins_count"]) if raw.get("bins_count") is not None else None
        self.assessed_at = float(raw["assessed_at"]) if raw.get("assessed_at") is not None else None

    def observe(self, image: bytes, detections: list[Detection], now: float) -> dict | None:
        """Track one frame; returns a candidate change worth assessing."""
        zone_box = self.zone.bbox
        occluders = [
            d for d in detections
            if (d.label == "person" or d.label in VEHICLE_CLASSES) and _overlaps(d, zone_box, 0.1)
        ]
        if any(d.label == "truck" for d in occluders):
            # A lorry at the bins is what collection looks like from here.
            self.collection_at = now
            self.collection_check = True
        if occluders:
            self.settle = 0
            return None
        signature = zone_signature(image, zone_box)
        if signature is None:
            return None  # dark, blank or unreadable: no evidence either way
        if self.baseline is None:
            self.baseline = signature
            self.stable_frame = image
            if self.state == "unknown":
                return {"reason": "initial", "before": None, "after": image}
            return None
        if self.collection_check:
            self.collection_check = False
            before, self.stable_frame = self.stable_frame, image
            self.baseline = signature
            self.settle = 0
            return {"reason": "collection", "before": before, "after": image}
        distance = signature_distance(signature, self.baseline)
        if distance > settings.bin_change_threshold:
            self.settle += 1
            if self.settle >= settings.bin_settle_frames:
                before, self.stable_frame = self.stable_frame, image
                self.baseline = signature
                self.settle = 0
                return {"reason": "change", "before": before, "after": image, "distance": round(distance, 3)}
            return None
        self.settle = 0
        alpha = settings.bin_baseline_alpha
        self.baseline = [(1 - alpha) * b + alpha * s for b, s in zip(self.baseline, signature)]
        self.stable_frame = image
        return None

    def decide(self, candidate: dict, verdict: BinVerdict | None, now: float) -> tuple[str | None, dict]:
        """Apply an assessment; returns ``(transition or None, details)``."""
        details: dict = {"reason": candidate.get("reason"), "state_before": self.state}
        if verdict is None or verdict.bins_after is None:
            details["outcome"] = "unassessed"
            return None, details
        self.assessed_at = now
        after_state = "present" if verdict.bins_after > 0 else "absent"
        previous = self.state
        self.state = after_state
        self.bins_count = verdict.bins_after
        details.update({"state_after": after_state, "verdict": verdict.as_dict()})
        collection = (
            self.collection_at is not None
            and now - float(self.collection_at) <= settings.bin_collection_window_seconds
        )
        confident_emptied = verdict.emptied is True and verdict.confidence >= settings.mailbox_min_confidence
        details["collection_evidence_at"] = _iso(self.collection_at) if collection else None
        if previous == "unknown":
            details["outcome"] = "initial_state"
            return None, details
        if previous == "absent" and after_state == "present":
            self.emptied_reported = False
            return "bin_placed_out", details
        if previous == "present" and after_state == "absent":
            emptied = not self.emptied_reported and (
                collection or confident_emptied or settings.bin_emptied_on_disappearance
            )
            self.emptied_reported = False
            if emptied:
                details["basis"] = (
                    "collection_vehicle" if collection else "vision" if confident_emptied else "disappearance_rule"
                )
                return "bin_emptied", details
            details["outcome"] = "taken_in_without_collection_evidence"
            return None, details
        if previous == "present" and after_state == "present" and not self.emptied_reported:
            if collection and verdict.emptied is not False:
                self.emptied_reported = True
                details["basis"] = "collection_vehicle"
                return "bin_emptied", details
        details["outcome"] = "no_transition"
        return None, details


# --- per camera -----------------------------------------------------------------


class CameraScene:
    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        self.vehicles = VehicleTracker()
        self.mailboxes: dict[str, MailboxWatcher] = {}
        self.bins: dict[str, BinWatcher] = {}
        self._saved_bins: dict[str, dict] = {}
        self._saved_mailboxes: dict[str, dict] = {}
        self.last_saved = 0.0
        self.dirty = False
        self.last_frame_at: float | None = None
        self.foundry_calls = 0

    # -- persistence

    def to_state(self, now: float) -> dict:
        bins = {**self._saved_bins, **{name: w.to_state() for name, w in self.bins.items()}}
        mailboxes = {**self._saved_mailboxes, **{name: w.to_state() for name, w in self.mailboxes.items()}}
        return {
            "version": STATE_VERSION,
            "saved_at": now,
            "vehicles": self.vehicles.to_state(),
            "bins": bins,
            "mailboxes": mailboxes,
        }

    def load_state(self, raw: dict, now: float) -> bool:
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            return False
        if now - float(raw.get("saved_at") or 0.0) > settings.scene_state_max_age_seconds:
            return False
        self.vehicles.load_state(raw.get("vehicles") or {})
        bins = raw.get("bins")
        mailboxes = raw.get("mailboxes")
        self._saved_bins = {k: v for k, v in bins.items() if isinstance(v, dict)} if isinstance(bins, dict) else {}
        self._saved_mailboxes = (
            {k: v for k, v in mailboxes.items() if isinstance(v, dict)} if isinstance(mailboxes, dict) else {}
        )
        return True

    def _sync_zones(self, zones: list[Zone]) -> None:
        mailbox_zones = {z.name: z for z in zones if z.kind == "mailbox"}
        bin_zones = {z.name: z for z in zones if z.kind == "bins"}
        for name in list(self.mailboxes):
            if name not in mailbox_zones or self.mailboxes[name].zone != mailbox_zones[name]:
                del self.mailboxes[name]
        for name, zone in mailbox_zones.items():
            if name not in self.mailboxes:
                watcher = MailboxWatcher(zone)
                try:
                    watcher.load_state(self._saved_mailboxes.get(name) or {})
                except Exception as exc:  # noqa: BLE001 - a fresh watcher is always safe
                    logger.warning("mailbox state for %s/%s discarded: %s", self.camera_id, name, exc)
                    watcher = MailboxWatcher(zone)
                self.mailboxes[name] = watcher
        for name in list(self.bins):
            if name not in bin_zones or self.bins[name].zone != bin_zones[name]:
                del self.bins[name]
                self._saved_bins.pop(name, None)
        for name, zone in bin_zones.items():
            if name not in self.bins:
                watcher = BinWatcher(zone)
                saved = self._saved_bins.get(name)
                if saved:
                    try:
                        watcher.load_state(saved)
                    except Exception as exc:  # noqa: BLE001 - a fresh watcher is always safe
                        logger.warning("bin state for %s/%s discarded: %s", self.camera_id, name, exc)
                        watcher = BinWatcher(zone)
                self.bins[name] = watcher

    # -- per frame

    def observe_vehicles(
        self, detections: list[Detection], now: float
    ) -> tuple[dict[int, VehicleTrack], list[ParkedUpdate], list[Detection]]:
        vehicles = [d for d in detections if d.label in VEHICLE_CLASSES]
        people = [d for d in detections if d.label == "person"]
        interactions, parked = self.vehicles.observe(vehicles, people, now)
        return interactions, parked, people

    async def observe_zones(
        self, image: bytes, detections: list[Detection], zones: list[Zone], now: float, camera_name: str
    ) -> list[TemporalEmission]:
        self._sync_zones(zones)
        emissions: list[TemporalEmission] = []
        for watcher in self.mailboxes.values():
            episode = watcher.observe(image, detections, now)
            if episode is None:
                continue
            emission = await self._judge_mailbox(watcher, episode, detections, now, camera_name)
            if emission is not None:
                emissions.append(emission)
        for watcher in self.bins.values():
            candidate = watcher.observe(image, detections, now)
            if candidate is None:
                continue
            emission = await self._judge_bins(watcher, candidate, detections, now, camera_name)
            self.dirty = True
            if emission is not None:
                emissions.append(emission)
        return emissions

    async def _judge_mailbox(
        self, watcher: MailboxWatcher, episode: dict, detections: list[Detection], now: float, camera_name: str
    ) -> TemporalEmission | None:
        zone = watcher.zone
        before = crop_zone(episode["before"], zone.bbox)
        during_frames = episode["during"]
        during = crop_zone(during_frames[len(during_frames) // 2], zone.bbox) if during_frames else None
        after = crop_zone(episode["after"], zone.bbox)
        unknowns: list[str] = []
        if before is None:
            unknowns.append("no clear frame of the mailbox just before")
        if after is None:
            unknowns.append("the person was still at the mailbox when the episode was closed")

        verdict: MailboxVerdict | None = None
        basis: str
        if episode["package_after"] and not episode["package_before"]:
            # A parcel that is in the zone after the visit and was not
            # before it: seen locally, no model needed.
            verdict = MailboxVerdict(deposited="yes", item="parcel", confidence=0.8)
            basis = "local_parcel_left"
        else:
            verifier = get_temporal_verifier()
            if verifier is not None and during is not None:
                self.foundry_calls += 1
                try:
                    verdict = await verifier.verify_mailbox(before, during, after)
                except Exception as exc:  # noqa: BLE001 - SPEC 43: degrade this feature only
                    logger.warning("mailbox verification failed for %s: %s", self.camera_id, exc)
                    verdict = None
            basis = "vision" if verdict is not None else "unverified"
            if verdict is None:
                unknowns.append("vision check unavailable; delivery not confirmed")

        if verdict is not None and verdict.deposited == "no":
            return None  # someone at the mailbox, nothing put in: not an event
        confirmed = (
            verdict is not None
            and verdict.deposited == "yes"
            and verdict.confidence >= settings.mailbox_min_confidence
        )
        watcher.cooldown_until = now + settings.mailbox_cooldown_seconds
        self.dirty = True

        tags = ["mailbox"]
        if confirmed:
            tags.append("mailbox_delivery")
            if verdict.item in ("parcel", "letter"):
                tags.append(verdict.item)
            item = {"parcel": "A parcel", "letter": "Mail"}.get(verdict.item, "Mail or a parcel")
            description = f"{item} was delivered to the {zone.name} on {camera_name}."
            event_type = "package"
        else:
            tags.append("mailbox_activity")
            if verdict is not None and verdict.deposited == "yes":
                unknowns.append("vision check was not confident enough")
            description = (
                f"Someone was at the {zone.name} on {camera_name}; whether anything was "
                "delivered could not be confirmed."
            )
            event_type = "motion"
        evidence = {role: crop for role, crop in (("before", before), ("during", during), ("after", after)) if crop}
        temporal = {
            "kind": "mailbox",
            "zone": zone.name,
            "verdict": (verdict.as_dict() if verdict else {"deposited": "unknown", "item": "unknown", "confidence": 0.0}),
            "confirmed": confirmed,
            "basis": basis,
            "person_frames": episode["person_frames"],
            "started_at": _iso(episode["started"]),
            "ended_at": _iso(episode["ended"]),
            "unknowns": unknowns,
            "photo": "scene",
        }
        trigger = during_frames[len(during_frames) // 2] if during_frames else episode["after"]
        return TemporalEmission(
            kind="mailbox",
            zone=zone.name,
            type=event_type,
            description=description,
            tags=tags,
            temporal=temporal,
            evidence=evidence,
            trigger_frame=trigger,
            detections=detections,
        )

    async def _judge_bins(
        self, watcher: BinWatcher, candidate: dict, detections: list[Detection], now: float, camera_name: str
    ) -> TemporalEmission | None:
        zone = watcher.zone
        before = crop_zone(candidate.get("before"), zone.bbox)
        after = crop_zone(candidate["after"], zone.bbox)
        verifier = get_temporal_verifier()
        verdict: BinVerdict | None = None
        if verifier is not None and after is not None:
            self.foundry_calls += 1
            try:
                verdict = await verifier.assess_bins(before, after)
            except Exception as exc:  # noqa: BLE001 - SPEC 43
                logger.warning("bin assessment failed for %s: %s", self.camera_id, exc)
        transition, details = watcher.decide(candidate, verdict, now)
        # The cooldown stops the same transition flapping; putting bins out and
        # then having them emptied a few minutes later are both real.
        if transition is None or (transition == watcher.last_transition and now < watcher.cooldown_until):
            if transition is not None:
                details["outcome"] = "cooldown"
            logger.info("bins %s/%s: %s", self.camera_id, zone.name, details)
            return None
        watcher.cooldown_until = now + settings.bin_cooldown_seconds
        watcher.last_transition = transition
        if transition == "bin_placed_out":
            description = f"Bins were put out at the {zone.name} on {camera_name}."
        else:
            description = f"Bins were emptied at the {zone.name} on {camera_name}."
        evidence = {role: crop for role, crop in (("before", before), ("after", after)) if crop}
        temporal = {
            "kind": "bins",
            "zone": zone.name,
            "transition": transition,
            **details,
            "unknowns": [] if before is not None else ["no stored frame from before the change"],
            "photo": "scene",
        }
        return TemporalEmission(
            kind="bins",
            zone=zone.name,
            type="motion",
            description=description,
            tags=["bins", transition],
            temporal=temporal,
            evidence=evidence,
            trigger_frame=candidate["after"],
            detections=detections,
        )

    def summary(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "last_frame_at": _iso(self.last_frame_at),
            "vehicles": self.vehicles.summary(),
            "recent_departures": [
                {"track_id": d.get("track_id"), "departed_at": _iso(d.get("at")), "box": d.get("box")}
                for d in self.vehicles.departed
            ],
            "mailboxes": [
                {
                    "zone": name,
                    "in_episode": watcher.episode is not None,
                    "cooldown_until": _iso(watcher.cooldown_until) if watcher.cooldown_until else None,
                    "walkbys_discarded": watcher.discarded_walkbys,
                }
                for name, watcher in self.mailboxes.items()
            ],
            "bins": [
                {
                    "zone": name,
                    "state": watcher.state,
                    "bins_count": watcher.bins_count,
                    "assessed_at": _iso(watcher.assessed_at),
                    "collection_evidence_at": _iso(watcher.collection_at),
                }
                for name, watcher in self.bins.items()
            ],
            "vision_checks": self.foundry_calls,
        }


class SceneEngine:
    """All cameras' temporal state, loaded lazily and saved on change."""

    def __init__(self) -> None:
        self._scenes: dict[str, CameraScene] = {}

    def peek(self, camera_id: str) -> CameraScene | None:
        return self._scenes.get(camera_id)

    async def scene(self, camera_id: str, session_factory, now: float | None = None) -> CameraScene:
        scene = self._scenes.get(camera_id)
        if scene is not None:
            return scene
        scene = CameraScene(camera_id)
        now = time.time() if now is None else now
        try:
            from ..models.db import SceneState

            async with session_factory() as session:
                row = await session.get(SceneState, camera_id)
                if row is not None and scene.load_state(row.state or {}, now):
                    logger.info(
                        "scene state restored for %s: %d vehicle track(s)", camera_id, len(scene.vehicles.tracks)
                    )
        except Exception as exc:  # noqa: BLE001 - a fresh state is always safe
            logger.warning("scene state load failed for %s: %s", camera_id, exc)
            scene = CameraScene(camera_id)  # never run on a half-loaded state
        scene.last_saved = now
        self._scenes[camera_id] = scene
        return scene

    async def save(self, scene: CameraScene, session_factory, now: float | None = None, force: bool = False) -> None:
        now = time.time() if now is None else now
        dirty = scene.dirty or scene.vehicles.dirty
        if not force and not dirty and now - scene.last_saved < _SAVE_EVERY_SECONDS:
            return
        try:
            from ..models.db import SceneState

            async with session_factory() as session:
                row = await session.get(SceneState, scene.camera_id)
                state = scene.to_state(now)
                stamp = datetime.fromtimestamp(now, tz=timezone.utc)
                if row is None:
                    session.add(SceneState(camera_id=scene.camera_id, state=state, updated_at=stamp))
                else:
                    row.state = state
                    row.updated_at = stamp
                await session.commit()
            scene.dirty = False
            scene.vehicles.dirty = False
            scene.last_saved = now
        except Exception as exc:  # noqa: BLE001 - state is best effort
            logger.warning("scene state save failed for %s: %s", scene.camera_id, exc)

    def discard(self, camera_id: str) -> None:
        """Forget one camera's in-memory state (it is rebuilt fresh)."""
        self._scenes.pop(camera_id, None)

    def reset(self) -> None:
        self._scenes.clear()


scene_engine = SceneEngine()
