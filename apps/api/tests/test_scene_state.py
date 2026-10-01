"""Persistent scene state: parked vehicles, mailbox deliveries, bins.

Production: a car parked in view of ch1 emitted a vehicle event (and a
Foundry call) every ~3 minutes, and nothing could say *what changed*. These
tests drive :func:`scene_state.process_frame` with explicit timestamps and
real JPEG frames (so colour signatures and region changes come from pixels),
and check that only evidenced transitions are emitted.
"""
from __future__ import annotations

import io

import pytest
from sqlalchemy import delete, select

from app.ai.detector import BoundingBox, Detection, mock_detector
from app.ai.scene_verifier import (
    build_scene_verifier,
    parse_bin_reply,
    parse_mailbox_reply,
    set_scene_verifier,
)
from app.config import settings
from app.db import SessionLocal
from app.models.db import Activity, AIAnalysis, CameraZone, Event, VehicleTrack
from app.providers.mock import mock_provider
from app.schemas_admin import CameraZoneIn
from app.services import ingestion, scene_state
from app.services import zones as zone_service

CAMERA = "mock-driveway"
NAME = "Driveway"
T0 = 1_700_000_000.0
STEP = 5.0

BACKGROUND = (90, 110, 90)
RED = (200, 30, 30)
BLUE = (30, 40, 200)
BIN_GREEN = (20, 90, 30)

CAR = BoundingBox(0.55, 0.40, 0.95, 0.84)
OTHER_SPOT = BoundingBox(0.05, 0.45, 0.35, 0.80)
PERSON_AT_MAILBOX = BoundingBox(0.05, 0.20, 0.35, 0.95)
MAILBOX = (0.10, 0.30, 0.30, 0.60)
BIN_ZONE = (0.60, 0.50, 0.80, 0.90)


def _frame(*shapes: tuple[BoundingBox, tuple[int, int, int]], size=(640, 360)) -> bytes:
    from PIL import Image, ImageDraw

    width, height = size
    image = Image.new("RGB", size, BACKGROUND)
    draw = ImageDraw.Draw(image)
    # Texture so region signatures of the empty scene are not flat.
    for x in range(0, width, 16):
        draw.line((x, 0, x, height), fill=(100, 120, 100))
    for box, colour in shapes:
        draw.rectangle((box.x1 * width, box.y1 * height, box.x2 * width, box.y2 * height), fill=colour)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def car(box: BoundingBox = CAR, confidence: float = 0.9, label: str = "car") -> Detection:
    return Detection(label, confidence, box)


def jitter(box: BoundingBox, dx: float) -> BoundingBox:
    return BoundingBox(box.x1 + dx, box.y1 - dx, box.x2 + dx, box.y2 - dx)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def tick(self, seconds: float = STEP) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
async def scene(client):
    """Feed frames at an explicit clock; collect transitions."""
    clock = Clock()

    async def step(detections=(), frame: bytes | None = None, advance: float = STEP):
        clock.tick(advance)
        async with SessionLocal() as session:
            return await scene_state.process_frame(
                session, CAMERA, NAME, frame if frame is not None else _frame(), list(detections), now=clock.now
            )

    async def run(n: int, detections=(), frame: bytes | None = None, advance: float = STEP):
        out = []
        for _ in range(n):
            out += await step(detections, frame, advance)
        return out

    async def state():
        async with SessionLocal() as session:
            return await scene_state.snapshot(session, CAMERA)

    class _S:
        pass

    s = _S()
    s.clock, s.step, s.run, s.state = clock, step, run, state
    yield s
    async with SessionLocal() as session:
        await session.execute(delete(CameraZone).where(CameraZone.camera_id == CAMERA))
        await session.commit()
    scene_state.invalidate_zone_cache()


def _kinds(transitions) -> list[str]:
    return [t.transition for t in transitions]


# --- vehicles -------------------------------------------------------------------


async def test_a_jittering_parked_car_is_reported_once_then_parked(scene):
    frame = _frame((CAR, RED))
    out = []
    for i in range(12):
        box = jitter(CAR, 0.006 if i % 2 else -0.004)
        partial = car(BoundingBox(0.58, 0.45, 0.80, 0.80), 0.55)
        out += await scene.step([car(box), partial] if i % 3 == 0 else [car(box)], frame)

    assert _kinds(out) == ["first_seen"]  # already there when watching began
    [transition] = out
    assert transition.event_type == "vehicle"
    assert "vehicle_arrived" not in transition.tags
    assert transition.metadata["vehicle_track"]["observation_count"] == 2
    [track] = (await scene.state())["vehicles"]
    assert track["state"] == "stable"
    assert track["observation_count"] == 12
    assert track["stationary_since"] is not None


