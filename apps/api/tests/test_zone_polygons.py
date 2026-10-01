"""Polygon zones and the admin still endpoint that backs the visual editor.

Covers the drawing geometry (normalization, clipping-based overlap), the
polygon-aware admin CRUD, and the authenticated still capture including its
offline/cooldown behaviour so a disconnected channel is never hammered.
"""
import pytest
from types import SimpleNamespace
from sqlalchemy import delete

from app.ai.detector import BoundingBox
from app.ai.zones import (
    MAX_ZONE_POINTS,
    Zone,
    bbox_of_points,
    clip_polygon_to_bbox,
    normalize_points,
    polygon_area,
    zones_for_bbox,
)
from app.db import SessionLocal
from app.models.db import CameraZone
from app.services import camera_stills


@pytest.fixture(autouse=True)
async def _clean_zones(client):
    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(CameraZone))
            await session.commit()

    camera_stills.reset_cooldowns()
    await _clear()
    yield
    camera_stills.reset_cooldowns()
    await _clear()


async def _headers(client) -> dict[str, str]:
    email = "polygon-tests@example.com"
    password = "supersecret1"
    register = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert register.status_code in (201, 409), register.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["access_token"]}


TRIANGLE = [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]


# --- geometry ---------------------------------------------------------------


def test_points_are_normalized_into_tuples():
    assert normalize_points([[0.1, 0.2], [0.4, 0.2], [0.4, 0.6]]) == (
        (0.1, 0.2),
        (0.4, 0.2),
        (0.4, 0.6),
    )


def test_missing_points_stay_missing():
    assert normalize_points(None) is None
    assert normalize_points([]) is None


@pytest.mark.parametrize(
    "points",
    [
        [[0.1, 0.1], [0.2, 0.2]],  # too few to enclose anything
        [[0.1, 0.1], [0.2, 0.2], [0.3, 0.3]],  # collinear, zero area
        [[0.1, 0.1], [1.4, 0.2], [0.3, 0.3]],  # outside 0..1
        [[0.1, 0.1], [0.2], [0.3, 0.3]],  # not an (x, y) pair
        [[0.1, 0.1], ["a", 0.2], [0.3, 0.3]],  # not numeric
        [[0.1, 0.1], [True, 0.2], [0.3, 0.3]],  # booleans are not coordinates
        [[0, 0], [1, 1], [0, 1], [0.5, 0]],  # nonzero-area self-intersection
        [[0, 0], [1, 0], [1, 1], [0, 1], [0.5, 0.5], [0.5, 0]],  # non-adjacent touch
        [[0, 0], [1, 0], [0.5, 0], [1, 1], [0, 1]],  # overlapping adjacent edges
        [[0.1, 0.1]] * (MAX_ZONE_POINTS + 1),  # unbounded polygons
        "not-a-list",
    ],
)
def test_bad_polygons_are_rejected(points):
    with pytest.raises(ValueError):
        normalize_points(points)


def test_polygon_from_persisted_row_rejects_self_intersection():
    row = SimpleNamespace(
        name="bad",
        kind="mailbox",
        x1=0.0,
        y1=0.0,
        x2=1.0,
        y2=1.0,
        points=[[0, 0], [1, 1], [0, 1], [0.5, 0]],
    )
    with pytest.raises(ValueError, match="self-intersect"):
        Zone.from_row(row)


def test_bounding_box_encloses_every_point():
    box = bbox_of_points(normalize_points(TRIANGLE))
    assert (box.x1, box.y1, box.x2, box.y2) == (0.1, 0.1, 0.9, 0.9)


def test_polygon_area_is_orientation_independent():
    square = ((0.0, 0.0), (0.0, 0.5), (0.5, 0.5), (0.5, 0.0))
    assert polygon_area(square) == pytest.approx(0.25)
    assert polygon_area(tuple(reversed(square))) == pytest.approx(0.25)


def test_clipping_keeps_only_the_overlapping_part():
    square = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    clipped = clip_polygon_to_bbox(square, BoundingBox(0.25, 0.25, 0.75, 0.75))
    assert polygon_area(clipped) == pytest.approx(0.25)


def test_detection_outside_the_polygon_does_not_match_its_bounding_box():
    """The corner of a triangle's box is inside the box but outside the shape."""
    points = normalize_points([[0.0, 1.0], [1.0, 1.0], [1.0, 0.0]])
    zone = Zone(name="triangle", kind="mailbox", bbox=bbox_of_points(points), points=points)
    corner = BoundingBox(0.0, 0.0, 0.2, 0.2)
    assert zone.overlap(corner) == pytest.approx(0.0, abs=1e-6)
    assert zones_for_bbox(corner, [zone], 0.1) == []

    inside = BoundingBox(0.7, 0.7, 0.95, 0.95)
    assert zone.overlap(inside) > 0.5
    assert [z.name for z, _ in zones_for_bbox(inside, [zone], 0.1)] == ["triangle"]


def test_rectangle_zones_keep_plain_overlap():
    zone = Zone(name="drive", kind="driveway", bbox=BoundingBox(0.0, 0.0, 0.5, 1.0))
    assert zone.overlap(BoundingBox(0.0, 0.0, 0.5, 1.0)) == pytest.approx(1.0)


# --- admin CRUD -------------------------------------------------------------


