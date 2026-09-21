"""Admin provider-configuration tests (SPEC admin plane).

Covers: Dahua/Eufy config CRUD, secrets never appearing in any read
response, write-only secret update semantics, enable/disable behavior
(including its effect on the live provider registry), and provider
isolation when a configured-but-unreachable provider is enabled.
"""
import pytest

from app.crypto import decrypt_secret, encrypt_secret


@pytest.fixture(autouse=True)
async def _clean_provider_configs(client):
    """Provider configs live in the shared session-scoped test database, so
    clear them before and after each test to keep these tests independent
    of ordering and of each other. Depends on ``client`` so the app's
    ``lifespan`` (which creates tables) has already run."""
    from sqlalchemy import delete

    from app.db import SessionLocal
    from app.models.db import ProviderConfig

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(ProviderConfig))
            await session.commit()

    await _clear()
    yield
    await _clear()


async def _authed_headers(client) -> dict[str, str]:
    email = "admin-tests@example.com"
    password = "supersecret1"
    register = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    if register.status_code not in (201, 409):
        raise AssertionError(register.text)
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _assert_no_secret_leak(payload: dict) -> None:
    assert "password" not in payload
    assert "adapter_token" not in payload
    assert "secret_encrypted" not in payload
    for value in payload.values():
        assert value != "not-the-real-password"
        assert value != "not-the-real-token"


@pytest.mark.asyncio
async def test_crypto_roundtrip_and_tamper_detection():
    token = encrypt_secret("super-secret-value")
    assert "super-secret-value" not in token
    assert decrypt_secret(token) == "super-secret-value"

    from app.crypto import SecretDecryptionError

    with pytest.raises(SecretDecryptionError):
        decrypt_secret("garbage")


@pytest.mark.asyncio
async def test_admin_endpoints_require_authentication(client):
    r = await client.get("/api/v1/admin/providers")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_update_delete_dahua_config(client):
    headers = await _authed_headers(client)

    create = await client.post(
        "/api/v1/admin/providers/dahua",
        json={
            "name": "Test NVR",
            "host": "192.0.2.50",
            "username": "local-user",
            "password": "not-the-real-password",
            "channels": "1:Front Door",
            "enabled": False,
        },
        headers=headers,
    )
    assert create.status_code == 201
    body = create.json()
    _assert_no_secret_leak(body)
    assert body["provider_type"] == "dahua"
    assert body["has_secret"] is True
    assert body["host"] == "192.0.2.50"
    config_id = body["id"]

    listing = await client.get("/api/v1/admin/providers", headers=headers)
    assert listing.status_code == 200
    for entry in listing.json():
        _assert_no_secret_leak(entry)

    update = await client.put(
        f"/api/v1/admin/providers/dahua/{config_id}",
        json={"host": "192.0.2.51"},
        headers=headers,
    )
    assert update.status_code == 200
    updated_body = update.json()
    _assert_no_secret_leak(updated_body)
    assert updated_body["host"] == "192.0.2.51"
    # Secret was preserved even though the update omitted it.
    assert updated_body["has_secret"] is True

    delete = await client.delete(f"/api/v1/admin/providers/{config_id}", headers=headers)
    assert delete.status_code == 204

    after_delete = await client.get("/api/v1/admin/providers", headers=headers)
    assert config_id not in {entry["id"] for entry in after_delete.json()}


@pytest.mark.asyncio
async def test_create_update_delete_eufy_config(client):
    headers = await _authed_headers(client)

    create = await client.post(
        "/api/v1/admin/providers/eufy",
        json={
            "name": "Test Adapter",
            "adapter_url": "http://127.0.0.1:8090",
            "adapter_token": "not-the-real-token",
            "enabled": False,
        },
        headers=headers,
    )
    assert create.status_code == 201
    body = create.json()
    _assert_no_secret_leak(body)
    assert body["provider_type"] == "eufy"
    assert body["has_secret"] is True
    config_id = body["id"]

    update = await client.put(
        f"/api/v1/admin/providers/eufy/{config_id}",
        json={"adapter_url": "http://127.0.0.1:9090"},
        headers=headers,
    )
    assert update.status_code == 200
    updated_body = update.json()
    _assert_no_secret_leak(updated_body)
    assert updated_body["adapter_url"] == "http://127.0.0.1:9090"
    assert updated_body["has_secret"] is True

    delete = await client.delete(f"/api/v1/admin/providers/{config_id}", headers=headers)
    assert delete.status_code == 204


