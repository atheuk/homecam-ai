"""Temporal scene engine: parked vehicles, mailbox deliveries, garbage bins.

Every test drives the real ingestion loop (``ingestion.poll_once``) with
real JPEG frames and a scripted detector. The vehicle tracker, the mailbox
and bin watchers, event creation, enrichment, evidence storage and
persistence are all exercised as they run in production. Only the Foundry
vision check is replaced by a scripted verifier, so tests can check both
how often it is asked and what happens with each answer.
"""
from __future__ import annotations

import io
import random

import pytest
from sqlalchemy import delete

from app.ai.detector import BoundingBox, Detection, mock_detector
from app.ai.temporal_vision import (
    BinVerdict,
    MailboxVerdict,
    parse_bins_reply,
    parse_mailbox_reply,
    set_temporal_verifier,
)
from app.config import settings
from app.db import SessionLocal
from app.models.db import Activity, AIAnalysis, CameraZone, Event, EventEvidence, SceneState
from app.providers.base import CameraOfflineError
from app.providers.mock import mock_provider
from app.services import ingestion
from app.services.scene_state import scene_engine, signature_distance, zone_signature

from test_ai_pipeline import _add_zone
from test_subject_selection import CAR, CAR_RGB, _scene

CAMERA = "mock-front-door"

CAR2 = Detection("car", 0.9, BoundingBox(0.05, 0.45, 0.40, 0.85))
PERSON_AT_CAR = Detection("person", 0.8, BoundingBox(0.50, 0.40, 0.62, 0.90))
FAR_CAR = Detection("car", 0.72, BoundingBox(0.13, 0.00, 0.22, 0.08))

MAILBOX = {"name": "mailbox", "kind": "mailbox", "x1": 0.7, "y1": 0.3, "x2": 0.95, "y2": 0.7}
AT_MAILBOX = Detection("person", 0.85, BoundingBox(0.72, 0.30, 0.90, 0.90))
PASSER_BY = Detection("person", 0.85, BoundingBox(0.05, 0.30, 0.20, 0.90))
PARCEL_AT_MAILBOX = Detection("package", 0.7, BoundingBox(0.78, 0.55, 0.86, 0.68))

BINS = {"name": "kerb", "kind": "bins", "x1": 0.55, "y1": 0.45, "x2": 0.95, "y2": 0.95}
TRUCK_AT_BINS = Detection("truck", 0.9, BoundingBox(0.45, 0.30, 1.00, 1.00))


class Camera:
    """The mock camera, serving whatever frame the test puts in front of it."""

    def __init__(self) -> None:
        self.frame = _scene()
        self.offline = False

    async def snapshot(self, camera_id: str) -> bytes:
        if self.offline:
            raise CameraOfflineError(camera_id)
        return self.frame

    async def show(self, frame: bytes, detections: list[Detection], polls: int = 1) -> int:
        self.frame = frame
        mock_detector().set_script(CAMERA, detections)
        created = 0
        for _ in range(polls):
            created += await ingestion.poll_once()
        return created


class Verifier:
    """Scripted stand-in for the Foundry closed-question vision check."""

    name = "scripted"

    def __init__(self) -> None:
        self.mailbox: MailboxVerdict | None = MailboxVerdict("yes", "letter", 0.9)
        self.bins = BinVerdict(bins_after=0, confidence=0.9)
        self.mailbox_calls: list[tuple] = []
        self.bin_calls: list[tuple] = []

    async def verify_mailbox(self, before, during, after):
        self.mailbox_calls.append((before, during, after))
        return self.mailbox

    async def assess_bins(self, before, after):
        self.bin_calls.append((before, after))
        return self.bins


@pytest.fixture(autouse=True)
async def _clean(client):
    async def _clear():
        async with SessionLocal() as session:
            for model in (EventEvidence, AIAnalysis, Activity, Event, CameraZone, SceneState):
                await session.execute(delete(model))
            await session.commit()

    ingestion.reset_cooldowns()
    await _clear()
    yield
    ingestion.reset_cooldowns()
    await _clear()


