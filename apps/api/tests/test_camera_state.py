import pytest


@pytest.mark.asyncio
async def test_simulate_camera_offline_updates_status_and_blocks_media(client):
    r = await client.post("/api/v1/mock/cameras/mock-driveway/status", json={"status": "offline"})
    assert r.status_code == 200
    assert r.json()["status"] == "offline"

    camera = (await client.get("/api/v1/cameras/mock-driveway")).json()
    assert camera["status"] == "offline"
    assert camera["online"] is False

    # SPEC 43: a failure must be reported gracefully, not crash the app.
    snapshot = await client.get("/api/v1/cameras/mock-driveway/snapshot")
    assert snapshot.status_code == 503
    live = await client.get("/api/v1/cameras/mock-driveway/live")
    assert live.status_code == 503


@pytest.mark.asyncio
async def test_simulate_camera_degraded(client):
    r = await client.post("/api/v1/mock/cameras/mock-backyard/status", json={"status": "degraded"})
    assert r.status_code == 200
    camera = (await client.get("/api/v1/cameras/mock-backyard")).json()
    assert camera["status"] == "degraded"


@pytest.mark.asyncio
async def test_partial_offline_marks_provider_degraded(client):
    await client.post("/api/v1/mock/cameras/mock-garden/status", json={"status": "offline"})
    providers = (await client.get("/api/v1/providers")).json()
    mock = next(p for p in providers if p["provider_id"] == "mock")
    assert mock["status"] == "DEGRADED"
    assert mock["online_camera_count"] == 3


@pytest.mark.asyncio
async def test_invalid_status_value_is_422(client):
    r = await client.post("/api/v1/mock/cameras/mock-driveway/status", json={"status": "not-a-status"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_status_for_unknown_camera_404(client):
    r = await client.post("/api/v1/mock/cameras/does-not-exist/status", json={"status": "offline"})
    assert r.status_code == 404
