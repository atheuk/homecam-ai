"""Best-photo selection: pick one sharp, representative frame per event.

The user requirement is explicit: when a person (or other relevant object)
is detected, HomeCam must keep *one good, sharp photo of that object*, not a
raw motion frame. We therefore sample a few frames around the trigger using
each provider's existing snapshot mechanism, score them, and keep the best
one cropped to the detection box.

Dependency policy: Pillow/numpy are used when importable for a real
Laplacian-variance sharpness metric and a real crop, but they are optional.
Without them the module falls back to a deterministic pure-Python byte-delta
metric and stores the uncropped frame, so the default install (and CI) needs
nothing extra.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from .detector import Detection, DetectionContext, LocalDetector

logger = logging.getLogger(__name__)

# Weighting between "is the object clearly there" and "is the frame sharp".
CONFIDENCE_WEIGHT = 0.6
SHARPNESS_WEIGHT = 0.4
CROP_PADDING = 0.08


@dataclass(frozen=True)
class BestPhoto:
    frame_index: int
    score: float
    sharpness: float
    detection: Detection | None
    image: bytes
    cropped: bool

    def as_dict(self) -> dict:
        return {
            "frame_index": self.frame_index,
            "score": round(self.score, 4),
            "sharpness": round(self.sharpness, 4),
            "cropped": self.cropped,
            "detection": self.detection.as_dict() if self.detection else None,
        }


def _pillow_sharpness(image: bytes) -> float | None:
    try:
        import numpy as np
        from PIL import Image
    except ImportError:  # pragma: no cover - depends on optional extras
        return None
    try:
        frame = Image.open(io.BytesIO(image)).convert("L")
    except Exception:  # noqa: BLE001 - non-image bytes (e.g. mock snapshots)
        return None
    array = np.asarray(frame, dtype="float32")
    if array.size < 9:
        return None
    # 4-neighbour Laplacian; its variance is the classic blur metric.
    laplacian = (
        -4 * array[1:-1, 1:-1]
        + array[:-2, 1:-1]
        + array[2:, 1:-1]
        + array[1:-1, :-2]
        + array[1:-1, 2:]
    )
    variance = float(laplacian.var())
    # Squash into 0..1; 500 is an empirically reasonable "clearly sharp" point.
    return min(variance / 500.0, 1.0)


def _fallback_sharpness(image: bytes) -> float:
    """Deterministic pure-Python proxy for frames we cannot decode.

    Mean absolute byte-to-byte delta correlates with high-frequency content,
    which is what a blur metric measures. It is only a proxy, and is
    documented as such, but it is stable and dependency-free.
    """
    if len(image) < 2:
        return 0.0
    total = sum(abs(image[i] - image[i - 1]) for i in range(1, len(image)))
    return min((total / (len(image) - 1)) / 64.0, 1.0)


def sharpness_score(image: bytes) -> float:
    """Return a 0..1 sharpness estimate for a candidate frame."""
    value = _pillow_sharpness(image)
    return value if value is not None else _fallback_sharpness(image)


def crop_to_detection(image: bytes, detection: Detection, padding: float = CROP_PADDING) -> tuple[bytes, bool]:
    """Crop to the detection box with padding; no-op without Pillow."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - depends on optional extras
        return image, False
    try:
        frame = Image.open(io.BytesIO(image))
        width, height = frame.size
        box = detection.bbox
        left = int(max(box.x1 - padding, 0.0) * width)
        top = int(max(box.y1 - padding, 0.0) * height)
        right = int(min(box.x2 + padding, 1.0) * width)
        bottom = int(min(box.y2 + padding, 1.0) * height)
        if right - left < 2 or bottom - top < 2:
            return image, False
        buffer = io.BytesIO()
        frame.crop((left, top, right, bottom)).save(buffer, format=frame.format or "PNG")
        return buffer.getvalue(), True
    except Exception as exc:  # noqa: BLE001 - never fail an event over a crop
        logger.debug("best-photo crop skipped: %s", exc)
        return image, False


def score_frame(
    frame: bytes, detections: list[Detection], target_labels: frozenset[str] | set[str]
) -> tuple[float, float, Detection | None]:
    """Score one candidate frame; returns ``(score, sharpness, detection)``."""
    sharpness = sharpness_score(frame)
    candidates = [d for d in detections if d.label in target_labels] or detections
    best = max(candidates, key=lambda d: d.confidence) if candidates else None
    confidence = best.confidence if best else 0.0
    score = CONFIDENCE_WEIGHT * confidence + SHARPNESS_WEIGHT * sharpness
    return score, sharpness, best


def select_best_photo(
    frames: list[bytes],
    detector: LocalDetector,
    context: DetectionContext,
    target_labels: frozenset[str] | set[str],
    crop: bool = True,
) -> BestPhoto | None:
    """Pick the sharpest frame that most confidently contains the target.

    Frames are scored independently, so a blurry motion frame loses to a
    later sharp frame of the same person even if both detect equally well.
    """
    if not frames:
        return None
    best: BestPhoto | None = None
    for index, frame in enumerate(frames):
        detections = detector.detect(frame, context)
        score, sharpness, detection = score_frame(frame, detections, target_labels)
        if best is not None and score <= best.score:
            continue
        image, cropped = (crop_to_detection(frame, detection) if crop and detection else (frame, False))
        best = BestPhoto(
            frame_index=index,
            score=score,
            sharpness=sharpness,
            detection=detection,
            image=image,
            cropped=cropped,
        )
    return best
