"""Subject-specific event photos and multi-subject ingestion (incident fix).

Production incident: a camera watching a parked car reported every
sighting as a car. Person events were photographed from the higher-scoring
car in the same frame, so appearance analysis and re-identification were
handed a car (or nothing), and the per-camera cooldown held by the parked
car silenced the people and animals that walked past it.

These tests use a real JPEG with distinct coloured regions for each
subject, so "which object did the crop actually contain?" is answered from
pixels, not from metadata the code under test wrote itself.
"""
from __future__ import annotations

import io

import pytest
from sqlalchemy import delete

from app.ai.best_photo import select_best_photo
from app.ai.detector import (
    MAX_DETECTIONS_PER_FRAME,
    BoundingBox,
    Detection,
    DetectionContext,
    mock_detector,
    refine_detections,
)
from app.ai.dwell import DwellTracker
from app.ai.semantics import derive_semantics
from app.config import settings
from app.db import SessionLocal
from app.models.db import Activity, AIAnalysis, Camera, Event
from app.providers.mock import mock_provider
from app.services import ai_pipeline, ingestion, provider_registry

CAMERA = "mock-front-door"
BACKGROUND = (40, 140, 40)
PERSON_RGB = (220, 20, 20)
CAR_RGB = (20, 20, 220)
DOG_RGB = (230, 200, 20)

# Car is the most confident object in every scene, as it was live.
PERSON = Detection("person", 0.61, BoundingBox(0.05, 0.20, 0.20, 0.90))
CAR = Detection("car", 0.95, BoundingBox(0.55, 0.40, 0.95, 0.85))
DOG = Detection("dog", 0.56, BoundingBox(0.08, 0.60, 0.28, 0.85))


def _scene(*subjects: tuple[Detection, tuple[int, int, int]], size=(1280, 720)) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, BACKGROUND)
    draw = ImageDraw.Draw(image)
    width, height = size
    for detection, colour in subjects:
        box = detection.bbox
        draw.rectangle(
            (box.x1 * width, box.y1 * height, box.x2 * width, box.y2 * height), fill=colour
        )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _dominant(image: bytes) -> tuple[int, int, int]:
    import numpy as np
    from PIL import Image

    pixels = np.asarray(Image.open(io.BytesIO(image)).convert("RGB")).reshape(-1, 3).tolist()
    counts: dict[tuple[int, int, int], int] = {}
    for red, green, blue in pixels:
        key = (round(red, -1), round(green, -1), round(blue, -1))
        counts[key] = counts.get(key, 0) + 1
    return max(counts, key=counts.get)


def _close(actual: tuple[int, int, int], expected: tuple[int, int, int]) -> bool:
    return all(abs(a - e) <= 30 for a, e in zip(actual, expected))


@pytest.fixture(autouse=True)
async def _clean(client):
    ingestion.reset_cooldowns()

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(AIAnalysis))
            await session.execute(delete(Activity))
            await session.execute(delete(Event))
            await session.commit()

    await _clear()
    yield
    await _clear()
    ingestion.reset_cooldowns()


@pytest.fixture
def scene(monkeypatch):
    """Serve a real JPEG from the mock camera and capture what analysis saw."""
    seen: dict[str, list] = {"appearance": [], "animal": [], "embedded": []}
    # Single-frame scenarios: report a vehicle on its first sighting.
    monkeypatch.setattr(settings, "vehicle_confirm_observations", 1)

    def use(frame: bytes) -> dict[str, list]:
        async def snapshot(camera_id: str) -> bytes:
            return frame

        monkeypatch.setattr(mock_provider, "get_snapshot", snapshot)
        return seen

    class _Analyzer:
        async def describe_person(self, image, content_type):
            seen["appearance"].append(image)
            return None

    class _Identifier:
        async def identify_animal(self, image, content_type):
            seen["animal"].append(image)
            return None

    async def embed(photo):
        seen["embedded"].append(photo)
        return []

    monkeypatch.setattr(ai_pipeline, "get_appearance_analyzer", lambda: _Analyzer())
    monkeypatch.setattr(ai_pipeline, "get_animal_identifier", lambda: _Identifier())
    monkeypatch.setattr(ai_pipeline, "_embed_photo", embed)
    return use


async def _events(client) -> list[dict]:
    return (await client.get("/api/v1/events")).json()


# --- event photo follows the event's own subject ----------------------------


