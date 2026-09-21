"""User-defined camera zones and detection/zone overlap math.

Zones are just *labeled rectangles* in normalized image coordinates — there
is no computer-vision zone detection here. A detection "is in" a zone when a
configurable fraction of the detection's bounding box overlaps the zone
rectangle, which is cheap, deterministic and provider-independent.
"""
from __future__ import annotations

from dataclasses import dataclass

from .detector import BoundingBox, Detection

# Canonical zone kinds HomeCam understands semantically. Any other name is
# still allowed and preserved; it simply carries no extra meaning.
ZONE_KINDS: tuple[str, ...] = ("driveway", "parking", "mailbox", "entry", "street", "garden", "other")

DEFAULT_MIN_OVERLAP = 0.3


@dataclass(frozen=True)
class Zone:
    name: str
    kind: str
    bbox: BoundingBox

    def as_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, **self.bbox.as_dict()}

    @classmethod
    def from_row(cls, row) -> "Zone":
        return cls(
            name=row.name,
            kind=row.kind,
            bbox=BoundingBox(row.x1, row.y1, row.x2, row.y2),
        )


def intersection_area(a: BoundingBox, b: BoundingBox) -> float:
    width = min(a.x2, b.x2) - max(a.x1, b.x1)
    height = min(a.y2, b.y2) - max(a.y1, b.y1)
    if width <= 0 or height <= 0:
        return 0.0
    return width * height


def overlap_ratio(detection_bbox: BoundingBox, zone_bbox: BoundingBox) -> float:
    """Fraction of the *detection* box that falls inside the zone box."""
    if detection_bbox.area <= 0:
        return 0.0
    return intersection_area(detection_bbox, zone_bbox) / detection_bbox.area


def zones_for_bbox(
    bbox: BoundingBox, zones: list[Zone], min_overlap: float = DEFAULT_MIN_OVERLAP
) -> list[tuple[Zone, float]]:
    """Return ``(zone, overlap)`` pairs sorted by strongest overlap first."""
    matches = [(zone, overlap_ratio(bbox, zone.bbox)) for zone in zones]
    hits = [(zone, ratio) for zone, ratio in matches if ratio >= min_overlap]
    return sorted(hits, key=lambda item: item[1], reverse=True)


def zones_for_detection(
    detection: Detection, zones: list[Zone], min_overlap: float = DEFAULT_MIN_OVERLAP
) -> list[Zone]:
    return [zone for zone, _ in zones_for_bbox(detection.bbox, zones, min_overlap)]


def primary_zone(
    detection: Detection, zones: list[Zone], min_overlap: float = DEFAULT_MIN_OVERLAP
) -> Zone | None:
    hits = zones_for_bbox(detection.bbox, zones, min_overlap)
    return hits[0][0] if hits else None
