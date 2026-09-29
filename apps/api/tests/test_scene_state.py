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


async def test_a_walk_by_is_not_a_delivery(scene):
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


async def test_a_parcel_carried_past_is_not_a_delivery(scene):
    await _zone("mailbox", MAILBOX)
    verifier = FakeVerifier(mailbox=YES)
    set_scene_verifier(verifier)
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(3, [person(), PARCEL], WITH_PERSON)
    out += await scene.run(3, [], EMPTY_MAILBOX)
    assert out == []
    assert verifier.calls == []


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

    # The parcel sitting there afterwards, and another visit within the
    # dedupe window, do not produce more deliveries.
    out = await scene.run(3, [PARCEL], EMPTY_MAILBOX)
    out += await scene.run(3, [person()], WITH_PERSON)
    out += await scene.run(3, [], EMPTY_MAILBOX)
    assert out == []


async def test_foundry_confirms_mail_the_detector_cannot_see(scene):
    await _zone("mailbox", MAILBOX)
    verifier = FakeVerifier(mailbox=YES)
    set_scene_verifier(verifier)
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(3, [person()], WITH_PERSON)
    out += await scene.run(3, [], EMPTY_MAILBOX)
    [delivery] = out
    assert verifier.calls == [("mailbox", ["BEFORE", "DURING", "AFTER"])]
    assert delivery.tags == ["mailbox", "mailbox_delivery", "mail"]
    assert delivery.metadata["scene"]["source"] == "foundry"
    assert delivery.metadata["mailbox"]["evidence"] == "envelope in slot"


@pytest.mark.parametrize(
    "answer",
    [
        {**YES, "item_deposited": "no"},
        {**YES, "item_deposited": "unknown"},
        {**YES, "person_interacted": "no"},
        None,
    ],
)
async def test_no_or_unknown_from_foundry_is_not_a_delivery(scene, answer):
    await _zone("mailbox", MAILBOX)
    set_scene_verifier(FakeVerifier(mailbox=answer))
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(3, [person()], WITH_PERSON)
    out += await scene.run(3, [], EMPTY_MAILBOX)
    assert out == []


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
        "item_type": "unknown",
        "confidence": 0.9,
        "evidence": "envelope pushed in",
    }
    assert parse_mailbox_reply("I think so") is None
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
