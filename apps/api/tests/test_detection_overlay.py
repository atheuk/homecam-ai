"""Detection borders drawn over the stored event photo.

The photo HomeCam stores is a *crop* of the source frame, while the
detector reports boxes normalized to the *whole* frame. Getting that
mapping wrong is invisible in an API response and obvious in the UI (a
border floating in empty grass), so it is pinned down here.
"""
from __future__ import annotations

import io

from app.ai.best_photo import (
    MIN_VISIBLE_FRACTION,
    _readable_crop_box,
    crop_to_detection,
    overlay_boxes,
    select_best_photo,
)
from app.ai.detector import BoundingBox, Detection, DetectionContext, MockDetector


def _jpeg(size: tuple[int, int] = (1280, 720), colour: tuple[int, int, int] = (60, 90, 140)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG")
    return buffer.getvalue()


def _person(x1: float, y1: float, x2: float, y2: float, confidence: float = 0.9) -> Detection:
    return Detection(label="person", confidence=confidence, bbox=BoundingBox(x1, y1, x2, y2))


def test_box_is_expressed_relative_to_the_crop_not_the_frame():
    frame_size = (1000, 1000)
    detection = _person(0.40, 0.40, 0.60, 0.60)
    crop = (300, 300, 700, 700)  # 400x400 window starting at 30% of the frame

    [box] = overlay_boxes([detection], crop, frame_size)

    # Detection occupies pixels 400..600, i.e. 100..300 within the crop.
    assert box["box"] == {"x1": 0.25, "y1": 0.25, "x2": 0.75, "y2": 0.75}
    assert box["label"] == "person"
    assert box["clipped"] is False


def test_uncropped_photo_keeps_the_detectors_own_coordinates():
    detection = _person(0.1, 0.2, 0.3, 0.4)

    [box] = overlay_boxes([detection], None, None)

    assert box["box"] == {"x1": 0.1, "y1": 0.2, "x2": 0.3, "y2": 0.4}


def test_every_detection_in_the_photo_gets_its_own_border():
    frame_size = (1000, 1000)
    crop = (0, 0, 1000, 1000)

    boxes = overlay_boxes([_person(0.1, 0.1, 0.2, 0.5), _person(0.6, 0.1, 0.7, 0.5)], crop, frame_size)

    assert len(boxes) == 2


def test_a_detection_outside_the_crop_is_dropped():
    frame_size = (1000, 1000)
    crop = (0, 0, 400, 400)

    assert overlay_boxes([_person(0.7, 0.7, 0.9, 0.9)], crop, frame_size) == []


def test_a_barely_visible_sliver_is_dropped_rather_than_drawn():
    """A border round 2% of a person points at nothing useful."""
    frame_size = (1000, 1000)
    crop = (0, 0, 500, 1000)
    # Only 10% of this box's width survives the crop, under the floor.
    sliver = _person(0.49, 0.1, 0.59, 0.5)

    assert overlay_boxes([sliver], crop, frame_size) == []


def test_a_partly_visible_detection_is_kept_and_marked_clipped():
    frame_size = (1000, 1000)
    crop = (0, 0, 500, 1000)
    # Half of this box survives, comfortably above the floor.
    straddling = _person(0.4, 0.1, 0.6, 0.5)
    assert 0.5 > MIN_VISIBLE_FRACTION

    [box] = overlay_boxes([straddling], crop, frame_size)

    assert box["clipped"] is True
    assert box["box"]["x2"] == 1.0  # clamped to the crop's right edge


def test_boxes_survive_the_real_crop_pipeline_for_a_far_away_person():
    """End-to-end: the border must land on the person in the saved photo.

    A distant person triggers the readable-crop expansion *and* the
    slide-back-inside-the-frame path, which is precisely where a naive
    reuse of the raw bbox goes wrong.
    """
    width, height = 1280, 720
    frame = _jpeg((width, height))
    detection = _person(0.04, 0.30, 0.07, 0.40)

    _, cropped, crop_w, crop_h = crop_to_detection(frame, detection)
    assert cropped
    window = _readable_crop_box(detection.bbox, width, height, 0.08)
    [box] = overlay_boxes([detection], window, (width, height))

    # The border must sit inside the photo that is actually served...
    assert 0.0 <= box["box"]["x1"] < box["box"]["x2"] <= 1.0
    assert 0.0 <= box["box"]["y1"] < box["box"]["y2"] <= 1.0
    # ...and still cover the person's real pixels, not the frame origin.
    left, top = window[0], window[1]
    assert abs((box["box"]["x1"] * (window[2] - left) + left) - detection.bbox.x1 * width) < 2
    assert abs((box["box"]["y1"] * (window[3] - top) + top) - detection.bbox.y1 * height) < 2
    assert crop_w and crop_h


def test_selected_photo_publishes_its_boxes():
    frame = _jpeg((640, 480))
    detector = MockDetector({"cam-1": [_person(0.3, 0.3, 0.5, 0.8)]})

    photo = select_best_photo([frame], detector, DetectionContext(camera_id="cam-1"), {"person"})

    assert photo is not None
    assert photo.as_dict()["boxes"], "a detected person must publish a drawable border"
    assert photo.as_dict()["boxes"][0]["label"] == "person"


def test_photo_without_detections_publishes_no_boxes():
    frame = _jpeg((640, 480))
    detector = MockDetector({"cam-1": []})

    photo = select_best_photo([frame], detector, DetectionContext(camera_id="cam-1"), {"person"})

    assert photo is not None
    assert photo.as_dict()["boxes"] == []


def test_undecodable_frame_degrades_to_no_boxes_instead_of_failing():
    """SPEC 43: an overlay is never worth losing the photo over."""
    detector = MockDetector({"cam-1": [_person(0.3, 0.3, 0.5, 0.8)]})

    photo = select_best_photo([b"not-an-image"], detector, DetectionContext(camera_id="cam-1"), {"person"})

    assert photo is not None
    # Crop is impossible, so the stored photo is the raw frame and the
    # detector's own coordinates still apply to it.
    assert photo.cropped is False
    assert photo.as_dict()["boxes"][0]["box"]["x1"] == 0.3