async def test_passing_traffic_seen_once_is_not_reported(scene):
    out = await scene.run(1, [car(OTHER_SPOT)], _frame((OTHER_SPOT, BLUE)))
    out += await scene.run(40, [])
    assert out == []
    assert (await scene.state())["vehicles"] == []


async def test_an_arrival_after_watching_an_empty_scene_is_arrived(scene):
    await scene.run(40, [])  # 200s of an empty driveway
    out = await scene.run(3, [car()], _frame((CAR, RED)))
    assert _kinds(out) == ["arrived"]
    assert out[0].tags == ["car", "vehicle_arrived"]
    assert out[0].metadata["scene"]["transition"] == "arrived"


async def test_a_parked_car_that_moves_emits_moved_and_restarts_the_count(scene):
    await scene.run(6, [car()], _frame((CAR, RED)))
    moved_box = BoundingBox(0.40, 0.40, 0.80, 0.84)
    out = await scene.run(1, [car(moved_box)], _frame((moved_box, RED)))
    assert _kinds(out) == ["moved"]
    assert "vehicle_moved" in out[0].tags
    [track] = (await scene.state())["vehicles"]
    assert track["state"] == "tracking" and track["observation_count"] == 1

    # Parks again in the new spot: no further events.
    assert await scene.run(8, [car(moved_box)], _frame((moved_box, RED))) == []
    [track] = (await scene.state())["vehicles"]
    assert track["state"] == "stable"


async def test_departure_then_return_of_the_same_car(scene):
    await scene.run(40, [])
    first = await scene.run(6, [car()], _frame((CAR, RED)))
    assert _kinds(first) == ["arrived"]

    gone = await scene.run(40, [])  # 200s of frames without it
    assert _kinds(gone) == ["departed"]
    assert gone[0].tags == ["car", "vehicle_departed"]

    back = await scene.run(6, [car()], _frame((CAR, RED)))
    assert _kinds(back) == ["returned"]
    assert back[0].tags == ["car", "vehicle_arrived", "vehicle_returned"]
    assert back[0].metadata["scene"]["previous_track_id"] == first[0].track_id
    # A fresh five-observation sequence: parked again, quietly.
    [track] = [v for v in (await scene.state())["vehicles"] if v["state"] != "departed"]
    assert track["state"] == "stable"


async def test_a_different_looking_car_in_the_same_spot_is_an_arrival(scene):
    await scene.run(40, [])
    await scene.run(6, [car()], _frame((CAR, RED)))
    await scene.run(40, [])
    out = await scene.run(3, [car()], _frame((CAR, BLUE)))
    assert _kinds(out) == ["arrived"]


async def test_two_cars_are_tracked_independently(scene):
    await scene.run(40, [])
    both = _frame((CAR, RED), (OTHER_SPOT, BLUE))
    out = await scene.run(6, [car(), car(OTHER_SPOT)], both)
    assert sorted(_kinds(out)) == ["arrived", "arrived"]
    assert len({t.track_id for t in out}) == 2

    # The blue car leaves; the red car stays parked.
    out = await scene.run(40, [car()], _frame((CAR, RED)))
    assert _kinds(out) == ["departed"]
    assert out[0].metadata["vehicle_track"]["box"]["x1"] == pytest.approx(OTHER_SPOT.x1)
    states = sorted(v["state"] for v in (await scene.state())["vehicles"])
    assert states == ["departed", "stable"]


async def test_tracks_survive_a_restart(scene):
    await scene.run(6, [car()], _frame((CAR, RED)))
    async with SessionLocal() as session:
        [row] = (await session.execute(select(VehicleTrack).where(VehicleTrack.camera_id == CAMERA))).scalars()
        assert row.state == "stable" and row.observation_count == 6

    scene_state.reset_memory()  # process restart: only the DB remains
    out = await scene.run(5, [car()], _frame((CAR, RED)), advance=STEP)
    assert out == []
    [track] = (await scene.state())["vehicles"]
    assert track["observation_count"] == 11 and track["state"] == "stable"


async def test_a_camera_outage_is_not_a_departure(scene):
    await scene.run(6, [car()], _frame((CAR, RED)))
    # Camera dark for an hour, then the stream is back but the detector
    # misses the car for a couple of frames: no departure yet.
    out = await scene.step([], advance=3600)
    out += await scene.run(5, [])
    assert out == []
    # And when it is found again it simply continues.
    out = await scene.run(2, [car()], _frame((CAR, RED)))
    assert out == []


async def test_a_person_at_the_parked_car_is_an_interaction(scene):
    await scene.run(6, [car()], _frame((CAR, RED)))
    person = Detection("person", 0.8, BoundingBox(0.60, 0.35, 0.70, 0.85))
    out = await scene.run(2, [car(), person], _frame((CAR, RED)))
    assert _kinds(out) == ["interaction"]
    assert "vehicle_interaction" in out[0].tags
    # Cooldown: the same person lingering does not repeat it.
    assert await scene.run(4, [car(), person], _frame((CAR, RED))) == []


