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
    Two signals, combined per visit:

    * Opened/closed, independent of people: the zone is compared with a
      rolling, brightness-normalized reference of the idle mailbox (updated
      only while unoccluded, nobody near and unchanged). A difference above
      ``mailbox_open_threshold`` for ``mailbox_open_min_frames`` samples (one
      while a person is near) means opened; back below 70% means closed. A
      change the whole frame shares (IR switch) re-bases instead.
    * Proximity: a person covering ``mailbox_min_zone_overlap`` of the zone
      grown by ``mailbox_proximity_margin`` starts a visit (and boosts the
      camera's stream sampling). One sample counts when the mailbox opened
      or a package appeared/disappeared; otherwise the visit needs
      ``mailbox_min_observations`` samples or it is a walk-by.

    When the visit ends, the before / during / after evidence classifies it:
    package appeared -> ``mailbox_delivery``; package gone ->
    ``mailbox_retrieval`` (tagged ``package_removed``); otherwise the
    Foundry verifier's ``action`` (deposited / retrieved / opened_only /
    none), falling back locally to ``mailbox_opened`` (appearance changed)
    or ``mailbox_visit`` (outcome unknown). Every counted visit emits
    exactly one event, deduplicated per transition locally and across
    replicas by a DB claim. An opening with nobody seen near is a
    standalone ``mailbox_opened``. Identity is never inferred.

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
from . import ingestion_lease
from . import zones as zone_service
from .ingestion_lease import LeaseLost, LeaseToken

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
    # Labelled JPEG evidence (e.g. {"before": ..., "after": ...}) to persist
    # as retrievable EventEvidence rows alongside the event.
    evidence_images: dict[str, bytes] | None = None
    # Cross-replica dedup: ingestion must win a DB claim on this key
    # (app.services.scene_dedup) before emitting the event.
    dedup_key: str | None = None
    dedup_window_seconds: float = 0.0
    observed_at: float | None = None


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
    # Ingestion lease this state was loaded under. Every save is fenced on it
    # so a replica that lost the camera cannot overwrite the new holder.
    lease: LeaseToken | None = None


_scenes: dict[str, CameraScene] = {}


def _iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def reset_memory() -> None:
    """Drop in-memory state (tests simulate a restart with this)."""
    _scenes.clear()
    _mailbox_stats.clear()


def forget(camera_id: str) -> None:
    """Drop one camera's cached scene so the next frame reloads it from the
    database. Called whenever this replica gains or loses the camera's
    ingestion lease: another replica may have advanced the state meanwhile."""
    scene = _scenes.pop(camera_id, None)
    if scene is not None:
        for task, _ in scene.pending.values():
            task.cancel()


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
    scene = await _read(session, camera_id)
    _scenes[camera_id] = scene
    return scene


async def _read(session: AsyncSession, camera_id: str) -> CameraScene:
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
    return scene


async def _save(session: AsyncSession, scene: CameraScene) -> None:
    if scene.lease is not None and not await ingestion_lease.fence(session, scene.lease):
        # Another replica took the camera while this frame was processing:
        # discard the write and the stale cache (the next frame reloads).
        await session.rollback()
        forget(scene.camera_id)
        logger.info("scene state for %s discarded: ingestion lease epoch %d lost", scene.camera_id, scene.lease.epoch)
        raise LeaseLost(scene.camera_id)
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
    lease: LeaseToken | None = None,
) -> list[SceneTransition]:
    """Advance every state machine for one camera by one real frame.

    With ``lease`` the save is fenced on it: if the lease was lost
    meanwhile, nothing is written and no transitions are returned."""
    now = time.time() if now is None else now
    try:
        return await _process(session, camera_id, camera_name, frame, detections, now, lease)
    except LeaseLost:
        return []
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
    lease: LeaseToken | None = None,
) -> list[SceneTransition]:
    scene = await _load(session, camera_id)
    scene.lease = lease
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
            transitions += await _mailbox_step(
                scene, zone_id, zone, camera_name, frame, detections, image, now, outage
            )
            continue
        if kind == "bin" and settings.bin_detection_enabled:
            result = await _bin_step(scene, zone_id, zone, camera_name, frame, detections, image, now, outage)
        else:
            continue
        if result is not None:
            transitions.append(result)
    await _save(session, scene)
    return transitions


