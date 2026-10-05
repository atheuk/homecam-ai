"""Google sign-in (OIDC authorization code + PKCE) end-to-end tests.

Google itself is replaced at the two network boundaries only: the token
endpoint (``exchange_code``) and the JWKS lookup (``resolve_signing_key``).
ID tokens are real RS256 JWTs signed with a throwaway key, so signature,
issuer, audience, expiry, nonce and ``email_verified`` checks run for real.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from app.auth import google
from app.auth.security import hash_password
from app.config import settings
from app.db import SessionLocal
from app.models.db import AuditLog, OAuthLoginState, User

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret-value"
WEB = "https://web.example.test"
ADMIN_EMAIL = "a.heukels@gmail.com"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def google_enabled(monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "google_oauth_client_secret", CLIENT_SECRET)
    monkeypatch.setattr(settings, "google_oauth_redirect_uri", "http://test/api/v1/auth/google/callback")
    monkeypatch.setattr(settings, "web_app_base_url", WEB)
    monkeypatch.setattr(settings, "google_admin_emails", ADMIN_EMAIL)

    async def fake_key(id_token):
        return _KEY.public_key()

    monkeypatch.setattr(google, "resolve_signing_key", fake_key)
    exchanged: dict = {"token": None, "calls": []}

    async def fake_exchange(code, verifier):
        exchanged["calls"].append((code, verifier))
        if exchanged["token"] is None:
            raise google.GoogleAuthError("token_exchange_failed")
        return exchanged["token"]

    monkeypatch.setattr(google, "exchange_code", fake_exchange)
    return exchanged


def make_token(*, sub, email, nonce, iss="https://accounts.google.com", aud=CLIENT_ID, verified=True,
               exp_offset=600, key=_KEY, extra=None, alg="RS256"):
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": sub, "email": email, "email_verified": verified,
              "nonce": nonce, "iat": now, "exp": now + exp_offset}
    if extra:
        claims.update(extra)
    if alg == "HS256":
        return jwt.encode(claims, "x" * 32, algorithm="HS256")
    return jwt.encode(claims, key, algorithm=alg)


async def start(client, **params):
    response = await client.get("/api/v1/auth/google/start", params=params or None)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(google.AUTHORIZATION_ENDPOINT + "?")
    query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
    return query


async def callback(client, state, code="auth-code"):
    return await client.get("/api/v1/auth/google/callback", params={"state": state, "code": code})


def error_of(response) -> str | None:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(WEB + "/")
    return parse_qs(urlsplit(location).query).get("google_error", [None])[0]


async def full_login(client, exchanged, *, sub, email, **token_kwargs):
    query = await start(client)
    exchanged["token"] = make_token(sub=sub, email=email, nonce=query["nonce"], **token_kwargs)
    return await callback(client, query["state"])


async def get_user(**where) -> User | None:
    async with SessionLocal() as session:
        stmt = select(User)
        for key, value in where.items():
            stmt = stmt.where(getattr(User, key) == value)
        return (await session.execute(stmt)).scalars().first()


def uniq(prefix="g"):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def create_local_user(email, *, role="admin", disabled=False, password="supersecret1"):
    async with SessionLocal() as session:
        user = User(
            id=str(uuid.uuid4()), email=email, password_hash=hash_password(password), role=role,
            created_at=datetime.now(timezone.utc),
            disabled_at=datetime.now(timezone.utc) if disabled else None,
        )
        session.add(user)
        await session.commit()
        return user.id


# --- configuration -----------------------------------------------------------

async def test_disabled_until_configured(anonymous_client, monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_client_id", "")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "")
    status = await anonymous_client.get("/api/v1/auth/google/status")
    assert status.json() == {"enabled": False}
    response = await anonymous_client.get("/api/v1/auth/google/start")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "GOOGLE_OAUTH_CLIENT_ID" in detail and "docs/google-auth.md" in detail
    callback_response = await anonymous_client.get("/api/v1/auth/google/callback", params={"state": "x", "code": "y"})
    assert callback_response.status_code == 503
    # Local password sign-in is unaffected.
    email = f"{uniq('local')}@example.com"
    await anonymous_client.post("/api/v1/auth/register", json={"email": email, "password": "supersecret1"})
    login = await anonymous_client.post("/api/v1/auth/login", json={"email": email, "password": "supersecret1"})
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "admin"
    assert login.json()["user"]["google_linked"] is False


async def test_production_requires_https_redirect(monkeypatch, google_enabled):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "google_oauth_redirect_uri", "http://insecure.example/cb")
    assert not google.is_enabled()
    assert any("https" in p for p in google.configuration_problems())
    # Secrets never appear in the actionable error text.
    assert all(CLIENT_SECRET not in p and CLIENT_ID not in p for p in google.configuration_problems())


def test_admin_allowlist_is_exact_and_normalized(monkeypatch):
    monkeypatch.setattr(settings, "google_admin_emails", " A.Heukels@Gmail.com , @gmail.com, *@gmail.com, gmail.com, ")
    assert google.admin_emails() == frozenset({"a.heukels@gmail.com"})


async def test_status_enabled_and_start_redirect(anonymous_client, google_enabled):
    assert (await anonymous_client.get("/api/v1/auth/google/status")).json() == {"enabled": True}
    response = await anonymous_client.get("/api/v1/auth/google/start")
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == ["http://test/api/v1/auth/google/callback"]
    assert query["scope"] == ["openid email profile"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["response_type"] == ["code"]
    assert CLIENT_SECRET not in response.headers["location"]
    cookie = response.headers["set-cookie"]
    assert "homecam_google_oauth=" in cookie and "HttpOnly" in cookie and "Path=/api/v1/auth/google" in cookie
    # Only hashes of state and binding are persisted.
    async with SessionLocal() as session:
        row = (await session.execute(
            select(OAuthLoginState).where(OAuthLoginState.nonce == query["nonce"][0])
        )).scalars().one()
    assert row.state_hash != query["state"][0]
    # PKCE: the challenge sent to Google is S256(verifier) of the stored verifier.
    expected = base64.urlsafe_b64encode(hashlib.sha256(row.code_verifier.encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == [expected]


# --- state / CSRF / replay ----------------------------------------------------

async def test_missing_and_unknown_state(anonymous_client, google_enabled):
    response = await anonymous_client.get("/api/v1/auth/google/callback", params={"code": "c"})
    assert error_of(response) == "invalid_state"
    assert error_of(await callback(anonymous_client, "forged-state")) == "invalid_state"
    assert google_enabled["calls"] == []


async def test_callback_replay_is_rejected(anonymous_client, google_enabled):
    email = f"{uniq('replay')}@example.com"
    query = await start(anonymous_client)
    google_enabled["token"] = make_token(sub=uniq(), email=email, nonce=query["nonce"])
    first = await callback(anonymous_client, query["state"])
    assert error_of(first) == "pending_approval"
    # Re-arm the binding cookie as an attacker replaying the same URL would need to.
    second = await callback(anonymous_client, query["state"])
    assert error_of(second) == "invalid_state"
    assert len(google_enabled["calls"]) == 1


async def test_binding_cookie_required(anonymous_client, google_enabled, api_transport):
    """Login CSRF: a callback URL minted in one browser fails in another."""
    from httpx import AsyncClient

    query = await start(anonymous_client)
    google_enabled["token"] = make_token(sub=uniq(), email=f"{uniq()}@example.com", nonce=query["nonce"])
    async with AsyncClient(transport=api_transport, base_url="http://test") as victim:
        response = await callback(victim, query["state"])
    assert error_of(response) == "invalid_state"
    assert google_enabled["calls"] == []
    # The state was consumed by the failed attempt; it cannot be retried.
    assert error_of(await callback(anonymous_client, query["state"])) == "invalid_state"


async def test_expired_state(anonymous_client, google_enabled, monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_state_ttl_seconds", -1)
    query = await start(anonymous_client)
    assert error_of(await callback(anonymous_client, query["state"])) == "expired_state"


async def test_google_error_param(anonymous_client, google_enabled):
    query = await start(anonymous_client)
    response = await anonymous_client.get(
        "/api/v1/auth/google/callback", params={"state": query["state"], "error": "access_denied"}
    )
    assert error_of(response) == "access_denied"


async def test_token_exchange_failure(anonymous_client, google_enabled):
    query = await start(anonymous_client)
    google_enabled["token"] = None
    assert error_of(await callback(anonymous_client, query["state"])) == "token_exchange_failed"


# --- ID token validation ------------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {"iss": "https://evil.example.com"},
    {"aud": "someone-else.apps.googleusercontent.com"},
    {"exp_offset": -3600},
    {"key": _OTHER_KEY},
    {"alg": "HS256"},
    {"extra": {"azp": "another-client"}},
])
async def test_invalid_id_tokens_rejected(anonymous_client, google_enabled, overrides):
    email = f"{uniq('bad')}@example.com"
    response = await full_login(anonymous_client, google_enabled, sub=uniq(), email=email, **overrides)
    assert error_of(response) == "invalid_token"
    assert await get_user(email=email) is None
    assert "homecam_session" not in response.headers.get("set-cookie", "")


async def test_nonce_mismatch_rejected(anonymous_client, google_enabled):
    query = await start(anonymous_client)
    google_enabled["token"] = make_token(sub=uniq(), email=f"{uniq()}@example.com", nonce="not-the-nonce")
    assert error_of(await callback(anonymous_client, query["state"])) == "invalid_token"


async def test_bare_issuer_form_accepted(anonymous_client, google_enabled):
    response = await full_login(
        anonymous_client, google_enabled, sub=uniq(), email=f"{uniq()}@example.com", iss="accounts.google.com"
    )
    assert error_of(response) == "pending_approval"


@pytest.mark.parametrize("verified", [False, "false", None])
async def test_unverified_email_rejected(anonymous_client, google_enabled, monkeypatch, verified):
    # Even an allowlisted address is refused when Google has not verified it.
    email = f"{uniq('unverified')}@example.com"
    monkeypatch.setattr(settings, "google_admin_emails", email)
    response = await full_login(anonymous_client, google_enabled, sub=uniq(), email=email, verified=verified)
    assert error_of(response) == "email_not_verified"
    assert await get_user(email=email) is None


# --- account outcomes ---------------------------------------------------------

async def test_ordinary_user_is_pending_without_session(anonymous_client, google_enabled):
    email = f"{uniq('ordinary')}@example.com"
    sub = uniq("sub")
    response = await full_login(anonymous_client, google_enabled, sub=sub, email=email)
    assert error_of(response) == "pending_approval"
    assert "homecam_session" not in response.headers.get("set-cookie", "")
    user = await get_user(google_sub=sub)
    assert user.role == "pending" and user.email == email
    # Second attempt still has no access, and nothing promoted it.
    again = await full_login(anonymous_client, google_enabled, sub=sub, email=email)
    assert error_of(again) == "pending_approval"
    assert (await get_user(google_sub=sub)).role == "pending"


async def test_admin_email_case_normalized_gets_admin_session(anonymous_client, google_enabled, caplog):
    sub = uniq("admin-sub")
    caplog.set_level(logging.DEBUG)
    response = await full_login(anonymous_client, google_enabled, sub=sub, email="A.Heukels@Gmail.COM")
    assert response.status_code == 303
    assert response.headers["location"] == f"{WEB}/"
    assert error_of(response) is None
    set_cookie = response.headers.get_list("set-cookie")
    assert any(c.startswith("homecam_session=") and "HttpOnly" in c for c in set_cookie)
    assert any(c.startswith("homecam_google_oauth=") and "Max-Age=0" in c for c in set_cookie)
    user = await get_user(google_sub=sub)
    assert user.role == "admin" and user.email == ADMIN_EMAIL
    # The resulting session works and exposes no Google identifiers.
    me = await anonymous_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["role"] == "admin" and body["google_linked"] is True
    assert sub not in me.text
    # Neither the code, ID token nor client secret reach logs.
    assert CLIENT_SECRET not in caplog.text and google_enabled["token"] not in caplog.text
    async with SessionLocal() as session:
        actions = {
            a.action for a in (await session.execute(select(AuditLog).where(AuditLog.target_id == user.id))).scalars()
        }
    assert {"auth.google_user_created", "auth.login"} <= actions
    # Second sign-in matches on sub and stays admin.
    anonymous_client.cookies.clear()
    again = await full_login(anonymous_client, google_enabled, sub=sub, email=ADMIN_EMAIL)
    assert error_of(again) is None


async def test_admin_email_allowlist_promotes_only_matching_pending_account(anonymous_client, google_enabled, monkeypatch):
    # A Google account that signed in before being allowlisted is promoted
    # once allowlisted - and nobody else changes.
    pending_sub = uniq("later-admin")
    other_sub = uniq("bystander")
    email = f"{uniq('later')}@example.com"
    other_email = f"{uniq('bystander')}@example.com"
    await full_login(anonymous_client, google_enabled, sub=pending_sub, email=email)
    await full_login(anonymous_client, google_enabled, sub=other_sub, email=other_email)
    monkeypatch.setattr(settings, "google_admin_emails", f"{ADMIN_EMAIL},{email.upper()}")
    response = await full_login(anonymous_client, google_enabled, sub=pending_sub, email=email)
    assert error_of(response) is None
    assert (await get_user(google_sub=pending_sub)).role == "admin"
    assert (await get_user(google_sub=other_sub)).role == "pending"


async def test_admin_never_demoted_when_removed_from_allowlist(anonymous_client, google_enabled, monkeypatch):
    sub = uniq("kept-admin")
    monkeypatch.setattr(settings, "google_admin_emails", "kept-admin@example.com")
    assert error_of(await full_login(anonymous_client, google_enabled, sub=sub, email="kept-admin@example.com")) is None
    monkeypatch.setattr(settings, "google_admin_emails", "")
    anonymous_client.cookies.clear()
    assert error_of(await full_login(anonymous_client, google_enabled, sub=sub, email="kept-admin@example.com")) is None
    assert (await get_user(google_sub=sub)).role == "admin"


async def test_existing_local_account_is_not_silently_linked(anonymous_client, google_enabled):
    email = f"{uniq('owner')}@example.com"
    user_id = await create_local_user(email)
    # Mixed case from Google must still collide with the stored address.
    response = await full_login(anonymous_client, google_enabled, sub=uniq(), email=email.upper())
    assert error_of(response) == "account_exists"
    assert "homecam_session" not in response.headers.get("set-cookie", "")
    user = await get_user(id=user_id)
    assert user.google_sub is None and user.role == "admin"


async def test_existing_local_admin_email_on_allowlist_still_requires_link(anonymous_client, google_enabled, monkeypatch):
    email = f"{uniq('allow-owner')}@example.com"
    user_id = await create_local_user(email)
    monkeypatch.setattr(settings, "google_admin_emails", email)
    response = await full_login(anonymous_client, google_enabled, sub=uniq(), email=email)
    assert error_of(response) == "account_exists"
    assert (await get_user(id=user_id)).google_sub is None


async def test_disabled_google_account_refused_even_if_allowlisted(anonymous_client, google_enabled, monkeypatch):
    sub = uniq("disabled")
    email = f"{uniq('disabled')}@example.com"
    monkeypatch.setattr(settings, "google_admin_emails", email)
    assert error_of(await full_login(anonymous_client, google_enabled, sub=sub, email=email)) is None
    me = await anonymous_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.google_sub == sub))).scalars().one()
        user.disabled_at = datetime.now(timezone.utc)
        await session.commit()
    # Existing session stops working immediately.
    assert (await anonymous_client.get("/api/v1/auth/me")).status_code == 403
    anonymous_client.cookies.clear()
    assert error_of(await full_login(anonymous_client, google_enabled, sub=sub, email=email)) == "account_disabled"
    assert (await get_user(google_sub=sub)).disabled_at is not None


async def test_disabled_and_pending_local_accounts_cannot_password_login(anonymous_client):
    disabled = f"{uniq('dis')}@example.com"
    pending = f"{uniq('pen')}@example.com"
    await create_local_user(disabled, disabled=True)
    await create_local_user(pending, role="pending")
    r1 = await anonymous_client.post("/api/v1/auth/login", json={"email": disabled, "password": "supersecret1"})
    r2 = await anonymous_client.post("/api/v1/auth/login", json={"email": pending, "password": "supersecret1"})
    assert r1.status_code == 403 and r1.json()["detail"] == "Account disabled"
    assert r2.status_code == 403 and r2.json()["detail"] == "Account awaiting approval"
    # Wrong passwords still get the generic 401 (no account-state oracle).
    r3 = await anonymous_client.post("/api/v1/auth/login", json={"email": disabled, "password": "wrong-password"})
    assert r3.status_code == 401


async def test_google_only_account_has_no_usable_password(anonymous_client, google_enabled):
    sub = uniq("pw")
    email = f"{uniq('pw')}@example.com"
    await full_login(anonymous_client, google_enabled, sub=sub, email=email)
    for password in ("!google-only", "", "google-only"):
        response = await anonymous_client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert response.status_code == 401


# --- explicit linking ---------------------------------------------------------

async def _password_session(client, email, password="supersecret1"):
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def _link(client, exchanged, *, sub, email, headers=None):
    begin = await client.post(
        "/api/v1/auth/google/link", headers=headers or {"X-HomeCam-Request": "1"}
    )
    assert begin.status_code == 200, begin.text
    url = begin.json()["url"]
    assert url.startswith("/api/v1/auth/google/start?")
    response = await client.get(url)
    if response.status_code != 302:
        return response
    query = {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}
    exchanged["token"] = make_token(sub=sub, email=email, nonce=query["nonce"])
    return await callback(client, query["state"])


async def test_link_existing_local_account_then_sign_in_with_google(anonymous_client, google_enabled):
    email = f"{uniq('link')}@example.com"
    user_id = await create_local_user(email)
    await _password_session(anonymous_client, email)
    sub = uniq("link-sub")
    response = await _link(anonymous_client, google_enabled, sub=sub, email=email)
    assert response.status_code == 303
    assert response.headers["location"] == f"{WEB}/?tab=system&google=linked"
    user = await get_user(id=user_id)
    assert user.google_sub == sub and user.role == "admin"
    assert user.password_hash.startswith("pbkdf2_")  # local login preserved
    anonymous_client.cookies.clear()
    assert error_of(await full_login(anonymous_client, google_enabled, sub=sub, email=email)) is None
    assert (await anonymous_client.get("/api/v1/auth/me")).json()["id"] == user_id
    anonymous_client.cookies.clear()
    assert (await anonymous_client.post("/api/v1/auth/login", json={"email": email, "password": "supersecret1"})).status_code == 200


async def test_link_with_different_google_email_is_allowed_but_never_promotes(anonymous_client, google_enabled):
    email = f"{uniq('link2')}@example.com"
    user_id = await create_local_user(email)
    await _password_session(anonymous_client, email)
    sub = uniq()
    response = await _link(anonymous_client, google_enabled, sub=sub, email=f"{uniq('personal')}@example.com")
    assert response.headers["location"] == f"{WEB}/?tab=system&google=linked"
    assert (await get_user(id=user_id)).google_sub == sub


async def test_link_requires_authenticated_csrf_protected_post(anonymous_client, google_enabled):
    assert (await anonymous_client.post("/api/v1/auth/google/link")).status_code == 401
    email = f"{uniq('csrf')}@example.com"
    await create_local_user(email)
    await _password_session(anonymous_client, email)
    # Cookie-only cross-site POST without the custom header is refused.
    assert (await anonymous_client.post("/api/v1/auth/google/link")).status_code == 403
    # A forged start with intent=link and no ticket does nothing.
    response = await anonymous_client.get("/api/v1/auth/google/start", params={"intent": "link"})
    assert error_of(response) == "link_expired"
    response = await anonymous_client.get(
        "/api/v1/auth/google/start", params={"intent": "link", "ticket": "guessed"}
    )
    assert error_of(response) == "link_expired"


async def test_leaked_link_ticket_is_useless_without_owner_session(anonymous_client, google_enabled, api_transport):
    from httpx import AsyncClient

    email = f"{uniq('leak')}@example.com"
    user_id = await create_local_user(email)
    await _password_session(anonymous_client, email)
    begin = await anonymous_client.post("/api/v1/auth/google/link", headers={"X-HomeCam-Request": "1"})
    url = begin.json()["url"]
    async with AsyncClient(transport=api_transport, base_url="http://test") as attacker:
        response = await attacker.get(url)
    assert error_of(response) == "link_requires_session"
    # Single use: the owner cannot reuse it either.
    assert error_of(await anonymous_client.get(url)) == "link_expired"
    assert (await get_user(id=user_id)).google_sub is None


async def test_link_callback_requires_same_session(anonymous_client, google_enabled):
    email = f"{uniq('swap')}@example.com"
    user_id = await create_local_user(email)
    await _password_session(anonymous_client, email)
    begin = await anonymous_client.post("/api/v1/auth/google/link", headers={"X-HomeCam-Request": "1"})
    response = await anonymous_client.get(begin.json()["url"])
    query = {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}
    # Session cookie disappears (signed out / different browser profile).
    anonymous_client.cookies.delete("homecam_session")
    google_enabled["token"] = make_token(sub=uniq(), email=email, nonce=query["nonce"])
    assert error_of(await callback(anonymous_client, query["state"])) == "link_requires_session"
    assert (await get_user(id=user_id)).google_sub is None


async def test_link_conflicts(anonymous_client, google_enabled, api_transport):
    from httpx import AsyncClient

    taken_sub = uniq("taken")
    await full_login(anonymous_client, google_enabled, sub=taken_sub, email=f"{uniq('taken')}@example.com")
    email = f"{uniq('conf')}@example.com"
    user_id = await create_local_user(email)
    async with AsyncClient(transport=api_transport, base_url="http://test") as owner:
        await _password_session(owner, email)
        response = await _link(owner, google_enabled, sub=taken_sub, email=email)
        assert error_of(response) == "google_account_in_use"
        first_sub = uniq("first")
        assert (await _link(owner, google_enabled, sub=first_sub, email=email)).headers["location"].endswith("google=linked")
        # Re-linking the same identity is idempotent; a different one is refused.
        assert (await _link(owner, google_enabled, sub=first_sub, email=email)).headers["location"].endswith("google=linked")
        assert error_of(await _link(owner, google_enabled, sub=uniq(), email=email)) == "already_linked_other"
    assert (await get_user(id=user_id)).google_sub == first_sub


# --- hygiene ------------------------------------------------------------------

async def test_session_fixation_previous_session_retired(anonymous_client, google_enabled, monkeypatch):
    email = f"{uniq('fix')}@example.com"
    await create_local_user(email)
    old_token = await _password_session(anonymous_client, email)
    google_email = f"{uniq('fix-google')}@example.com"
    monkeypatch.setattr(settings, "google_admin_emails", google_email)
    response = await full_login(anonymous_client, google_enabled, sub=uniq(), email=google_email)
    assert error_of(response) is None
    old = await anonymous_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_token}"})
    assert old.status_code == 401


def test_access_log_redacts_oauth_query():
    from app.main import _RedactOAuthQueryFilter

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/api/v1/auth/google/callback?state=s3cret&code=c0de", "1.1", 303), None,
    )
    _RedactOAuthQueryFilter().filter(record)
    message = record.getMessage()
    assert "s3cret" not in message and "c0de" not in message
    assert "/api/v1/auth/google/callback?[redacted]" in message


async def test_verify_id_token_unit(monkeypatch, google_enabled):
    token = make_token(sub="unit-sub", email=" Mixed@Example.com ", nonce="n1", verified="true")
    identity = await google.verify_id_token(token, expected_nonce="n1")
    assert identity == google.GoogleIdentity(sub="unit-sub", email="mixed@example.com")
    with pytest.raises(google.GoogleAuthError):
        await google.verify_id_token(make_token(sub="", email="a@b.co", nonce="n1"), expected_nonce="n1")