@pytest.fixture
def camera(monkeypatch) -> Camera:
    cam = Camera()
    monkeypatch.setattr(settings, "event_cooldown_seconds", 0.0)
    monkeypatch.setattr(settings, "event_poll_interval_seconds", 0.0)
    monkeypatch.setattr(settings, "vehicle_absence_seconds", 0.0)
    monkeypatch.setattr(mock_provider, "get_snapshot", cam.snapshot)
    return cam


@pytest.fixture
def verifier() -> Verifier:
    scripted = Verifier()
    set_temporal_verifier(scripted)
    return scripted


async def _events(client) -> list[dict]:
    rows = [e for e in (await client.get("/api/v1/events?limit=200")).json() if e["camera_id"] == CAMERA]
    return sorted(rows, key=lambda e: e["start_time"])


def _with(events: list[dict], tag: str) -> list[dict]:
    return [e for e in events if tag in e["tags"]]


def _restart() -> None:
    """What a redeploy does to in-memory state; the database survives."""
    ingestion.reset_cooldowns()
    scene_engine.reset()


CAR_FRAME = _scene((CAR, CAR_RGB))


# --- parked vehicles -------------------------------------------------------------


async def test_a_parked_car_stops_emitting_after_five_observations(client, camera):
    await camera.show(CAR_FRAME, [CAR], polls=12)

    events = await _events(client)
    assert [e["type"] for e in events] == ["vehicle"]
    # Present from the first frame ever seen: it may have been there all
    # along, so it is parked but not claimed to have "arrived".
    assert "vehicle_parked" in events[0]["tags"]
    assert "vehicle_arrived" not in events[0]["tags"]

    state = (await client.get(f"/api/v1/cameras/{CAMERA}/scene-state")).json()
    [track] = state["vehicles"]
    assert track["state"] == "parked"
    assert track["observation_count"] == 12
    assert track["stationary_since"] is not None


async def test_a_car_that_drives_in_and_parks_is_one_arrived_and_parked_event(client, camera):
    await camera.show(_scene(), [])
    await camera.show(CAR_FRAME, [CAR], polls=8)

    [event] = await _events(client)
    assert event["type"] == "vehicle"
    assert {"vehicle_arrived", "vehicle_parked"} <= set(event["tags"])
    [track] = event["metadata"]["vehicle_tracks"]
    assert track["state"] == "parked"
    assert track["observation_count"] >= settings.vehicle_parked_observations


async def test_parked_car_jitter_flicker_and_edge_traffic_stay_silent(client, camera):
    await camera.show(CAR_FRAME, [CAR], polls=6)
    rng = random.Random(7)
    for index in range(30):
        d = [rng.uniform(-0.01, 0.01) for _ in range(4)]
        jitter = Detection(
            "car", rng.uniform(0.5, 0.95),
            BoundingBox(CAR.bbox.x1 + d[0], CAR.bbox.y1 + d[1], CAR.bbox.x2 + d[2], CAR.bbox.y2 + d[3]),
        )
        frame = [] if index % 7 == 3 else [jitter]  # detector misses it now and then
        if index % 5 == 0:
            frame.append(FAR_CAR)
        await camera.show(CAR_FRAME, frame)

    assert [e["type"] for e in await _events(client)] == ["vehicle"]
    state = (await client.get(f"/api/v1/cameras/{CAMERA}/scene-state")).json()
    parked = [t for t in state["vehicles"] if t["state"] == "parked"]
    assert len(parked) == 1


