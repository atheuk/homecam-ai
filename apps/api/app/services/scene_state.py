"""Persistent, per-camera scene state: vehicles, mailbox deliveries, bins.

The ingestion loop used to decide "should this frame become an event?"
from a cooldown alone, so a car parked in view all day re-emitted a vehicle
event (and its Foundry enrichment) every few minutes, and nothing could say
*what changed*. This module keeps state across frames - and across restarts,
via the ``vehicle_tracks`` / ``scene_states`` tables - and turns only real,
evidenced changes into :class:`SceneTransition` objects the ingestion loop
emits as ordinary events. Event types stay within the SPEC 9 enum
(``vehicle`` / ``package``); what happened is carried by tags and metadata.

Vehicles
    Each vehicle is a track. A box overlapping the track (IoU/containment)
    continues it; a box in the same place (IoU with the anchor box >=
    ``vehicle_stable_iou``) counts as another observation. The track is
    reported (one event: arrived / first seen / returned) once confirmed,
    and after ``vehicle_stable_observations`` observations it is parked:
    nothing more is emitted while it stays. It emits again only when it
    moves materially, departs (unseen for ``vehicle_absence_seconds`` of
    camera time that actually delivered frames), or a person interacts with
    it. A departed vehicle seen again in the same place with a compatible
    colour signature is "returned".

Mailbox (zones of kind ``mailbox``)
    A person overlapping the mailbox for >= ``mailbox_min_observations``
    samples is a visit (one sample is a walk-by). When they leave, the
    before / during / after evidence decides: a package the local detector
    sees in the zone after but not before is a deposit; a package seen only
    while the person was there was carried past. Otherwise the Foundry
    vision deployment is asked a closed question about the before/during/
    after crops. Only "yes" emits, once per ``mailbox_dedupe_seconds``.

Bins (zones of kind ``bin``)
    The zone is compared with a brightness-normalized baseline. A change
    confirmed over several unoccluded checks is a candidate; presence
    before/now comes from local detections or a closed Foundry question.
    absent -> present is ``bin_placed_out``. ``bin_emptied`` needs present
    before *and* after plus interaction evidence (collection vehicle, or a
    person and the bin moved/tipped). Disappearance alone is never emptied
    unless ``bin_removal_counts_as_emptied`` is set, and never across a
    camera outage.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai import scene_signals as signals
from ..ai.detector import VEHICLE_CLASSES, BoundingBox, Detection, iou
from ..ai.scene_verifier import get_scene_verifier
from ..ai.zones import Zone, intersection_area, overlap_ratio, primary_zone
from ..config import settings
from ..models.db import Event, SceneState, VehicleTrack
from . import zones as zone_service

logger = logging.getLogger(__name__)

TENTATIVE, TRACKING, STABLE, DEPARTED = "tentative", "tracking", "stable", "departed"

# Share of a person box that must overlap a vehicle to count as being at it.
_PERSON_AT_VEHICLE = 0.25
# Share of an unmatched vehicle box inside an existing track that makes it a
# partial duplicate box of the same car rather than a new vehicle.
_DUPLICATE_CONTAINMENT = 0.6
# Share of the bin zone a person/vehicle must cover to occlude it.
_OCCLUDES_ZONE = 0.15
_ZONE_CACHE_SECONDS = 30.0
_MAILBOX_MAX_VISIT_SECONDS = 300.0


@dataclass
class SceneTransition:
    camera_id: str
    kind: str
    transition: str
    event_type: str
    description: str
    tags: list[str]
    metadata: dict
    zone: str | None = None
    priority: str = "normal"
    frames: list[bytes] | None = None
    track_id: str | None = None


@dataclass
class Track:
    id: str
    camera_id: str
    label: str
    zone: str | None
    state: str
    box: BoundingBox
    observation_count: int
    first_seen_at: float
    last_seen_at: float
    anchored_at: float
    stationary_since: float | None = None
    departed_at: float | None = None
    confidence: float = 0.0
    appearance: list[float] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    last_event_id: str | None = None

    @property
    def reported(self) -> bool:
        return bool(self.data.get("reported"))

    def summary(self, transition: str | None = None) -> dict:
        return {
            "track_id": self.id,
            "label": self.label,
            "state": self.state,
            "observation_count": self.observation_count,
            "first_seen": _iso(self.first_seen_at),
            "stationary_since": _iso(self.stationary_since),
            "transition": transition,
            "box": self.box.as_dict(),
        }


@dataclass
class ZoneRecord:
    id: str
    camera_id: str
    kind: str
    zone_id: str
    state: str
    data: dict


@dataclass
class CameraScene:
    camera_id: str
    tracks: dict[str, Track] = field(default_factory=dict)
    zones: dict[str, ZoneRecord] = field(default_factory=dict)
    deleted_tracks: set[str] = field(default_factory=set)
    dirty_tracks: set[str] = field(default_factory=set)
    dirty_zones: set[str] = field(default_factory=set)
    # Frame continuity, in memory: a restart is itself an outage.
    observed_since: float | None = None
    last_frame_at: float | None = None
    # Evidence crops per zone (memory only; bytes are not worth persisting).
    crops: dict[str, dict] = field(default_factory=dict)
    zone_cache: list[tuple[str, Zone]] = field(default_factory=list)
    zone_cache_at: float = 0.0
    # Foundry checks in flight per zone: (task, context). They run off the
    # frame path; a later frame picks up the answer. Memory only.
    pending: dict[str, tuple[asyncio.Task, dict]] = field(default_factory=dict)


_scenes: dict[str, CameraScene] = {}


def _iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def reset_memory() -> None:
    """Drop in-memory state (tests simulate a restart with this)."""
    _scenes.clear()


def _overlap(track_box: BoundingBox, box: BoundingBox) -> float:
    inter = intersection_area(track_box, box)
    if inter <= 0:
        return 0.0
    union = track_box.area + box.area - inter
    return max(inter / union if union > 0 else 0.0, inter / box.area if box.area > 0 else 0.0)


def _expand(box: BoundingBox, margin: float) -> BoundingBox:
    return BoundingBox(
        max(0.0, box.x1 - margin),
        max(0.0, box.y1 - margin),
        min(1.0, box.x2 + margin),
        min(1.0, box.y2 + margin),
    )


def _zone_cover(box: BoundingBox, zone: BoundingBox) -> float:
    """Share of the zone covered by ``box``."""
    return intersection_area(box, zone) / zone.area if zone.area > 0 else 0.0


# --- persistence --------------------------------------------------------------


def _track_from_row(row: VehicleTrack) -> Track:
    return Track(
        id=row.id,
        camera_id=row.camera_id,
        label=row.label,
        zone=row.zone,
        state=row.state,
        box=BoundingBox(row.x1, row.y1, row.x2, row.y2),
        observation_count=row.observation_count,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        anchored_at=row.anchored_at,
        stationary_since=row.stationary_since,
        departed_at=row.departed_at,
        confidence=row.confidence,
        appearance=list(row.appearance or []),
        data=dict(row.data or {}),
        last_event_id=row.last_event_id,
    )


async def _load(session: AsyncSession, camera_id: str) -> CameraScene:
    scene = _scenes.get(camera_id)
    if scene is not None:
        return scene
    scene = CameraScene(camera_id=camera_id)
    rows = (
        await session.execute(select(VehicleTrack).where(VehicleTrack.camera_id == camera_id))
    ).scalars()
    for row in rows:
        try:
            scene.tracks[row.id] = _track_from_row(row)
        except ValueError:
            scene.deleted_tracks.add(row.id)
    states = (
        await session.execute(select(SceneState).where(SceneState.camera_id == camera_id))
    ).scalars()
    for row in states:
        if row.zone_id:
            scene.zones[row.zone_id] = ZoneRecord(
                id=row.id,
                camera_id=row.camera_id,
                kind=row.kind,
                zone_id=row.zone_id,
                state=row.state,
                data=dict(row.data or {}),
            )
    _scenes[camera_id] = scene
    return scene


async def _save(session: AsyncSession, scene: CameraScene) -> None:
    now = datetime.now(timezone.utc)
    for track_id in scene.deleted_tracks:
        row = await session.get(VehicleTrack, track_id)
        if row is not None:
            await session.delete(row)
    scene.deleted_tracks.clear()
    for track_id in scene.dirty_tracks:
        track = scene.tracks.get(track_id)
        if track is None:
            continue
        row = await session.get(VehicleTrack, track_id)
        if row is None:
            row = VehicleTrack(id=track.id, camera_id=track.camera_id)
            session.add(row)
        row.label = track.label
        row.zone = track.zone
        row.state = track.state
        row.x1, row.y1, row.x2, row.y2 = track.box.x1, track.box.y1, track.box.x2, track.box.y2
        row.observation_count = track.observation_count
        row.first_seen_at = track.first_seen_at
        row.last_seen_at = track.last_seen_at
        row.anchored_at = track.anchored_at
        row.stationary_since = track.stationary_since
        row.departed_at = track.departed_at
        row.confidence = track.confidence
        row.appearance = list(track.appearance)
        row.data = dict(track.data)
        row.last_event_id = track.last_event_id
        row.updated_at = now
    scene.dirty_tracks.clear()
    for zone_id in scene.dirty_zones:
        record = scene.zones.get(zone_id)
        if record is None:
            continue
        row = await session.get(SceneState, record.id)
        if row is None:
            row = SceneState(id=record.id, camera_id=record.camera_id, kind=record.kind, zone_id=zone_id)
            session.add(row)
        row.state = record.state
        row.data = dict(record.data)
        row.updated_at = now
    scene.dirty_zones.clear()
    await session.commit()


async def _zones(session: AsyncSession, scene: CameraScene, now: float) -> list[tuple[str, Zone]]:
    if scene.zone_cache_at and now - scene.zone_cache_at < _ZONE_CACHE_SECONDS:
        return scene.zone_cache
    rows = await zone_service.list_zones(session, scene.camera_id)
    zones: list[tuple[str, Zone]] = []
    for row in rows:
        try:
            zones.append((row.id, Zone.from_row(row)))
        except ValueError:
            continue
    scene.zone_cache = zones
    scene.zone_cache_at = now
    return zones


def invalidate_zone_cache(camera_id: str | None = None) -> None:
    for key, scene in _scenes.items():
        if camera_id is None or key == camera_id:
            scene.zone_cache_at = 0.0


# --- public entry points ------------------------------------------------------


class _LazyImage:
    def __init__(self, frame: bytes | None) -> None:
        self._frame = frame
        self._image = None
        self._done = False

    def get(self):
        if not self._done:
            self._done = True
            self._image = signals.decode(self._frame) if self._frame else None
        return self._image


async def process_frame(
    session: AsyncSession,
    camera_id: str,
    camera_name: str,
    frame: bytes | None,
    detections: list[Detection],
    now: float | None = None,
) -> list[SceneTransition]:
    """Advance every state machine for one camera by one real frame."""
    now = time.time() if now is None else now
    try:
        return await _process(session, camera_id, camera_name, frame, detections, now)
    except Exception:  # noqa: BLE001 - corrupt state must not fail every frame
        logger.exception("scene state for %s is unusable; resetting it", camera_id)
        await session.rollback()
        await reset_camera(session, camera_id)
        return []


async def reset_camera(session: AsyncSession, camera_id: str) -> None:
    """Forget a camera's scene state (memory and DB)."""
    _scenes.pop(camera_id, None)
    await session.execute(delete(VehicleTrack).where(VehicleTrack.camera_id == camera_id))
    await session.execute(delete(SceneState).where(SceneState.camera_id == camera_id))
    await session.commit()


