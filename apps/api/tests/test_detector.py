"""Local detector abstraction tests (SPEC section 13)."""
import pytest

from app.ai.detector import (
    DETECTION_CLASSES,
    BoundingBox,
    Detection,
    DetectionContext,
    MockDetector,
    build_detector,
    mock_detector,
)


def test_mock_detector_is_deterministic():
    detector = MockDetector()
    context = DetectionContext(camera_id="mock-front-door", camera_name="Front Door", event_type="person")
    first = detector.detect(b"frame-bytes", context)
    second = detector.detect(b"frame-bytes", context)
    assert first == second
    assert [d.label for d in first] == ["person"]
    assert 0.0 < first[0].confidence <= 1.0


def test_mock_detector_labels_follow_event_type():
    detector = MockDetector()
    for event_type, label in (("vehicle", "car"), ("animal", "dog"), ("package", "package"), ("doorbell", "person")):
        context = DetectionContext(camera_id="c", event_type=event_type)
        assert [d.label for d in detector.detect(b"x", context)] == [label]


def test_mock_detector_reports_nothing_for_bare_motion():
    """A mock detector cannot see the frame, so it must not invent a class."""
    detector = MockDetector()
    assert detector.detect(b"x", DetectionContext(camera_id="c", event_type="motion")) == []


def test_mock_detector_supports_scripted_detections():
    detector = MockDetector()
    scripted = [Detection("person", 0.9, BoundingBox(0.1, 0.1, 0.3, 0.6))]
    detector.set_script("cam-1", scripted)
    assert detector.detect(b"anything", DetectionContext(camera_id="cam-1")) == scripted
    detector.set_script("cam-1", None)
    assert detector.detect(b"anything", DetectionContext(camera_id="cam-1")) == []


def test_all_spec_categories_are_supported():
    assert set(DETECTION_CLASSES) == {
        "person", "car", "truck", "bicycle", "motorcycle", "dog", "cat", "package",
    }


def test_unknown_backend_falls_back_to_mock():
    assert build_detector("does-not-exist") is mock_detector()


def test_onnx_backend_without_model_falls_back_to_mock():
    """Opt-in backends must degrade, never crash the ingestion path."""
    assert build_detector("onnx", model_path="") is mock_detector()


def test_bounding_box_rejects_invalid_geometry():
    with pytest.raises(ValueError):
        BoundingBox(0.5, 0.1, 0.2, 0.4)
    with pytest.raises(ValueError):
        BoundingBox(-0.1, 0.1, 0.5, 0.4)


def test_bounding_box_area_and_center():
    box = BoundingBox(0.2, 0.2, 0.6, 0.4)
    assert box.area == pytest.approx(0.08)
    assert box.center == pytest.approx((0.4, 0.3))
