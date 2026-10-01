"""Unit tests for the notification policy, payload, and senders.

These cover the decisions that have to be right at 3am: quiet hours that
still let a break-in through, a payload that never claims who someone is,
and secrets that never reach a log line.
"""
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.services.notifications import payload as payload_module
from app.services.notifications.payload import build_payload
from app.services.notifications.policy import in_quiet_hours, parse_hhmm, should_notify
from app.services.notifications.senders import (
    ChannelSpec,
    scrub,
    validate_config,
    validate_https_url,
)
from app.services.notifications.senders.webhook import signature
from datetime import time as dt_time


def _policy(**overrides):
    base = dict(
        severity="high",
        now=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        enabled=True,
        min_severity="low",
        quiet_hours_enabled=False,
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        quiet_hours_override_severity="critical",
    )
    base.update(overrides)
    return should_notify(**base)


def test_disabled_blocks_everything():
    assert _policy(enabled=False) == (False, "notifications_disabled")


def test_below_min_severity_is_dropped():
    allowed, reason = _policy(severity="low", min_severity="high")
    assert (allowed, reason) == (False, "below_min_severity")


def test_quiet_hours_suppress_normal_alerts_but_not_critical():
    night = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc)
    assert _policy(severity="high", now=night, quiet_hours_enabled=True)[1] == "quiet_hours"
    assert _policy(severity="critical", now=night, quiet_hours_enabled=True)[0] is True


def test_quiet_hours_window_wraps_midnight():
    assert in_quiet_hours(datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc), "22:00", "07:00")
    assert in_quiet_hours(datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc), "22:00", "07:00")
    assert not in_quiet_hours(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc), "22:00", "07:00")


def test_empty_quiet_window_is_not_always_quiet():
    assert not in_quiet_hours(datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc), "07:00", "07:00")


def test_malformed_quiet_hours_fall_back_instead_of_raising():
    assert parse_hhmm("nonsense", dt_time(22, 0)) == dt_time(22, 0)
    assert parse_hhmm(None, dt_time(7, 0)) == dt_time(7, 0)


def test_payload_describes_the_event_without_identity_claims():
    result = build_payload(
        {
            "id": "inc-1",
            "kind": "intrusion",
            "severity": "high",
            "summary": "Person detected in the driveway zone on Front Door while away.",
            "last_seen_at": "2026-01-01T21:14:00+00:00",
        },
        camera_name="Front Door",
        reason="created",
    )
    assert result.title.startswith("New: Intrusion - Front Door")
    assert "Person detected" in result.body
    assert "high severity" in result.body
    lowered = (result.title + result.body).lower()
    assert "known" not in lowered and "identified as" not in lowered


def test_payload_deep_link_is_absent_without_a_configured_base_url(monkeypatch):
    monkeypatch.setattr(settings, "web_app_base_url", None)
    assert build_payload({"id": "inc-1"}, camera_name="Cam").url is None
    monkeypatch.setattr(settings, "web_app_base_url", "https://home.example.com/")
    assert build_payload({"id": "inc-1"}, camera_name="Cam").url == "https://home.example.com/?incident=inc-1"


def test_payload_survives_a_malformed_timestamp():
    result = build_payload({"id": "x", "last_seen_at": "not-a-time"}, camera_name="Cam")
    assert isinstance(result.occurred_at, datetime)


def test_unknown_kind_renders_readably():
    assert "Doorbell press" in build_payload({"id": "x", "kind": "doorbell_press"}, camera_name="C").title


def test_scrub_removes_secrets():
    assert "tok_supersecret" not in scrub("failed with token tok_supersecret", "tok_supersecret")
    # Very short values are not scrubbed (they would mangle unrelated text).
    assert scrub("abc", "ab") == "abc"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",
        "https://127.0.0.1/hook",
        "https://localhost/hook",
        "https://10.0.0.5/hook",
        "https://169.254.169.254/latest/meta-data",
        "not-a-url",
    ],
)
def test_webhook_urls_must_be_https_and_public(url):
    with pytest.raises(ValueError):
        validate_https_url(url, field_name="url")


def test_public_https_url_is_accepted():
    assert validate_https_url("https://example.com/hook/", field_name="url") == "https://example.com/hook"


def test_channel_config_validation():
    assert validate_config("ntfy", {"topic": "alerts"})["server"] == "https://ntfy.sh"
    assert validate_config("telegram", {"chat_id": 12345})["chat_id"] == "12345"
    with pytest.raises(ValueError):
        validate_config("ntfy", {"topic": "with/slash"})
    with pytest.raises(ValueError):
        validate_config("telegram", {})
    with pytest.raises(ValueError):
        validate_config("carrier_pigeon", {})


def test_images_require_opt_in_and_private_delivery():
    # Opt-in off: never.
    assert not ChannelSpec(id="1", type="telegram", name="t", secret="x").supports_images()
    # Telegram with a token and opt-in: allowed.
    assert ChannelSpec(id="1", type="telegram", name="t", secret="x", attach_images=True).supports_images()
    # ntfy without a token is a public topic: never, even with opt-in.
    assert not ChannelSpec(id="2", type="ntfy", name="n", attach_images=True).supports_images()
    assert ChannelSpec(id="2", type="ntfy", name="n", secret="tok", attach_images=True).supports_images()
    # Web push and webhooks never carry imagery.
    assert not ChannelSpec(id="3", type="webpush", name="w", attach_images=True).supports_images()
    assert not ChannelSpec(id="4", type="webhook", name="h", secret="s", attach_images=True).supports_images()


def test_webhook_signature_is_stable_and_keyed():
    body = b'{"a":1}'
    assert signature(body, "key1").startswith("sha256=")
    assert signature(body, "key1") != signature(body, "key2")


def test_severity_rank_order():
    ranks = [payload_module.severity_rank(s) for s in ("low", "medium", "high", "critical")]
    assert ranks == sorted(ranks) and len(set(ranks)) == 4
    assert payload_module.severity_rank("bogus") == 0