async def _verify(call, camera_id: str, what: str) -> dict | None:
    try:
        return await asyncio.wait_for(call, timeout=settings.foundry_timeout_seconds + 5.0)
    except Exception as exc:  # noqa: BLE001 - verification must not break ingestion
        logger.warning("%s verification failed for %s: %s", what, camera_id, exc)
        return None


def _take_answer(scene: CameraScene, zone_id: str) -> tuple[bool, dict | None, dict | None]:
    """``(finished, answer, context)`` for a zone's in-flight check."""
    pending = scene.pending.get(zone_id)
    if pending is None or not pending[0].done():
        return False, None, None
    task, context = scene.pending.pop(zone_id)
    answer = None if task.cancelled() or task.exception() else task.result()
    return True, answer, context


async def _process(
    session: AsyncSession,
    camera_id: str,
    camera_name: str,
    frame: bytes | None,
    detections: list[Detection],
    now: float,
) -> list[SceneTransition]:
    scene = await _load(session, camera_id)
    outage = scene.last_frame_at is None or now - scene.last_frame_at > settings.scene_outage_seconds
    if outage:
        scene.observed_since = now
    scene.last_frame_at = now

    image = _LazyImage(frame)
    zones = await _zones(session, scene, now)
    transitions: list[SceneTransition] = []
    if settings.vehicle_tracking_enabled:
        transitions += await _vehicle_step(
            session, scene, camera_name, detections, [z for _, z in zones], image, now
        )
    for zone_id, zone in zones:
        kind = zone.kind.casefold()
        if kind == "mailbox" and settings.mailbox_delivery_enabled:
            result = await _mailbox_step(scene, zone_id, zone, camera_name, frame, detections, image, now, outage)
        elif kind == "bin" and settings.bin_detection_enabled:
            result = await _bin_step(scene, zone_id, zone, camera_name, frame, detections, image, now, outage)
        else:
            continue
        if result is not None:
            transitions.append(result)
    await _save(session, scene)
    return transitions


