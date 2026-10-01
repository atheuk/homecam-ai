"""Local detector abstraction tests (SPEC section 13)."""
import logging
import sys

import pytest

from app.ai.detector import (
    DETECTION_CLASSES,
    SUPPORTED_BACKENDS,
    BoundingBox,
    Detection,
    DetectionContext,
    DetectorConfigurationError,
    MockDetector,
    OpenCvDetector,
    build_detector,
    mock_detector,
    resolve_detector,
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
        "person", "car", "truck", "bicycle", "motorcycle",
        "dog", "cat", "bird", "animal", "package",
    }


def test_unknown_backend_falls_back_to_mock_but_is_reported_degraded(caplog):
    """Incident 2026-09-30: AI_DETECTOR_BACKEND=rtdetr-r50 was not accepted,
    the code logged one WARNING and returned the mock detector, and nothing
    said the system was blind for three hours."""
    with caplog.at_level(logging.ERROR):
        resolution = resolve_detector("does-not-exist")

    assert resolution.detector is mock_detector()
    status = resolution.status
    assert status.degraded is True
    assert status.recognised is False
    assert status.detecting is False
    assert status.intended_backend == "does-not-exist"
    assert status.active_backend == "mock"
    assert "does-not-exist" in (status.reason or "")
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def test_unknown_backend_is_fatal_in_strict_mode():
    with pytest.raises(DetectorConfigurationError) as excinfo:
        resolve_detector("rtdetr-r50-typo", strict=True)
    assert "rtdetr-r50-typo" in str(excinfo.value)


def test_explicit_mock_backend_is_not_degraded():
    """Mock is a deliberate opt-in, not an accident."""
    status = resolve_detector("mock").status
    assert status.degraded is False
    assert status.detecting is False
    assert status.reason is None


def test_missing_model_file_names_the_path_and_degrades_loudly(tmp_path, caplog):
    """The second half of the incident: a real backend name pointing at a
    model file the image does not contain."""
    missing = str(tmp_path / "rtdetr-r50.onnx")
    with caplog.at_level(logging.ERROR):
        resolution = resolve_detector("rtdetr", model_path=missing)

    assert resolution.detector is mock_detector()
    assert resolution.status.degraded is True
    assert resolution.status.recognised is True
    assert missing in (resolution.status.reason or "")
    assert missing in caplog.text


def test_missing_model_file_is_fatal_in_strict_mode(tmp_path):
    missing = str(tmp_path / "nope.onnx")
    with pytest.raises(DetectorConfigurationError) as excinfo:
        resolve_detector("rtdetr", model_path=missing, strict=True)
    assert missing in str(excinfo.value)


def test_backend_alias_never_masks_a_missing_model(tmp_path):
    """``rtdetr-r50`` is tolerated as a spelling of ``rtdetr`` — but only the
    code path is aliased. A model file that is not there still fails."""
    missing = str(tmp_path / "rtdetr-r50.onnx")
    status = resolve_detector("rtdetr-r50", model_path=missing).status
    assert status.intended_backend == "rtdetr"
    assert status.recognised is True
    assert status.degraded is True
    assert missing in (status.reason or "")


def test_supported_backends_are_the_documented_set():
    assert set(SUPPORTED_BACKENDS) == {"mock", "opencv", "onnx", "rtdetr"}


def test_onnx_backend_without_model_falls_back_to_mock():
    """Opt-in backends must degrade, never crash the ingestion path — but
    the degradation is recorded, not silent."""
    resolution = resolve_detector("onnx", model_path="")
    assert resolution.detector is mock_detector()
    assert resolution.status.degraded is True


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
