"""Smart notification priority.

Arlo/Eufy-style notification triage, done with a deterministic additive
rule set rather than a model: the same event in the same context always
gets the same priority, the score can be explained line by line, and a
provider outage cannot change how loudly the system shouts.

Priority is exposed on every event and is also used to keep the incident
feed actionable - anything below ``incident_min_priority`` never opens an
incident (see :mod:`app.services.incidents`).

Nothing here looks at *who* the subject is. Priority is a function of what
was detected, where, when and in which arming mode, never of identity.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import settings

PRIORITIES = ("low", "normal", "high", "critical")
PRIORITY_RANK = {name: index for index, name in enumerate(PRIORITIES)}

#: Base weight per event type. Absent types score 0 and rely on signals.
_BASE_BY_TYPE = {"person": 2, "vehicle": 1, "package": 1, "animal": 0, "suspicious_activity": 3}
#: Zone kinds that make any detection more significant.
_ZONE_BONUS = {"entry": 1, "restricted": 2, "perimeter": 1}


@dataclass(frozen=True)
class PriorityResult:
    priority: str
    score: int
    #: Human-readable reasons, in the order they were applied.
    reasons: tuple[str, ...] = ()

    @property
    def rank(self) -> int:
        return PRIORITY_RANK[self.priority]


def _bucket(score: int) -> str:
    if score >= 5:
        return "critical"
    if score >= 3:
        return "high"
    if score >= 1:
        return "normal"
    return "low"


def score_event(
    *,
    event_type: str | None,
    mode: str | None = None,
    zone_kind: str | None = None,
    confidence: float | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
    loitering: bool = False,
    unusual: bool = False,
    package_theft: bool = False,
) -> PriorityResult:
    """Score one event into ``low``/``normal``/``high``/``critical``."""
    tag_set = {str(tag) for tag in (tags or [])}
    score = _BASE_BY_TYPE.get((event_type or "").lower(), 0)
    reasons: list[str] = []
    if score:
        reasons.append(f"{event_type} detected")

    if zone_kind and zone_kind in _ZONE_BONUS:
        score += _ZONE_BONUS[zone_kind]
        reasons.append(f"{zone_kind} zone")

    if package_theft or "package_removed" in tag_set:
        score += 3
        reasons.append("package removed")
    elif "mailbox_retrieval" in tag_set:
        score += 1
        reasons.append("item taken from mailbox")

    if "mailbox_visit" in tag_set:
        score -= 1
        reasons.append("mailbox visit, outcome unknown")

    if loitering or "loitering" in tag_set:
        score += 1
        reasons.append("loitering")

    if unusual or "unusual_activity" in tag_set:
        score += 1
        reasons.append("unusual for this time")
    if "suspicious" in tag_set:
        score += 2
        reasons.append("suspicious behaviour")
    elif "elevated" in tag_set:
        score += 1
        reasons.append("elevated behaviour score")

    if mode in {"away", "night"}:
        score += 1
        reasons.append(f"armed {mode}")
    elif mode == "disarmed":
        score -= 1
        reasons.append("disarmed")

    if confidence is not None and confidence < 0.5:
        score -= 1
        reasons.append("low confidence")

    score = max(0, score)
    if tag_set & {"mailbox_delivery", "mailbox_retrieval"}:
        # A delivery or retrieval is always worth a normal notification,
        # even disarmed or with a low-confidence classification.
        score = max(score, 1)
    return PriorityResult(_bucket(score), score, tuple(reasons))


def meets_minimum(priority: str | None, minimum: str | None = None) -> bool:
    """Whether ``priority`` is at least ``minimum`` (default: configured).

    Unknown values fail open - an unrecognised priority is never silently
    suppressed.
    """
    if not settings.notification_priority_enabled:
        return True
    threshold = (minimum or settings.incident_min_priority or "low").lower()
    if threshold not in PRIORITY_RANK:
        return True
    if priority is None or priority.lower() not in PRIORITY_RANK:
        return True
    return PRIORITY_RANK[priority.lower()] >= PRIORITY_RANK[threshold]
