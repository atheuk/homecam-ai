"""Semantic event derivation: detections + zones + dwell -> richer events.

Backward-compatibility rule (deliberate, spec-compatible extension): the
``HomeCamEvent.type`` enum from SPEC section 9 is left untouched
(motion/person/vehicle/animal/package/doorbell/intrusion/unknown). Everything
the user asked for beyond that — "car parked on the driveway", "mailbox
opened", "someone accessed the driveway" — is expressed through the new
nullable ``zone`` column and the ``tags`` list rather than by inventing new
top-level event types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .detector import ANIMAL_CLASSES, VEHICLE_CLASSES, Detection
from .dwell import DwellTracker
from .zones import Zone, primary_zone

DRIVEWAY_KINDS = frozenset({"driveway"})
PARKING_KINDS = frozenset({"driveway", "parking"})
MAILBOX_KINDS = frozenset({"mailbox"})

# Event types that describe a provider action rather than an object in the
# frame; detections enrich them but must never overwrite them.
PROTECTED_EVENT_TYPES = frozenset({"doorbell", "battery_low"})


@dataclass
class SemanticResult:
    type: str
    zone: str | None = None
    tags: list[str] = field(default_factory=list)
    description: str | None = None
    primary_detection: Detection | None = None

    def as_dict(self) -> dict:
        return {"type": self.type, "zone": self.zone, "tags": list(self.tags)}


def _type_for_label(label: str) -> str:
    if label == "person":
        return "person"
    if label in VEHICLE_CLASSES:
        return "vehicle"
    if label in ANIMAL_CLASSES:
        return "animal"
    if label == "package":
        return "package"
    return "motion"


def _rank(detection: Detection) -> tuple[int, float]:
    """People outrank vehicles/animals; ties break on confidence."""
    priority = {"person": 4, "package": 3}.get(
        detection.label, 2 if detection.label in VEHICLE_CLASSES else 1
    )
    return (priority, detection.confidence)


def derive_semantics(
    *,
    camera_id: str,
    camera_name: str,
    base_event_type: str,
    detections: list[Detection],
    zones: list[Zone],
    tracker: DwellTracker,
    at: datetime,
    parked_after_seconds: float,
) -> SemanticResult:
    """Derive a spec-compatible event type plus zone/tags from detections."""
    if not detections:
        return SemanticResult(type=base_event_type, tags=[])

    primary = max(detections, key=_rank)
    zone = primary_zone(primary, zones)
    zone_name = zone.name if zone else None
    zone_kind = zone.kind if zone else None

    tags: list[str] = [detection.label for detection in detections]
    derived_type = _type_for_label(primary.label)
    if base_event_type in PROTECTED_EVENT_TYPES:
        derived_type = base_event_type

    # Every observed detection feeds the dwell tracker so a stationary
    # vehicle accumulates real, timestamp-backed dwell time.
    for detection in detections:
        detection_zone = primary_zone(detection, zones)
        tracker.observe(
            camera_id,
            detection.label,
            detection_zone.name if detection_zone else None,
            detection.bbox,
            at,
        )

    description: str | None = None

    if primary.label in VEHICLE_CLASSES and zone_kind in PARKING_KINDS:
        stationary = tracker.stationary_seconds(camera_id, primary.label, zone_name)
        if stationary >= parked_after_seconds:
            tags.append("parked")
            description = (
                f"A {primary.label} has been parked on the {zone_name} at {camera_name} "
                f"for about {int(stationary)}s."
            )
        else:
            tags.append("passing")
            description = f"A {primary.label} is moving through the {zone_name} at {camera_name}."
    elif primary.label == "person" and zone_kind in DRIVEWAY_KINDS:
        tags.append("driveway-access")
        description = f"A person is in the {zone_name} at {camera_name}."
    elif zone_kind in MAILBOX_KINDS:
        tags.append("mailbox")
        if primary.label == "package":
            derived_type = "package"
            description = f"Package activity at the {zone_name} on {camera_name}."
        else:
            derived_type = "package" if base_event_type not in PROTECTED_EVENT_TYPES else derived_type
            description = (
                f"The {zone_name} was accessed by a {primary.label} on {camera_name} "
                "(mailbox opened or reached into)."
            )
    elif primary.label in ANIMAL_CLASSES:
        where = f" in the {zone_name}" if zone_name else ""
        # ``animal`` is the generic "some other creature" class, so it needs
        # its own article rather than the detector's literal label.
        subject = "An animal" if primary.label == "animal" else f"A {primary.label}"
        description = f"{subject} passed{where} at {camera_name}."
    elif primary.label == "person":
        where = f" in the {zone_name}" if zone_name else ""
        description = f"A person was detected{where} at {camera_name}."

    if zone_name and zone_name not in tags:
        tags.append(zone_name)

    # Stable, de-duplicated tag order keeps assertions and UI output steady.
    seen: set[str] = set()
    ordered = [tag for tag in tags if not (tag in seen or seen.add(tag))]
    return SemanticResult(
        type=derived_type,
        zone=zone_name,
        tags=ordered,
        description=description,
        primary_detection=primary,
    )
