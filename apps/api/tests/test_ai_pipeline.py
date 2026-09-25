"""End-to-end AI pipeline tests: zones admin CRUD, semantic enrichment,
AI analysis persistence, activity correlation and audio capability."""
import pytest
from sqlalchemy import delete, select

from app.ai.animals import AnimalIdentity
from app.ai.detector import BoundingBox, Detection, mock_detector
from app.db import SessionLocal
from app.models.db import Activity, AIAnalysis, CameraZone, Event
from app.services import ai_pipeline


@pytest.fixture(autouse=True)
async def _clean_pipeline_tables(client):
    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(CameraZone))
            await session.execute(delete(AIAnalysis))
            await session.execute(delete(Activity))
            await session.execute(delete(Event))
            await session.commit()

    await _clear()
    yield
    await _clear()


async def _headers(client) -> dict[str, str]:
    email = "zone-tests@example.com"
    password = "supersecret1"
    register = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert register.status_code in (201, 409), register.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


DRIVEWAY = {"name": "driveway", "kind": "driveway", "x1": 0.0, "y1": 0.4, "x2": 0.6, "y2": 1.0}
MAILBOX = {"name": "mailbox", "kind": "mailbox", "x1": 0.7, "y1": 0.3, "x2": 0.95, "y2": 0.7}


# --- admin plane ------------------------------------------------------------


async def test_zone_routes_require_authentication(client):
    assert (await client.get("/api/v1/admin/cameras/mock-front-door/zones")).status_code == 401
    assert (
        await client.post("/api/v1/admin/cameras/mock-front-door/zones", json=DRIVEWAY)
    ).status_code == 401


