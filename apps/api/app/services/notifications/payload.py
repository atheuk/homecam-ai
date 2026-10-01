"""The notification payload: what a human actually reads on their phone.

Deliberately boring and deterministic. The text is built from recorded
facts (camera name, incident kind, severity, time) plus the incident's own
rule-derived summary. It carries no identity claim - the system reports
"person detected", never who that person is - which keeps the alert
consistent with the RAI rules in docs/ai-features.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ...config import settings

#: Human labels for incident kinds. Unknown kinds degrade to a readable
#: form rather than leaking an internal identifier verbatim.
KIND_LABELS = {
    "intrusion": "Intrusion",
    "suspicious_activity": "Suspicious activity",
    "package_theft": "Package taken",
    "mailbox": "Mailbox activity",
    "camera_offline": "Camera offline",
    "camera_obstruction": "Camera obstructed",
    "camera_frozen": "Camera frozen",
}

REASON_LABELS = {
    "created": "New",
    "escalated": "Escalated",
    "test": "Test",
}

SEVERITY_ORDER = ("low", "medium", "high", "critical")


def severity_rank(severity: str | None) -> int:
    try:
        return SEVERITY_ORDER.index((severity or "low").lower())
    except ValueError:
        return 0


@dataclass(frozen=True)
class NotificationPayload:
    """One rendered alert, independent of the channel that carries it."""

    incident_id: str
    kind: str
    severity: str
    reason: str
    camera_name: str
    title: str
    body: str
    occurred_at: datetime
    url: str | None = None

    def as_dict(self) -> dict:
        """Machine-readable form (web push data, webhook body)."""
        return {
            "incident_id": self.incident_id,
            "kind": self.kind,
            "severity": self.severity,
            "reason": self.reason,
            "camera_name": self.camera_name,
            "title": self.title,
            "body": self.body,
            "occurred_at": self.occurred_at.isoformat(),
            "url": self.url,
        }


def _local_time(moment: datetime) -> str:
    """Render in the household's timezone - "22:14" is actionable, a UTC
    ISO string at 2am is not."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        local = moment.astimezone(ZoneInfo(settings.home_timezone))
    except Exception:  # pragma: no cover - bad tz config should not break alerts
        local = moment.astimezone(timezone.utc)
    return local.strftime("%H:%M")


def deep_link(incident_id: str) -> str | None:
    """Link that opens this incident in the web app.

    Returns ``None`` when no web app base URL is configured, so a payload
    carries no link rather than a broken one.
    """
    base = (settings.web_app_base_url or "").strip().rstrip("/")
    if not base or not incident_id:
        return None
    # ``tab=security`` is what the web app routes on; ``incident`` selects
    # the one that triggered the alert.
    return f"{base}/?tab=security&incident={incident_id}"


def _parse_time(raw) -> datetime:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def build_payload(incident: dict, *, camera_name: str, reason: str = "created") -> NotificationPayload:
    """Render ``incident`` (an ``incidents.to_dict()`` snapshot) for delivery."""
    kind = incident.get("kind") or "incident"
    severity = (incident.get("severity") or "low").lower()
    label = KIND_LABELS.get(kind, kind.replace("_", " ").capitalize())
    prefix = REASON_LABELS.get(reason, "Update")
    occurred_at = _parse_time(incident.get("last_seen_at") or incident.get("created_at"))

    title = f"{prefix}: {label} - {camera_name}"
    # The incident's own summary is rule-derived text ("2 person detections
    # in the driveway zone..."), which is exactly what a human needs. The AI
    # summary is intentionally left out: it is advisory, labelled in the UI,
    # and a lock-screen notification has no room for that labelling.
    summary = (incident.get("summary") or f"{label} on {camera_name}.").strip()
    body = f"{summary} ({severity} severity, {_local_time(occurred_at)})"
    incident_id = str(incident.get("id") or "")
    return NotificationPayload(
        incident_id=incident_id,
        kind=kind,
        severity=severity,
        reason=reason,
        camera_name=camera_name,
        title=title[:200],
        body=body[:500],
        occurred_at=occurred_at,
        url=deep_link(incident_id),
    )