async def test_two_cars_are_tracked_separately(client, camera):
    await camera.show(CAR_FRAME, [CAR, CAR2], polls=6)
    events = await _events(client)
    assert len(events) == 1
    assert len(events[0]["metadata"]["vehicle_tracks"]) == 2

    # The second car pulls out; the first stays put and stays silent.
    moved = Detection("car", 0.9, BoundingBox(0.15, 0.30, 0.50, 0.70))
    await camera.show(CAR_FRAME, [CAR, moved])
    events = await _events(client)
    assert len(events) == 2
    assert "vehicle_moved" in events[-1]["tags"]
    [moved_track] = events[-1]["metadata"]["vehicle_tracks"]
    assert moved_track["state"] == "moving"

    await camera.show(CAR_FRAME, [CAR, moved], polls=8)
    assert len(await _events(client)) == 2  # it parks again, silently
    state = (await client.get(f"/api/v1/cameras/{CAMERA}/scene-state")).json()
    assert sorted(t["state"] for t in state["vehicles"]) == ["parked", "parked"]


async def test_departure_needs_frames_without_the_car_and_an_outage_is_not_one(client, camera):
    await camera.show(CAR_FRAME, [CAR], polls=6)

    camera.offline = True  # no frames at all: nothing is known about the car
    for _ in range(25):
        await ingestion.poll_once()
    camera.offline = False
    assert len(await _events(client)) == 1
    state = (await client.get(f"/api/v1/cameras/{CAMERA}/scene-state")).json()
    assert state["vehicles"][0]["state"] == "parked"

    # Frames that show the spot empty are evidence; a few are not enough.
    await camera.show(_scene(), [], polls=settings.vehicle_absence_frames - 1)
    assert len(await _events(client)) == 1
    await camera.show(_scene(), [])
    events = await _events(client)
    assert len(events) == 2
    assert events[-1]["type"] == "vehicle"
    assert "vehicle_departed" in events[-1]["tags"]
    assert events[-1]["metadata"]["vehicle_tracks"][0]["state"] == "departed"

    # The same car coming back to the same spot is a return.
    await camera.show(CAR_FRAME, [CAR], polls=2)
    events = await _events(client)
    assert len(events) == 3
    assert "vehicle_returned" in events[-1]["tags"]


async def test_departure_waits_for_the_absence_window(client, camera, monkeypatch):
    monkeypatch.setattr(settings, "vehicle_absence_seconds", 3600.0)
    await camera.show(CAR_FRAME, [CAR], polls=6)
    await camera.show(_scene(), [], polls=20)
    assert len(await _events(client)) == 1


async def test_a_person_at_a_parked_car_is_an_interaction_then_a_move_speaks(client, camera):
    await camera.show(CAR_FRAME, [CAR], polls=6)
    await camera.show(CAR_FRAME, [CAR, PERSON_AT_CAR], polls=2)

    people = [e for e in await _events(client) if e["type"] == "person"]
    assert len(people) == 2
    # One frame of overlap could be someone walking past in front of it.
    assert "vehicle_interaction" not in people[0]["tags"]
    assert "vehicle_interaction" in people[1]["tags"]
    assert people[1]["metadata"]["vehicle_tracks"][0]["state"] == "attended"

    away = Detection("car", 0.9, BoundingBox(0.30, 0.35, 0.70, 0.80))
    await camera.show(CAR_FRAME, [away])
    vehicles = [e for e in await _events(client) if e["type"] == "vehicle"]
    assert len(vehicles) == 2
    assert "vehicle_moved" in vehicles[-1]["tags"]


async def test_parked_state_survives_a_restart(client, camera):
    await camera.show(CAR_FRAME, [CAR], polls=6)
    assert len(await _events(client)) == 1

    _restart()
    await camera.show(CAR_FRAME, [CAR], polls=10)
    assert len(await _events(client)) == 1
    state = (await client.get(f"/api/v1/cameras/{CAMERA}/scene-state")).json()
    [track] = state["vehicles"]
    assert track["state"] == "parked"
    # Saved on each transition (and at least once a minute), so the count
    # may trail by the frames since the last save; the state must not.
    assert track["observation_count"] >= 10 + settings.vehicle_parked_observations


async def test_without_persisted_state_a_restart_re_announces_once(client, camera):
    # The control for the test above: it is the persisted state, not luck,
    # that keeps the car silent.
    await camera.show(CAR_FRAME, [CAR], polls=6)
    async with SessionLocal() as session:
        await session.execute(delete(SceneState))
        await session.commit()
    _restart()
    await camera.show(CAR_FRAME, [CAR], polls=10)
    assert len(await _events(client)) == 2