async def note_event(session: AsyncSession, transition: SceneTransition, event_id: str) -> None:
    """Remember which event reported a track (so parking can annotate it)."""
    if transition.track_id is None:
        return
    scene = _scenes.get(transition.camera_id)
    track = scene.tracks.get(transition.track_id) if scene else None
    if track is None:
        return
    track.last_event_id = event_id
    scene.dirty_tracks.add(track.id)
    await _save(session, scene)


async def snapshot(session: AsyncSession, camera_id: str) -> dict:
    """Current state for the admin/verification endpoint."""
    scene = await _load(session, camera_id)
    return {
        "camera_id": camera_id,
        "observed_since": _iso(scene.observed_since),
        "last_frame_at": _iso(scene.last_frame_at),
        "vehicles": [
            {**track.summary(), "last_seen": _iso(track.last_seen_at), "departed_at": _iso(track.departed_at),
             "reported": track.reported, "last_event_id": track.last_event_id}
            for track in sorted(scene.tracks.values(), key=lambda t: t.first_seen_at)
        ],
        "zones": [
            {"zone_id": record.zone_id, "kind": record.kind, "state": record.state,
             "data": {k: v for k, v in record.data.items() if k not in {"baseline", "pending_sig"}}}
            for record in scene.zones.values()
        ],
    }


# --- vehicles -----------------------------------------------------------------


def _vehicle_transition(
    track: Track, transition: str, tags: list[str], description: str, zone: str | None,
    extra: dict | None = None,
) -> SceneTransition:
    scene_meta = {
        "kind": "vehicle",
        "transition": transition,
        "label": track.label,
        "zone": zone,
        "confidence": round(track.confidence, 3),
        "track_id": track.id,
    }
    if extra:
        scene_meta.update(extra)
    return SceneTransition(
        camera_id=track.camera_id,
        kind="vehicle",
        transition=transition,
        event_type="vehicle",
        description=description,
        tags=[track.label, *tags],
        metadata={"vehicle_track": track.summary(transition), "scene": scene_meta},
        zone=zone,
        track_id=track.id,
    )


def _where(zone: str | None, camera_name: str) -> str:
    return f"in the {zone} at {camera_name}" if zone else f"at {camera_name}"