async def test_person_event_is_photographed_from_the_person_not_the_car(client, scene):
    seen = scene(_scene((PERSON, PERSON_RGB), (CAR, CAR_RGB)))
    mock_detector().set_script(CAMERA, [CAR, PERSON])

    created = await client.post("/api/v1/mock/events", json={"camera_id": CAMERA, "type": "person"})
    body = (await client.get(f"/api/v1/events/{created.json()['id']}")).json()

    assert body["type"] == "person"
    best = body["metadata"]["best_photo"]
    assert best["detection"]["label"] == "person"
    # The subject crop handed to re-ID and the readable crop handed to the
    # appearance model both actually contain the person.
    [photo] = seen["embedded"]
    assert _close(_dominant(photo.subject_image), PERSON_RGB)
    [readable] = seen["appearance"]
    assert _close(_dominant(readable), PERSON_RGB) or _close(_dominant(readable), BACKGROUND)
    assert not _close(_dominant(readable), CAR_RGB)
    # Every object in the frame is still recorded, in full-frame coordinates.
    assert {box["label"] for box in best["frame_boxes"]} == {"person", "car"}
    assert "person" in {box["label"] for box in body["photo_boxes"]}


async def test_vehicle_event_in_the_same_frame_uses_the_car(client, scene):
    seen = scene(_scene((PERSON, PERSON_RGB), (CAR, CAR_RGB)))
    mock_detector().set_script(CAMERA, [PERSON, CAR])

    created = await client.post("/api/v1/mock/events", json={"camera_id": CAMERA, "type": "vehicle"})
    body = (await client.get(f"/api/v1/events/{created.json()['id']}")).json()

    assert body["type"] == "vehicle"
    assert body["metadata"]["best_photo"]["detection"]["label"] == "car"
    # A car is never sent for person appearance analysis or re-ID.
    assert seen["appearance"] == []
    assert seen["embedded"] == []


async def test_global_fallback_only_when_the_subject_is_absent(client, scene):
    scene(_scene((CAR, CAR_RGB)))
    mock_detector().set_script(CAMERA, [CAR])

    created = await client.post("/api/v1/mock/events", json={"camera_id": CAMERA, "type": "person"})
    body = (await client.get(f"/api/v1/events/{created.json()['id']}")).json()

    assert body["metadata"]["best_photo"]["detection"]["label"] == "car"
    assert body["appearance"] is None


# --- ingestion emits one event per coexisting subject -----------------------


def _by_type(events: list[dict]) -> dict[str, dict]:
    return {event["type"]: event for event in events if event["camera_id"] == CAMERA}


async def test_car_and_person_in_one_frame_raise_both_events(client, scene):
    seen = scene(_scene((PERSON, PERSON_RGB), (CAR, CAR_RGB)))
    mock_detector().set_script(CAMERA, [CAR, PERSON])

    assert await ingestion.poll_once() >= 2

    events = _by_type(await _events(client))
    assert set(events) >= {"person", "vehicle"}
    assert events["person"]["metadata"]["best_photo"]["detection"]["label"] == "person"
    assert events["person"]["priority"] == "high"
    assert events["vehicle"]["metadata"]["best_photo"]["detection"]["label"] == "car"
    assert len(seen["embedded"]) == 1
    assert _close(_dominant(seen["embedded"][0].subject_image), PERSON_RGB)


async def test_animal_is_not_suppressed_by_a_more_confident_car(client, scene):
    seen = scene(_scene((DOG, DOG_RGB), (CAR, CAR_RGB)))
    mock_detector().set_script(CAMERA, [CAR, DOG])

    await ingestion.poll_once()

    events = _by_type(await _events(client))
    assert set(events) >= {"animal", "vehicle"}
    assert events["animal"]["metadata"]["best_photo"]["detection"]["label"] == "dog"
    assert events["vehicle"]["metadata"]["best_photo"]["detection"]["label"] == "car"
    identified = seen["animal"][0]
    assert _close(_dominant(identified), DOG_RGB)


async def test_a_parked_car_does_not_hold_the_cooldown_for_people(client, scene, monkeypatch):
    monkeypatch.setattr(settings, "event_cooldown_seconds", 9999.0)
    monkeypatch.setattr(settings, "event_poll_interval_seconds", 0.0)
    scene(_scene((PERSON, PERSON_RGB), (CAR, CAR_RGB)))

    mock_detector().set_script(CAMERA, [CAR])
    await ingestion.poll_once()
    mock_detector().set_script(CAMERA, [CAR, PERSON])
    await ingestion.poll_once()

    rows = [event for event in await _events(client) if event["camera_id"] == CAMERA]
    assert sorted(event["type"] for event in rows) == ["person", "vehicle"]


