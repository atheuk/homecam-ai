import pytest


@pytest.mark.asyncio
async def test_register_login_me_logout_flow(client):
    me_unauthenticated = await client.get("/api/v1/auth/me")
    assert me_unauthenticated.status_code == 401

    register = await client.post("/api/v1/auth/register", json={"email": "Owner@Example.com", "password": "supersecret1"})
    assert register.status_code == 201
    assert register.json()["email"] == "owner@example.com"
    assert "password" not in register.json()

    login = await client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "supersecret1"})
    assert login.status_code == 200
    body = login.json()
    assert body["token_type"] == "bearer"
    token = body["access_token"]
    assert token

    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "owner@example.com"

    logout = await client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert logout.status_code == 200

    # The login call also set a session cookie; clear it so this check
    # exercises the bearer token only (the cookie was cleared by logout too,
    # but drop it explicitly to keep the assertion independent of that).
    client.cookies.clear()
    me_after_logout = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_after_logout.status_code == 401



@pytest.mark.asyncio
async def test_duplicate_registration_is_rejected(client):
    payload = {"email": "dup@example.com", "password": "supersecret1"}
    first = await client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 201
    second = await client.post("/api/v1/auth/register", json=payload)
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_login_with_wrong_password_is_rejected(client):
    await client.post("/api/v1/auth/register", json={"email": "wrongpw@example.com", "password": "supersecret1"})
    r = await client.post("/api/v1/auth/login", json={"email": "wrongpw@example.com", "password": "not-the-password"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_password_hash_is_never_returned_or_stored_in_plaintext(client):
    from app.auth.security import hash_password, verify_password

    hashed = hash_password("supersecret1")
    assert "supersecret1" not in hashed
    assert verify_password("supersecret1", hashed) is True
    assert verify_password("wrong", hashed) is False


@pytest.mark.asyncio
async def test_register_missing_password_field_is_422(client):
    r = await client.post("/api/v1/auth/register", json={"email": "missing@example.com"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_register_invalid_email_is_422(client):
    r = await client.post("/api/v1/auth/register", json={"email": "not-an-email", "password": "supersecret1"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_register_short_password_is_422(client):
    r = await client.post("/api/v1/auth/register", json={"email": "shortpw@example.com", "password": "short"})
    assert r.status_code == 422