async def test_disabled_vehicle_tracking_emits_nothing(scene, monkeypatch):
    monkeypatch.setattr(settings, "vehicle_tracking_enabled", False)
    assert await scene.run(6, [car()], _frame((CAR, RED))) == []


# --- mailbox ---------------------------------------------------------------------


async def _zone(kind: str, coords: tuple[float, float, float, float], name: str | None = None) -> None:
    x1, y1, x2, y2 = coords
    async with SessionLocal() as session:
        await zone_service.create_zone(
            session, CAMERA, CameraZoneIn(name=name or kind, kind=kind, x1=x1, y1=y1, x2=x2, y2=y2)
        )


class FakeVerifier:
    name = "fake"

    def __init__(self, mailbox: dict | None = None, bins: list[dict] | None = None) -> None:
        self.mailbox = mailbox
        self.bins = list(bins or [])
        self.calls: list[tuple[str, list[str]]] = []

    async def verify_mailbox(self, images):
        self.calls.append(("mailbox", [label for label, _ in images]))
        return self.mailbox

    async def verify_bin(self, images):
        self.calls.append(("bin", [label for label, _ in images]))
        return self.bins.pop(0) if self.bins else None


def person(box: BoundingBox = PERSON_AT_MAILBOX) -> Detection:
    return Detection("person", 0.85, box)


PARCEL = Detection("package", 0.6, BoundingBox(0.14, 0.40, 0.26, 0.55))
MAILBOX_BOX = BoundingBox(*MAILBOX)
WITH_PERSON = _frame((MAILBOX_BOX, (60, 60, 60)), (PERSON_AT_MAILBOX, RED))
EMPTY_MAILBOX = _frame((MAILBOX_BOX, (60, 60, 60)))
YES = {"person_interacted": "yes", "item_deposited": "yes", "item_type": "mail", "confidence": 0.8, "evidence": "envelope in slot"}


LID = BoundingBox(0.10, 0.22, 0.30, 0.40)
OPENED = _frame((MAILBOX_BOX, (60, 60, 60)), (LID, (190, 190, 170)))
FAR_PERSON = BoundingBox(0.60, 0.20, 0.75, 0.95)
WITH_FAR_PERSON = _frame((MAILBOX_BOX, (60, 60, 60)), (FAR_PERSON, RED))


