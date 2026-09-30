"""Incident grouping/lifecycle/escalation/export (SPEC follow-up).

Exercises the full path through the real API: mock events -> the
``events.py`` hook -> ``incidents.route_event`` -> incident visible via
``/api/v1/security/incidents``. Deliberately does not call
``incidents.py`` internals directly for the routing tests, since the whole
point is that a real ingested event ends up as an incident without any
extra wiring in the test itself.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.db import SessionLocal
from app.models.db import CameraZone, Incident


async def _headers(client, email: str) -> dict[str, str]:
    password = "supersecret1"
    register = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert register.status_code in (201, 409), register.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["access_token"]}


@pytest.fixture(autouse=True)
async def _clean_incidents():
    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(Incident))
            await session.execute(delete(CameraZone))
            await session.commit()

    await _clear()
    yield
    await _clear()


async def _set_mode(client, headers, mode: str):
    r = await client.put("/api/v1/security/mode", json={"mode": mode}, headers=headers)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_person_event_while_disarmed_raises_no_incident(client):
    headers = await _headers(client, "incidents-disarmed@example.com")
    await _set_mode(client, headers, "disarmed")

    r = await client.post("/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": "person"})
    assert r.status_code == 200

    incidents = (await client.get("/api/v1/security/incidents", headers=headers)).json()
    assert incidents == []


@pytest.mark.asyncio
async def test_person_event_while_away_raises_intrusion_incident(client):
    headers = await _headers(client, "incidents-away@example.com")
    await _set_mode(client, headers, "away")

    r = await client.post("/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": "person"})
    assert r.status_code == 200

    incidents = (await client.get("/api/v1/security/incidents", headers=headers)).json()
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident["kind"] == "intrusion"
    assert incident["status"] == "open"
    assert incident["severity"] == "high"
    assert incident["mode_at_creation"] == "away"
    assert incident["event_count"] == 1
    assert incident["summary"]

    await _set_mode(client, headers, "disarmed")


@pytest.mark.asyncio
async def test_repeated_events_within_merge_window_are_grouped_not_duplicated(client):
    headers = await _headers(client, "incidents-merge@example.com")
    await _set_mode(client, headers, "away")

    await client.post("/api/v1/mock/events", json={"camera_id": "mock-garden", "type": "person"})
    await client.post("/api/v1/mock/events", json={"camera_id": "mock-garden", "type": "person"})

    incidents = (await client.get("/api/v1/security/incidents", headers=headers)).json()
    assert len(incidents) == 1
    assert incidents[0]["event_count"] == 2
    assert len(incidents[0]["event_ids"]) == 2

    await _set_mode(client, headers, "disarmed")


@pytest.mark.asyncio
async def test_events_outside_merge_window_start_a_new_incident(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "incident_merge_window_seconds", 0.01)
    headers = await _headers(client, "incidents-window@example.com")
    await _set_mode(client, headers, "away")

    await client.post("/api/v1/mock/events", json={"camera_id": "mock-backyard", "type": "person"})
    await asyncio.sleep(0.05)
    await client.post("/api/v1/mock/events", json={"camera_id": "mock-backyard", "type": "person"})

    incidents = (await client.get("/api/v1/security/incidents", headers=headers)).json()
    assert len(incidents) == 2
    assert all(i["event_count"] == 1 for i in incidents)

    await _set_mode(client, headers, "disarmed")


@pytest.mark.asyncio
async def test_driveway_zone_is_alert_worthy_while_home(client):
    """A ``driveway`` zone is alert-worthy while ``home`` (unlike a plain
    unzoned front-door event, see ``test_person_event_while_home_is_quiet``
    for the un-zoned counterpart), per the kind-based arming rule."""
    from app.services import events as event_service

    headers = await _headers(client, "incidents-driveway@example.com")
    zone = await client.post(
        "/api/v1/admin/cameras/mock-front-door/zones",
        json={"name": "Driveway", "kind": "driveway", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0},
        headers=headers,
    )
    assert zone.status_code == 201

    await _set_mode(client, headers, "home")
    async with SessionLocal() as session:
        await event_service.create_and_broadcast_event(
            session,
            {
                "id": "evt-driveway-1",
                "camera_id": "mock-front-door",
                "camera_name": "Front Door",
                "type": "person",
                "priority": "high",
                "source": "provider",
                "start_time": datetime.now(timezone.utc).isoformat(),
                "description": "Mock person detected",
                "zone": "Driveway",
            },
        )

    incidents = (await client.get("/api/v1/security/incidents", headers=headers)).json()
    assert any(i["zone"] == "Driveway" for i in incidents)

    await _set_mode(client, headers, "disarmed")


@pytest.mark.asyncio
async def test_person_event_while_home_is_quiet(client):
    """The un-zoned counterpart: a plain (no zone) person event is not
    alert-worthy while ``home`` -- only ``away``/``night`` by default."""
    headers = await _headers(client, "incidents-home-quiet@example.com")
    await _set_mode(client, headers, "home")

    r = await client.post("/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": "person"})
    assert r.status_code == 200

    incidents = (await client.get("/api/v1/security/incidents", headers=headers)).json()
    assert incidents == []

    await _set_mode(client, headers, "disarmed")


@pytest.mark.asyncio
async def test_acknowledge_then_resolve_then_export_lifecycle(client):
    headers = await _headers(client, "incidents-lifecycle@example.com")
    await _set_mode(client, headers, "away")

    await client.post("/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": "person"})
    incidents = (await client.get("/api/v1/security/incidents", headers=headers)).json()
    incident_id = incidents[0]["id"]

    ack = await client.post(f"/api/v1/security/incidents/{incident_id}/acknowledge", headers=headers)
    assert ack.status_code == 200
    assert ack.json()["status"] == "acknowledged"
    assert ack.json()["acknowledged_by"]

    resolve = await client.post(f"/api/v1/security/incidents/{incident_id}/resolve", headers=headers)
    assert resolve.status_code == 200
    assert resolve.json()["status"] == "resolved"
    assert resolve.json()["resolved_by"]

    export = await client.get(f"/api/v1/security/incidents/{incident_id}/export", headers=headers)
    assert export.status_code == 200
    body = export.json()
    assert body["incident"]["id"] == incident_id
    assert len(body["events"]) == 1
    # Evidence export never includes raw camera credentials/secrets.
    assert "secret" not in str(body).lower()
    assert "password" not in str(body).lower()

    audit = await client.get("/api/v1/security/audit-log", params={"action": "incident.exported"}, headers=headers)
    assert any(e["target_id"] == incident_id for e in audit.json())

    await _set_mode(client, headers, "disarmed")


@pytest.mark.asyncio
async def test_get_unknown_incident_is_404(client):
    headers = await _headers(client, "incidents-404@example.com")
    r = await client.get("/api/v1/security/incidents/does-not-exist", headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_escalation_bumps_level_for_stale_unacknowledged_incidents(client):
    from app.services import incidents as incident_service

    headers = await _headers(client, "incidents-escalate@example.com")
    await _set_mode(client, headers, "away")
    await client.post("/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": "person"})

    incidents = (await client.get("/api/v1/security/incidents", headers=headers)).json()
    incident_id = incidents[0]["id"]

    # Force the incident to look overdue without waiting real wall-clock time.
    async with SessionLocal() as session:
        incident = await session.get(Incident, incident_id)
        incident.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await session.commit()

    escalated = await incident_service.escalate_due_incidents(SessionLocal)
    assert escalated >= 1

    updated = (await client.get(f"/api/v1/security/incidents/{incident_id}", headers=headers)).json()
    assert updated["escalation_level"] >= 1
    assert updated["last_escalated_at"]

    await _set_mode(client, headers, "disarmed")