async def note_event(session: AsyncSession, transition: SceneTransition, event_id: str) -> None:
    """Remember which event reported a track (so parking can annotate it).

    Fenced like every save: raises :class:`LeaseLost` if the lease is gone."""
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
    """Current state for the admin/verification endpoint.

    A replica that does not ingest this camera has no cached scene (it is
    dropped when the lease is lost) and reads the database afresh without
    caching, so it never later resumes from a stale copy.
    """
    scene = _scenes.get(camera_id) or await _read(session, camera_id)
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
             "data": {k: v for k, v in record.data.items() if k not in {"baseline", "pending_sig", "ref_sig", "global_ref"}}}
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

# Region the opened/closed detector compares: the zone plus a little edge
# texture, so a flat mailbox face does not amplify sensor noise.
_LID_REGION_MARGIN = 0.02
# Contrast floor (grey levels) for the lid signature, see region_signature.
_LID_MIN_STD = 6.0
# Weight of the newest idle frame in the rolling closed-mailbox reference.
_LID_REFERENCE_RATE = 0.1
# An "open" state nobody is near for this long is the new normal (a door
# left open, a sticker, a moved camera): it becomes the reference.
_LID_REBASE_SECONDS = 1800.0
_LID_CLOSE_RATIO = 0.7
# Whole-frame change gate: the frame is cut into a grid, tiles touching the
# mailbox are ignored, and when most of the remaining tiles changed as much
# as the mailbox did, it is lighting/IR - not the lid.
_LID_GRID = 4
_LID_GLOBAL_FRACTION = 0.5


def _context_tiles(zone_box: BoundingBox) -> list[BoundingBox]:
    margin = _expand(zone_box, 0.05)
    step = 1.0 / _LID_GRID
    tiles = []
    for row in range(_LID_GRID):
        for col in range(_LID_GRID):
            tile = BoundingBox(col * step, row * step, (col + 1) * step, (row + 1) * step)
            if tile.x2 <= margin.x1 or tile.x1 >= margin.x2 or tile.y2 <= margin.y1 or tile.y1 >= margin.y2:
                tiles.append(tile)
    return tiles


def _context_signatures(picture, zone_box: BoundingBox) -> list[list[float]]:
    return [signals.region_signature(picture, tile, min_std=_LID_MIN_STD) for tile in _context_tiles(zone_box)]


def _whole_frame_changed(now_sigs: list[list[float]], ref_sigs: list[list[float]], threshold: float) -> bool:
    if not ref_sigs or len(ref_sigs) != len(now_sigs):
        return False
    diffs = [signals.region_difference(a, b) for a, b in zip(now_sigs, ref_sigs)]
    valid = [d for d in diffs if d is not None]
    if not valid:
        return False
    return sum(d >= threshold for d in valid) >= _LID_GLOBAL_FRACTION * len(valid)

MAILBOX_TRANSITIONS = ("mailbox_delivery", "mailbox_retrieval", "mailbox_opened", "mailbox_visit")
_ACTION_TRANSITION = {
    "deposited": "mailbox_delivery",
    "retrieved": "mailbox_retrieval",
    "opened_only": "mailbox_opened",
    "none": "mailbox_visit",
}
_MAILBOX_PRIORITY = {
    "mailbox_delivery": "normal",
    "mailbox_retrieval": "normal",
    "mailbox_opened": "normal",
    "mailbox_visit": "low",
}
MAILBOX_STAT_KEYS = (
    "mailbox_visits",
    "mailbox_walk_by",
    "mailbox_events",
    "mailbox_opened",
    "mailbox_deduped",
)
# Per-camera mailbox counters for the periodic ingestion stats line.
_mailbox_stats: dict[str, dict[str, int]] = {}


