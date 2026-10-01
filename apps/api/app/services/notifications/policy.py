"""When an alert may be sent: severity floors and quiet hours.

Pure functions over explicit inputs so the policy is directly testable
without a database, a clock, or a network.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from ...config import settings
from .payload import severity_rank


def parse_hhmm(value: str | None, fallback: time) -> time:
    """Parse ``"HH:MM"``; fall back rather than raise, because a malformed
    stored value must not take the whole alerting path down."""
    try:
        hour, minute = (value or "").split(":", 1)
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return fallback


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    """Is ``now`` (compared in the home timezone) inside the window?

    Handles the normal case where quiet hours wrap past midnight
    (22:00 -> 07:00). An empty window (start == end) is treated as "no
    quiet hours" rather than "always quiet".
    """
    try:
        local = now.astimezone(ZoneInfo(settings.home_timezone))
    except Exception:  # pragma: no cover - bad tz config must not silence alerts
        local = now.astimezone(timezone.utc)
    begin = parse_hhmm(start, time(22, 0))
    finish = parse_hhmm(end, time(7, 0))
    if begin == finish:
        return False
    current = local.time()
    if begin < finish:
        return begin <= current < finish
    return current >= begin or current < finish


def should_notify(
    *,
    severity: str,
    now: datetime,
    enabled: bool,
    min_severity: str,
    quiet_hours_enabled: bool,
    quiet_hours_start: str,
    quiet_hours_end: str,
    quiet_hours_override_severity: str,
) -> tuple[bool, str]:
    """Decide whether an incident of ``severity`` notifies right now.

    Returns ``(allowed, reason)`` where ``reason`` explains a refusal so it
    can be logged (and shown in the admin UI) without guesswork.
    """
    if not enabled:
        return False, "notifications_disabled"
    if severity_rank(severity) < severity_rank(min_severity):
        return False, "below_min_severity"
    if quiet_hours_enabled and in_quiet_hours(now, quiet_hours_start, quiet_hours_end):
        # Quiet hours must never be able to silence a break-in; anything at
        # or above the override severity still goes out.
        if severity_rank(severity) < severity_rank(quiet_hours_override_severity):
            return False, "quiet_hours"
    return True, "ok"
