import pytest


@pytest.mark.asyncio
async def test_capabilities_are_status_enum_values(client):
    """SPEC section 5: capabilities are a status map (SUPPORTED/
    UNSUPPORTED/UNAVAILABLE/UNKNOWN), not a flat feature list."""
    r = await client.get("/api/v1/cameras/mock-eufy-doorbell/capabilities")
    assert r.status_code == 200
    caps = r.json()
    assert caps["doorbellEvents"] == "SUPPORTED"
    assert caps["battery"] == "SUPPORTED"
    assert caps["vehicleEvents"] == "UNSUPPORTED"
    assert set(caps.values()) <= {"SUPPORTED", "UNSUPPORTED", "UNAVAILABLE", "UNKNOWN"}


@pytest.mark.asyncio
async def test_camera_without_battery_reports_unsupported(client):
    r = await client.get("/api/v1/cameras/mock-front-door/capabilities")
    assert r.status_code == 200
    assert r.json()["battery"] == "UNSUPPORTED"


@pytest.mark.asyncio
async def test_capabilities_for_unknown_camera_404(client):
    r = await client.get("/api/v1/cameras/does-not-exist/capabilities")
    assert r.status_code == 404