def _count(camera_id: str, key: str) -> None:
    stats = _mailbox_stats.setdefault(camera_id, dict.fromkeys(MAILBOX_STAT_KEYS, 0))
    stats[key] = stats.get(key, 0) + 1


def drain_mailbox_stats(camera_id: str) -> dict[str, int]:
    """Mailbox counters since the last call (empty for cameras without a
    mailbox zone). Read and reset by the ingestion stats line."""
    stats = _mailbox_stats.pop(camera_id, None)
    return dict(stats) if stats else {}


def _boost_sampling(camera_id: str) -> None:
    """Ask the relayed stream for faster samples while someone is at the
    mailbox. Snapshot-only cameras are never boosted (NVR budget)."""
    if not settings.stream_frames_enabled or settings.mailbox_boost_seconds <= 0:
        return
    from .stream_frames import stream_hub

    try:
        stream_hub.boost(camera_id, settings.mailbox_boost_seconds)
    except Exception:  # noqa: BLE001 - boosting is best effort
        logger.debug("mailbox sampling boost failed for %s", camera_id, exc_info=True)


def _occluded(zone: Zone, detections: list[Detection]) -> bool:
    return any(
        (d.label == "person" or d.label in VEHICLE_CLASSES) and _zone_cover(d.bbox, zone.bbox) >= _OCCLUDES_ZONE
        for d in detections
    )


