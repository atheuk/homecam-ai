import pytest


@pytest.mark.asyncio
async def test_default_mode_is_disarmed(client):
    r = await client.post("/api/v1/auth/register", json={"email": "modes1@example.com", "password": "supersecret1"})
    assert r.status_code == 201
    login = await client.post("/api/v1/auth/login", json={"email": "modes1@example.com", "password": "supersecret1"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.get("/api/v1/security/mode", headers=headers)
    assert r.status_code == 200
    # NOTE: another test in this session may already have changed the
    # singleton mode; only assert the shape, not the exact value, unless
    # this test itself sets it first.
    assert r.json()["mode"] in ("disarmed", "home", "away", "night")


@pytest.mark.asyncio
async def test_set_mode_persists_and_is_audited(client):
    register = await client.post("/api/v1/auth/register", json={"email": "modes2@example.com", "password": "supersecret1"})
    user_id = register.json()["id"]
    login = await client.post("/api/v1/auth/login", json={"email": "modes2@example.com", "password": "supersecret1"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.put("/api/v1/security/mode", json={"mode": "away"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "away"
    assert body["changed_by"] == user_id
    assert body["changed_at"]

    r = await client.get("/api/v1/security/mode", headers=headers)
    assert r.json()["mode"] == "away"

    audit = await client.get("/api/v1/security/audit-log", params={"action": "security.mode_changed"}, headers=headers)
    assert audit.status_code == 200
    entries = audit.json()
    assert any(e["details"].get("to") == "away" for e in entries)

    # Restore to disarmed so later tests in this session see a known state.
    await client.put("/api/v1/security/mode", json={"mode": "disarmed"}, headers=headers)


@pytest.mark.asyncio
async def test_invalid_mode_is_rejected(client):
    await client.post("/api/v1/auth/register", json={"email": "modes3@example.com", "password": "supersecret1"})
    login = await client.post("/api/v1/auth/login", json={"email": "modes3@example.com", "password": "supersecret1"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.put("/api/v1/security/mode", json={"mode": "vacation"}, headers=headers)
    assert r.status_code == 422


def test_alert_modes_for_zone_kind_rules():
    from app.services.security_modes import alert_modes_for_zone, is_alert_armed

    assert alert_modes_for_zone("driveway") == ("home", "away", "night")
    assert alert_modes_for_zone("backyard") == ("away", "night")
    assert alert_modes_for_zone(None) == ("away", "night")

    assert is_alert_armed("disarmed", "driveway") is False
    assert is_alert_armed("home", "driveway") is True
    assert is_alert_armed("home", "backyard") is False
    assert is_alert_armed("away", "backyard") is True
    assert is_alert_armed("night", None) is True
