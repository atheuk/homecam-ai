"""Auth hardening: failed-login lockout and bulk session revocation."""
import pytest


@pytest.mark.asyncio
async def test_account_locks_after_max_failed_attempts_then_recovers(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "auth_max_failed_attempts", 3)
    monkeypatch.setattr(settings, "auth_lockout_minutes", 15.0)

    email = "lockout@example.com"
    password = "supersecret1"
    await client.post("/api/v1/auth/register", json={"email": email, "password": password})

    for _ in range(3):
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
        assert r.status_code == 401

    locked = await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
    assert locked.status_code == 423

    # Even the *correct* password is rejected with 423 while locked -- must
    # not leak whether the password would otherwise have been right.
    still_locked = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert still_locked.status_code == 423

    # Lift the lockout window (simulate time passing) and confirm recovery.
    monkeypatch.setattr(settings, "auth_lockout_minutes", 0.0)
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.db import User

    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    recovered = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert recovered.status_code == 200

    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        assert user.failed_attempts == 0
        assert user.locked_until is None


@pytest.mark.asyncio
async def test_successful_login_resets_failed_attempt_counter(client):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.db import User

    email = "reset-attempts@example.com"
    password = "supersecret1"
    await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})

    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        assert user.failed_attempts == 1

    ok = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert ok.status_code == 200

    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        assert user.failed_attempts == 0


@pytest.mark.asyncio
async def test_revoke_all_sessions_invalidates_existing_bearer_tokens(client):
    email = "revoke-all@example.com"
    password = "supersecret1"
    await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}

    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200

    revoked = await client.post("/api/v1/auth/sessions/revoke-all", headers=headers)
    assert revoked.status_code == 200

    client.cookies.clear()
    me_after = await client.get("/api/v1/auth/me", headers=headers)
    assert me_after.status_code == 401


@pytest.mark.asyncio
async def test_failed_login_and_lockout_are_audit_logged(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "auth_max_failed_attempts", 2)

    email = "audit-lockout@example.com"
    password = "supersecret1"
    register = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}

    await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})

    audit = await client.get("/api/v1/security/audit-log", params={"action": "auth.account_locked"}, headers=headers)
    assert audit.status_code == 200
    assert any(e["target_id"] == register.json()["id"] for e in audit.json())


@pytest.mark.asyncio
async def test_concurrent_bad_logins_cannot_bypass_the_lockout_threshold(client, monkeypatch):
    """Regression test: the failed-attempt counter is read, checked, and
    written back across two ``await`` points (the SELECT and the COMMIT).
    Without per-account serialization, N concurrent bad-password requests
    could all read the same pre-increment count and each write back
    ``count + 1``, silently losing increments and letting an attacker get
    more than ``auth_max_failed_attempts`` guesses in before the account
    locks."""
    import asyncio

    from app.config import settings

    monkeypatch.setattr(settings, "auth_max_failed_attempts", 3)
    monkeypatch.setattr(settings, "auth_lockout_minutes", 15.0)

    email = "concurrent-lockout@example.com"
    password = "supersecret1"
    await client.post("/api/v1/auth/register", json={"email": email, "password": password})

    # Fire more concurrent bad-password attempts than the threshold allows.
    responses = await asyncio.gather(
        *[
            client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password"})
            for _ in range(6)
        ]
    )
    statuses = [r.status_code for r in responses]
    # Exactly `auth_max_failed_attempts` attempts must have been scored as
    # plain wrong-password (401); every attempt after the counter hits the
    # threshold must be rejected as locked (423) instead of getting a fresh
    # extra guess.
    assert statuses.count(401) == settings.auth_max_failed_attempts
    assert statuses.count(423) == len(responses) - settings.auth_max_failed_attempts

    # The correct password must also now be refused - the account is locked.
    correct_attempt = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert correct_attempt.status_code == 423
