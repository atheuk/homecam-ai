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
# A detection box can legitimately be a tiny fraction of a 4K frame (a person
# at the far end of a driveway occupies ~2% of the width). Cropping tightly to
# that box yields a handful of unreadable pixels, which fails the actual
# requirement that the saved photo be understandable to a human. These floors
# expand the crop outward around the detection's centre until it covers at
# least this much of the frame, so the person is shown *in context* and at a
# usable size. Upscaling instead would only magnify blur; including
# surroundings is what makes the shot readable.
MIN_CROP_WIDTH_FRACTION = 0.22
MIN_CROP_HEIGHT_FRACTION = 0.30
# Never emit a crop smaller than this on the long edge; below it, browsers
# render a thumbnail no one can interpret.
MIN_CROP_PIXELS = 224
# Keep a person-shaped (portrait) aspect so heads aren't cut off by a box that
# was wider than it was tall.
TARGET_ASPECT_RATIO = 3 / 4  # width / height
JPEG_QUALITY = 88


@dataclass(frozen=True)
class BestPhoto:
    frame_index: int
    score: float
    sharpness: float
    detection: Detection | None
    image: bytes
    cropped: bool
    content_type: str = "image/jpeg"
    width: int | None = None
    height: int | None = None

    def as_dict(self) -> dict:
        return {
            "frame_index": self.frame_index,
            "score": round(self.score, 4),
            "sharpness": round(self.sharpness, 4),
            "cropped": self.cropped,
            "width": self.width,
            "height": self.height,
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


def _readable_crop_box(
    box, width: int, height: int, padding: float
) -> tuple[int, int, int, int]:
    """Expand a detection box into a human-readable crop window.

    Grows the box around its own centre until it meets the minimum frame
    fraction / pixel floors and a portrait-ish aspect, then slides it back
    inside the frame rather than clipping it (clipping would re-shrink the
    very crop we just widened, putting a far-away person back off-centre).
    """
    centre_x = (box.x1 + box.x2) / 2 * width
    centre_y = (box.y1 + box.y2) / 2 * height

    crop_width = (box.x2 - box.x1 + 2 * padding) * width
    crop_height = (box.y2 - box.y1 + 2 * padding) * height

    crop_width = max(crop_width, MIN_CROP_WIDTH_FRACTION * width, MIN_CROP_PIXELS)
    crop_height = max(crop_height, MIN_CROP_HEIGHT_FRACTION * height, MIN_CROP_PIXELS)

    # Enforce the portrait target without ever shrinking a dimension.
    if crop_width / crop_height > TARGET_ASPECT_RATIO:
        crop_height = crop_width / TARGET_ASPECT_RATIO
    else:
        crop_width = crop_height * TARGET_ASPECT_RATIO

    crop_width = min(crop_width, float(width))
    crop_height = min(crop_height, float(height))

    left = centre_x - crop_width / 2
    top = centre_y - crop_height / 2
    left = max(0.0, min(left, width - crop_width))
    top = max(0.0, min(top, height - crop_height))
    return int(left), int(top), int(left + crop_width), int(top + crop_height)


def crop_to_detection(
    image: bytes, detection: Detection, padding: float = CROP_PADDING
) -> tuple[bytes, bool, int | None, int | None]:
    """Crop around the detection so a human can actually tell who it is.

    Returns ``(bytes, cropped, width, height)``; a no-op without Pillow.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - depends on optional extras
        return image, False, None, None
    try:
        frame = Image.open(io.BytesIO(image))
        width, height = frame.size
        left, top, right, bottom = _readable_crop_box(detection.bbox, width, height, padding)
        if right - left < 2 or bottom - top < 2:
            return image, False, width, height
        cropped = frame.crop((left, top, right, bottom)).convert("RGB")
        buffer = io.BytesIO()
        cropped.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        return buffer.getvalue(), True, cropped.width, cropped.height
    except Exception as exc:  # noqa: BLE001 - never fail an event over a crop
        logger.debug("best-photo crop skipped: %s", exc)
        return image, False, None, None


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
        if crop and detection:
            image, cropped, width, height = crop_to_detection(frame, detection)
        else:
            image, cropped, width, height = frame, False, None, None
        best = BestPhoto(
            frame_index=index,
            score=score,
            sharpness=sharpness,
            detection=detection,
            image=image,
            cropped=cropped,
            content_type="image/jpeg" if cropped else _sniff_content_type(image),
            width=width,
            height=height,
        )
    return best


def _sniff_content_type(image: bytes) -> str:
    """Media type of an uncropped frame, from its magic bytes.

    Needed because the stored photo is served straight back to a browser:
    labelling a PNG as JPEG (or as ``application/octet-stream``) makes it
    render as a broken image or download instead of displaying.
    """
    if image.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image.startswith(b"GIF8"):
        return "image/gif"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"
