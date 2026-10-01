"""Dispatch behaviour: dedupe, rate limiting, severity floors, failures.

Every test here drives the real dispatcher against the real database and
swaps only the outermost network call, so the policy/dedupe/rate-limit
logic is exercised exactly as it runs in production.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from app.db import SessionLocal
from app.models.db import NotificationChannel, NotificationDelivery, NotificationSetting
from app.services.notifications import dispatch, store


@pytest.fixture(autouse=True)
async def _clean_notification_tables(client):
    """The test DB is shared across the session; start from a clean slate."""
    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(NotificationDelivery))
            await session.execute(delete(NotificationChannel))
            await session.execute(delete(NotificationSetting))
            await session.commit()

    await _clear()
    yield
    await _clear()


async def _make_channel(**overrides):
    data = {
        "type": "webhook",
        "name": "test hook",
        "enabled": True,
        "config": {"url": "https://example.com/hook"},
        "secret": "shhh-secret-value",
    }
    data.update(overrides)
    async with SessionLocal() as session:
        return await store.create_channel(session, data)


def _incident(**overrides):
    base = {
        "id": "incident-1",
        "kind": "intrusion",
        "severity": "high",
        "camera_id": "cam-1",
        "summary": "Person detected in the driveway zone.",
        "event_ids": [],
        "escalation_level": 0,
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


@pytest.fixture
def sent(monkeypatch):
    """Capture sends instead of hitting the network."""
    calls = []

    async def _fake(spec, payload, image, timeout):
        calls.append({"channel": spec.id, "payload": payload, "image": image, "secret": spec.secret})
        return "sent"

    monkeypatch.setitem(dispatch.SENDERS, "webhook", _fake)
    return calls


async def test_incident_is_delivered_to_an_enabled_channel(sent):
    channel = await _make_channel()
    results = await dispatch.dispatch_incident(_incident(), reason="created")
    assert results[channel.id] == "sent"
    assert len(sent) == 1
    assert "driveway" in sent[0]["payload"].body
    # The decrypted secret reaches the sender, and nothing else.
    assert sent[0]["secret"] == "shhh-secret-value"


async def test_duplicate_incident_notification_is_suppressed(sent):
    channel = await _make_channel()
    await dispatch.dispatch_incident(_incident(), reason="created")
    results = await dispatch.dispatch_incident(_incident(), reason="created")
    assert results[channel.id] == "duplicate"
    assert len(sent) == 1


async def test_escalation_notifies_again_per_level(sent):
    await _make_channel()
    await dispatch.dispatch_incident(_incident(), reason="created")
    await dispatch.dispatch_incident(_incident(escalation_level=1), reason="escalated")
    await dispatch.dispatch_incident(_incident(escalation_level=2), reason="escalated")
    assert len(sent) == 3
    assert sent[1]["payload"].title.startswith("Escalated")


async def test_rate_limit_stops_a_notification_storm(sent):
    channel = await _make_channel()
    async with SessionLocal() as session:
        await store.update_settings(session, {"max_per_hour": 2}, user_id=None)
    for index in range(4):
        await dispatch.dispatch_incident(_incident(id=f"incident-{index}"), reason="created")
    assert len(sent) == 2
    results = await dispatch.dispatch_incident(_incident(id="incident-9"), reason="created")
    assert results[channel.id] == "rate_limited"


async def test_channel_min_severity_filters_low_priority_incidents(sent):
    channel = await _make_channel(min_severity="critical")
    results = await dispatch.dispatch_incident(_incident(severity="high"), reason="created")
    assert results[channel.id] == "below_channel_min_severity"
    assert sent == []


async def test_disabled_channel_is_never_used(sent):
    await _make_channel(enabled=False)
    results = await dispatch.dispatch_incident(_incident(), reason="created")
    assert results == {"_policy": "no_enabled_channels"}


async def test_global_min_severity_blocks_before_channels(sent):
    await _make_channel()
    async with SessionLocal() as session:
        await store.update_settings(session, {"min_severity": "critical"}, user_id=None)
    results = await dispatch.dispatch_incident(_incident(severity="high"), reason="created")
    assert results == {"_policy": "below_min_severity"}
    assert sent == []


async def test_sender_failure_is_recorded_without_leaking_the_secret(monkeypatch):
    from app.services.notifications.senders import NotificationError, scrub

    async def _boom(spec, payload, image, timeout):
        raise NotificationError(scrub(f"connect failed for {spec.secret}", spec.secret))

    monkeypatch.setitem(dispatch.SENDERS, "webhook", _boom)
    channel = await _make_channel()
    results = await dispatch.dispatch_incident(_incident(), reason="created")
    assert results[channel.id] == "failed"
    async with SessionLocal() as session:
        refreshed = await session.get(NotificationChannel, channel.id)
        assert refreshed.last_status == "failed"
        assert "shhh-secret-value" not in (refreshed.last_message or "")


async def test_notify_incident_is_a_no_op_when_disabled(monkeypatch):
    """The master switch short-circuits before any task is scheduled."""
    from app.config import settings

    called = False

    async def _fake_dispatch(incident, *, reason, session_factory=None):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(dispatch, "dispatch_incident", _fake_dispatch)
    monkeypatch.setattr(settings, "notifications_enabled", False)
    dispatch.notify_incident(_incident(), reason="created")
    import asyncio

    await asyncio.sleep(0)
    assert called is False


async def test_notify_incident_schedules_a_background_task(monkeypatch):
    captured = {}

    async def _fake_dispatch(incident, *, reason, session_factory=None):
        captured["reason"] = reason
        return {}

    monkeypatch.setattr(dispatch, "dispatch_incident", _fake_dispatch)
    dispatch.notify_incident(_incident(), reason="escalated")
    # Let the scheduled task run.
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert captured["reason"] == "escalated"
