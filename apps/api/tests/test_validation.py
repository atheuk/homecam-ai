import pytest


@pytest.mark.asyncio
async def test_get_unknown_camera_is_404(client):
    assert (await client.get("/api/v1/cameras/does-not-exist")).status_code == 404


@pytest.mark.asyncio
async def test_snapshot_unknown_camera_is_404(client):
    assert (await client.get("/api/v1/cameras/does-not-exist/snapshot")).status_code == 404


@pytest.mark.asyncio
async def test_live_unknown_camera_is_404(client):
    assert (await client.get("/api/v1/cameras/does-not-exist/live")).status_code == 404


@pytest.mark.asyncio
async def test_mock_event_unknown_camera_is_404(client):
    r = await client.post("/api/v1/mock/events", json={"camera_id": "does-not-exist", "type": "motion"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_provider_detail_unknown_is_404(client):
    assert (await client.get("/api/v1/providers/does-not-exist")).status_code == 404


@pytest.mark.asyncio
async def test_invalid_json_body_is_422(client):
    r = await client.post("/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": 12345})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_health_and_readiness_endpoints_respond(client):
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/ready")).status_code == 200
    r = await client.get("/api/v1/system/health")
    assert r.status_code == 200
    assert "providers" in r.json()
