"""Audit trail: ordering, filtering, and content-safety (SPEC follow-up)."""
import pytest


async def _headers(client, email: str) -> dict[str, str]:
    password = "supersecret1"
    register = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert register.status_code in (201, 409), register.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["access_token"]}


@pytest.mark.asyncio
async def test_mode_changes_are_audit_logged_most_recent_first(client):
    headers = await _headers(client, "audit-order@example.com")

    await client.put("/api/v1/security/mode", json={"mode": "home"}, headers=headers)
    await client.put("/api/v1/security/mode", json={"mode": "away"}, headers=headers)
    await client.put("/api/v1/security/mode", json={"mode": "disarmed"}, headers=headers)

    r = await client.get("/api/v1/security/audit-log", params={"action": "security.mode_changed"}, headers=headers)
    assert r.status_code == 200
    entries = r.json()
    assert len(entries) >= 3
    timestamps = [e["created_at"] for e in entries]
    assert timestamps == sorted(timestamps, reverse=True)

    # The most recent entry recorded is the disarmed transition.
    latest = entries[0]
    assert latest["details"]["to"] == "disarmed"
    assert latest["action"] == "security.mode_changed"
    assert latest["actor_user_id"]


@pytest.mark.asyncio
async def test_audit_log_never_contains_camera_credentials_or_raw_media(client):
    headers = await _headers(client, "audit-content-safety@example.com")
    await client.put("/api/v1/security/mode", json={"mode": "away"}, headers=headers)
    await client.post("/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": "person"})

    incidents = (await client.get("/api/v1/security/incidents", headers=headers)).json()
    incident_id = incidents[0]["id"]
    await client.get(f"/api/v1/security/incidents/{incident_id}/export", headers=headers)

    audit = await client.get("/api/v1/security/audit-log", headers=headers)
    dump = str(audit.json()).lower()
    for forbidden in ("password", "rtsp://", "token", "secret", "api_key"):
        assert forbidden not in dump

    await client.put("/api/v1/security/mode", json={"mode": "disarmed"}, headers=headers)


@pytest.mark.asyncio
async def test_unknown_action_filter_returns_empty_list(client):
    headers = await _headers(client, "audit-unknown-filter@example.com")
    r = await client.get("/api/v1/security/audit-log", params={"action": "not.a.real.action"}, headers=headers)
    assert r.status_code == 200
    assert r.json() == []
