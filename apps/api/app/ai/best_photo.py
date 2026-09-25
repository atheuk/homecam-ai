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

# A detection that survives the crop as a barely-visible sliver along one
# edge draws a border that points at nothing. Below this fraction of the
# original box remaining visible, the box is dropped instead of drawn.
MIN_VISIBLE_FRACTION = 0.15

# Matching crop: subject pixels only. The margin is a fraction of the
# detection box, not of the frame - a fixed frame fraction would swamp a
# far-away subject with exactly the background we are trying to exclude.
SUBJECT_CROP_PADDING = 0.06
# Azure's image vectoriser rejects very small inputs; upscale to clear it.
SUBJECT_MIN_PIXELS = 64


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
    # Tight crop of the subject, used for identity matching only. Never
    # shown to the user — `image` is the readable version.
    subject_image: bytes | None = None
    # Detection borders expressed in *this photo's* own normalized
    # coordinates, ready to be drawn over it. See :func:`overlay_boxes` for
    # why the detector's own bbox cannot be used directly.
    boxes: tuple[dict, ...] = ()

    def as_dict(self) -> dict:
        return {
            "frame_index": self.frame_index,
            "score": round(self.score, 4),
            "sharpness": round(self.sharpness, 4),
            "cropped": self.cropped,
            "width": self.width,
            "height": self.height,
            "detection": self.detection.as_dict() if self.detection else None,
            "boxes": [dict(box) for box in self.boxes],
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


def crop_to_subject(image: bytes, detection: Detection) -> bytes:
    """Crop tightly to the detection, for embedding rather than display.

    The readable crop deliberately includes a lot of surroundings so a human
    can see who it is. That is actively harmful for re-identification:
    measured against the deployed Azure multimodal embedder, two *different*
    frames of the same empty garden scored 0.969 cosine similarity, because
    the vector mostly describes the scene. Feeding those wide crops to the
    matcher would merge everyone who stands in the same driveway into a
    single identity.

    So the matcher gets the subject pixels only, with a small margin. Crops
    below the embedder's usable size are upscaled rather than widened —
    widening would put the background straight back in.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - depends on optional extras
        return image
    try:
        frame = Image.open(io.BytesIO(image))
        width, height = frame.size
        box = detection.bbox
        pad_x = SUBJECT_CROP_PADDING * (box.x2 - box.x1) * width
        pad_y = SUBJECT_CROP_PADDING * (box.y2 - box.y1) * height
        left = max(0.0, box.x1 * width - pad_x)
        top = max(0.0, box.y1 * height - pad_y)
        right = min(float(width), box.x2 * width + pad_x)
        bottom = min(float(height), box.y2 * height + pad_y)
        if right - left < 2 or bottom - top < 2:
            return image
        subject = frame.crop((int(left), int(top), int(right), int(bottom))).convert("RGB")
        if subject.width < SUBJECT_MIN_PIXELS or subject.height < SUBJECT_MIN_PIXELS:
            scale = SUBJECT_MIN_PIXELS / min(subject.width, subject.height)
            subject = subject.resize(
                (max(1, round(subject.width * scale)), max(1, round(subject.height * scale))),
                Image.LANCZOS,
            )
        buffer = io.BytesIO()
        subject.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 - never fail an event over a crop
        logger.debug("subject crop skipped: %s", exc)
        return image


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


def _image_size(image: bytes) -> tuple[int, int] | None:
    """Pixel size of a frame, or ``None`` when it cannot be decoded."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - depends on optional extras
        return None
    try:
        with Image.open(io.BytesIO(image)) as frame:
            return frame.size
    except Exception:  # noqa: BLE001 - non-image bytes (e.g. mock snapshots)
        return None


def _describe(detection: Detection, box: tuple[float, float, float, float], clipped: bool) -> dict:
    x1, y1, x2, y2 = box
    return {
        "label": detection.label,
        "confidence": round(detection.confidence, 4),
        "clipped": clipped,
        "box": {
            "x1": round(x1, 5),
            "y1": round(y1, 5),
            "x2": round(x2, 5),
            "y2": round(y2, 5),
        },
    }


def overlay_boxes(
    detections: list[Detection],
    crop: tuple[int, int, int, int] | None,
    frame_size: tuple[int, int] | None,
) -> list[dict]:
    """Re-express detections in the *stored photo's* coordinate space.

    A detector reports boxes normalized to the full source frame, but the
    photo we actually store and serve is a readable crop of that frame
    (:func:`crop_to_detection`). Drawing the raw bbox over the crop would
    therefore put the border somewhere else entirely — usually off the
    photo, because the crop is centred on the subject.

    Passing ``crop=None`` means the stored photo *is* the whole frame, in
    which case the detector's coordinates already apply unchanged.

    Every detection in the frame is mapped, not just the chosen subject, so
    a photo containing two people gets two borders.
    """
    if crop is None or frame_size is None:
        return [_describe(d, (d.bbox.x1, d.bbox.y1, d.bbox.x2, d.bbox.y2), False) for d in detections]

    left, top, right, bottom = crop
    frame_width, frame_height = frame_size
    crop_width, crop_height = right - left, bottom - top
    if crop_width <= 0 or crop_height <= 0:
        return []

    boxes: list[dict] = []
    for detection in detections:
        bbox = detection.bbox
        px1, px2 = bbox.x1 * frame_width, bbox.x2 * frame_width
        py1, py2 = bbox.y1 * frame_height, bbox.y2 * frame_height
        original = (px2 - px1) * (py2 - py1)
        if original <= 0:
            continue
        ix1, iy1 = max(px1, left), max(py1, top)
        ix2, iy2 = min(px2, right), min(py2, bottom)
        if ix2 <= ix1 or iy2 <= iy1:
            continue
        if ((ix2 - ix1) * (iy2 - iy1)) / original < MIN_VISIBLE_FRACTION:
            continue
        clipped = ix1 > px1 or iy1 > py1 or ix2 < px2 or iy2 < py2
        boxes.append(
            _describe(
                detection,
                (
                    (ix1 - left) / crop_width,
                    (iy1 - top) / crop_height,
                    (ix2 - left) / crop_width,
                    (iy2 - top) / crop_height,
                ),
                clipped,
            )
        )
    return boxes


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


def _boxes_for_photo(
    frame: bytes, detection: Detection | None, detections: list[Detection], cropped: bool
) -> tuple[dict, ...]:
    """Detection borders for the photo that will actually be stored.

    Defensive per SPEC 43: an overlay is a nicety, so any failure here
    yields no borders rather than losing the photo or the event.
    """
    if not detections:
        return ()
    try:
        if not cropped or detection is None:
            return tuple(overlay_boxes(detections, None, None))
        size = _image_size(frame)
        if size is None:
            return ()
        window = _readable_crop_box(detection.bbox, size[0], size[1], CROP_PADDING)
        return tuple(overlay_boxes(detections, window, size))
    except Exception as exc:  # noqa: BLE001 - never fail an event over an overlay
        logger.debug("detection overlay skipped: %s", exc)
        return ()


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
        best = BestPhoto(            frame_index=index,
            score=score,
            sharpness=sharpness,
            detection=detection,
            image=image,
            cropped=cropped,
            content_type="image/jpeg" if cropped else _sniff_content_type(image),
            width=width,
            height=height,
            subject_image=crop_to_subject(frame, detection) if detection else None,
            boxes=_boxes_for_photo(frame, detection, detections, cropped),
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