async def test_small_distant_vehicles_are_never_announced(client, camera):
    await camera.show(_scene(), [FAR_CAR], polls=6)
    # ...nor is their leaving.
    await camera.show(_scene(), [], polls=settings.vehicle_absence_frames + 2)
    assert await _events(client) == []


async def test_corrupt_persisted_state_never_silences_a_person(client, camera):
    # Every part of the saved state is malformed in a different way.
    corrupt = {
        "version": 1,
        "saved_at": 9e12,
        "vehicles": {"tracks": [{"id": "x", "box": "nope"}, 7], "departed": [{"box": None}, "?"]},
        "bins": {"kerb": {"baseline": ["invalid"] * 1024, "collection_at": "soon"}, "other": 3},
        "mailboxes": {"mailbox": {"cooldown_until": "later"}},
    }
    await _add_zone(client, CAMERA, BINS)
    await _add_zone(client, CAMERA, MAILBOX)
    async with SessionLocal() as session:
        from datetime import datetime, timezone

        session.add(SceneState(camera_id=CAMERA, state=corrupt, updated_at=datetime.now(timezone.utc)))
        await session.commit()
    _restart()

    await camera.show(_scene(), [PASSER_BY])
    assert [e["type"] for e in await _events(client)] == ["person"]


async def test_a_failing_scene_engine_falls_back_to_plain_events(client, camera, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    from app.services.scene_state import CameraScene

    monkeypatch.setattr(CameraScene, "observe_vehicles", broken)
    await camera.show(CAR_FRAME, [CAR, PASSER_BY])
    assert sorted(e["type"] for e in await _events(client)) == ["person", "vehicle"]


async def test_scene_state_route_tolerates_a_malformed_row(client):
    from datetime import datetime, timezone

    async with SessionLocal() as session:
        session.add(SceneState(camera_id=CAMERA, state={"vehicles": None}, updated_at=datetime.now(timezone.utc)))
        await session.commit()
    _restart()
    response = await client.get(f"/api/v1/cameras/{CAMERA}/scene-state")
    assert response.status_code == 200
    assert response.json()["vehicles"] == []


async def test_a_person_beside_the_parked_car_is_never_suppressed(client, camera):
    await camera.show(CAR_FRAME, [CAR], polls=6)
    walker = Detection("person", 0.62, BoundingBox(0.05, 0.20, 0.20, 0.90))
    await camera.show(CAR_FRAME, [CAR, walker], polls=3)
    types = [e["type"] for e in await _events(client)]
    assert types.count("person") == 3
    assert types.count("vehicle") == 1


# --- mailbox ------------------------------------------------------------------------


async def _mailbox(client) -> None:
    await _add_zone(client, CAMERA, MAILBOX)


async def test_a_mail_delivery_is_one_verified_package_event_with_evidence(client, camera, verifier):
    await _mailbox(client)
    await camera.show(_scene(), [])
    await camera.show(_scene((AT_MAILBOX, (200, 30, 30))), [AT_MAILBOX], polls=3)
    await camera.show(_scene(), [])

    deliveries = _with(await _events(client), "mailbox_delivery")
    assert len(deliveries) == 1
    event = deliveries[0]
    assert event["type"] == "package"
    assert event["zone"] == "mailbox"
    assert {"mailbox", "mailbox_delivery", "letter"} <= set(event["tags"])
    temporal = event["metadata"]["temporal"]
    assert temporal["kind"] == "mailbox"
    assert temporal["verdict"] == {"deposited": "yes", "item": "letter", "confidence": 0.9}
    assert temporal["basis"] == "vision"
    assert temporal["person_frames"] == 3
    assert set(temporal["evidence"]) == {"before", "during", "after"}
    for url in temporal["evidence"].values():
        response = await client.get(url)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"

    # One closed-question call per delivery, with all three frames.
    assert len(verifier.mailbox_calls) == 1
    before, during, after = verifier.mailbox_calls[0]
    assert before and during and after
    # The person event for the same moment is still a person event.
    assert any(e["type"] == "person" for e in await _events(client))


async def test_a_walk_by_at_the_mailbox_is_not_a_delivery_and_costs_no_call(client, camera, verifier):
    await _mailbox(client)
    await camera.show(_scene(), [])
    await camera.show(_scene(), [AT_MAILBOX])
    await camera.show(_scene(), [], polls=2)
    await camera.show(_scene(), [PASSER_BY], polls=3)
    await camera.show(_scene(), [])

    events = await _events(client)
    assert _with(events, "mailbox_delivery") == []
    assert _with(events, "mailbox_activity") == []
    assert verifier.mailbox_calls == []


async def test_a_parcel_carried_past_the_mailbox_is_not_a_delivery(client, camera, verifier):
    await _mailbox(client)
    verifier.mailbox = MailboxVerdict("no", "unknown", 0.9)
    await camera.show(_scene(), [])
    await camera.show(_scene(), [AT_MAILBOX, PARCEL_AT_MAILBOX], polls=2)
    await camera.show(_scene(), [])

    events = await _events(client)
    assert _with(events, "mailbox_delivery") == []
    assert _with(events, "mailbox_activity") == []
    assert len(verifier.mailbox_calls) == 1


async def test_a_parcel_left_at_the_mailbox_is_confirmed_locally(client, camera, verifier):
    await _mailbox(client)
    await camera.show(_scene(), [])
    await camera.show(_scene(), [AT_MAILBOX], polls=2)
    await camera.show(_scene(), [PARCEL_AT_MAILBOX])

    [event] = _with(await _events(client), "mailbox_delivery")
    assert "parcel" in event["tags"]
    assert event["metadata"]["temporal"]["basis"] == "local_parcel_left"
    assert verifier.mailbox_calls == []


async def test_an_unverifiable_visit_is_reported_honestly_as_unknown(client, camera):
    await _mailbox(client)  # no verifier configured
    await camera.show(_scene(), [])
    await camera.show(_scene(), [AT_MAILBOX], polls=2)
    await camera.show(_scene(), [])

    events = await _events(client)
    assert _with(events, "mailbox_delivery") == []
    [event] = _with(events, "mailbox_activity")
    assert event["type"] == "motion"
    temporal = event["metadata"]["temporal"]
    assert temporal["confirmed"] is False
    assert temporal["verdict"]["deposited"] == "unknown"
    assert temporal["unknowns"]


async def test_a_low_confidence_yes_is_not_a_delivery(client, camera, verifier):
    await _mailbox(client)
    verifier.mailbox = MailboxVerdict("yes", "parcel", 0.3)
    await camera.show(_scene(), [])
    await camera.show(_scene(), [AT_MAILBOX], polls=2)
    await camera.show(_scene(), [])

    events = await _events(client)
    assert _with(events, "mailbox_delivery") == []
    assert len(_with(events, "mailbox_activity")) == 1


async def test_mailbox_deliveries_have_a_cooldown(client, camera, verifier):
    await _mailbox(client)
    for _ in range(2):
        await camera.show(_scene(), [])
        await camera.show(_scene(), [AT_MAILBOX], polls=2)
        await camera.show(_scene(), [])

    assert len(_with(await _events(client), "mailbox_delivery")) == 1
    assert len(verifier.mailbox_calls) == 1


async def test_mailbox_detection_is_off_until_a_zone_is_configured(client, camera, verifier):
    await camera.show(_scene(), [])
    await camera.show(_scene(), [AT_MAILBOX], polls=3)
    await camera.show(_scene(), [])
    assert _with(await _events(client), "mailbox") == []
    assert verifier.mailbox_calls == []


# --- bins ---------------------------------------------------------------------------------


def _kerb(bins: int, dark: bool = False) -> bytes:
    """A textured kerb, with ``bins`` wheelie bins standing in the bins zone."""
    from PIL import Image, ImageDraw

    width, height = 640, 360
    if dark:
        image = Image.new("RGB", (width, height), (0, 0, 0))
    else:
        rng = random.Random(1)
        image = Image.new("RGB", (width, height))
        image.putdata([(v, v, v) for v in (rng.randint(90, 170) for _ in range(width * height))])
        draw = ImageDraw.Draw(image)
        for index in range(bins):
            left = 0.60 + index * 0.17
            draw.rectangle((left * width, 0.55 * height, (left + 0.13) * width, 0.93 * height), fill=(20, 70, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


async def _bins_out(client, camera, verifier) -> None:
    await _add_zone(client, CAMERA, BINS)
    verifier.bins = BinVerdict(bins_after=0, confidence=0.9)
    await camera.show(_kerb(0), [])  # first look: learns "no bins", silently
    verifier.bins = BinVerdict(bins_before=0, bins_after=2, confidence=0.9)
    await camera.show(_kerb(2), [], polls=settings.bin_settle_frames)


def test_the_zone_fingerprint_sees_bins_but_not_brightness():
    zone = BoundingBox(0.55, 0.45, 0.95, 0.95)
    empty = zone_signature(_kerb(0), zone)
    full = zone_signature(_kerb(2), zone)
    assert signature_distance(empty, full) > settings.bin_change_threshold

    from PIL import Image, ImageEnhance

    darker = io.BytesIO()
    ImageEnhance.Brightness(Image.open(io.BytesIO(_kerb(2)))).enhance(0.6).save(darker, format="JPEG", quality=92)
    assert signature_distance(full, zone_signature(darker.getvalue(), zone)) < settings.bin_change_threshold
    assert zone_signature(_kerb(0, dark=True), zone) is None  # night/blank: no evidence


async def test_bins_put_out_is_one_event_with_before_and_after(client, camera, verifier):
    await _bins_out(client, camera, verifier)

    [event] = _with(await _events(client), "bin_placed_out")
    assert event["type"] == "motion"
    assert event["zone"] == "kerb"
    assert "bins" in event["tags"]
    temporal = event["metadata"]["temporal"]
    assert temporal["state_before"] == "absent"
    assert temporal["state_after"] == "present"
    assert set(temporal["evidence"]) == {"before", "after"}
    assert (await client.get(temporal["evidence"]["after"])).status_code == 200
    # An initial look plus one per real change, never one per frame.
    assert len(verifier.bin_calls) == 2

    await camera.show(_kerb(2), [], polls=10)
    assert len(_with(await _events(client), "bins")) == 1
    assert len(verifier.bin_calls) == 2


async def test_bins_emptied_needs_collection_evidence(client, camera, verifier):
    await _bins_out(client, camera, verifier)

    # The lorry stops at the bins, then drives off.
    await camera.show(_kerb(2), [TRUCK_AT_BINS], polls=2)
    verifier.bins = BinVerdict(bins_before=2, bins_after=2, emptied=None, confidence=0.8)
    await camera.show(_kerb(2), [])
    [emptied] = _with(await _events(client), "bin_emptied")
    assert emptied["metadata"]["temporal"]["basis"] == "collection_vehicle"
    assert emptied["metadata"]["temporal"]["collection_evidence_at"]

    # Taking the emptied bins back in later is not a second "emptied".
    verifier.bins = BinVerdict(bins_before=2, bins_after=0, confidence=0.9)
    await camera.show(_kerb(0), [], polls=settings.bin_settle_frames)
    assert len(_with(await _events(client), "bin_emptied")) == 1


async def test_bins_disappearing_alone_is_not_emptied(client, camera, verifier):
    await _bins_out(client, camera, verifier)
    verifier.bins = BinVerdict(bins_before=2, bins_after=0, emptied=None, confidence=0.9)
    await camera.show(_kerb(0), [], polls=settings.bin_settle_frames)

    events = await _events(client)
    assert _with(events, "bin_emptied") == []
    state = (await client.get(f"/api/v1/cameras/{CAMERA}/scene-state")).json()
    assert state["bins"][0]["state"] == "absent"


async def test_bins_disappearing_can_count_when_configured(client, camera, verifier, monkeypatch):
    monkeypatch.setattr(settings, "bin_emptied_on_disappearance", True)
    await _bins_out(client, camera, verifier)
    verifier.bins = BinVerdict(bins_before=2, bins_after=0, confidence=0.9)
    await camera.show(_kerb(0), [], polls=settings.bin_settle_frames)

    [event] = _with(await _events(client), "bin_emptied")
    assert event["metadata"]["temporal"]["basis"] == "disappearance_rule"


async def test_a_camera_outage_or_dark_frames_do_not_change_the_bins(client, camera, verifier):
    await _bins_out(client, camera, verifier)
    calls = len(verifier.bin_calls)

    camera.offline = True
    for _ in range(10):
        await ingestion.poll_once()
    camera.offline = False
    await camera.show(_kerb(0, dark=True), [], polls=5)
    await camera.show(_kerb(2), [], polls=3)

    assert len(_with(await _events(client), "bins")) == 1
    assert len(verifier.bin_calls) == calls
    state = (await client.get(f"/api/v1/cameras/{CAMERA}/scene-state")).json()
    assert state["bins"][0]["state"] == "present"


async def test_a_person_at_the_bins_is_not_a_bin_change(client, camera, verifier):
    await _bins_out(client, camera, verifier)
    calls = len(verifier.bin_calls)
    at_bins = Detection("person", 0.9, BoundingBox(0.60, 0.40, 0.80, 0.95))
    await camera.show(_kerb(0), [at_bins], polls=5)  # bins hidden behind them
    assert len(verifier.bin_calls) == calls


async def test_bin_state_survives_a_restart(client, camera, verifier):
    await _bins_out(client, camera, verifier)
    calls = len(verifier.bin_calls)

    _restart()
    await camera.show(_kerb(2), [], polls=5)
    assert len(_with(await _events(client), "bins")) == 1
    assert len(verifier.bin_calls) == calls  # no fresh "initial" look needed
    state = (await client.get(f"/api/v1/cameras/{CAMERA}/scene-state")).json()
    assert state["bins"][0]["state"] == "present"


async def test_bins_need_the_vision_check(client, camera):
    await _add_zone(client, CAMERA, BINS)
    await camera.show(_kerb(0), [])
    await camera.show(_kerb(2), [], polls=5)
    assert _with(await _events(client), "bins") == []


# --- vision replies -------------------------------------------------------------------------


def test_mailbox_replies_are_parsed_strictly():
    assert parse_mailbox_reply('{"deposited": "yes", "item": "envelope", "confidence": 0.8}') == MailboxVerdict(
        "yes", "letter", 0.8
    )
    assert parse_mailbox_reply('```json\n{"deposited": "no", "item": "parcel", "confidence": 2}\n```') == (
        MailboxVerdict("no", "unknown", 1.0)
    )
    assert parse_mailbox_reply('{"deposited": "probably", "confidence": 0.9}').deposited == "unknown"
    assert parse_mailbox_reply("I think so") is None


def test_bin_replies_are_parsed_strictly():
    verdict = parse_bins_reply('{"bins_before": 0, "bins_after": 2, "emptied": null, "confidence": 0.7}')
    assert verdict == BinVerdict(0, 2, None, 0.7)
    assert parse_bins_reply('{"bins_before": "two", "bins_after": 99, "emptied": "yes"}') == BinVerdict(
        None, None, None, 0.0
    )


def test_the_vision_prompts_never_ask_about_people():
    from app.ai.temporal_vision import BINS_SYSTEM_PROMPT, MAILBOX_SYSTEM_PROMPT

    for prompt in (MAILBOX_SYSTEM_PROMPT, BINS_SYSTEM_PROMPT):
        lowered = prompt.lower()
        assert "do not describe" in lowered
        for word in ("gender", "ethnic", "race", "age", "male", "female"):
            assert f" {word}" not in lowered.replace("garbage", "").replace("package", "")
