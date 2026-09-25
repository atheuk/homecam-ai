"""Detection quality controls: overlap suppression and plausibility.

These exist because the shipped detector (OpenCV HOG) reports the same
pedestrian several times at neighbouring scales, and the ONNX head emits a
row per anchor. Without suppression the user sees a stack of nested borders
around one person and reasonably concludes the detection is broken.
"""
from app.ai.detector import (
    MAX_DETECTIONS_PER_FRAME,
    BoundingBox,
    Detection,
    iou,
    plausible,
    refine_detections,
    suppress_overlaps,
)


def box(x, y, w, h):
    return BoundingBox(x1=x, y1=y, x2=x + w, y2=y + h)


def person(x, y, w, h, confidence, label="person"):
    return Detection(label=label, confidence=confidence, bbox=box(x, y, w, h))


def test_iou_of_identical_boxes_is_one():
    assert iou(box(0.1, 0.1, 0.2, 0.4), box(0.1, 0.1, 0.2, 0.4)) == 1.0


def test_iou_of_disjoint_boxes_is_zero():
    assert iou(box(0.0, 0.0, 0.1, 0.1), box(0.5, 0.5, 0.1, 0.1)) == 0.0


def test_nested_duplicates_collapse_to_the_most_confident():
    """HOG's multi-scale pass reports one person three times."""
    detections = [
        person(0.30, 0.20, 0.12, 0.40, 0.61),
        person(0.31, 0.21, 0.13, 0.41, 0.88),
        person(0.29, 0.19, 0.12, 0.42, 0.55),
    ]
    kept = suppress_overlaps(detections)
    assert len(kept) == 1
    assert kept[0].confidence == 0.88


def test_two_separate_people_both_survive():
    detections = [
        person(0.05, 0.20, 0.10, 0.40, 0.80),
        person(0.70, 0.22, 0.10, 0.40, 0.75),
    ]
    assert len(suppress_overlaps(detections)) == 2


def test_overlapping_boxes_of_different_labels_both_survive():
    """A person walking a dog genuinely produces overlapping boxes."""
    detections = [
        person(0.30, 0.10, 0.15, 0.60, 0.90),
        person(0.33, 0.55, 0.14, 0.18, 0.70, label="dog"),
    ]
    kept = suppress_overlaps(detections)
    assert {item.label for item in kept} == {"person", "dog"}


def test_specks_are_rejected():
    assert plausible(person(0.5, 0.5, 0.01, 0.01, 0.9)) is False


def test_wide_flat_person_is_rejected():
    """Fence panels and shadows across a path are the classic HOG artefact."""
    assert plausible(person(0.1, 0.5, 0.60, 0.10, 0.9)) is False


def test_wide_flat_box_is_allowed_for_non_person_labels():
    """A car legitimately is wider than it is tall."""
    assert plausible(person(0.1, 0.5, 0.60, 0.10, 0.9, label="car")) is True


def test_upright_person_is_kept():
    assert plausible(person(0.3, 0.2, 0.10, 0.45, 0.9)) is True


def test_refine_caps_the_number_of_boxes():
    detections = [
        person(i / 40.0, 0.2, 0.02, 0.30, 0.5 + i / 100.0)
        for i in range(MAX_DETECTIONS_PER_FRAME + 8)
    ]
    kept = refine_detections(detections)
    assert len(kept) == MAX_DETECTIONS_PER_FRAME
    # The cap must keep the strongest evidence, not the first rows seen.
    assert kept[0].confidence == max(item.confidence for item in detections)


def test_refine_drops_implausible_before_suppressing():
    detections = [
        person(0.1, 0.5, 0.60, 0.10, 0.99),  # fence-shaped, highest score
        person(0.30, 0.20, 0.12, 0.40, 0.70),
    ]
    kept = refine_detections(detections)
    assert len(kept) == 1
    assert kept[0].confidence == 0.70


def test_refine_of_empty_input_is_empty():
    assert refine_detections([]) == []
