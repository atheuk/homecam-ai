import pytest


@pytest.mark.asyncio
async def test_provider_outage_is_isolated_from_other_providers(client):
    """SPEC 2.5/43: one provider failing (simulated Eufy HomeBase outage)
    must not prevent cameras from the other provider from being served."""
    outage = await client.post("/api/v1/mock/providers/mock-eufy/outage", json={"unavailable": True})
    assert outage.status_code == 200
    assert outage.json()["status"] == "OFFLINE"

    cameras = (await client.get("/api/v1/cameras")).json()
    ids = {c["id"] for c in cameras}
    # The four mock-provider cameras are still listed even though mock-eufy is down.
    assert {"mock-front-door", "mock-driveway", "mock-backyard", "mock-garden"} <= ids

    providers = (await client.get("/api/v1/providers")).json()
    by_id = {p["provider_id"]: p for p in providers}
    assert by_id["mock-eufy"]["status"] == "OFFLINE"
    assert by_id["mock"]["status"] == "ONLINE"


@pytest.mark.asyncio
async def test_outage_recovers_when_cleared(client):
    await client.post("/api/v1/mock/providers/mock-eufy/outage", json={"unavailable": True})
    await client.post("/api/v1/mock/providers/mock-eufy/outage", json={"unavailable": False})
    providers = (await client.get("/api/v1/providers")).json()
    by_id = {p["provider_id"]: p for p in providers}
    assert by_id["mock-eufy"]["status"] == "ONLINE"


@pytest.mark.asyncio
async def test_outage_for_unknown_provider_404(client):
    r = await client.post("/api/v1/mock/providers/does-not-exist/outage", json={"unavailable": True})
    assert r.status_code == 404
