"""Local detector abstraction tests (SPEC section 13)."""
import sys

import pytest

from app.ai.detector import (
    DETECTION_CLASSES,
    BoundingBox,
    Detection,
    DetectionContext,
    MockDetector,
    OpenCvDetector,
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


def test_opencv_backend_without_cv2_falls_back_to_mock(monkeypatch):
    """No prebuilt cv2 wheel on some hosts (e.g. Windows ARM64 dev): the
    backend must degrade exactly like the onnx backend does, never crash.
    Forces the absence rather than depending on the test environment,
    since CI (unlike this host) does have opencv installed."""
    monkeypatch.setitem(sys.modules, "cv2", None)
    assert build_detector("opencv") is mock_detector()


def _install_fake_cv2(monkeypatch, boxes, weights, frame_shape=(200, 100, 3)):
    """Injects a minimal stand-in ``cv2`` module so the real detection/
    normalization/bbox math in :class:`OpenCvDetector` can be exercised
    without the actual (platform-limited) OpenCV wheel installed."""
    import types

    import numpy as np

    class _FakeHOGDescriptor:
        def setSVMDetector(self, model):
            self._svm = model

        def detectMultiScale(self, frame, **kwargs):
            assert frame.shape == frame_shape
            return np.array(boxes), np.array(weights)

    fake_cv2 = types.SimpleNamespace(
        HOGDescriptor=_FakeHOGDescriptor,
        HOGDescriptor_getDefaultPeopleDetector=lambda: object(),
        imdecode=lambda array, flags: np.zeros(frame_shape, dtype=np.uint8),
        IMREAD_COLOR=1,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)


def test_opencv_detector_converts_hog_boxes_into_normalized_person_detections(monkeypatch):
    _install_fake_cv2(monkeypatch, boxes=[[10, 20, 40, 90]], weights=[2.0])

    detector = OpenCvDetector()
    detections = detector.detect(b"fake-jpeg-bytes", DetectionContext(camera_id="cam-1", camera_name="Front Door"))

    assert [d.label for d in detections] == ["person"]
    box = detections[0].bbox
    assert 0.0 < detections[0].confidence <= 1.0
    assert box.x1 == pytest.approx(10 / 100)
    assert box.y1 == pytest.approx(20 / 200)
    assert box.x2 == pytest.approx(50 / 100)
    assert box.y2 == pytest.approx(110 / 200)


def test_opencv_detector_drops_low_confidence_boxes(monkeypatch):
    _install_fake_cv2(monkeypatch, boxes=[[0, 0, 10, 10]], weights=[-5.0], frame_shape=(50, 50, 3))

    detector = OpenCvDetector()
    assert detector.detect(b"x", DetectionContext(camera_id="cam-1")) == []


def test_build_detector_opencv_backend_uses_opencv_detector(monkeypatch):
    _install_fake_cv2(monkeypatch, boxes=[], weights=[], frame_shape=(10, 10, 3))
    detector = build_detector("opencv")
    assert isinstance(detector, OpenCvDetector)


def test_bounding_box_rejects_invalid_geometry():
    with pytest.raises(ValueError):
        BoundingBox(0.5, 0.1, 0.2, 0.4)
    with pytest.raises(ValueError):
        BoundingBox(-0.1, 0.1, 0.5, 0.4)


def test_bounding_box_area_and_center():
    box = BoundingBox(0.2, 0.2, 0.6, 0.4)
    assert box.area == pytest.approx(0.08)
    assert box.center == pytest.approx((0.4, 0.3))