# --- unit level --------------------------------------------------------------


class _PerFrameDetector:
    name = "per-frame"

    def __init__(self, by_frame: dict[bytes, list[Detection]]) -> None:
        self._by_frame = by_frame

    def detect(self, image, context):
        return list(self._by_frame.get(image, []))


def test_a_frame_with_the_target_beats_a_sharper_frame_without_it():
    with_person = _scene((PERSON, PERSON_RGB), (CAR, CAR_RGB))
    car_only = _scene((CAR, CAR_RGB))
    detector = _PerFrameDetector({with_person: [CAR, PERSON], car_only: [CAR]})

    photo = select_best_photo(
        [car_only, with_person], detector, DetectionContext(camera_id=CAMERA), {"person"}
    )

    assert photo is not None
    assert photo.frame_index == 1
    assert photo.detection.label == "person"


def test_semantics_keep_the_event_subject_over_a_higher_ranked_class():
    from datetime import datetime, timezone

    result = derive_semantics(
        camera_id=CAMERA,
        camera_name="Front Door",
        base_event_type="animal",
        detections=[CAR, PERSON, DOG],
        zones=[],
        tracker=DwellTracker(),
        at=datetime.now(timezone.utc),
        parked_after_seconds=60,
    )

    assert result.type == "animal"
    assert result.primary_detection == DOG
    assert set(result.tags) >= {"car", "person", "dog"}


def test_a_crowd_of_cars_cannot_push_a_person_out_of_the_frame_cap():
    cars = [
        Detection("car", 0.9 - index * 0.01, BoundingBox(0.07 * index, 0.1, 0.07 * index + 0.06, 0.2))
        for index in range(MAX_DETECTIONS_PER_FRAME + 2)
    ]
    kept = refine_detections([*cars, PERSON])

    assert PERSON in kept


# --- production camera listing ----------------------------------------------


class _RealProvider:
    id = "real-test"

    async def discover_devices(self):
        return [
            {
                "id": "real-test-cam", "provider_id": self.id, "name": "Real Test Cam",
                "type": "camera", "model": "X", "online": True, "status": "online",
                "battery_level": None,
                "capabilities": {"snapshot": "UNSUPPORTED", "audioDetection": "UNSUPPORTED"},
            }
        ]

    def has_camera(self, camera_id):
        return camera_id == "real-test-cam"


@pytest.fixture
async def _real_provider(monkeypatch):
    async def configured():
        return [_RealProvider()]

    monkeypatch.setattr(provider_registry, "_configured_real_providers", configured)
    provider_registry.reset_discovery_cache()
    yield
    async with SessionLocal() as session:
        await session.execute(delete(Camera).where(Camera.id == "real-test-cam"))
        await session.commit()


async def test_production_hides_mock_cameras_once_real_providers_exist(client, monkeypatch, _real_provider):
    # Seed persisted mock rows first, as an earlier deployment would have.
    await client.get("/api/v1/cameras")
    monkeypatch.setattr(settings, "app_env", "production")
    provider_registry.reset_discovery_cache()

    ids = {camera["id"] for camera in (await client.get("/api/v1/cameras")).json()}

    assert "real-test-cam" in ids
    assert not any(camera_id.startswith("mock-") for camera_id in ids)
    assert (await client.get("/api/v1/cameras/mock-front-door")).status_code == 404
    response = await client.post("/api/v1/mock/events", json={"camera_id": CAMERA, "type": "person"})
    assert response.status_code == 404


async def test_mock_cameras_stay_visible_outside_production(client, _real_provider):
    ids = {camera["id"] for camera in (await client.get("/api/v1/cameras")).json()}
    assert "real-test-cam" in ids
    assert CAMERA in ids


async def test_mock_cameras_can_be_forced_off(client, monkeypatch):
    monkeypatch.setattr(settings, "mock_cameras_enabled", False)
    ids = {camera["id"] for camera in (await client.get("/api/v1/cameras")).json()}
    assert not any(camera_id.startswith("mock-") for camera_id in ids)