def _relit(frame: bytes, factor: float) -> bytes:
    from PIL import Image, ImageEnhance

    image = ImageEnhance.Brightness(Image.open(io.BytesIO(frame))).enhance(factor)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def _ir_frame() -> bytes:
    """Night/IR: the whole picture's texture changes, mailbox included."""
    from PIL import Image, ImageDraw

    width, height = 640, 360
    image = Image.new("RGB", (width, height), (140, 140, 140))
    draw = ImageDraw.Draw(image)
    for y in range(0, height, 12):
        draw.line((0, y, width, y), fill=(170, 170, 170))
    draw.rectangle((0.1 * width, 0.3 * height, 0.3 * width, 0.6 * height), fill=(200, 200, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def _stats() -> dict[str, int]:
    return scene_state.drain_mailbox_stats(CAMERA)


async def test_a_walk_by_away_from_the_mailbox_does_not_trigger(scene):
    await _zone("mailbox", MAILBOX)
    verifier = FakeVerifier(mailbox=YES)
    set_scene_verifier(verifier)
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(3, [person(FAR_PERSON)], WITH_FAR_PERSON)
    out += await scene.run(3, [], EMPTY_MAILBOX)
    assert out == []
    assert verifier.calls == []
    stats = _stats()
    assert stats["mailbox_visits"] == 0 and stats["mailbox_events"] == 0


async def test_a_brief_pass_with_nothing_changed_is_a_walk_by_when_two_frames_are_required(scene, monkeypatch):
    monkeypatch.setattr(settings, "mailbox_min_observations", 2)
    await _zone("mailbox", MAILBOX)
    verifier = FakeVerifier(mailbox=YES)
    set_scene_verifier(verifier)
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(1, [person()], WITH_PERSON)
    out += await scene.run(3, [], EMPTY_MAILBOX)
    assert out == []
    assert verifier.calls == []
    [zone] = (await scene.state())["zones"]
    assert zone["data"]["last_outcome"] == "walk_by"
    assert _stats()["mailbox_walk_by"] == 1


async def test_a_single_frame_at_the_mailbox_is_one_low_priority_visit(scene):
    await _zone("mailbox", MAILBOX, "Mailbox")
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(1, [person()], WITH_PERSON)
    out += await scene.run(3, [], EMPTY_MAILBOX)
    [visit] = out
    assert visit.transition == "mailbox_visit"
    assert visit.priority == "low"
    assert visit.tags == ["mailbox", "mailbox_visit"]
    assert visit.dedup_key.startswith("mailbox_visit:")
    assert "unclear" in visit.description
    assert visit.metadata["mailbox"]["observations"] == 1
    assert visit.metadata["mailbox"]["action"] == "none"


async def test_a_single_frame_drop_with_the_lid_opening_is_a_delivery(scene, monkeypatch):
    # Production cadence (5-23s) sees a 3-6s mail drop in at most one frame.
    monkeypatch.setattr(settings, "mailbox_min_observations", 2)
    await _zone("mailbox", MAILBOX)
    verifier = FakeVerifier(mailbox=YES)
    set_scene_verifier(verifier)
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(1, [person()], WITH_PERSON)
    out += await scene.run(2, [], OPENED)  # the flap was left open
    out += await scene.run(1, [], OPENED)  # Foundry answer picked up
    out += await scene.run(3, [], EMPTY_MAILBOX)
    [delivery] = out
    assert delivery.transition == "mailbox_delivery"
    assert verifier.calls == [("mailbox", ["BEFORE", "DURING", "AFTER"])]
    assert delivery.metadata["mailbox"]["state_change"] is True
    assert delivery.metadata["mailbox"]["diff_score"] >= settings.mailbox_open_threshold


async def test_a_single_frame_drop_with_a_visible_parcel_is_a_local_delivery(scene, monkeypatch):
    monkeypatch.setattr(settings, "mailbox_min_observations", 2)
    await _zone("mailbox", MAILBOX)
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(1, [person()], WITH_PERSON)
    out += await scene.run(3, [PARCEL], EMPTY_MAILBOX)
    [delivery] = out
    assert delivery.transition == "mailbox_delivery"
    assert delivery.priority == "normal"
    assert delivery.metadata["mailbox"]["source"] == "local"


async def test_a_lid_change_without_a_verifier_is_reported_as_opened(scene, monkeypatch):
    monkeypatch.setattr(settings, "mailbox_min_observations", 2)
    await _zone("mailbox", MAILBOX)
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(1, [person()], WITH_PERSON)
    out += await scene.run(2, [], OPENED)
    out += await scene.run(3, [], EMPTY_MAILBOX)
    [opened] = out
    assert opened.transition == "mailbox_opened"
    assert opened.metadata["mailbox"]["action"] == "opened_only"
    assert opened.metadata["mailbox"]["state_change"] is True


async def test_a_parcel_taken_from_the_mailbox_is_a_retrieval(scene):
    await _zone("mailbox", MAILBOX, "Mailbox")
    await scene.run(3, [PARCEL], EMPTY_MAILBOX)
    out = await scene.run(1, [person()], WITH_PERSON)
    out += await scene.run(3, [], EMPTY_MAILBOX)
    [retrieval] = out
    assert retrieval.transition == "mailbox_retrieval"
    assert retrieval.tags == ["mailbox", "mailbox_retrieval", "parcel", "package_removed"]
    assert retrieval.priority == "high"
    evidence = retrieval.metadata["mailbox"]
    assert evidence["before"]["package_detected"] is True
    assert evidence["after"]["package_detected"] is False
    assert evidence["item_removed"] == "yes"
    assert retrieval.description == "A package was taken from the Mailbox at Driveway."


async def test_foundry_reports_mail_taken_out(scene):
    await _zone("mailbox", MAILBOX)
    set_scene_verifier(FakeVerifier(mailbox={**YES, "item_deposited": "no", "action": "retrieved"}))
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(2, [person()], WITH_PERSON)
    out += await scene.run(4, [], EMPTY_MAILBOX)
    [retrieval] = out
    assert retrieval.transition == "mailbox_retrieval"
    assert retrieval.tags == ["mailbox", "mailbox_retrieval", "mail"]
    assert retrieval.priority == "normal"
    assert retrieval.metadata["mailbox"]["source"] == "foundry"
    assert "Mail was taken out of" in retrieval.description


async def test_the_mailbox_opening_with_nobody_there_is_one_opened_event(scene):
    await _zone("mailbox", MAILBOX, "Mailbox")
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(4, [], OPENED)
    [opened] = out
    assert opened.transition == "mailbox_opened"
    assert opened.priority == "normal"
    assert set(opened.evidence_images) >= {"before", "opened"}
    assert opened.metadata["mailbox"]["observations"] == 0
    assert opened.metadata["mailbox"]["opened"] == {"image": True}
    assert opened.description.startswith("The Mailbox at Driveway was opened")
    [zone] = (await scene.state())["zones"]
    assert zone["data"]["lid"] == "open"

    # Closing, and opening again within the cooldown: still one event.
    out = await scene.run(2, [], EMPTY_MAILBOX)
    [zone] = (await scene.state())["zones"]
    assert zone["data"]["lid"] == "closed"
    out += await scene.run(3, [], OPENED)
    assert out == []
    stats = _stats()
    assert stats["mailbox_opened"] == 2 and stats["mailbox_deduped"] == 1 and stats["mailbox_events"] == 1


async def test_one_changed_frame_with_nobody_there_is_not_an_opening(scene):
    await _zone("mailbox", MAILBOX)
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(1, [], OPENED)
    out += await scene.run(3, [], EMPTY_MAILBOX)
    assert out == []


async def test_lighting_and_ir_changes_are_not_openings(scene):
    await _zone("mailbox", MAILBOX)
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(3, [], _relit(EMPTY_MAILBOX, 1.3))
    out += await scene.run(3, [], _relit(EMPTY_MAILBOX, 0.5))
    out += await scene.run(3, [], _ir_frame())
    assert out == []
    [zone] = (await scene.state())["zones"]
    assert zone["data"]["lid"] == "closed"
    # Back to daylight (re-bases again) - and a real opening is still caught.
    assert await scene.run(3, [], EMPTY_MAILBOX) == []
    assert _kinds(await scene.run(2, [], _relit(OPENED, 0.8))) == ["mailbox_opened"]


async def test_open_detection_can_be_disabled(scene, monkeypatch):
    monkeypatch.setattr(settings, "mailbox_open_detection_enabled", False)
    await _zone("mailbox", MAILBOX)
    await scene.run(3, [], EMPTY_MAILBOX)
    assert await scene.run(4, [], OPENED) == []


async def test_a_parcel_carried_past_is_a_visit_not_a_delivery(scene):
    await _zone("mailbox", MAILBOX)
    verifier = FakeVerifier(mailbox={**YES, "item_deposited": "no", "action": "none"})
    set_scene_verifier(verifier)
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(3, [person(), PARCEL], WITH_PERSON)
    out += await scene.run(4, [], EMPTY_MAILBOX)
    [visit] = out
    assert visit.transition == "mailbox_visit"
    assert visit.priority == "low"


async def test_a_locally_seen_parcel_left_at_the_mailbox_is_one_delivery(scene):
    await _zone("mailbox", MAILBOX, "Mailbox")
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(3, [person()], WITH_PERSON)
    out += await scene.run(3, [PARCEL], EMPTY_MAILBOX)
    [delivery] = out
    assert delivery.event_type == "package"
    assert delivery.tags == ["mailbox", "mailbox_delivery", "parcel"]
    assert delivery.zone == "Mailbox"
    evidence = delivery.metadata["mailbox"]
    assert evidence["source"] == "local"
    assert evidence["before"]["package_detected"] is False
    assert evidence["after"]["package_detected"] is True

    # The parcel sitting there afterwards produces nothing; another visit
    # that leaves it in place is a visit, not another delivery.
    out = await scene.run(3, [PARCEL], EMPTY_MAILBOX)
    assert out == []
    out = await scene.run(3, [person()], WITH_PERSON)
    out += await scene.run(3, [PARCEL], EMPTY_MAILBOX)
    assert _kinds(out) == ["mailbox_visit"]


async def test_a_second_delivery_within_the_dedupe_window_is_suppressed(scene, monkeypatch):
    monkeypatch.setattr(settings, "scene_verifier_min_interval_seconds", 0)
    await _zone("mailbox", MAILBOX)
    set_scene_verifier(FakeVerifier(mailbox=YES))
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(2, [person()], WITH_PERSON)
    out += await scene.run(4, [], EMPTY_MAILBOX)
    assert _kinds(out) == ["mailbox_delivery"]
    out = await scene.run(2, [person()], WITH_PERSON)
    out += await scene.run(4, [], EMPTY_MAILBOX)
    assert out == []
    [zone] = (await scene.state())["zones"]
    assert zone["data"]["last_outcome"] == "deduplicated"


async def test_foundry_found_the_mailbox_opened_only(scene):
    await _zone("mailbox", MAILBOX)
    set_scene_verifier(FakeVerifier(mailbox={**YES, "item_deposited": "no", "action": "opened_only"}))
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(2, [person()], WITH_PERSON)
    out += await scene.run(4, [], EMPTY_MAILBOX)
    assert _kinds(out) == ["mailbox_opened"]


@pytest.mark.parametrize(
    "answer",
    [
        {**YES, "item_deposited": "no"},
        {**YES, "item_deposited": "unknown"},
        {**YES, "person_interacted": "no"},
        {**YES, "action": "none"},
        None,
    ],
)
async def test_no_or_unknown_from_foundry_is_a_visit_not_a_delivery(scene, answer):
    await _zone("mailbox", MAILBOX)
    set_scene_verifier(FakeVerifier(mailbox=answer))
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(3, [person()], WITH_PERSON)
    out += await scene.run(4, [], EMPTY_MAILBOX)
    assert _kinds(out) == ["mailbox_visit"]


async def test_mailbox_detection_is_off_without_a_mailbox_zone(scene):
    verifier = FakeVerifier(mailbox=YES)
    set_scene_verifier(verifier)
    out = await scene.run(3, [person()], WITH_PERSON)
    out += await scene.run(3, [PARCEL], EMPTY_MAILBOX)
    assert out == [] and verifier.calls == []


# --- bins ---------------------------------------------------------------------------


BIN = BoundingBox(0.64, 0.55, 0.76, 0.88)
TIPPED_BIN = BoundingBox(0.61, 0.70, 0.79, 0.86)
NO_BIN = _frame()
WITH_BIN = _frame((BIN, BIN_GREEN))
WITH_TIPPED_BIN = _frame((TIPPED_BIN, BIN_GREEN))
TRUCK = Detection("truck", 0.8, BoundingBox(0.20, 0.30, 0.62, 0.95))
BIN_STEP = 20.0


def bin_answer(before: str, now: str, moved: str = "unknown", vehicle: str = "unknown") -> dict:
    return {
        "bin_present_before": before,
        "bin_present_now": now,
        "bin_moved_or_tipped": moved,
        "collection_vehicle_visible": vehicle,
        "confidence": 0.8,
        "evidence": "green wheelie bin at curb",
    }


async def _bin_out(scene, verifier) -> list:
    await _zone("bin", BIN_ZONE, "Curb")
    set_scene_verifier(verifier)
    await scene.run(2, [], NO_BIN, advance=BIN_STEP)
    return await scene.run(4, [], WITH_BIN, advance=BIN_STEP)


async def test_a_bin_put_out_is_reported_once(scene):
    verifier = FakeVerifier(bins=[bin_answer("no", "yes")])
    out = await _bin_out(scene, verifier)
    [placed] = out
    assert placed.tags == ["bin", "bin_placed_out"]
    assert placed.event_type == "motion"
    assert placed.metadata["bin"]["before"] == "absent"
    assert placed.metadata["bin"]["now"] == "present"
    assert verifier.calls == [("bin", ["BEFORE", "NOW"])]
    # Staying out produces nothing more.
    assert await scene.run(6, [], WITH_BIN, advance=BIN_STEP) == []
    [zone] = (await scene.state())["zones"]
    assert zone["state"] == "present"


async def test_a_bin_emptied_by_a_collection_vehicle(scene):
    verifier = FakeVerifier(bins=[bin_answer("no", "yes"), bin_answer("yes", "yes", moved="yes", vehicle="no")])
    assert _kinds(await _bin_out(scene, verifier)) == ["bin_placed_out"]
    # The collection truck stops beside the bin; afterwards it stands differently.
    out = await scene.run(2, [TRUCK], _frame((BIN, BIN_GREEN), (TRUCK.bbox, (200, 200, 200))), advance=BIN_STEP)
    out += await scene.run(4, [], WITH_TIPPED_BIN, advance=BIN_STEP)
    [emptied] = [t for t in out if t.kind == "bin"]
    assert emptied.tags == ["bin", "bin_emptied"]
    assert emptied.metadata["bin"]["interaction"] == "collection_vehicle"


async def test_a_bin_that_disappears_is_not_emptied(scene):
    verifier = FakeVerifier(bins=[bin_answer("no", "yes"), bin_answer("yes", "no")])
    await _bin_out(scene, verifier)
    out = await scene.run(6, [], NO_BIN, advance=BIN_STEP)
    assert out == []
    [zone] = (await scene.state())["zones"]
    assert zone["state"] == "absent"


async def test_a_camera_outage_never_produces_emptied(scene, monkeypatch):
    # Even with the explicit "removal counts as emptied" rule and a truck
    # seen before the outage, a change across an outage has no timeline.
    monkeypatch.setattr(settings, "bin_removal_counts_as_emptied", True)
    verifier = FakeVerifier(bins=[bin_answer("no", "yes"), bin_answer("yes", "no"), bin_answer("yes", "yes", moved="yes")])
    await _bin_out(scene, verifier)
    await scene.run(1, [TRUCK], WITH_BIN, advance=BIN_STEP)
    out = await scene.step([], NO_BIN, advance=600)  # camera dark for 10 minutes
    out += await scene.run(5, [], NO_BIN, advance=BIN_STEP)
    assert out == []


async def test_removal_rule_needs_a_collection_vehicle(scene, monkeypatch):
    monkeypatch.setattr(settings, "bin_removal_counts_as_emptied", True)
    verifier = FakeVerifier(bins=[bin_answer("no", "yes"), bin_answer("yes", "no")])
    await _bin_out(scene, verifier)
    assert await scene.run(5, [], NO_BIN, advance=BIN_STEP) == []


async def test_removal_rule_with_a_collection_vehicle_counts(scene, monkeypatch):
    monkeypatch.setattr(settings, "bin_removal_counts_as_emptied", True)
    verifier = FakeVerifier(bins=[bin_answer("no", "yes"), bin_answer("yes", "no")])
    await _bin_out(scene, verifier)
    await scene.run(1, [TRUCK], WITH_BIN, advance=BIN_STEP)
    out = await scene.run(5, [], NO_BIN, advance=BIN_STEP)
    assert _kinds(out) == ["bin_emptied"]
    assert out[0].metadata["bin"]["rule"] == "bin_removal_counts_as_emptied"


async def test_without_local_evidence_or_foundry_bins_stay_unknown(scene):
    out = await _bin_out(scene, None)
    assert out == []
    [zone] = (await scene.state())["zones"]
    assert zone["state"] == "unknown"


async def test_a_person_standing_in_front_of_the_bin_is_not_a_change(scene):
    verifier = FakeVerifier(bins=[bin_answer("no", "yes")])
    await _bin_out(scene, verifier)
    blocker = Detection("person", 0.9, BoundingBox(0.58, 0.40, 0.82, 0.95))
    out = await scene.run(6, [blocker], _frame((blocker.bbox, RED)), advance=BIN_STEP)
    assert out == []
    assert verifier.calls == [("bin", ["BEFORE", "NOW"])]


# --- verifier replies -------------------------------------------------------------


def test_mailbox_reply_is_normalized_to_closed_answers():
    reply = parse_mailbox_reply(
        '```json\n{"person_interacted": "Yes", "item_deposited": true, "item_type": "letter",'
        ' "confidence": 0.9, "evidence": "  envelope  pushed in  "}\n```'
    )
    assert reply == {
        "person_interacted": "yes",
        "item_deposited": "yes",
        "action": "unknown",
        "item_type": "unknown",
        "confidence": 0.9,
        "evidence": "envelope pushed in",
    }
    assert parse_mailbox_reply("I think so") is None
    assert parse_mailbox_reply('{"action": "Opened only"}')["action"] == "opened_only"
    assert parse_mailbox_reply('{"action": "RETRIEVED"}')["action"] == "retrieved"
    assert parse_mailbox_reply('{"action": "stole it"}')["action"] == "unknown"
    assert parse_mailbox_reply('{"item_deposited": "probably"}')["item_deposited"] == "unknown"


def test_bin_reply_is_normalized_to_closed_answers():
    reply = parse_bin_reply('{"bin_present_before": "no", "bin_present_now": "YES", "bin_moved_or_tipped": "maybe"}')
    assert reply["bin_present_before"] == "no"
    assert reply["bin_present_now"] == "yes"
    assert reply["bin_moved_or_tipped"] == "unknown"
    assert reply["collection_vehicle_visible"] == "unknown"


def test_scene_verifier_needs_foundry_and_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "foundry_endpoint", None)
    assert build_scene_verifier(settings) is None
    monkeypatch.setattr(settings, "foundry_endpoint", "https://example.invalid")
    monkeypatch.setattr(settings, "foundry_api_key", "key")
    assert build_scene_verifier(settings) is not None
    monkeypatch.setattr(settings, "scene_verifier_enabled", False)
    assert build_scene_verifier(settings) is None


# --- ingestion end to end ------------------------------------------------------------


@pytest.fixture
async def live(client, monkeypatch):
    ingestion.reset_cooldowns()
    monkeypatch.setattr(settings, "event_cooldown_seconds", 0.0)
    monkeypatch.setattr(settings, "event_poll_interval_seconds", 0.0)
    frames = {"current": EMPTY_MAILBOX}

    async def snapshot(camera_id: str) -> bytes:
        return frames["current"]

    monkeypatch.setattr(mock_provider, "get_snapshot", snapshot)

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(AIAnalysis))
            await session.execute(delete(Activity))
            await session.execute(delete(Event))
            await session.execute(delete(CameraZone).where(CameraZone.camera_id == CAMERA))
            await session.commit()
        scene_state.invalidate_zone_cache()

    await _clear()
    yield frames
    await _clear()
    ingestion.reset_cooldowns()