def _lid_update(
    scene: CameraScene,
    record: ZoneRecord,
    zone: Zone,
    detections: list[Detection],
    image: _LazyImage,
    frame: bytes | None,
    now: float,
    near: bool,
) -> tuple[str | None, float | None]:
    """Advance the mailbox opened/closed detector by one frame.

    Returns ``(change, diff)``: ``change`` is ``"opened"``, ``"closed"`` or
    ``None``; ``diff`` the brightness-normalized difference from the closed
    reference (``None`` when the zone could not be checked).

    The reference is a rolling average of the zone while it is unoccluded,
    nobody is near and it matches the reference - so slow light changes are
    absorbed and an open lid/door is not. A change the whole frame shares
    (IR switching on, a sudden exposure jump) re-bases instead of firing.
    """
    s = settings
    data = record.data
    if _occluded(zone, detections):
        return None, None
    picture = image.get()
    if picture is None:
        return None, None
    region = _expand(zone.bbox, _LID_REGION_MARGIN)
    signature = signals.region_signature(picture, region, min_std=_LID_MIN_STD)
    if not signature:
        return None, None
    crops = scene.crops.setdefault(record.zone_id, {})
    evidence_region = _expand(zone.bbox, 0.08)
    scene.dirty_zones.add(record.zone_id)
    reference = data.get("ref_sig")
    if not reference or len(reference) != len(signature):
        data.update(
            ref_sig=signature,
            global_ref=_context_signatures(picture, zone.bbox),
            lid="closed",
            open_pending=0,
            lid_changed_at=now,
        )
        if not near:
            crops["before"] = signals.crop_jpeg(picture, evidence_region) or crops.get("before")
        return None, 0.0

    diff = signals.region_difference(signature, reference)
    data["last_diff"] = round(diff, 3)
    threshold = s.mailbox_open_threshold

    def rebase() -> None:
        data.update(
            ref_sig=signature,
            global_ref=_context_signatures(picture, zone.bbox),
            lid="closed",
            open_pending=0,
            lid_changed_at=now,
        )

    if data.get("lid") == "open":
        if diff < threshold * _LID_CLOSE_RATIO:
            data.update(lid="closed", lid_changed_at=now, open_pending=0)
            return "closed", diff
        changed_at = float(data.get("lid_changed_at") or now)
        if not near and record.state != "visit" and now - changed_at > _LID_REBASE_SECONDS:
            logger.info(
                "mailbox %s/%s: open for %.0fs with nobody near; adopting it as the reference",
                scene.camera_id, zone.name, now - changed_at,
            )
            rebase()
        return None, diff

    if diff >= threshold:
        if _whole_frame_changed(_context_signatures(picture, zone.bbox), data.get("global_ref") or [], threshold):
            logger.info(
                "mailbox %s/%s: whole-frame change (lighting/IR), re-basing (diff=%.2f)",
                scene.camera_id, zone.name, diff,
            )
            rebase()
            return None, diff
        pending = int(data.get("open_pending", 0)) + 1
        data["open_pending"] = pending
        crops["opened"] = signals.crop_jpeg(picture, evidence_region) or crops.get("opened")
        crops["opened_frame"] = frame
        # With a person at the mailbox one changed frame is enough: at the
        # production cadence a visit is often only one or two samples.
        if pending >= s.mailbox_open_min_frames or near or record.state == "visit":
            data.update(lid="open", lid_changed_at=now, open_pending=0)
            return "opened", diff
        return None, diff

    data["open_pending"] = 0
    if not near and record.state != "visit":
        rate = _LID_REFERENCE_RATE
        data["ref_sig"] = [(1.0 - rate) * r + rate * v for r, v in zip(reference, signature)]
        global_now = _context_signatures(picture, zone.bbox)
        global_ref = data.get("global_ref") or []
        data["global_ref"] = (
            [[(1.0 - rate) * r + rate * v for r, v in zip(ref, cur)] for ref, cur in zip(global_ref, global_now)]
            if len(global_ref) == len(global_now)
            and all(len(ref) == len(cur) for ref, cur in zip(global_ref, global_now))
            else global_now
        )
        crops["before"] = signals.crop_jpeg(picture, evidence_region) or crops.get("before")
    return None, diff


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
) -> list[SceneTransition]:
    s = settings
    record = _record(scene, zone_id, "mailbox", "idle")
    crops = scene.crops.setdefault(zone_id, {})
    data = record.data
    _mailbox_stats.setdefault(scene.camera_id, dict.fromkeys(MAILBOX_STAT_KEYS, 0))
    results: list[SceneTransition] = []

    finished, answer, context = _take_answer(scene, zone_id)
    if finished:
        scene.dirty_zones.add(zone_id)
        verdict = {**answer, "source": "foundry"} if answer is not None else None
        result = _mailbox_result(scene, record, zone, camera_name, context, verdict)
        if result is not None:
            results.append(result)

    proximity = _expand(zone.bbox, s.mailbox_proximity_margin)
    cover = max(
        (_zone_cover(d.bbox, proximity) for d in detections if d.label == "person"), default=0.0
    )
    near = cover >= s.mailbox_min_zone_overlap
    if near:
        _boost_sampling(scene.camera_id)
    package_here = any(
        d.label == "package" and overlap_ratio(d.bbox, zone.bbox) >= 0.3 for d in detections
    )
    region = _expand(zone.bbox, 0.08)

    if outage and record.state == "visit":
        # What happened while we could not see is unknown: drop the visit.
        record.state = "idle"
        data["last_outcome"] = "abandoned_outage"
        for key in ("during", "during_frame", "after"):
            crops.pop(key, None)
        scene.dirty_zones.add(zone_id)
        logger.info("mailbox %s/%s: visit abandoned (camera outage)", scene.camera_id, zone.name)

    lid_change, diff = (None, None)
    if s.mailbox_open_detection_enabled:
        lid_change, diff = _lid_update(scene, record, zone, detections, image, frame, now, near)

    if record.state != "visit":
        if near:
            record.state = "visit"
            data.update(
                visit_id="mb-" + uuid.uuid4().hex[:12],
                visit_started=now,
                visit_ended_at=None,
                observations=1,
                misses=0,
                max_cover=cover,
                max_diff=diff or 0.0,
                visit_lid_changed=lid_change == "opened",
                package_before=bool(data.get("package_present")),
                package_during=package_here,
            )
            crops["during"] = signals.crop_jpeg(image.get(), region)
            crops["during_frame"] = frame
            scene.dirty_zones.add(zone_id)
            _count(scene.camera_id, "mailbox_visits")
            return results
        if data.get("package_present") != package_here:
            data["package_present"] = package_here
            scene.dirty_zones.add(zone_id)
        if lid_change == "opened":
            result = _standalone_open(scene, record, zone, camera_name, crops, frame, diff, now)
            if result is not None:
                results.append(result)
        return results

    if lid_change is not None:
        data["visit_lid_changed"] = True
    if diff is not None and diff > float(data.get("max_diff") or 0.0):
        data["max_diff"] = diff
    if near:
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
            logger.info(
                "mailbox %s/%s: visit %s abandoned after %.0fs",
                scene.camera_id, zone.name, data.get("visit_id"), now - float(data["visit_started"]),
            )
        return results

    data["misses"] = int(data.get("misses", 0)) + 1
    scene.dirty_zones.add(zone_id)
    if data["misses"] < s.mailbox_end_after_misses:
        return results
    crops["after"] = signals.crop_jpeg(image.get(), region)
    record.state = "idle"
    data["package_present"] = package_here
    data["visit_ended_at"] = now
    result = await _finish_visit(scene, record, zone, camera_name, package_here, crops, frame, now)
    if result is not None:
        results.append(result)
    return results


