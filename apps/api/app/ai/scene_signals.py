"""Small, local image signals used by the scene state machines.

Nothing here detects objects. These are cheap summaries of a region of a
frame that let a tracker ask two questions without a model call:

* "does this vehicle still look like the one we were following?"
  (:func:`appearance_signature` / :func:`appearance_similarity`), and
* "has this fixed region of the scene changed?" (:func:`region_signature` /
  :func:`region_difference`), used for bins.

Both are designed to be robust to the ordinary lighting drift of an outdoor
camera and to degrade to "no opinion" (``None`` / empty) when the bytes are
not a decodable image, so callers never read a decode failure as evidence.
"""
from __future__ import annotations

import io
import logging

from .detector import BoundingBox
from .imaging import open_frame

logger = logging.getLogger(__name__)

_HUE_BINS = 12
_VALUE_BINS = 8
# Below this mean saturation (0-255) a crop is effectively greyscale (night
# IR, overcast, a white/grey car) and its hue histogram is noise.
_LOW_SATURATION = 30.0
_REGION_SIZE = 24


def decode(frame: bytes):
    """Decoded RGB image, or ``None``."""
    image = open_frame(frame)
    if image is None:
        return None
    try:
        return image.convert("RGB")
    except Exception:  # noqa: BLE001 - truncated beyond repair
        return None


def _crop(image, box: BoundingBox, margin: float = 0.0):
    width, height = image.size
    x1 = max(0.0, box.x1 - margin)
    y1 = max(0.0, box.y1 - margin)
    x2 = min(1.0, box.x2 + margin)
    y2 = min(1.0, box.y2 + margin)
    left, top = int(x1 * width), int(y1 * height)
    right, bottom = max(left + 1, int(x2 * width)), max(top + 1, int(y2 * height))
    return image.crop((left, top, right, bottom))


def crop_jpeg(image, box: BoundingBox, margin: float = 0.0, max_side: int = 512) -> bytes | None:
    """JPEG of ``box`` (plus ``margin``) from a decoded image."""
    if image is None:
        return None
    try:
        crop = _crop(image, box, margin)
        crop.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        crop.save(buffer, format="JPEG", quality=85)
        return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.debug("crop failed: %s", exc)
        return None


def appearance_signature(image, box: BoundingBox) -> list[float]:
    """Colour signature of a vehicle crop: hue (saturation-weighted) + value.

    Returns ``[]`` when no image is available. The last element records the
    mean saturation so comparisons can ignore hue for greyscale crops.
    """
    if image is None:
        return []
    try:
        import numpy as np

        crop = _crop(image, box).resize((32, 32)).convert("HSV")
        hsv = np.asarray(crop, dtype=np.float32).reshape(-1, 3)
    except Exception:  # noqa: BLE001
        return []
    hue, sat, val = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    hue_hist, _ = np.histogram(hue, bins=_HUE_BINS, range=(0, 256), weights=sat)
    val_hist, _ = np.histogram(val, bins=_VALUE_BINS, range=(0, 256))
    hue_total = float(hue_hist.sum()) or 1.0
    val_total = float(val_hist.sum()) or 1.0
    return (
        [float(v) / hue_total for v in hue_hist]
        + [float(v) / val_total for v in val_hist]
        + [float(sat.mean())]
    )


def appearance_similarity(left: list[float], right: list[float]) -> float | None:
    """Histogram intersection in 0..1, or ``None`` when either is unknown."""
    size = _HUE_BINS + _VALUE_BINS + 1
    if len(left) != size or len(right) != size:
        return None
    value = sum(min(a, b) for a, b in zip(left[_HUE_BINS:-1], right[_HUE_BINS:-1]))
    if left[-1] < _LOW_SATURATION or right[-1] < _LOW_SATURATION:
        return value
    hue = sum(min(a, b) for a, b in zip(left[:_HUE_BINS], right[:_HUE_BINS]))
    return (hue + value) / 2.0


def blend_signature(old: list[float], new: list[float], weight: float = 0.2) -> list[float]:
    """Slowly follow lighting drift on a matched track."""
    if not old or len(old) != len(new):
        return list(new)
    return [(1 - weight) * a + weight * b for a, b in zip(old, new)]


def region_signature(image, box: BoundingBox) -> list[float]:
    """Brightness-normalized greyscale thumbnail of a fixed region.

    Zero mean / unit variance, so a global lighting change (sun behind a
    cloud) barely moves it while an object appearing or leaving does.
    """
    if image is None:
        return []
    try:
        import numpy as np

        crop = _crop(image, box).convert("L").resize((_REGION_SIZE, _REGION_SIZE))
        values = np.asarray(crop, dtype=np.float32).reshape(-1)
    except Exception:  # noqa: BLE001
        return []
    std = float(values.std())
    if std < 1e-3:
        return [0.0] * values.size
    return [float(v) for v in (values - values.mean()) / std]


def region_difference(left: list[float], right: list[float]) -> float | None:
    """Mean absolute difference of two region signatures (0 = identical)."""
    if not left or len(left) != len(right):
        return None
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)