async def _camera_events(client) -> list[dict]:
    return [e for e in (await client.get("/api/v1/events")).json() if e["camera_id"] == CAMERA]


async def test_ingestion_emits_a_mailbox_delivery_event_that_enrichment_keeps(client, live):
    await _zone("mailbox", MAILBOX, "Mailbox")
    set_scene_verifier(FakeVerifier(mailbox=YES))
    for frame, detections in (
        (EMPTY_MAILBOX, []),
        (WITH_PERSON, [person()]),
        (WITH_PERSON, [person()]),
        (EMPTY_MAILBOX, []),
        (EMPTY_MAILBOX, []),
        (EMPTY_MAILBOX, []),  # the Foundry answer is picked up off the frame path
    ):
        live["current"] = frame
        mock_detector().set_script(CAMERA, detections)
        await ingestion.poll_once()

    deliveries = [e for e in await _camera_events(client) if "mailbox_delivery" in (e["tags"] or [])]
    [delivery] = deliveries
    assert delivery["type"] == "package"
    assert delivery["zone"] == "Mailbox"
    assert {"mailbox", "mailbox_delivery", "mail"} <= set(delivery["tags"])
    assert delivery["scene"]["transition"] == "mailbox_delivery"
    assert delivery["metadata"]["frame_source"] == "snapshot"
    assert delivery["description"].startswith("Mail was put in the Mailbox")