async def _vehicle_step(
    session: AsyncSession,
    scene: CameraScene,
    camera_name: str,
    detections: list[Detection],
    zones: list[Zone],
    image: _LazyImage,
    now: float,
) -> list[SceneTransition]:
    s = settings
    vehicles = [d for d in detections if d.label in VEHICLE_CLASSES]
    persons = [d for d in detections if d.label == "person"]
    active = [t for t in scene.tracks.values() if t.state != DEPARTED]
    if not vehicles and not active:
        _expire_departed(scene, now)
        return []
    out: list[SceneTransition] = []

    pairs = sorted(
        (
            (iou(track.box, det.bbox), _overlap(track.box, det.bbox), ti, di)
            for ti, track in enumerate(active)
            for di, det in enumerate(vehicles)
        ),
        reverse=True,
    )
    used_tracks: set[int] = set()
    used_dets: set[int] = set()
    for _iou, score, ti, di in pairs:
        if score < s.vehicle_match_iou or ti in used_tracks or di in used_dets:
            continue
        track, det = active[ti], vehicles[di]
        signature = signals.appearance_signature(image.get(), det.bbox)
        if now - track.last_seen_at >= s.vehicle_appearance_gap_seconds:
            similarity = signals.appearance_similarity(track.appearance, signature)
            if similarity is not None and similarity < s.vehicle_appearance_min_similarity:
                # Same place after a gap, but a different-looking vehicle.
                continue
        used_tracks.add(ti)
        used_dets.add(di)
        out += _continue_track(scene, track, det, signature, zones, camera_name, now)

    for di, det in enumerate(vehicles):
        if di in used_dets or det.confidence < s.vehicle_new_track_min_confidence:
            continue
        if any(
            intersection_area(t.box, det.bbox) / det.bbox.area >= _DUPLICATE_CONTAINMENT
            for t in active
            if det.bbox.area > 0
        ):
            continue
        track = Track(
            id="vt-" + uuid.uuid4().hex[:16],
            camera_id=scene.camera_id,
            label=det.label,
            zone=_zone_name(det, zones),
            state=TENTATIVE,
            box=det.bbox,
            observation_count=1,
            first_seen_at=now,
            last_seen_at=now,
            anchored_at=now,
            confidence=det.confidence,
            appearance=signals.appearance_signature(image.get(), det.bbox),
        )
        scene.tracks[track.id] = track
        scene.dirty_tracks.add(track.id)
        active.append(track)
        used_tracks.add(len(active) - 1)
        confirmed = _maybe_confirm(scene, track, camera_name, now)
        if confirmed is not None:
            out.append(confirmed)

    for ti, track in enumerate(active):
        if ti in used_tracks:
            continue
        track.data["missed"] = int(track.data.get("missed", 0)) + 1
        scene.dirty_tracks.add(track.id)
        since = max(track.last_seen_at, scene.observed_since or track.last_seen_at)
        if now - since >= s.vehicle_absence_seconds and track.data["missed"] >= 2:
            if track.reported:
                track.state = DEPARTED
                track.departed_at = now
                track.data["interaction_streak"] = 0
                out.append(
                    _vehicle_transition(
                        track,
                        "departed",
                        ["vehicle_departed"],
                        f"The {track.label} that was {_where(track.zone, camera_name)} has left.",
                        track.zone,
                        {"last_seen": _iso(track.last_seen_at)},
                    )
                )
            else:
                scene.tracks.pop(track.id, None)
                scene.dirty_tracks.discard(track.id)
                scene.deleted_tracks.add(track.id)

    for track in list(scene.tracks.values()):
        if track.state in (TRACKING, STABLE):
            interaction = _person_interaction(scene, track, persons, camera_name, now)
            if interaction is not None:
                out.append(interaction)

    for track in list(scene.tracks.values()):
        if track.state == STABLE and not track.data.get("parked_noted") and track.last_event_id:
            track.data["parked_noted"] = True
            scene.dirty_tracks.add(track.id)
            await _annotate_parked(session, track)
    _expire_departed(scene, now)
    return out


def _zone_name(det: Detection, zones: list[Zone]) -> str | None:
    zone = primary_zone(det, zones)
    return zone.name if zone else None


def _continue_track(
    scene: CameraScene,
    track: Track,
    det: Detection,
    signature: list[float],
    zones: list[Zone],
    camera_name: str,
    now: float,
) -> list[SceneTransition]:
    s = settings
    out: list[SceneTransition] = []
    scene.dirty_tracks.add(track.id)
    partial = (
        det.bbox.area > 0
        and det.bbox.area < track.box.area * 0.7
        and intersection_area(track.box, det.bbox) / det.bbox.area >= _DUPLICATE_CONTAINMENT
    )
    if iou(track.box, det.bbox) >= s.vehicle_stable_iou:
        track.observation_count += 1
    elif partial:
        # Only part of the car was found (occlusion, a second partial box):
        # it is still there, but that is no evidence it moved.
        track.last_seen_at = now
        track.data["missed"] = 0
        return out
    else:
        was_parked = track.state == STABLE or bool(track.data.get("interacted"))
        old_zone = track.zone
        track.box = det.bbox
        track.zone = _zone_name(det, zones)
        track.anchored_at = now
        track.observation_count = 1
        track.stationary_since = None
        track.data["interacted"] = False
        track.data["parked_noted"] = False
        if track.reported:
            track.state = TRACKING
        if was_parked and track.reported:
            track.confidence = det.confidence
            out.append(
                _vehicle_transition(
                    track,
                    "moved",
                    ["vehicle_moved"],
                    f"The parked {track.label} {_where(old_zone, camera_name)} has moved.",
                    track.zone or old_zone,
                )
            )
    track.last_seen_at = now
    track.confidence = det.confidence
    track.data["missed"] = 0
    if signature:
        track.appearance = signals.blend_signature(track.appearance, signature)
    confirmed = _maybe_confirm(scene, track, camera_name, now)
    if confirmed is not None:
        out.append(confirmed)
    if track.reported and track.state == TRACKING and track.observation_count >= s.vehicle_stable_observations:
        track.state = STABLE
        track.stationary_since = track.anchored_at
    return out