def _log_visit(scene: CameraScene, zone: Zone, data: dict, outcome: str, **extra) -> None:
    details = " ".join(f"{key}={value}" for key, value in extra.items())
    logger.info(
        "mailbox %s/%s: visit %s observations=%s cover=%.2f diff=%.2f package_before=%s "
        "package_after=%s lid_changed=%s outcome=%s%s",
        scene.camera_id,
        zone.name,
        data.get("visit_id"),
        data.get("observations"),
        float(data.get("max_cover") or 0.0),
        float(data.get("max_diff") or 0.0),
        bool(data.get("package_before")),
        bool(data.get("package_present")),
        bool(data.get("visit_lid_changed")),
        outcome,
        f" {details}" if details else "",
    )


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
    observations = int(data.get("observations", 0))
    package_before = bool(data.get("package_before"))
    state_change = bool(data.get("visit_lid_changed"))
    changed = state_change or package_before != package_after
    if observations < s.mailbox_min_observations and not changed:
        data["last_outcome"] = "walk_by"
        _count(scene.camera_id, "mailbox_walk_by")
        _log_visit(scene, zone, data, "walk_by")
        return None

    context = {
        "visit_id": data.get("visit_id"),
        "observations": observations,
        "max_cover": round(float(data.get("max_cover") or 0.0), 3),
        "diff_score": round(float(data.get("max_diff") or 0.0), 3),
        "state_change": state_change,
        "package_before": package_before,
        "package_after": package_after,
        "now": now,
        "crops": {key: crops.get(key) for key in ("before", "during", "opened", "after")},
        "frames": [f for f in (crops.get("during_frame"), frame) if f],
    }
    if package_after and not package_before:
        verdict = {
            "action": "deposited",
            "item_type": "parcel",
            "confidence": 0.7,
            "source": "local",
            "evidence": "package detected in the mailbox zone after the visit, not before",
        }
        return _mailbox_result(scene, record, zone, camera_name, context, verdict)
    if package_before and not package_after:
        # Whether a package leaving is a theft or the household collecting
        # their own parcel is *not* something the camera can know, so this
        # only reports the observable fact; escalation is decided later by
        # the incident router from the arming mode (docs/ai-features.md).
        verdict = {
            "action": "retrieved",
            "item_type": "parcel",
            "confidence": 0.7,
            "source": "local",
            "evidence": "package detected in the zone before the visit and absent after it",
            "package_removed": True,
        }
        return _mailbox_result(scene, record, zone, camera_name, context, verdict)
    verifier = get_scene_verifier()
    images = [
        (label, crops[key])
        for label, key in (("BEFORE", "before"), ("DURING", "during"), ("AFTER", "after"))
        if crops.get(key)
    ]
    if (
        verifier is not None
        and crops.get("after")
        and len(images) >= 2
        and _verifier_allowed(record, now)
        and record.zone_id not in scene.pending
    ):
        data["verifier_last_at"] = now
        data["last_outcome"] = "verifying"
        _log_visit(scene, zone, data, "verifying")
        task = asyncio.create_task(_verify(verifier.verify_mailbox(images), scene.camera_id, "mailbox"))
        scene.pending[record.zone_id] = (task, context)
        return None
    return _mailbox_result(scene, record, zone, camera_name, context, None)


