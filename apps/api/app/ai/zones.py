"""User-defined camera zones and detection/zone overlap math.

Zones are user-drawn *shapes* in normalized image coordinates — there is no
computer-vision zone detection here. A detection "is in" a zone when a
configurable fraction of the detection's bounding box overlaps the zone,
which is cheap, deterministic and provider-independent.

A zone is either a plain rectangle (``x1/y1/x2/y2``, the original shape) or
a polygon of >= 3 points drawn on a still from the camera. A polygon always
also stores its bounding box, because the stateful mailbox/bin detectors
crop and compare *rectangular* image regions; only overlap matching uses
the exact polygon.
"""
from __future__ import annotations

from dataclasses import dataclass

from .detector import BoundingBox, Detection

Point = tuple[float, float]

# Upper bound on polygon complexity. Drawing is per-click, so this is high
# enough to never be reached by hand yet keeps stored/clipped geometry small.
MAX_ZONE_POINTS = 64
_GEOMETRY_EPSILON = 1e-12

# Canonical zone kinds HomeCam understands semantically. Any other name is
# still allowed and preserved; it simply carries no extra meaning.
ZONE_KINDS: tuple[str, ...] = (
    "driveway", "parking", "mailbox", "bin", "entry", "street", "garden", "other",
)

DEFAULT_MIN_OVERLAP = 0.3


@dataclass(frozen=True)
class Zone:
    name: str
    kind: str
    bbox: BoundingBox
    #: Exact drawn outline, or ``None`` for a plain rectangle zone.
    points: tuple[Point, ...] | None = None
    #: Loitering threshold in seconds, or ``None`` to use the configured
    #: default. Carried here so the pipeline never has to re-read the row.
    dwell_seconds: float | None = None

    def as_dict(self) -> dict:
        data = {"name": self.name, "kind": self.kind, **self.bbox.as_dict()}
        if self.points is not None:
            data["points"] = [list(point) for point in self.points]
        if self.dwell_seconds is not None:
            data["dwell_seconds"] = self.dwell_seconds
        return data

    @classmethod
    def from_row(cls, row) -> "Zone":
        points = normalize_points(getattr(row, "points", None))
        return cls(
            name=row.name,
            kind=row.kind,
            bbox=BoundingBox(row.x1, row.y1, row.x2, row.y2),
            points=points,
            dwell_seconds=getattr(row, "dwell_seconds", None),
        )

    def overlap(self, detection_bbox: BoundingBox) -> float:
        """Fraction of ``detection_bbox`` that falls inside this zone."""
        if self.points is None:
            return overlap_ratio(detection_bbox, self.bbox)
        if detection_bbox.area <= 0:
            return 0.0
        clipped = clip_polygon_to_bbox(self.points, detection_bbox)
        return polygon_area(clipped) / detection_bbox.area


