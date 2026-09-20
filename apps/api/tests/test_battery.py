import pytest


@pytest.mark.asyncio
async def test_low_battery_generates_high_priority_event(client):
    r = await client.post("/api/v1/mock/cameras/mock-eufy-doorbell/battery", json={"battery_level": 15})
    assert r.status_code == 200
    assert r.json()["battery_level"] == 15

    events = (await client.get("/api/v1/events")).json()
    low_battery_events = [e for e in events if e["type"] == "battery_low"]
    assert len(low_battery_events) == 1
    assert low_battery_events[0]["priority"] == "high"
    assert "15" in low_battery_events[0]["description"]


@pytest.mark.asyncio
async def test_battery_above_threshold_does_not_warn(client):
    before = (await client.get("/api/v1/events")).json()
    before_count = len([e for e in before if e["type"] == "battery_low"])

    await client.post("/api/v1/mock/cameras/mock-eufy-doorbell/battery", json={"battery_level": 90})

    after = (await client.get("/api/v1/events")).json()
    after_count = len([e for e in after if e["type"] == "battery_low"])
    assert after_count == before_count


@pytest.mark.asyncio
async def test_battery_on_non_battery_camera_is_422(client):
    r = await client.post("/api/v1/mock/cameras/mock-front-door/battery", json={"battery_level": 10})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_battery_out_of_range_is_422(client):
    r = await client.post("/api/v1/mock/cameras/mock-eufy-doorbell/battery", json={"battery_level": 150})
    assert r.status_code == 422