@pytest.mark.asyncio
async def test_only_one_enabled_config_per_provider_type(client):
    headers = await _authed_headers(client)

    first = await client.post(
        "/api/v1/admin/providers/dahua",
        json={"host": "192.0.2.60", "username": "u", "password": "p", "channels": "1:A", "enabled": True},
        headers=headers,
    )
    second = await client.post(
        "/api/v1/admin/providers/dahua",
        json={"host": "192.0.2.61", "username": "u", "password": "p", "channels": "1:B", "enabled": True},
        headers=headers,
    )
    assert first.status_code == 201 and second.status_code == 201

    listing = (await client.get("/api/v1/admin/providers", headers=headers)).json()
    dahua_entries = [e for e in listing if e["provider_type"] == "dahua"]
    enabled = [e for e in dahua_entries if e["enabled"]]
    assert len(enabled) == 1
    assert enabled[0]["id"] == second.json()["id"]

    # Re-enabling the first disables the second.
    re_enable = await client.post(
        f"/api/v1/admin/providers/{first.json()['id']}/enabled", json={"enabled": True}, headers=headers
    )
    assert re_enable.status_code == 200
    listing_after = (await client.get("/api/v1/admin/providers", headers=headers)).json()
    enabled_after = [e for e in listing_after if e["provider_type"] == "dahua" and e["enabled"]]
    assert len(enabled_after) == 1
    assert enabled_after[0]["id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_enable_disable_wires_provider_registry(client):
    from app.services.provider_registry import all_providers

    headers = await _authed_headers(client)

    baseline_ids = {p.id for p in await all_providers()}
    assert "dahua" not in baseline_ids

    create = await client.post(
        "/api/v1/admin/providers/dahua",
        json={"host": "192.0.2.70", "username": "u", "password": "p", "channels": "1:A", "enabled": True},
        headers=headers,
    )
    config_id = create.json()["id"]

    enabled_ids = {p.id for p in await all_providers()}
    assert "dahua" in enabled_ids

    disable = await client.post(
        f"/api/v1/admin/providers/{config_id}/enabled", json={"enabled": False}, headers=headers
    )
    assert disable.status_code == 200
    disabled_ids = {p.id for p in await all_providers()}
    assert "dahua" not in disabled_ids


@pytest.mark.asyncio
async def test_broken_dahua_config_does_not_break_other_providers(client):
    """SPEC 2.5/43: an enabled-but-unreachable configured provider must not
    prevent mock cameras (or other providers) from being served.

    Uses the registry functions directly (not the ``/cameras`` route) so
    this test does not persist a transient Dahua camera row into the shared
    test database, which would otherwise leak into unrelated tests.
    """
    from app.services.provider_registry import discover_all_cameras, get_all_provider_health

    headers = await _authed_headers(client)

    await client.post(
        "/api/v1/admin/providers/dahua",
        json={
            "host": "192.0.2.254",  # unreachable TEST-NET-1 address
            "username": "u",
            "password": "p",
            "channels": "1:Unreachable",
            "enabled": True,
        },
        headers=headers,
    )

    cameras = await discover_all_cameras()
    ids = {c["id"] for c in cameras}
    assert {"mock-front-door", "mock-driveway", "mock-backyard", "mock-garden"} <= ids

    health = await get_all_provider_health()
    by_id = {h["provider_id"]: h for h in health}
    assert by_id["mock"]["status"] == "ONLINE"
    assert by_id["dahua"]["status"] in {"OFFLINE", "DEGRADED"}


@pytest.mark.asyncio
async def test_dahua_test_connection_reports_failure_without_leaking_secret(client):
    headers = await _authed_headers(client)

    result = await client.post(
        "/api/v1/admin/providers/dahua/test",
        json={"host": "192.0.2.253", "username": "u", "password": "super-secret-password", "channels": "1:A"},
        headers=headers,
    )
    assert result.status_code == 200
    body = result.json()
    assert body["success"] is False
    assert body["status"] in {"OFFLINE", "DEGRADED"}
    assert "super-secret-password" not in body["message"]


@pytest.mark.asyncio
async def test_eufy_test_connection_reports_failure_without_leaking_secret(client):
    headers = await _authed_headers(client)

    result = await client.post(
        "/api/v1/admin/providers/eufy/test",
        json={"adapter_url": "http://192.0.2.252:8090", "adapter_token": "super-secret-token"},
        headers=headers,
    )
    assert result.status_code == 200
    body = result.json()
    assert body["success"] is False
    assert "super-secret-token" not in body["message"]


@pytest.mark.asyncio
async def test_test_connection_by_config_id_uses_stored_secret(client):
    headers = await _authed_headers(client)

    create = await client.post(
        "/api/v1/admin/providers/dahua",
        json={"host": "192.0.2.251", "username": "u", "password": "stored-secret", "channels": "1:A", "enabled": False},
        headers=headers,
    )
    config_id = create.json()["id"]

    result = await client.post(
        "/api/v1/admin/providers/dahua/test",
        json={"config_id": config_id},
        headers=headers,
    )
    assert result.status_code == 200
    assert "stored-secret" not in result.json()["message"]

    listing = (await client.get("/api/v1/admin/providers", headers=headers)).json()
    entry = next(e for e in listing if e["id"] == config_id)
    assert entry["last_test_status"] in {"OFFLINE", "DEGRADED"}
    assert entry["last_test_at"] is not None


@pytest.mark.asyncio
async def test_unknown_config_id_returns_404(client):
    headers = await _authed_headers(client)
    r = await client.put(
        "/api/v1/admin/providers/dahua/does-not-exist",
        json={"host": "example"},
        headers=headers,
    )
    assert r.status_code == 404
    r2 = await client.delete("/api/v1/admin/providers/does-not-exist", headers=headers)
    assert r2.status_code == 404