def normalize_points(raw) -> tuple[Point, ...] | None:
    """Validate stored polygon points, or ``None`` when there are none.

    Raises ``ValueError`` for malformed geometry so callers can skip the
    row instead of feeding nonsense into the pipeline.
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise ValueError("zone points must be a list")
    if not raw:
        return None
    if len(raw) < 3:
        raise ValueError("a zone polygon needs at least 3 points")
    if len(raw) > MAX_ZONE_POINTS:
        raise ValueError(f"a zone polygon may have at most {MAX_ZONE_POINTS} points")
    points: list[Point] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("each zone point must be an [x, y] pair")
        x, y = item
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError("zone point coordinates must be numbers")
        if isinstance(x, bool) or isinstance(y, bool):
            raise ValueError("zone point coordinates must be numbers")
        if not (0.0 <= x <= 1.0) or not (0.0 <= y <= 1.0):
            raise ValueError("zone point coordinates must be between 0 and 1")
        points.append((float(x), float(y)))
    if any(points[index] == points[(index + 1) % len(points)] for index in range(len(points))):
        raise ValueError("a zone polygon cannot have duplicate adjacent points")
    if polygon_area(points) <= 0.0:
        raise ValueError("a zone polygon must enclose a non-zero area")
    _validate_simple_polygon(points)
    return tuple(points)


def _validate_simple_polygon(points: list[Point]) -> None:
    """Reject polygon edges that cross or overlap except at adjacent endpoints."""
    count = len(points)
    for first in range(count):
        a, b = points[first], points[(first + 1) % count]
        for second in range(first + 1, count):
            c, d = points[second], points[(second + 1) % count]
            adjacent = second == first + 1 or (first == 0 and second == count - 1)
            if not _segments_intersect(a, b, c, d):
                continue
            if adjacent:
                shared = b if second == first + 1 else a
                other_first = a if second == first + 1 else b
                other_second = d if second == first + 1 else c
                if abs(_cross(shared, other_first, other_second)) <= _GEOMETRY_EPSILON:
                    first_vector = (other_first[0] - shared[0], other_first[1] - shared[1])
                    second_vector = (other_second[0] - shared[0], other_second[1] - shared[1])
                    if first_vector[0] * second_vector[0] + first_vector[1] * second_vector[1] > 0:
                        raise ValueError("a zone polygon cannot have overlapping adjacent edges")
                continue
            raise ValueError("a zone polygon cannot self-intersect")


def _cross(origin: Point, a: Point, b: Point) -> float:
    return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    ab_c = _orientation(_cross(a, b, c))
    ab_d = _orientation(_cross(a, b, d))
    cd_a = _orientation(_cross(c, d, a))
    cd_b = _orientation(_cross(c, d, b))
    if ab_c * ab_d < 0 and cd_a * cd_b < 0:
        return True
    return (
        (ab_c == 0 and _on_segment(a, b, c))
        or (ab_d == 0 and _on_segment(a, b, d))
        or (cd_a == 0 and _on_segment(c, d, a))
        or (cd_b == 0 and _on_segment(c, d, b))
    )


def _orientation(value: float) -> int:
    if abs(value) <= _GEOMETRY_EPSILON:
        return 0
    return 1 if value > 0 else -1


def _on_segment(a: Point, b: Point, point: Point) -> bool:
    return (
        min(a[0], b[0]) - _GEOMETRY_EPSILON
        <= point[0]
        <= max(a[0], b[0]) + _GEOMETRY_EPSILON
        and min(a[1], b[1]) - _GEOMETRY_EPSILON
        <= point[1]
        <= max(a[1], b[1]) + _GEOMETRY_EPSILON
    )


def bbox_of_points(points) -> BoundingBox:
    """Axis-aligned bounding box of a polygon, used for region crops."""
    xs = [float(x) for x, _ in points]
    ys = [float(y) for _, y in points]
    return BoundingBox(min(xs), min(ys), max(xs), max(ys))


def polygon_area(points) -> float:
    """Unsigned shoelace area; ``0.0`` for degenerate input."""
    points = list(points)
    if len(points) < 3:
        return 0.0
    total = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def clip_polygon_to_bbox(points, box: BoundingBox) -> list[Point]:
    """Sutherland-Hodgman clip of a polygon against an axis-aligned box."""
    output: list[Point] = [(float(x), float(y)) for x, y in points]
    edges = (
        ("x", box.x1, True),
        ("x", box.x2, False),
        ("y", box.y1, True),
        ("y", box.y2, False),
    )
    for axis, limit, keep_greater in edges:
        if not output:
            return []
        subject, output = output, []
        previous = subject[-1]
        for current in subject:
            current_in = _inside(current, axis, limit, keep_greater)
            previous_in = _inside(previous, axis, limit, keep_greater)
            if current_in:
                if not previous_in:
                    output.append(_intersect(previous, current, axis, limit))
                output.append(current)
            elif previous_in:
                output.append(_intersect(previous, current, axis, limit))
            previous = current
    return output


def _inside(point: Point, axis: str, limit: float, keep_greater: bool) -> bool:
    value = point[0] if axis == "x" else point[1]
    return value >= limit if keep_greater else value <= limit


def _intersect(a: Point, b: Point, axis: str, limit: float) -> Point:
    ax, ay = a
    bx, by = b
    if axis == "x":
        if bx == ax:
            return (limit, by)
        t = (limit - ax) / (bx - ax)
        return (limit, ay + t * (by - ay))
    if by == ay:
        return (bx, limit)
    t = (limit - ay) / (by - ay)
    return (ax + t * (bx - ax), limit)


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
    matches = [(zone, zone.overlap(bbox)) for zone in zones]
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
