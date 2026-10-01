"""Deterrence: the safety properties matter more than the feature.

These tests exist to make the hard limits executable - no autonomous
deterrence, no emergency dispatch, confirmation always required.
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal, init_db
from app.models.db import AuditLog, DeterrenceAction
from app.services import deterrence


@pytest.fixture(autouse=True)
async def _enabled(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "deterrence_enabled", True)

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(DeterrenceAction))
            await session.commit()

    await _clear()
    yield
    await _clear()


async def _request(action="siren", camera_id="mock-front-door"):
    async with SessionLocal() as session:
        row = await deterrence.request_action(
            session, camera_id=camera_id, action=action, reason="test", requested_by="user-1"
        )
        return row.id


# --- the hard limits --------------------------------------------------------------


def test_no_emergency_or_dispatch_action_exists():
    """HomeCam must never be able to contact emergency services."""
    assert deterrence.ACTIONS == ("siren", "light", "voice")
    for banned in ("emergency", "dispatch", "call", "police", "911", "999"):
        assert not any(banned in action for action in deterrence.ACTIONS)


def test_no_module_calls_deterrence_execution_automatically():
    """Nothing in the detection path may reach confirm/execute."""
    services = Path(__file__).resolve().parents[1] / "app" / "services"
    callers = []
    for path in services.glob("*.py"):
        if path.name == "deterrence.py":
            continue
        source = path.read_text(encoding="utf-8")
        if re.search(r"deterrence\.(confirm_action|request_action)", source):
            callers.append(path.name)
    assert callers == [], f"deterrence must not be driven from {callers}"


@pytest.mark.asyncio
async def test_requesting_an_action_executes_nothing(monkeypatch):
    executed = []

    async def _spy(action, camera_id):
        executed.append((action, camera_id))
        return "ran"

    monkeypatch.setattr(deterrence.get_provider(), "execute", _spy)
    action_id = await _request()
    async with SessionLocal() as session:
        row = await session.get(DeterrenceAction, action_id)
    assert row.status == "pending"
    assert row.confirmed_by is None
    assert executed == []


@pytest.mark.asyncio
async def test_capabilities_always_declare_human_confirmation():
    caps = deterrence.capabilities()
    assert caps["requires_human_confirmation"] is True
    assert {entry["action"] for entry in caps["actions"]} == set(deterrence.ACTIONS)


# --- lifecycle --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmation_executes_and_records_the_human():
    action_id = await _request()
    async with SessionLocal() as session:
        row = await deterrence.confirm_action(session, action_id, confirmed_by="user-1")
    assert row.status == "executed"
    assert row.confirmed_by == "user-1"
    assert "simulated siren" in row.result


@pytest.mark.asyncio
async def test_double_confirmation_is_rejected():
    action_id = await _request()
    async with SessionLocal() as session:
        await deterrence.confirm_action(session, action_id, confirmed_by="user-1")
        with pytest.raises(deterrence.DeterrenceError, match="already executed"):
            await deterrence.confirm_action(session, action_id, confirmed_by="user-1")


@pytest.mark.asyncio
async def test_expired_confirmation_is_refused():
    action_id = await _request()
    async with SessionLocal() as session:
        row = await session.get(DeterrenceAction, action_id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        await session.commit()
    async with SessionLocal() as session:
        with pytest.raises(deterrence.DeterrenceError, match="expired"):
            await deterrence.confirm_action(session, action_id, confirmed_by="user-1")
        row = await session.get(DeterrenceAction, action_id)
    assert row.status == "expired"


@pytest.mark.asyncio
async def test_cancelled_action_cannot_be_confirmed():
    action_id = await _request()
    async with SessionLocal() as session:
        await deterrence.cancel_action(session, action_id, actor="user-1")
        with pytest.raises(deterrence.DeterrenceError, match="already cancelled"):
            await deterrence.confirm_action(session, action_id, confirmed_by="user-1")


@pytest.mark.asyncio
async def test_unknown_action_kind_is_refused():
    async with SessionLocal() as session:
        with pytest.raises(deterrence.DeterrenceError, match="Unsupported"):
            await deterrence.request_action(session, camera_id="c", action="taser")


@pytest.mark.asyncio
async def test_disabled_feature_refuses_requests(monkeypatch):
    monkeypatch.setattr(settings, "deterrence_enabled", False)
    async with SessionLocal() as session:
        with pytest.raises(deterrence.DeterrenceError, match="disabled"):
            await deterrence.request_action(session, camera_id="c", action="siren")


@pytest.mark.asyncio
async def test_provider_failure_is_recorded_not_retried(monkeypatch):
    async def _boom(action, camera_id):
        raise RuntimeError("siren offline")

    monkeypatch.setattr(deterrence.get_provider(), "execute", _boom)
    action_id = await _request()
    async with SessionLocal() as session:
        row = await deterrence.confirm_action(session, action_id, confirmed_by="user-1")
    assert row.status == "failed"
    assert "siren offline" in row.result


@pytest.mark.asyncio
async def test_every_step_is_audit_logged():
    action_id = await _request()
    async with SessionLocal() as session:
        await deterrence.confirm_action(session, action_id, confirmed_by="user-1")
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.target_id == action_id))
        ).scalars().all()
    assert {row.action for row in rows} == {"deterrence.requested", "deterrence.executed"}


@pytest.mark.asyncio
async def test_list_actions_filters_by_camera():
    await _request(camera_id="mock-front-door")
    await _request(camera_id="mock-garden")
    async with SessionLocal() as session:
        assert len(await deterrence.list_actions(session)) == 2
        rows = await deterrence.list_actions(session, camera_id="mock-garden")
    assert [row.camera_id for row in rows] == ["mock-garden"]


# --- API ---------------------------------------------------------------------------


async def _headers(client) -> dict:
    email, password = "deterrence@example.com", "Sup3rSecret!"
    await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.mark.asyncio
async def test_deterrence_routes_require_auth(anonymous_client):
    assert (await anonymous_client.get("/api/v1/security/deterrence/capabilities")).status_code == 401
    r = await anonymous_client.post(
        "/api/v1/security/deterrence/actions",
        json={"camera_id": "mock-front-door", "action": "siren"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_request_then_confirm_over_the_api(client):
    headers = await _headers(client)
    created = await client.post(
        "/api/v1/security/deterrence/actions",
        json={"camera_id": "mock-front-door", "action": "light", "reason": "visitor at 2am"},
        headers=headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "pending"
    assert body["confirmed_by"] is None

    confirmed = await client.post(
        f"/api/v1/security/deterrence/actions/{body['id']}/confirm", headers=headers
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "executed"
    assert confirmed.json()["confirmed_by"]


@pytest.mark.asyncio
async def test_api_rejects_unsupported_action(client):
    headers = await _headers(client)
    r = await client.post(
        "/api/v1/security/deterrence/actions",
        json={"camera_id": "mock-front-door", "action": "emergency_call"},
        headers=headers,
    )
    assert r.status_code == 422
