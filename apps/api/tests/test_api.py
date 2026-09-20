import pytest


@pytest.mark.asyncio
async def test_cameras_and_mock_event(client):
    r = await client.get("/api/v1/cameras")
    assert r.status_code == 200
    cameras = r.json()
    assert len(cameras) == 5
    assert {c["id"] for c in cameras} == {
        "mock-front-door", "mock-driveway", "mock-backyard", "mock-garden", "mock-eufy-doorbell",
    }

    doorbell_event = await client.post("/api/v1/mock/events", json={"camera_id": "mock-eufy-doorbell", "type": "doorbell"})
    assert doorbell_event.status_code == 200
    assert doorbell_event.json()["type"] == "doorbell"
    assert doorbell_event.json()["priority"] == "high"

    person_event = await client.post("/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": "person"})
    assert person_event.json()["priority"] == "normal"

    events = (await client.get("/api/v1/events")).json()
    event_ids = {e["id"] for e in events}
    assert doorbell_event.json()["id"] in event_ids
    assert person_event.json()["id"] in event_ids