def _classify(context: dict, verdict: dict | None) -> tuple[str, dict]:
    """``(action, verdict)`` for a visit; the local fallback when the
    verifier is unavailable or gave nothing usable."""
    state_change = bool(context.get("state_change"))
    fallback = "opened_only" if state_change else "none"
    if verdict is None:
        return fallback, {
            "action": fallback,
            "item_type": "unknown",
            "source": "local",
            "evidence": "mailbox appearance changed during the visit" if state_change else None,
        }
    action = verdict.get("action")
    if action not in _ACTION_TRANSITION:
        action = "deposited" if verdict.get("item_deposited") == "yes" else None
    if verdict.get("source") == "foundry" and verdict.get("person_interacted") == "no" and action in (
        "deposited", "retrieved"
    ):
        action = None
    if action in (None, "none"):
        action = fallback
    return action, verdict


def _mailbox_result(
    scene: CameraScene,
    record: ZoneRecord,
    zone: Zone,
    camera_name: str,
    context: dict,
    verdict: dict | None,
) -> SceneTransition | None:
    """The one event for a visit that passed the interaction test."""
    data = record.data
    now = float(context["now"])
    action, verdict = _classify(context, verdict)
    transition = _ACTION_TRANSITION[action]
    data["last_verdict"] = {k: v for k, v in verdict.items() if k != "package_removed"}
    window = (
        settings.mailbox_dedupe_seconds
        if transition in ("mailbox_delivery", "mailbox_retrieval")
        else settings.mailbox_open_cooldown_seconds
    )
    last_emitted = data.setdefault("last_emitted", {})
    last = last_emitted.get(transition)
    if last is not None and now - float(last) < window:
        data["last_outcome"] = "deduplicated"
        _count(scene.camera_id, "mailbox_deduped")
        _log_visit(scene, zone, data, f"{transition}_deduplicated")
        return None
    last_emitted[transition] = now
    data["last_outcome"] = transition
    if transition == "mailbox_delivery":
        data["last_delivery_at"] = now
        data["last_delivery_visit"] = context["visit_id"]

    removed = bool(verdict.get("package_removed")) and settings.package_theft_detection_enabled
    item = verdict.get("item_type") or "unknown"
    tags = ["mailbox", transition]
    if item in ("parcel", "mail"):
        tags.append(item)
    if removed:
        tags.append("package_removed")
    priority = "high" if removed else _MAILBOX_PRIORITY[transition]
    noun = {"parcel": "A parcel", "mail": "Mail"}.get(item, "An item")
    where = f"the {zone.name} at {camera_name}"
    description = {
        "mailbox_delivery": f"{noun} was put in {where}.",
        "mailbox_retrieval": (
            f"A package was taken from {where}." if removed else f"{noun} was taken out of {where}."
        ),
        "mailbox_opened": f"{where[0].upper()}{where[1:]} was opened or checked; no item change was seen.",
        "mailbox_visit": f"Someone was at {where}; what they did is unclear.",
    }[transition]
    crops = context["crops"]
    labels = ("before", "during", "after")
    evidence_images = {label: crops[label] for label in labels if crops.get(label)}
    if transition == "mailbox_opened" and crops.get("opened"):
        evidence_images["opened"] = crops["opened"]
    source = verdict.get("source")
    evidence = {
        "visit_id": context["visit_id"],
        "zone": zone.name,
        "observations": context["observations"],
        "max_cover": context.get("max_cover"),
        "diff_score": context.get("diff_score"),
        "state_change": bool(context.get("state_change")),
        "action": action,
        "item_deposited": "yes" if action == "deposited" else verdict.get("item_deposited", "no"),
        "item_removed": "yes" if action == "retrieved" else "no",
        "item_type": item,
        "confidence": verdict.get("confidence"),
        "source": source,
        "evidence": verdict.get("evidence"),
        # image_url is filled in once the crops are stored as EventEvidence
        # rows (app.services.events), so it points at a retrievable image.
        "before": {"package_detected": context["package_before"], "image": "before" in evidence_images},
        "after": {"package_detected": context["package_after"], "image": "after" in evidence_images},
    }
    if "during" in evidence_images:
        evidence["during"] = {"image": True}
    if "opened" in evidence_images:
        evidence["opened"] = {"image": True}
    _count(scene.camera_id, "mailbox_events")
    _log_visit(scene, zone, data, transition, action=action, source=source)
    return SceneTransition(
        camera_id=scene.camera_id,
        kind="mailbox",
        transition=transition,
        event_type="package",
        priority=priority,
        description=description,
        tags=tags,
        metadata={
            "mailbox": evidence,
            "scene": {
                "kind": "mailbox",
                "transition": transition,
                "zone": zone.name,
                "item_type": item,
                "action": action,
                "confidence": verdict.get("confidence"),
                "source": source,
            },
        },
        zone=zone.name,
        frames=context["frames"] or None,
        evidence_images=evidence_images or None,
        dedup_key=f"{transition}:{scene.camera_id}:{record.zone_id}",
        dedup_window_seconds=float(window),
        observed_at=now,
    )


