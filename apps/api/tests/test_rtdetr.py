"""RT-DETR decoding tests.

These exercise :meth:`RtDetrDetector._decode` directly with synthetic head
output rather than running the real 21MB model, so CI stays fast and does not
need the baked model file. The real model is validated separately against
COCO control images (see ``docs/ai-pipeline.md``).
"""
from __future__ import annotations

import math

import pytest

from app.ai.detector import (
    DEFAULT_RTDETR_MODEL_PATH,
    DetectorUnavailableError,
    RtDetrDetector,
    build_detector,
    mock_detector,
)

np = pytest.importorskip("numpy")


def _logits(pairs: list[tuple[int, float]], classes: int = 80):
    """Build a (queries, classes) logit array scoring ``class_id`` at ``p``."""
    out = np.full((len(pairs), classes), -20.0, dtype=np.float32)
    for query, (class_id, probability) in enumerate(pairs):
        out[query][class_id] = math.log(probability / (1 - probability))
    return out


def _decoder() -> RtDetrDetector:
    """An instance whose __init__ is bypassed - we only test _decode."""
    detector = RtDetrDetector.__new__(RtDetrDetector)
    detector._confidence_threshold = 0.5
    return detector


def test_decodes_a_confident_person_into_a_normalized_box():
    logits = _logits([(0, 0.95)])
    boxes = np.array([[0.5, 0.5, 0.2, 0.6]], dtype=np.float32)
    detections = _decoder()._decode(logits, boxes)
    assert len(detections) == 1
    detection = detections[0]
    assert detection.label == "person"
    assert detection.confidence == pytest.approx(0.95, abs=1e-3)
    assert detection.bbox.x1 == pytest.approx(0.4)
    assert detection.bbox.x2 == pytest.approx(0.6)
    assert detection.bbox.y1 == pytest.approx(0.2)
    assert detection.bbox.y2 == pytest.approx(0.8)


def test_scores_use_sigmoid_not_softmax():
    """Two classes can both be confident; softmax would force them to sum to 1."""
    logits = np.full((2, 80), -20.0, dtype=np.float32)
    logits[0][0] = math.log(0.9 / 0.1)
    logits[0][16] = math.log(0.8 / 0.2)
    logits[1][16] = math.log(0.85 / 0.15)
    boxes = np.array([[0.3, 0.5, 0.2, 0.6], [0.7, 0.6, 0.2, 0.3]], dtype=np.float32)
    detections = _decoder()._decode(logits, boxes)
    # Query 0 argmaxes to person at 0.9 - a softmax read would have dropped it
    # to ~0.52 and fallen under the 0.5 floor only marginally; the point is the
    # reported confidence must match the sigmoid value.
    person = [d for d in detections if d.label == "person"]
    assert person and person[0].confidence == pytest.approx(0.9, abs=1e-3)


def test_drops_queries_below_the_confidence_floor():
    logits = _logits([(0, 0.95), (0, 0.10)])
    boxes = np.array([[0.5, 0.5, 0.2, 0.6], [0.1, 0.1, 0.05, 0.15]], dtype=np.float32)
    assert len(_decoder()._decode(logits, boxes)) == 1


def test_ignores_classes_outside_the_homecam_label_map():
    """COCO 'toaster' (70) is not a HomeCam class and must not be reported."""
    logits = _logits([(70, 0.99)])
    boxes = np.array([[0.5, 0.5, 0.2, 0.3]], dtype=np.float32)
    assert _decoder()._decode(logits, boxes) == []


def test_maps_animal_classes():
    logits = _logits([(16, 0.9), (15, 0.88), (14, 0.8), (21, 0.75)])
    boxes = np.array(
        [[0.2, 0.5, 0.1, 0.2], [0.5, 0.5, 0.1, 0.2], [0.8, 0.3, 0.1, 0.1], [0.4, 0.8, 0.2, 0.2]],
        dtype=np.float32,
    )
    labels = {d.label for d in _decoder()._decode(logits, boxes)}
    assert labels == {"dog", "cat", "bird", "animal"}


def test_refuses_a_model_with_an_unexpected_class_count():
    """A 91-class head would silently shift every label by one."""
    logits = _logits([(0, 0.95)], classes=91)
    boxes = np.array([[0.5, 0.5, 0.2, 0.6]], dtype=np.float32)
    assert _decoder()._decode(logits, boxes) == []


def test_rejects_mismatched_output_shapes():
    decoder = _decoder()
    assert decoder._decode(np.zeros((3, 80), dtype=np.float32), np.zeros((2, 4), dtype=np.float32)) == []
    assert decoder._decode(np.zeros((3, 80), dtype=np.float32), np.zeros((3, 6), dtype=np.float32)) == []
    assert decoder._decode(np.zeros(80, dtype=np.float32), np.zeros((3, 4), dtype=np.float32)) == []


def test_degenerate_boxes_are_dropped():
    logits = _logits([(0, 0.95)])
    boxes = np.array([[0.5, 0.5, 0.0, 0.0]], dtype=np.float32)
    assert _decoder()._decode(logits, boxes) == []


def test_clamps_boxes_that_run_off_frame():
    logits = _logits([(0, 0.9)])
    boxes = np.array([[0.05, 0.5, 0.4, 0.6]], dtype=np.float32)
    detection = _decoder()._decode(logits, boxes)[0]
    assert detection.bbox.x1 == 0.0
    assert 0.0 < detection.bbox.x2 <= 1.0


def test_implausible_wide_person_is_filtered():
    """A person box wider than tall is a fence panel, not a person."""
    logits = _logits([(0, 0.95)])
    boxes = np.array([[0.5, 0.5, 0.8, 0.1]], dtype=np.float32)
    assert _decoder()._decode(logits, boxes) == []


def test_missing_model_path_is_unavailable_not_a_crash():
    with pytest.raises(DetectorUnavailableError):
        RtDetrDetector("")


def test_unreadable_model_falls_back_to_mock():
    assert build_detector("rtdetr", "/nonexistent/model.onnx") is mock_detector()


def test_backend_alias_is_accepted():
    assert build_detector("rt-detr", "/nonexistent/model.onnx") is mock_detector()


def test_default_model_path_is_the_baked_image_location():
    assert DEFAULT_RTDETR_MODEL_PATH == "/app/models/rtdetr.onnx"