def _maybe_confirm(scene: CameraScene, track: Track, camera_name: str, now: float) -> SceneTransition | None:
    s = settings
    if track.reported or track.observation_count < s.vehicle_confirm_observations:
        return None
    track.data["reported"] = True
    track.state = TRACKING
    if track.observation_count >= s.vehicle_stable_observations:
        track.state = STABLE
        track.stationary_since = track.anchored_at
    returned = _returning_track(scene, track, now)
    where = _where(track.zone, camera_name)
    if returned is not None:
        scene.tracks.pop(returned.id, None)
        scene.deleted_tracks.add(returned.id)
        return _vehicle_transition(
            track,
            "returned",
            ["vehicle_arrived", "vehicle_returned"],
            f"A {track.label} returned {where}.",
            track.zone,
            {"previous_track_id": returned.id, "departed_at": _iso(returned.departed_at)},
        )
    watched_before = (
        scene.observed_since is not None
        and scene.observed_since <= track.first_seen_at - s.vehicle_absence_seconds
    )
    if watched_before:
        return _vehicle_transition(track, "arrived", ["vehicle_arrived"], f"A {track.label} arrived {where}.", track.zone)
    # Already there when observation began: say so rather than "arrived".
    return _vehicle_transition(track, "first_seen", [], f"A {track.label} is {where}.", track.zone)


def _returning_track(scene: CameraScene, track: Track, now: float) -> Track | None:
    s = settings
    best: tuple[float, Track] | None = None
    for other in scene.tracks.values():
        if other.id == track.id or other.state != DEPARTED or other.departed_at is None:
            continue
        if now - other.departed_at > s.vehicle_return_window_seconds:
            continue
        if _overlap(other.box, track.box) < s.vehicle_match_iou:
            continue
        similarity = signals.appearance_similarity(other.appearance, track.appearance)
        # "Returned" is a claim about identity: without a comparable colour
        # signature it is just an arrival.
        if similarity is None or similarity < s.vehicle_appearance_min_similarity:
            continue
        if best is None or similarity > best[0]:
            best = (similarity, other)
    return best[1] if best else None


def _person_interaction(
    scene: CameraScene, track: Track, persons: list[Detection], camera_name: str, now: float
) -> SceneTransition | None:
    s = settings
    at_vehicle = any(
        p.bbox.area > 0 and intersection_area(p.bbox, track.box) / p.bbox.area >= _PERSON_AT_VEHICLE
        for p in persons
    )
    streak = int(track.data.get("interaction_streak", 0))
    streak = streak + 1 if at_vehicle else 0
    if streak != track.data.get("interaction_streak", 0):
        track.data["interaction_streak"] = streak
        scene.dirty_tracks.add(track.id)
    if streak < s.vehicle_interaction_observations:
        return None
    last = track.data.get("last_interaction_at")
    if last is not None and now - float(last) < s.vehicle_interaction_cooldown_seconds:
        return None
    track.data["last_interaction_at"] = now
    track.data["interacted"] = True
    track.data["parked_noted"] = False
    track.state = TRACKING
    track.observation_count = 1
    track.anchored_at = now
    track.stationary_since = None
    scene.dirty_tracks.add(track.id)
    return _vehicle_transition(
        track,
        "interaction",
        ["vehicle_interaction"],
        f"Someone is at the {track.label} {_where(track.zone, camera_name)}.",
        track.zone,
    )


async def _annotate_parked(session: AsyncSession, track: Track) -> None:
    """Mark the reporting event parked instead of emitting another event."""
    if not track.last_event_id:
        return
    row = await session.get(Event, track.last_event_id)
    if row is None:
        return
    tags = list(row.tags or [])
    if "vehicle_parked" not in tags:
        tags.append("vehicle_parked")
    row.tags = tags
    metadata = dict(row.event_metadata or {})
    metadata["vehicle_track"] = track.summary(metadata.get("vehicle_track", {}).get("transition"))
    scene_meta = dict(metadata.get("scene") or {})
    scene_meta["parked"] = True
    metadata["scene"] = scene_meta
    row.event_metadata = metadata


def _expire_departed(scene: CameraScene, now: float) -> None:
    window = settings.vehicle_return_window_seconds
    for track in list(scene.tracks.values()):
        if track.state == DEPARTED and track.departed_at is not None and now - track.departed_at > window:
            scene.tracks.pop(track.id, None)
            scene.deleted_tracks.add(track.id)


# --- zone helpers ------------------------------------------------------------


def _record(scene: CameraScene, zone_id: str, kind: str, initial: str) -> ZoneRecord:
    record = scene.zones.get(zone_id)
    if record is None or record.kind != kind:
        record = ZoneRecord(
            id=f"{scene.camera_id}:{kind}:{zone_id}",
            camera_id=scene.camera_id,
            kind=kind,
            zone_id=zone_id,
            state=initial,
            data={},
        )
        scene.zones[zone_id] = record
        scene.dirty_zones.add(zone_id)
    return record


def _verifier_allowed(record: ZoneRecord, now: float) -> bool:
    last = record.data.get("verifier_last_at")
    return last is None or now - float(last) >= settings.scene_verifier_min_interval_seconds


# --- mailbox -------------------------------------------------------------------


