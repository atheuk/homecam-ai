"""Shared image decoding for the AI pipeline.

Exists for one reason: **the Dahua edge connector returns slightly truncated
JPEGs.** Observed live, a ~2 MB channel-1 snapshot arrives with its last
18-53 bytes missing. The frame is otherwise complete and perfectly usable.

The two decoders in this codebase disagree about what to do with that, and
the disagreement was silently producing wrong answers in both directions:

* ``cv2.imdecode`` (the old OpenCV/HOG path) is permissive. It returns a
  partial image whose missing tail is filled with garbage, and HOG then
  fires on that garbage - the origin of the phantom "person" boxes drawn on
  empty scenes.
* Pillow is strict. It raises ``OSError: image file is truncated``, so the
  RT-DETR path saw *no* frame at all for that camera and could never detect
  anything on it.

Neither "hallucinate on corruption" nor "go blind" is acceptable. Setting
``LOAD_TRUNCATED_IMAGES`` makes Pillow decode the bytes that did arrive,
which is the honest reading of an almost-complete JPEG. The real truncation
should still be fixed at the edge connector; this keeps the pipeline correct
in the meantime, and harmless if it is fixed.
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

_configured = False


def configure_pillow() -> None:
    """Allow Pillow to decode JPEGs whose final bytes are missing.

    Idempotent, and safe to call from any lazy ``from PIL import Image``
    site. ``LOAD_TRUNCATED_IMAGES`` is a process-global Pillow flag.
    """
    global _configured
    if _configured:
        return
    from PIL import ImageFile

    ImageFile.LOAD_TRUNCATED_IMAGES = True
    _configured = True


def open_frame(image: bytes):
    """Decode ``image`` with Pillow, tolerating a truncated tail.

    Returns a PIL ``Image`` or ``None`` if the bytes are not an image at all
    (mock providers emit placeholder payloads). Callers must treat ``None``
    as "no frame", never as "nothing in the frame".
    """
    configure_pillow()
    from PIL import Image

    try:
        return Image.open(io.BytesIO(image))
    except Exception:  # noqa: BLE001 - non-image bytes are an expected input
        return None