async def test_polygon_zone_derives_its_bounding_box(client):
    headers = await _headers(client)
    created = await client.post(
        "/api/v1/admin/cameras/mock-front-door/zones",
        json={"name": "mailbox", "kind": "mailbox", "points": TRIANGLE},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    zone = created.json()
    assert zone["points"] == TRIANGLE
    assert (zone["x1"], zone["y1"], zone["x2"], zone["y2"]) == (0.1, 0.1, 0.9, 0.9)


async def test_polygon_zone_can_be_redrawn_and_flattened(client):
    headers = await _headers(client)
    created = await client.post(
        "/api/v1/admin/cameras/mock-front-door/zones",
        json={"name": "bin", "kind": "bin", "points": TRIANGLE},
        headers=headers,
    )
    zone_id = created.json()["id"]

    redrawn = await client.put(
        f"/api/v1/admin/cameras/mock-front-door/zones/{zone_id}",
        json={"points": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8]]},
        headers=headers,
    )
    assert redrawn.status_code == 200, redrawn.text
    assert redrawn.json()["points"] == [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8]]
    assert (redrawn.json()["x1"], redrawn.json()["x2"]) == (0.2, 0.8)

    flattened = await client.put(
        f"/api/v1/admin/cameras/mock-front-door/zones/{zone_id}",
        json={"points": []},
        headers=headers,
    )
    assert flattened.status_code == 200, flattened.text
    assert flattened.json()["points"] is None
    assert (flattened.json()["x1"], flattened.json()["x2"]) == (0.2, 0.8)


@pytest.mark.parametrize(
    "points",
    [
        [[0.1, 0.1], [0.5, 0.5]],
        [[0.1, 0.1], [0.5, 0.5], [1.5, 0.5]],
        [[0.1, 0.1], [0.2, 0.2], [0.3, 0.3]],
        [[0, 0], [1, 1], [0, 1], [0.5, 0]],
    ],
)
async def test_invalid_polygons_are_refused(client, points):
    headers = await _headers(client)
    response = await client.post(
        "/api/v1/admin/cameras/mock-front-door/zones",
        json={"name": "bad", "kind": "mailbox", "points": points},
        headers=headers,
    )
    assert response.status_code == 422, response.text


async def test_a_zone_needs_some_geometry(client):
    headers = await _headers(client)
    response = await client.post(
        "/api/v1/admin/cameras/mock-front-door/zones",
        json={"name": "empty", "kind": "mailbox"},
        headers=headers,
    )
    assert response.status_code == 422


async def test_rectangle_zones_still_have_no_points(client):
    headers = await _headers(client)
    created = await client.post(
        "/api/v1/admin/cameras/mock-front-door/zones",
        json={"name": "drive", "kind": "driveway", "x1": 0.0, "y1": 0.4, "x2": 0.6, "y2": 1.0},
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["points"] is None


# --- still endpoint ---------------------------------------------------------


async def test_still_requires_authentication(anonymous_client):
    response = await anonymous_client.get("/api/v1/admin/cameras/mock-front-door/still")
    assert response.status_code == 401


async def test_still_returns_a_data_url(client):
    headers = await _headers(client)
    response = await client.get("/api/v1/admin/cameras/mock-front-door/still", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["camera_id"] == "mock-front-door"
    assert body["image"].startswith("data:image/jpeg;base64,")
    assert body["source"] in ("stream", "snapshot")
    assert body["captured_at"]


async def test_still_tolerates_images_it_cannot_measure(client):
    """The mock provider returns a placeholder blob, not a real JPEG."""
    headers = await _headers(client)
    body = (await client.get("/api/v1/admin/cameras/mock-front-door/still", headers=headers)).json()
    assert body["width"] is None and body["height"] is None


async def test_still_of_an_unknown_camera_is_404(client):
    headers = await _headers(client)
    response = await client.get("/api/v1/admin/cameras/does-not-exist/still", headers=headers)
    assert response.status_code == 404


async def test_offline_cameras_are_not_contacted(client, monkeypatch):
    headers = await _headers(client)
    calls: list[str] = []

    async def _fail(session, camera_id):
        calls.append(camera_id)
        return None

    from app.services import cameras as camera_service

    original = camera_service.get_camera

    async def _offline(session, camera_id):
        row = await original(session, camera_id)
        if row is not None:
            row.online = False
        return row

    monkeypatch.setattr(camera_stills.camera_service, "get_camera", _offline)
    monkeypatch.setattr(camera_stills, "find_provider_for_camera", _fail)

    response = await client.get("/api/v1/admin/cameras/mock-front-door/still", headers=headers)
    assert response.status_code == 503
    assert "offline" in response.json()["detail"]
    assert calls == []


async def test_a_failing_camera_gets_a_cooldown(client, monkeypatch):
    headers = await _headers(client)
    attempts: list[str] = []

    class _Broken:
        id = "broken"

        async def get_snapshot(self, camera_id):
            attempts.append(camera_id)
            from app.providers.base import CameraOfflineError

            raise CameraOfflineError(camera_id)

    async def _provider(camera_id):
        return _Broken()

    monkeypatch.setattr(camera_stills, "find_provider_for_camera", _provider)

    first = await client.get("/api/v1/admin/cameras/mock-front-door/still", headers=headers)
    second = await client.get("/api/v1/admin/cameras/mock-front-door/still", headers=headers)
    assert first.status_code == 503 and second.status_code == 503
    assert attempts == ["mock-front-door"], "the second attempt must be served by the cooldown"
    assert "shortly" in second.json()["detail"]