async def _mailbox_step(
    scene: CameraScene,
    zone_id: str,
    zone: Zone,
    camera_name: str,
    frame: bytes | None,
    detections: list[Detection],
    image: _LazyImage,
    now: float,
    outage: bool,
) -> SceneTransition | None:
    s = settings
    record = _record(scene, zone_id, "mailbox", "idle")
    crops = scene.crops.setdefault(zone_id, {})
    data = record.data
    finished, answer, context = _take_answer(scene, zone_id)
    if finished:
        scene.dirty_zones.add(zone_id)
        verdict = None
        if answer is not None:
            verdict = {**answer, "source": "foundry"}
            if answer.get("person_interacted") == "no":
                verdict["item_deposited"] = "no"
        return _mailbox_result(scene, record, zone, camera_name, context, verdict)
    cover = max(
        (_zone_cover(d.bbox, zone.bbox) for d in detections if d.label == "person"), default=0.0
    )
    interacting = cover >= s.mailbox_min_zone_overlap
    package_here = any(
        d.label == "package" and overlap_ratio(d.bbox, zone.bbox) >= 0.3 for d in detections
    )
    region = _expand(zone.bbox, 0.08)

    if outage and record.state == "visit":
        # What happened while we could not see is unknown: drop the visit.
        record.state = "idle"
        data["last_outcome"] = "abandoned_outage"
        crops.clear()
        scene.dirty_zones.add(zone_id)

    if record.state != "visit":
        if interacting:
            record.state = "visit"
            data.update(
                visit_id="mb-" + uuid.uuid4().hex[:12],
                visit_started=now,
                observations=1,
                misses=0,
                max_cover=cover,
                package_before=bool(data.get("package_present")),
                package_during=package_here,
            )
            crops["during"] = signals.crop_jpeg(image.get(), region)
            crops["during_frame"] = frame
            scene.dirty_zones.add(zone_id)
        else:
            if data.get("package_present") != package_here:
                data["package_present"] = package_here
                scene.dirty_zones.add(zone_id)
            before = signals.crop_jpeg(image.get(), region)
            if before:
                crops["before"] = before
        return None

    if interacting:
        data["observations"] = int(data.get("observations", 0)) + 1
        data["misses"] = 0
        data["package_during"] = bool(data.get("package_during")) or package_here
        if cover >= float(data.get("max_cover", 0.0)):
            data["max_cover"] = cover
            crops["during"] = signals.crop_jpeg(image.get(), region) or crops.get("during")
            crops["during_frame"] = frame
        scene.dirty_zones.add(zone_id)
        if now - float(data.get("visit_started", now)) > _MAILBOX_MAX_VISIT_SECONDS:
            record.state = "idle"
            data["last_outcome"] = "abandoned_too_long"
        return None

    data["misses"] = int(data.get("misses", 0)) + 1
    scene.dirty_zones.add(zone_id)
    if data["misses"] < s.mailbox_end_after_misses:
        return None
    crops["after"] = signals.crop_jpeg(image.get(), region)
    record.state = "idle"
    data["package_present"] = package_here
    return await _finish_visit(scene, record, zone, camera_name, package_here, crops, frame, now)


async def _finish_visit(
    scene: CameraScene,
    record: ZoneRecord,
    zone: Zone,
    camera_name: str,
    package_after: bool,
    crops: dict,
    frame: bytes | None,
    now: float,
) -> SceneTransition | None:
    s = settings
    data = record.data
    visit_id = data.get("visit_id")
    observations = int(data.get("observations", 0))
    if observations < s.mailbox_min_observations:
        data["last_outcome"] = "walk_by"
        return None
    last = data.get("last_delivery_at")
    if last is not None and now - float(last) < s.mailbox_dedupe_seconds:
        data["last_outcome"] = "deduplicated"
        return None

    context = {
        "visit_id": visit_id,
        "observations": observations,
        "package_before": bool(data.get("package_before")),
        "package_after": package_after,
        "now": now,
        "crops": {key: crops.get(key) for key in ("before", "after")},
        "frames": [f for f in (crops.get("during_frame"), frame) if f],
    }
    package_during = bool(data.get("package_during"))
    if package_after and not context["package_before"]:
        verdict = {
            "item_deposited": "yes",
            "item_type": "parcel",
            "confidence": 0.7,
            "source": "local",
            "evidence": "package detected in the mailbox zone after the visit, not before",
        }
        return _mailbox_result(scene, record, zone, camera_name, context, verdict)
    if package_during and not package_after:
        data["last_outcome"] = "carried_past"
        return None
    verifier = get_scene_verifier()
    images = [(label, crops[key]) for label, key in (("BEFORE", "before"), ("DURING", "during"), ("AFTER", "after")) if crops.get(key)]
    if (
        verifier is not None
        and crops.get("after")
        and len(images) >= 2
        and _verifier_allowed(record, now)
        and record.zone_id not in scene.pending
    ):
        data["verifier_last_at"] = now
        data["last_outcome"] = "verifying"
        task = asyncio.create_task(_verify(verifier.verify_mailbox(images), scene.camera_id, "mailbox"))
        scene.pending[record.zone_id] = (task, context)
        return None
    return _mailbox_result(scene, record, zone, camera_name, context, None)