def _standalone_open(
    scene: CameraScene,
    record: ZoneRecord,
    zone: Zone,
    camera_name: str,
    crops: dict,
    frame: bytes | None,
    diff: float | None,
    now: float,
) -> SceneTransition | None:
    """The mailbox opened with nobody detected at it (missed between
    samples, out of frame, or a lid blown open)."""
    data = record.data
    _count(scene.camera_id, "mailbox_opened")
    ended = data.get("visit_ended_at")
    if ended is not None and now - float(ended) < settings.mailbox_open_cooldown_seconds:
        # Belongs to the visit that just ended, which already reported.
        logger.info(
            "mailbox %s/%s: opened (diff=%.2f) right after visit %s; not reported again",
            scene.camera_id, zone.name, diff or 0.0, data.get("visit_id"),
        )
        return None
    data.update(
        visit_id="mb-" + uuid.uuid4().hex[:12],
        observations=0,
        max_cover=0.0,
        max_diff=diff or 0.0,
        visit_lid_changed=True,
        package_before=bool(data.get("package_present")),
    )
    context = {
        "visit_id": data["visit_id"],
        "observations": 0,
        "max_cover": 0.0,
        "diff_score": round(diff or 0.0, 3),
        "state_change": True,
        "package_before": bool(data.get("package_present")),
        "package_after": bool(data.get("package_present")),
        "now": now,
        "crops": {"before": crops.get("before"), "opened": crops.get("opened")},
        "frames": [f for f in (crops.get("opened_frame"), frame) if f][:1],
    }
    verdict = {
        "action": "opened_only",
        "item_type": "unknown",
        "source": "local",
        "evidence": "mailbox appearance changed from its closed reference with nobody detected nearby",
    }
    return _mailbox_result(scene, record, zone, camera_name, context, verdict)


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