async def test_zone_crud_roundtrip(client):
    headers = await _headers(client)
    created = await client.post("/api/v1/admin/cameras/mock-front-door/zones", json=DRIVEWAY, headers=headers)
    assert created.status_code == 201, created.text
    zone = created.json()
    assert zone["name"] == "driveway"
    assert zone["camera_id"] == "mock-front-door"

    listed = await client.get("/api/v1/admin/cameras/mock-front-door/zones", headers=headers)
    assert [z["id"] for z in listed.json()] == [zone["id"]]

    updated = await client.put(
        f"/api/v1/admin/cameras/mock-front-door/zones/{zone['id']}",
        json={"name": "front-driveway", "x2": 0.7},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "front-driveway"
    assert updated.json()["x2"] == 0.7

    deleted = await client.delete(
        f"/api/v1/admin/cameras/mock-front-door/zones/{zone['id']}", headers=headers
    )
    assert deleted.status_code == 204
    assert (await client.get("/api/v1/admin/cameras/mock-front-door/zones", headers=headers)).json() == []


async def test_zone_rejects_inverted_rectangle(client):
    headers = await _headers(client)
    bad = dict(DRIVEWAY, x1=0.8, x2=0.2)
    response = await client.post("/api/v1/admin/cameras/mock-front-door/zones", json=bad, headers=headers)
    assert response.status_code == 422


async def test_zone_of_another_camera_is_not_reachable(client):
    headers = await _headers(client)
    created = await client.post("/api/v1/admin/cameras/mock-front-door/zones", json=DRIVEWAY, headers=headers)
    zone_id = created.json()["id"]
    response = await client.delete(f"/api/v1/admin/cameras/mock-garden/zones/{zone_id}", headers=headers)
    assert response.status_code == 404


# --- pipeline ---------------------------------------------------------------


async def _add_zone(client, camera_id: str, zone: dict) -> None:
    headers = await _headers(client)
    created = await client.post(f"/api/v1/admin/cameras/{camera_id}/zones", json=zone, headers=headers)
    assert created.status_code == 201, created.text


async def test_person_event_gets_ai_analysis_and_best_photo(client):
    created = await client.post("/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": "person"})
    assert created.status_code == 200, created.text
    event_id = created.json()["id"]

    detail = await client.get(f"/api/v1/events/{event_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["type"] == "person"
    assert "person" in body["tags"]
    assert body["ai_analysis"] is not None
    assert body["ai_analysis"]["objects"] == ["person"]
    assert body["ai_analysis"]["provider"] == "mock"
    assert body["best_photo_path"]

    async with SessionLocal() as session:
        rows = (await session.execute(select(AIAnalysis).where(AIAnalysis.event_id == event_id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].embedding_dimensions == len(rows[0].embedding) > 0
        assert rows[0].detections[0]["label"] == "person"


async def test_person_in_driveway_zone_is_tagged(client):
    await _add_zone(client, "mock-garden", DRIVEWAY)
    mock_detector().set_script(
        "mock-garden", [Detection("person", 0.92, BoundingBox(0.1, 0.5, 0.3, 0.9))]
    )
    created = await client.post("/api/v1/mock/events", json={"camera_id": "mock-garden", "type": "person"})
    body = (await client.get(f"/api/v1/events/{created.json()['id']}")).json()
    assert body["zone"] == "driveway"
    assert "driveway-access" in body["tags"]
    assert body["type"] == "person"


async def test_mailbox_zone_detection_becomes_a_package_event(client):
    await _add_zone(client, "mock-garden", MAILBOX)
    mock_detector().set_script(
        "mock-garden", [Detection("person", 0.88, BoundingBox(0.72, 0.35, 0.92, 0.65))]
    )
    created = await client.post("/api/v1/mock/events", json={"camera_id": "mock-garden", "type": "motion"})
    body = (await client.get(f"/api/v1/events/{created.json()['id']}")).json()
    assert body["zone"] == "mailbox"
    assert "mailbox" in body["tags"]
    assert body["type"] == "package"


async def test_animal_detection_produces_an_animal_event(client):
    mock_detector().set_script("mock-garden", [Detection("cat", 0.8, BoundingBox(0.4, 0.6, 0.5, 0.75))])
    created = await client.post("/api/v1/mock/events", json={"camera_id": "mock-garden", "type": "motion"})
    body = (await client.get(f"/api/v1/events/{created.json()['id']}")).json()
    assert body["type"] == "animal"
    assert "cat" in body["tags"]


class _StubIdentifier:
    """Stands in for the Foundry vision deployment."""

    name = "stub"

    def __init__(self, identity=None, error: Exception | None = None) -> None:
        self.identity = identity
        self.error = error

    async def identify_animal(self, image: bytes, content_type: str):
        if self.error is not None:
            raise self.error
        return self.identity


async def test_animal_event_reports_species_and_breed(client, monkeypatch):
    monkeypatch.setattr(
        ai_pipeline,
        "get_animal_identifier",
        lambda: _StubIdentifier(
            AnimalIdentity(species="dog", breed="Border Collie", confidence=0.83)
        ),
    )
    mock_detector().set_script("mock-garden", [Detection("dog", 0.9, BoundingBox(0.4, 0.5, 0.6, 0.9))])

    created = await client.post("/api/v1/mock/events", json={"camera_id": "mock-garden", "type": "motion"})
    body = (await client.get(f"/api/v1/events/{created.json()['id']}")).json()

    assert body["type"] == "animal"
    assert body["animal"] == {
        "species": "dog",
        "breed": "Border Collie",
        "confidence": 0.83,
        "description": None,
    }
    assert "Border Collie" in body["tags"]
    assert "Border Collie" in body["description"]


async def test_unnamed_species_still_produces_an_animal_event(client, monkeypatch):
    """"Something else" is a real answer, not a failure."""
    monkeypatch.setattr(
        ai_pipeline, "get_animal_identifier", lambda: _StubIdentifier(AnimalIdentity(species="other"))
    )
    mock_detector().set_script("mock-garden", [Detection("animal", 0.7, BoundingBox(0.3, 0.4, 0.5, 0.8))])

    created = await client.post("/api/v1/mock/events", json={"camera_id": "mock-garden", "type": "motion"})
    body = (await client.get(f"/api/v1/events/{created.json()['id']}")).json()

    assert body["type"] == "animal"
    assert body["animal"]["species"] == "other"
    assert body["animal"]["breed"] is None
    assert "An animal was seen" in body["description"]


async def test_identification_failure_never_breaks_the_event(client, monkeypatch):
    """SPEC 43: a breed lookup is not worth losing a sighting over."""
    monkeypatch.setattr(
        ai_pipeline,
        "get_animal_identifier",
        lambda: _StubIdentifier(error=RuntimeError("foundry is down")),
    )
    mock_detector().set_script("mock-garden", [Detection("dog", 0.9, BoundingBox(0.4, 0.5, 0.6, 0.9))])

    created = await client.post("/api/v1/mock/events", json={"camera_id": "mock-garden", "type": "motion"})
    assert created.status_code == 200
    body = (await client.get(f"/api/v1/events/{created.json()['id']}")).json()

    assert body["type"] == "animal"
    assert body["animal"] is None
    assert "dog" in body["tags"]


async def test_event_exposes_drawable_detection_borders(client):
    """The UI must be able to outline the subject without re-deriving the crop."""
    mock_detector().set_script(
        "mock-garden", [Detection("person", 0.93, BoundingBox(0.35, 0.3, 0.55, 0.85))]
    )
    created = await client.post("/api/v1/mock/events", json={"camera_id": "mock-garden", "type": "motion"})
    body = (await client.get(f"/api/v1/events/{created.json()['id']}")).json()

    boxes = body["photo_boxes"]
    assert boxes, "a detected person must publish a border for the preview image"
    box = boxes[0]["box"]
    assert boxes[0]["label"] == "person"
    # Normalized against the stored photo, so it is directly drawable.
    assert 0.0 <= box["x1"] < box["x2"] <= 1.0
    assert 0.0 <= box["y1"] < box["y2"] <= 1.0


async def test_motion_without_detections_stays_motion(client):
    created = await client.post("/api/v1/mock/events", json={"camera_id": "mock-garden", "type": "motion"})
    body = (await client.get(f"/api/v1/events/{created.json()['id']}")).json()
    assert body["type"] == "motion"
    assert body["zone"] is None


async def test_unknown_event_id_returns_404(client):
    assert (await client.get("/api/v1/events/does-not-exist")).status_code == 404


# --- activity correlation ---------------------------------------------------


async def test_close_events_across_cameras_form_one_activity(client):
    first = await client.post("/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": "person"})
    second = await client.post(
        "/api/v1/mock/events", json={"camera_id": "mock-eufy-doorbell", "type": "doorbell"}
    )
    ids = {first.json()["id"], second.json()["id"]}

    activities = (await client.get("/api/v1/activities")).json()
    assert len(activities) == 1
    activity = activities[0]
    assert set(activity["event_ids"]) == ids
    assert set(activity["cameras"]) == {"mock-front-door", "mock-eufy-doorbell"}
    assert activity["category"] == "visitor"
    assert activity["summary"]
    assert 0.0 < activity["confidence"] <= 0.9

    detail = await client.get(f"/api/v1/activities/{activity['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == activity["id"]
    assert len(detail.json()["events"]) == 2


async def test_activity_summary_only_mentions_real_events(client):
    created = await client.post("/api/v1/mock/events", json={"camera_id": "mock-garden", "type": "motion"})
    activity = (await client.get("/api/v1/activities")).json()[0]
    assert activity["event_ids"] == [created.json()["id"]]
    assert activity["summary"].count("|") == 0


async def test_unknown_activity_returns_404(client):
    assert (await client.get("/api/v1/activities/nope")).status_code == 404


# --- audio capability -------------------------------------------------------


async def test_audio_detection_capability_is_advertised_for_every_camera(client):
    cameras = (await client.get("/api/v1/cameras")).json()
    assert cameras
    for camera in cameras:
        status = camera["capabilities"]["audioDetection"]
        assert status in {"SUPPORTED", "UNSUPPORTED", "UNAVAILABLE", "UNKNOWN"}


async def test_audio_detection_is_unavailable_for_providers_without_audio(client):
    camera = (await client.get("/api/v1/cameras/mock-garden")).json()
    assert camera["capabilities"]["audioDetection"] in {"UNSUPPORTED", "UNAVAILABLE"}


async def test_audio_endpoint_is_disabled_by_default(client):
    response = await client.post(
        "/api/v1/cameras/mock-front-door/audio/analyze", json={"pcm_base64": "AAAA"}
    )
    assert response.status_code == 503


async def test_settings_expose_ai_pipeline_configuration(client):
    body = (await client.get("/api/v1/settings")).json()
    assert body["ai_detector_backend"] == "mock"
    assert body["ai_analysis_enabled"] is True
    assert body["audio_detection_enabled"] is False