def _mailbox_result(
    scene: CameraScene,
    record: ZoneRecord,
    zone: Zone,
    camera_name: str,
    context: dict,
    verdict: dict | None,
) -> SceneTransition | None:
    s = settings
    data = record.data
    now = float(context["now"])
    if verdict is None or verdict.get("item_deposited") != "yes":
        data["last_outcome"] = "no_deposit" if verdict else "unverified"
        data["last_verdict"] = verdict
        return None
    last = data.get("last_delivery_at")
    if last is not None and now - float(last) < s.mailbox_dedupe_seconds:
        data["last_outcome"] = "deduplicated"
        return None

    visit_id = context["visit_id"]
    data["last_delivery_at"] = now
    data["last_outcome"] = "delivery"
    data["last_delivery_visit"] = visit_id
    item = verdict.get("item_type") or "unknown"
    tags = ["mailbox", "mailbox_delivery"]
    if item in ("parcel", "mail"):
        tags.append(item)
    noun = {"parcel": "A parcel", "mail": "Mail"}.get(item, "An item")
    crops = context["crops"]
    evidence = {
        "visit_id": visit_id,
        "zone": zone.name,
        "observations": context["observations"],
        "item_deposited": "yes",
        "item_type": item,
        "confidence": verdict.get("confidence"),
        "source": verdict.get("source"),
        "evidence": verdict.get("evidence"),
        "before": {"package_detected": context["package_before"], "image": bool(crops.get("before"))},
        "after": {"package_detected": context["package_after"], "image": bool(crops.get("after"))},
    }
    frames = context["frames"]
    return SceneTransition(
        camera_id=scene.camera_id,
        kind="mailbox",
        transition="mailbox_delivery",
        event_type="package",
        description=f"{noun} was put in the {zone.name} at {camera_name}.",
        tags=tags,
        metadata={
            "mailbox": evidence,
            "scene": {
                "kind": "mailbox",
                "transition": "mailbox_delivery",
                "zone": zone.name,
                "item_type": item,
                "confidence": verdict.get("confidence"),
                "source": verdict.get("source"),
            },
        },
        zone=zone.name,
        frames=frames or None,
    )


# --- bins ------------------------------------------------------------------------


async def _bin_step(
    scene: CameraScene,
    zone_id: str,
    zone: Zone,
    camera_name: str,
    frame: bytes | None,
    detections: list[Detection],
    image: _LazyImage,
    now: float,
    outage: bool,
) -> SceneTransition | None:
    s = settings
    record = _record(scene, zone_id, "bin", "unknown")
    data = record.data
    crops = scene.crops.setdefault(zone_id, {})
    region = _expand(zone.bbox, 0.05)

    if outage and data.get("baseline"):
        # Anything that changed while the camera was dark has no timeline:
        # the next change may still establish presence, never "emptied".
        data["stale"] = True
        data.pop("pending_sig", None)
        data["pending_count"] = 0
        scene.dirty_zones.add(zone_id)

    near = _expand(zone.bbox, 0.1)
    interaction: str | None = None
    for d in detections:
        if d.label == "truck" and intersection_area(d.bbox, near) > 0:
            interaction = "collection_vehicle"
            break
        if d.label == "person" and _zone_cover(d.bbox, zone.bbox) >= _OCCLUDES_ZONE:
            interaction = "person"
    if interaction is not None:
        previous = data.get("interaction") or {}
        recent = previous.get("at") is not None and now - float(previous["at"]) < s.bin_interaction_window_seconds
        if not (recent and previous.get("kind") == "collection_vehicle" and interaction == "person"):
            data["interaction"] = {"kind": interaction, "at": now}
        else:
            data["interaction"] = {"kind": previous["kind"], "at": now}
        crops["during"] = signals.crop_jpeg(image.get(), region) or crops.get("during")
        scene.dirty_zones.add(zone_id)

    if zone_id in scene.pending:
        finished, answer, context = _take_answer(scene, zone_id)
        if not finished:
            return None  # a check is in flight; this candidate is decided then
        scene.dirty_zones.add(zone_id)
        return _bin_decide(scene, record, zone, camera_name, context, answer)

    occluded = any(
        d.label == "person" or d.label in VEHICLE_CLASSES
        for d in detections
        if _zone_cover(d.bbox, zone.bbox) >= _OCCLUDES_ZONE
    )
    if occluded:
        return None
    last_check = data.get("last_check_at")
    if last_check is not None and now - float(last_check) < s.bin_check_interval_seconds and not outage:
        return None
    signature = signals.region_signature(image.get(), zone.bbox)
    if not signature:
        return None
    data["last_check_at"] = now
    scene.dirty_zones.add(zone_id)
    local_labels = {label.strip() for label in s.bin_local_labels.split(",") if label.strip()}
    local_present = any(
        d.label in local_labels and overlap_ratio(d.bbox, zone.bbox) >= 0.5 for d in detections
    )

    baseline = data.get("baseline")
    if not baseline:
        data["baseline"] = signature
        crops["before"] = signals.crop_jpeg(image.get(), region)
        if local_present and record.state == "unknown":
            record.state = "present"
        return None
    difference = signals.region_difference(signature, baseline)
    if difference is None or difference < s.bin_change_threshold:
        data.pop("pending_sig", None)
        data["pending_count"] = 0
        if not crops.get("before"):
            crops["before"] = signals.crop_jpeg(image.get(), region)
        return None

    pending = data.get("pending_sig")
    pending_difference = signals.region_difference(signature, pending) if pending else None
    if pending_difference is not None and pending_difference < s.bin_change_threshold:
        data["pending_count"] = int(data.get("pending_count", 0)) + 1
    else:
        data["pending_sig"] = signature
        data["pending_count"] = 1
    if data["pending_count"] < s.bin_change_confirm_checks:
        return None

    now_crop = signals.crop_jpeg(image.get(), region)
    before_state = record.state
    now_state = "present" if local_present else "unknown"
    context = {
        "now": now,
        "signature": signature,
        "now_crop": now_crop,
        "difference": difference,
        "before_state": before_state,
        "now_state": now_state,
        "local_present": local_present,
        "frame": frame,
    }
    verifier = get_scene_verifier()
    if not local_present and verifier is not None and now_crop:
        if not _verifier_allowed(record, now):
            return None  # keep the candidate; ask on a later check
        images = [("BEFORE", crops["before"])] if crops.get("before") else []
        if crops.get("during") and data.get("interaction"):
            images.append(("DURING", crops["during"]))
        images.append(("NOW", now_crop))
        data["verifier_last_at"] = now
        task = asyncio.create_task(_verify(verifier.verify_bin(images), scene.camera_id, "bin"))
        scene.pending[zone_id] = (task, context)
        return None
    return _bin_decide(scene, record, zone, camera_name, context, None)