async def test_ingestion_reports_a_parked_car_once(client, live):
    live["current"] = _frame((CAR, RED))
    mock_detector().set_script(CAMERA, [car()])
    for _ in range(8):
        await ingestion.poll_once()

    vehicles = [e for e in await _camera_events(client) if e["type"] == "vehicle"]
    [event] = vehicles
    assert event["scene"]["kind"] == "vehicle"
    assert event["metadata"]["vehicle_track"]["track_id"]
    assert "vehicle_parked" in event["tags"]


# --- review follow-ups: latency and robustness ------------------------------------


class SlowVerifier(FakeVerifier):
    def __init__(self, gate, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gate = gate

    async def verify_mailbox(self, images):
        await self.gate.wait()
        return await super().verify_mailbox(images)


async def test_a_slow_foundry_check_does_not_hold_up_frames(scene):
    import asyncio
    import time as _time

    await _zone("mailbox", MAILBOX)
    gate = asyncio.Event()
    set_scene_verifier(SlowVerifier(gate, mailbox=YES))
    await scene.run(3, [], EMPTY_MAILBOX)
    await scene.run(3, [person()], WITH_PERSON)
    started = _time.monotonic()
    out = await scene.run(4, [], EMPTY_MAILBOX)  # visit ends; check in flight
    assert _time.monotonic() - started < 2.0
    assert out == []
    [zone] = (await scene.state())["zones"]
    assert zone["data"]["last_outcome"] == "verifying"

    gate.set()
    await asyncio.sleep(0)
    out = await scene.run(1, [], EMPTY_MAILBOX)
    assert _kinds(out) == ["mailbox_delivery"]


async def test_person_events_are_emitted_before_scene_checks(client, live, monkeypatch):
    order: list[str] = []
    real_subjects = ingestion._emit_subject_events
    real_scene = ingestion._emit_scene_transitions

    async def subjects(*args, **kwargs):
        order.append("subjects")
        return await real_subjects(*args, **kwargs)

    async def scene_step(*args, **kwargs):
        order.append("scene")
        return await real_scene(*args, **kwargs)

    monkeypatch.setattr(ingestion, "_emit_subject_events", subjects)
    monkeypatch.setattr(ingestion, "_emit_scene_transitions", scene_step)
    live["current"] = WITH_PERSON
    mock_detector().set_script(CAMERA, [person()])
    await ingestion.poll_once()
    assert order[:2] == ["subjects", "scene"]


async def test_unusable_persisted_state_resets_the_camera(scene):
    await scene.run(6, [car()], _frame((CAR, RED)))
    async with SessionLocal() as session:
        [row] = (await session.execute(select(VehicleTrack).where(VehicleTrack.camera_id == CAMERA))).scalars()
        row.data = ["not", "a", "dict"]  # e.g. hand-edited or from a bad migration
        await session.commit()
    scene_state.reset_memory()

    assert await scene.run(1, [car()], _frame((CAR, RED))) == []
    # Reset, not stuck: the next frames rebuild state from scratch.
    await scene.run(3, [car()], _frame((CAR, RED)))
    [track] = (await scene.state())["vehicles"]
    assert track["state"] == "tracking"


async def test_a_track_that_was_never_announced_never_departs_loudly(scene):
    await scene.run(40, [])
    far = BoundingBox(0.02, 0.02, 0.08, 0.06)
    out = await scene.run(1, [car(far, 0.75)], _frame((far, BLUE)))
    out += await scene.run(60, [])
    assert out == []


async def test_failed_enrichment_does_not_abort_the_poll(client, live, monkeypatch):
    from app.services import ai_pipeline

    async def broken(*args, **kwargs):
        raise TypeError("analysis blew up")

    monkeypatch.setattr(ai_pipeline, "enrich_event", broken)
    live["current"] = WITH_PERSON
    mock_detector().set_script(CAMERA, [person()])
    await ingestion.poll_once()  # must not raise
    async with SessionLocal() as session:
        rows = (await session.execute(select(Event).where(Event.camera_id == CAMERA))).scalars().all()
    assert [r.type for r in rows] == ["person"]
