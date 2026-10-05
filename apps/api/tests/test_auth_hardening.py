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


@pytest.mark.asyncio
async def test_failed_attempt_counter_is_atomic_at_the_database_layer_without_the_process_lock(monkeypatch):
    """The in-process ``asyncio.Lock`` in ``auth_routes.login`` only
    serializes concurrent attempts handled by *this* process; it does
    nothing across the two Container Apps replicas the deployed API can
    run as (see infra/modules/api.bicep's ``scale.maxReplicas: 2``), which
    share only the database. This test bypasses the lock entirely --
    calling the underlying atomic-update helper directly from separate
    sessions, simulating separate replica connections -- to prove the
    counter update itself, not just the lock, is safe against lost
    updates."""
    import asyncio
    import uuid
    from datetime import datetime, timezone

    from app.api.auth_routes import _register_failed_attempt
    from app.auth.security import hash_password
    from app.config import settings
    from app.db import SessionLocal
    from app.models.db import User

    # A high threshold means every one of the concurrent attempts below
    # increments the same counter without any early lockout short-circuit,
    # so the final count directly proves whether any increment was lost.
    monkeypatch.setattr(settings, "auth_max_failed_attempts", 1000)

    user_id = str(uuid.uuid4())
    async with SessionLocal() as session:
        session.add(
            User(
                id=user_id,
                email="atomic-attempt@example.com",
                password_hash=hash_password("irrelevant"),
                role="admin",
                created_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    concurrency = 20

    async def _bump():
        async with SessionLocal() as session:
            return await _register_failed_attempt(session, user_id, datetime.now(timezone.utc))

    results = await asyncio.gather(*[_bump() for _ in range(concurrency)])
    # Every call must have observed a strictly increasing, never-repeated
    # count -- a lost update would show up as a duplicate or a final total
    # short of `concurrency`.
    assert sorted(count for count, _ in results) == list(range(1, concurrency + 1))

    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        assert user.failed_attempts == concurrency


@pytest.mark.asyncio
async def test_a_correct_guess_verified_before_a_concurrent_lockout_cannot_still_complete_the_login(monkeypatch):
    """Regression test for a second, subtler cross-replica race than the
    lost-update one above: making the failed-attempt *counter* atomic is
    not enough on its own, because the login route verifies the password
    against a snapshot of the user row read *before* any lock-state
    changes, with no cross-replica lock held while it does so. Starting
    from ``failed_attempts == threshold - 1``, a wrong guess on one
    replica and a correct guess on another can both see the account as
    unlocked at the moment each verifies its own password. If the wrong
    guess's increment reaches the threshold and commits first, the correct
    guess must still be rejected as locked -- not silently allowed to
    complete just because its own password check happened to pass against
    a now-stale snapshot.

    This reproduces exactly that interleaving by driving the two atomic
    helpers directly, in the order that actually breaks unguarded code:
    the account-locking increment is forced to commit *before* the
    already-"password-verified" success path writes its reset, simulating
    two replicas racing with no shared in-process lock."""
    import uuid
    from datetime import datetime, timezone

    from app.api.auth_routes import _finalize_successful_login, _register_failed_attempt
    from app.auth.security import hash_password
    from app.config import settings
    from app.db import SessionLocal
    from app.models.db import User

    monkeypatch.setattr(settings, "auth_max_failed_attempts", 3)
    monkeypatch.setattr(settings, "auth_lockout_minutes", 15.0)

    user_id = str(uuid.uuid4())
    async with SessionLocal() as session:
        session.add(
            User(
                id=user_id,
                email="race-at-threshold@example.com",
                password_hash=hash_password("supersecret1"),
                role="admin",
                created_at=datetime.now(timezone.utc),
                failed_attempts=settings.auth_max_failed_attempts - 1,
            )
        )
        await session.commit()

    # Both "replicas" observe the account as unlocked and verify their own
    # guess against that snapshot before either writes anything back --
    # captured here as a single shared `now`, since the two requests are
    # concurrent in wall-clock time.
    now = datetime.now(timezone.utc)

    # The wrong guess's replica reaches the threshold and its atomic
    # increment commits first.
    async with SessionLocal() as session_a:
        new_failed_attempts, new_locked_until = await _register_failed_attempt(session_a, user_id, now)
    assert new_failed_attempts == settings.auth_max_failed_attempts
    assert new_locked_until is not None

    # The correct guess's replica already verified the password against
    # the pre-lockout snapshot and now tries to finalize the login. This
    # must be refused -- the fix re-checks lock state at the database
    # layer instead of trusting the stale snapshot.
    async with SessionLocal() as session_b:
        finalized = await _finalize_successful_login(session_b, user_id, now)
    assert finalized is False

    # The lockout must still be intact afterwards: the rejected "successful"
    # login must not have reset the counter or lifted the lock as a
    # side effect of its own (refused) write.
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        assert user.failed_attempts == settings.auth_max_failed_attempts
        assert user.locked_until is not None

    # Sanity check the other ordering too: if the correct guess's finalize
    # reaches the database *before* any concurrent wrong guess locks the
    # account, the login must still legitimately succeed and reset the
    # counter -- the fix must not be so conservative that it blocks
    # ordinary, non-racing logins.
    user_id_2 = str(uuid.uuid4())
    async with SessionLocal() as session:
        session.add(
            User(
                id=user_id_2,
                email="no-race-at-threshold@example.com",
                password_hash=hash_password("supersecret1"),
                role="admin",
                created_at=datetime.now(timezone.utc),
                failed_attempts=settings.auth_max_failed_attempts - 1,
            )
        )
        await session.commit()

    async with SessionLocal() as session_b:
        finalized_first = await _finalize_successful_login(session_b, user_id_2, now)
    assert finalized_first is True

    async with SessionLocal() as session:
        user = await session.get(User, user_id_2)
        assert user.failed_attempts == 0
        assert user.locked_until is None


@pytest.mark.asyncio
async def test_login_lock_cache_stays_bounded_for_many_distinct_emails(client):
    from app.api.auth_routes import _lock_for_email

    _lock_for_email.cache_clear()
    for index in range(300):
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": f"unknown-{index}@example.com", "password": "not-a-valid-account"},
        )
        assert response.status_code == 401

    info = _lock_for_email.cache_info()
    assert info.maxsize == 256
    assert info.currsize <= info.maxsize