def _bin_decide(
    scene: CameraScene,
    record: ZoneRecord,
    zone: Zone,
    camera_name: str,
    context: dict,
    answer: dict | None,
) -> SceneTransition | None:
    s = settings
    data = record.data
    crops = scene.crops.setdefault(record.zone_id, {})
    now = float(context["now"])
    signature = context["signature"]
    now_crop = context["now_crop"]
    difference = float(context["difference"])
    before_state = context["before_state"]
    now_state = context["now_state"]
    local_present = context["local_present"]
    frame = context["frame"]
    if answer is not None:
        now_state = {"yes": "present", "no": "absent"}.get(answer["bin_present_now"], now_state)
        if before_state == "unknown" and crops.get("before"):
            before_state = {"yes": "present", "no": "absent"}.get(answer["bin_present_before"], "unknown")

    interaction_info = data.get("interaction") or {}
    interaction_recent = (
        interaction_info.get("at") is not None
        and now - float(interaction_info["at"]) < s.bin_interaction_window_seconds
    )
    stale = bool(data.get("stale"))
    result: SceneTransition | None = None
    evidence = {
        "zone": zone.name,
        "before": before_state,
        "now": now_state,
        "difference": round(difference, 3),
        "interaction": interaction_info.get("kind") if interaction_recent else None,
        "after_outage": stale,
        "source": "local" if local_present else ("foundry" if answer else None),
        "verifier": answer,
    }
    if now_state == "present" and before_state == "absent":
        result = _bin_transition(scene, record, zone, camera_name, "bin_placed_out", evidence, frame, now)
    elif now_state == "present" and before_state == "present" and interaction_recent and not stale:
        by_vehicle = interaction_info.get("kind") == "collection_vehicle"
        confirmed = answer is not None and (
            answer["bin_moved_or_tipped"] == "yes" or answer["collection_vehicle_visible"] == "yes"
        )
        vetoed = answer is not None and answer["bin_moved_or_tipped"] == "no" and not by_vehicle
        if (by_vehicle or confirmed) and not vetoed:
            result = _bin_transition(scene, record, zone, camera_name, "bin_emptied", evidence, frame, now)
    elif (
        now_state == "absent"
        and before_state == "present"
        and s.bin_removal_counts_as_emptied
        and not stale
        and interaction_recent
        and interaction_info.get("kind") == "collection_vehicle"
    ):
        evidence["rule"] = "bin_removal_counts_as_emptied"
        result = _bin_transition(scene, record, zone, camera_name, "bin_emptied", evidence, frame, now)

    if now_state != "unknown":
        record.state = now_state
    elif before_state != "unknown" and record.state == "unknown":
        record.state = before_state
    data["baseline"] = signature
    data.pop("pending_sig", None)
    data["pending_count"] = 0
    data["stale"] = False
    data["last_change"] = {k: v for k, v in evidence.items() if k != "verifier"} | {"at": _iso(now)}
    if interaction_recent:
        data["interaction"] = None
    crops["before"] = now_crop or crops.get("before")
    crops.pop("during", None)
    return result


def _bin_transition(
    scene: CameraScene,
    record: ZoneRecord,
    zone: Zone,
    camera_name: str,
    transition: str,
    evidence: dict,
    frame: bytes | None,
    now: float,
) -> SceneTransition | None:
    s = settings
    last_events = dict(record.data.get("last_events") or {})
    last = last_events.get(transition)
    if last is not None and now - float(last) < s.bin_dedupe_seconds:
        record.data["last_outcome"] = f"{transition}_deduplicated"
        return None
    last_events[transition] = now
    record.data["last_events"] = last_events
    record.data["last_outcome"] = transition
    description = (
        f"A bin was put out in the {zone.name} at {camera_name}."
        if transition == "bin_placed_out"
        else f"The bin in the {zone.name} at {camera_name} was emptied."
    )
    confidence = (evidence.get("verifier") or {}).get("confidence")
    return SceneTransition(
        camera_id=scene.camera_id,
        kind="bin",
        transition=transition,
        event_type="motion",
        description=description,
        tags=["bin", transition],
        metadata={
            "bin": evidence,
            "scene": {
                "kind": "bin",
                "transition": transition,
                "zone": zone.name,
                "confidence": confidence,
                "source": evidence.get("source"),
                "interaction": evidence.get("interaction"),
            },
        },
        zone=zone.name,
        frames=[frame] if frame else None,
    )
