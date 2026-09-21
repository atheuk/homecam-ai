"""Zone overlap math, dwell tracking and semantic event derivation."""
from datetime import datetime, timedelta, timezone

import pytest

from app.ai.detector import BoundingBox, Detection
from app.ai.dwell import DwellTracker
from app.ai.semantics import derive_semantics
from app.ai.zones import (
    Zone,
    intersection_area,
    overlap_ratio,
    primary_zone,
    zones_for_detection,
)

DRIVEWAY = Zone("driveway", "driveway", BoundingBox(0.0, 0.4, 0.6, 1.0))
MAILBOX = Zone("mailbox", "mailbox", BoundingBox(0.7, 0.3, 0.95, 0.7))
START = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)


def _detection(label, box, confidence=0.9):
    return Detection(label=label, confidence=confidence, bbox=box)


def test_intersection_area_of_disjoint_boxes_is_zero():
    assert intersection_area(BoundingBox(0.0, 0.0, 0.2, 0.2), BoundingBox(0.5, 0.5, 0.9, 0.9)) == 0.0


def test_overlap_ratio_is_relative_to_the_detection():
    # Detection is half inside the zone.
    detection = BoundingBox(0.4, 0.5, 0.8, 0.9)
    assert overlap_ratio(detection, DRIVEWAY.bbox) == pytest.approx(0.5)


def test_overlap_ratio_full_containment():
    assert overlap_ratio(BoundingBox(0.1, 0.5, 0.3, 0.8), DRIVEWAY.bbox) == pytest.approx(1.0)


def test_zones_for_detection_respects_min_overlap():
    detection = _detection("person", BoundingBox(0.5, 0.5, 0.9, 0.9))
    # Overlaps both zones; the strongest overlap is returned first.
    assert zones_for_detection(detection, [DRIVEWAY, MAILBOX], min_overlap=0.2) == [MAILBOX, DRIVEWAY]
    assert zones_for_detection(detection, [DRIVEWAY, MAILBOX], min_overlap=0.9) == []


def test_primary_zone_picks_strongest_overlap():
    detection = _detection("person", BoundingBox(0.55, 0.45, 0.75, 0.65))
    assert primary_zone(detection, [DRIVEWAY, MAILBOX], min_overlap=0.05) is not None


def test_dwell_tracker_accumulates_stationary_time():
    tracker = DwellTracker()
    box = BoundingBox(0.1, 0.5, 0.3, 0.8)
    tracker.observe("cam", "car", "driveway", box, START)
    tracker.observe("cam", "car", "driveway", box, START + timedelta(seconds=90))
    assert tracker.stationary_seconds("cam", "car", "driveway") == pytest.approx(90.0)


def test_dwell_tracker_resets_when_the_object_moves():
    tracker = DwellTracker()
    tracker.observe("cam", "car", "driveway", BoundingBox(0.1, 0.5, 0.3, 0.8), START)
    tracker.observe(
        "cam", "car", "driveway", BoundingBox(0.4, 0.5, 0.6, 0.8), START + timedelta(seconds=90)
    )
    assert tracker.stationary_seconds("cam", "car", "driveway") == pytest.approx(0.0)


def test_dwell_tracks_are_isolated_per_camera():
    tracker = DwellTracker()
    box = BoundingBox(0.1, 0.5, 0.3, 0.8)
    tracker.observe("cam-a", "car", "driveway", box, START)
    tracker.observe("cam-a", "car", "driveway", box, START + timedelta(seconds=60))
    assert tracker.stationary_seconds("cam-b", "car", "driveway") == 0.0


def _derive(detections, zones=(DRIVEWAY, MAILBOX), at=START, base="motion", tracker=None, parked=60.0):
    return derive_semantics(
        camera_id="cam",
        camera_name="Driveway Cam",
        base_event_type=base,
        detections=list(detections),
        zones=list(zones),
        tracker=tracker or DwellTracker(),
        at=at,
        parked_after_seconds=parked,
    )


def test_no_detections_leaves_the_event_untouched():
    result = _derive([])
    assert result.type == "motion"
    assert result.zone is None
    assert result.tags == []


def test_person_in_driveway_is_a_driveway_access_event():
    result = _derive([_detection("person", BoundingBox(0.1, 0.5, 0.3, 0.9))])
    assert result.type == "person"
    assert result.zone == "driveway"
    assert "driveway-access" in result.tags
    assert "driveway" in (result.description or "")


def test_moving_vehicle_in_driveway_is_tagged_passing():
    tracker = DwellTracker()
    result = _derive([_detection("car", BoundingBox(0.05, 0.5, 0.35, 0.9))], tracker=tracker)
    assert result.type == "vehicle"
    assert "passing" in result.tags
    assert "parked" not in result.tags


def test_stationary_vehicle_becomes_a_parked_car_event():
    tracker = DwellTracker()
    box = BoundingBox(0.05, 0.5, 0.35, 0.9)
    _derive([_detection("car", box)], tracker=tracker, at=START)
    result = _derive(
        [_detection("car", box)], tracker=tracker, at=START + timedelta(seconds=120), parked=60.0
    )
    assert result.type == "vehicle"  # SPEC section 9 enum stays intact
    assert "parked" in result.tags
    assert result.zone == "driveway"
    assert "parked" in (result.description or "")


def test_detection_in_mailbox_zone_derives_a_mailbox_event():
    result = _derive([_detection("person", BoundingBox(0.72, 0.35, 0.92, 0.65))])
    assert result.zone == "mailbox"
    assert "mailbox" in result.tags
    assert result.type == "package"
    assert "mailbox" in (result.description or "").lower()


def test_animal_anywhere_is_an_animal_event():
    result = _derive([_detection("dog", BoundingBox(0.62, 0.05, 0.68, 0.15))])
    assert result.type == "animal"
    assert result.zone is None
    assert "dog" in result.tags


def test_person_outranks_a_vehicle_in_the_same_frame():
    result = _derive(
        [
            _detection("car", BoundingBox(0.05, 0.5, 0.35, 0.9), confidence=0.99),
            _detection("person", BoundingBox(0.1, 0.45, 0.2, 0.75), confidence=0.6),
        ]
    )
    assert result.type == "person"
    assert set(result.tags) >= {"car", "person"}


def test_doorbell_event_type_is_never_overwritten():
    result = _derive([_detection("person", BoundingBox(0.1, 0.5, 0.3, 0.9))], base="doorbell")
    assert result.type == "doorbell"


def test_tags_are_deduplicated_and_stable():
    detections = [
        _detection("dog", BoundingBox(0.1, 0.5, 0.2, 0.6)),
        _detection("dog", BoundingBox(0.3, 0.5, 0.4, 0.6)),
    ]
    result = _derive(detections)
    assert result.tags.count("dog") == 1
