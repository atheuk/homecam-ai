"""API tests for the notification endpoints.

The important guarantees here are the boring ones: nobody unauthenticated
gets in, and a secret that goes into the API never comes back out of it.
"""
import pytest
from sqlalchemy import delete

from app.db import SessionLocal
from app.models.db import NotificationChannel, NotificationDelivery, NotificationSetting

BASE = "/api/v1/notifications"


@pytest.fixture(autouse=True)
async def _clean(client):
    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(NotificationDelivery))
            await session.execute(delete(NotificationChannel))
            await session.execute(delete(NotificationSetting))
            await session.commit()

    await _clear()
    yield
    await _clear()


async def _create(client, **overrides):
    body = {
        "type": "ntfy",
        "name": "Phone",
        "enabled": True,
        "config": {"topic": "homecam-alerts"},
        "secret": "tk_super_secret_token",
    }
    body.update(overrides)
    return await client.post(f"{BASE}/channels", json=body)


async def test_channel_crud_round_trip(client):
    response = await _create(client)
    assert response.status_code == 201
    channel = response.json()
    assert channel["type"] == "ntfy"
    assert channel["has_secret"] is True

    listed = await client.get(f"{BASE}/channels")
    assert [row["id"] for row in listed.json()] == [channel["id"]]

    patched = await client.patch(
        f"{BASE}/channels/{channel['id']}", json={"name": "Phone (quiet)", "enabled": False}
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Phone (quiet)"
    assert patched.json()["enabled"] is False
    # Omitting the secret leaves the stored one alone.
    assert patched.json()["has_secret"] is True

    deleted = await client.delete(f"{BASE}/channels/{channel['id']}")
    assert deleted.status_code == 204
    assert (await client.get(f"{BASE}/channels")).json() == []


async def test_secret_is_never_returned_by_any_endpoint(client):
    secret = "tk_super_secret_token"
    created = await _create(client, secret=secret)
    channel_id = created.json()["id"]
    for response in (
        created,
        await client.get(f"{BASE}/channels"),
        await client.patch(f"{BASE}/channels/{channel_id}", json={"name": "Renamed"}),
        await client.get(f"{BASE}/status"),
    ):
        assert secret not in response.text


async def test_secret_can_be_cleared_but_not_read_back(client):
    created = await _create(client)
    channel_id = created.json()["id"]
    cleared = await client.patch(f"{BASE}/channels/{channel_id}", json={"secret": ""})
    assert cleared.json()["has_secret"] is False


async def test_invalid_channel_config_is_rejected(client):
    assert (await _create(client, config={"topic": "has/slash"})).status_code == 400
    assert (await _create(client, type="webhook", config={"url": "http://insecure.example.com"})).status_code == 400
    assert (await _create(client, type="webhook", config={"url": "https://127.0.0.1/hook"})).status_code == 400
    # Telegram is useless without a bot token, so it is refused at save time.
    assert (
        await _create(client, type="telegram", config={"chat_id": "123"}, secret=None)
    ).status_code == 400


async def test_settings_round_trip_and_validation(client):
    defaults = await client.get(f"{BASE}/settings")
    assert defaults.status_code == 200
    assert defaults.json()["enabled"] is True

    updated = await client.put(
        f"{BASE}/settings",
        json={
            "quiet_hours_enabled": True,
            "quiet_hours_start": "23:00",
            "quiet_hours_end": "06:30",
            "min_severity": "medium",
            "max_per_hour": 10,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["quiet_hours_start"] == "23:00"
    assert updated.json()["min_severity"] == "medium"

    assert (await client.put(f"{BASE}/settings", json={"quiet_hours_start": "25:99"})).status_code == 422
    assert (await client.put(f"{BASE}/settings", json={"min_severity": "apocalyptic"})).status_code == 422
    assert (await client.put(f"{BASE}/settings", json={"max_per_hour": 0})).status_code == 422


async def test_status_reports_capabilities(client):
    await _create(client)
    status = (await client.get(f"{BASE}/status")).json()
    assert status["channel_count"] == 1
    assert status["enabled_channel_count"] == 1
    assert "web_push_available" in status
    # Without web push configured there is no key to hand to the browser.
    if not status["web_push_available"]:
        assert status["vapid_public_key"] is None


async def test_test_send_reports_failure_without_leaking_the_secret(client, monkeypatch):
    from app.services.notifications import dispatch
    from app.services.notifications.senders import NotificationError, scrub

    async def _boom(spec, payload, image, timeout):
        raise NotificationError(scrub(f"502 from server using {spec.secret}", spec.secret))

    monkeypatch.setitem(dispatch.SENDERS, "ntfy", _boom)
    channel_id = (await _create(client)).json()["id"]
    result = await client.post(f"{BASE}/channels/{channel_id}/test")
    assert result.status_code == 200
    assert result.json()["status"] == "failed"
    assert "tk_super_secret_token" not in result.text


async def test_test_send_succeeds_and_records_health(client, monkeypatch):
    from app.services.notifications import dispatch

    async def _ok(spec, payload, image, timeout):
        assert image is None, "test sends never carry imagery"
        return "sent"

    monkeypatch.setitem(dispatch.SENDERS, "ntfy", _ok)
    channel_id = (await _create(client)).json()["id"]
    assert (await client.post(f"{BASE}/channels/{channel_id}/test")).json()["status"] == "sent"
    listed = (await client.get(f"{BASE}/channels")).json()
    assert listed[0]["last_status"] == "sent"


async def test_missing_channel_is_a_404(client):
    assert (await client.post(f"{BASE}/channels/does-not-exist/test")).status_code == 404
    assert (await client.delete(f"{BASE}/channels/does-not-exist")).status_code == 404
    assert (await client.patch(f"{BASE}/channels/does-not-exist", json={"name": "x"})).status_code == 404


async def test_push_subscription_lifecycle(client):
    body = {
        "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
        "p256dh": "BPublicKeyValue",
        "auth": "AuthSecretValue",
    }
    created = await client.post(f"{BASE}/push/subscriptions", json=body)
    assert created.status_code == 201
    # The full endpoint is a capability URL; only a hint is echoed back.
    assert body["endpoint"] not in created.text
    assert body["auth"] not in created.text

    # Re-subscribing the same browser updates rather than duplicates.
    await client.post(f"{BASE}/push/subscriptions", json=body)
    assert len((await client.get(f"{BASE}/push/subscriptions")).json()) == 1

    removed = await client.post(f"{BASE}/push/subscriptions/remove", json={"endpoint": body["endpoint"]})
    assert removed.status_code == 204
    assert (await client.get(f"{BASE}/push/subscriptions")).json() == []
    assert (
        await client.post(f"{BASE}/push/subscriptions/remove", json={"endpoint": body["endpoint"]})
    ).status_code == 404


async def test_push_subscription_requires_https_endpoint(client):
    response = await client.post(
        f"{BASE}/push/subscriptions",
        json={"endpoint": "http://example.com/push", "p256dh": "k", "auth": "a"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/status", None),
        ("get", "/settings", None),
        ("put", "/settings", {"enabled": False}),
        ("get", "/channels", None),
        ("post", "/channels", {"type": "ntfy", "name": "x", "config": {"topic": "t"}}),
        ("patch", "/channels/x", {"name": "y"}),
        ("delete", "/channels/x", None),
        ("post", "/channels/x/test", None),
        ("get", "/push/subscriptions", None),
        ("post", "/push/subscriptions", {"endpoint": "https://e/x", "p256dh": "k", "auth": "a"}),
        ("post", "/push/subscriptions/remove", {"endpoint": "https://e/x"}),
    ],
)
async def test_every_endpoint_rejects_anonymous_callers(anonymous_client, method, path, body):
    call = getattr(anonymous_client, method)
    response = await call(f"{BASE}{path}", json=body) if body is not None else await call(f"{BASE}{path}")
    assert response.status_code == 401


async def test_channel_changes_are_audit_logged(client):
    channel_id = (await _create(client)).json()["id"]
    await client.patch(f"{BASE}/channels/{channel_id}", json={"secret": "rotated_secret_value"})

    from sqlalchemy import select

    from app.models.db import AuditLog

    async with SessionLocal() as session:
        rows = (await session.execute(select(AuditLog).order_by(AuditLog.created_at.asc()))).scalars().all()
    actions = [row.action for row in rows]
    assert "notification.channel.created" in actions
    assert "notification.channel.updated" in actions
    # The audit trail records that a secret changed, never what it changed to.
    serialised = str([row.details for row in rows])
    assert "rotated_secret_value" not in serialised
    assert "secret_changed" in serialised
