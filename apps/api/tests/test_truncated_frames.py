"""Truncated-JPEG tolerance.

Regression test for a real production failure: the Dahua edge connector
returns JPEGs missing their final bytes. Strict Pillow raised on them, so the
RT-DETR backend saw no frame at all for that camera and could never detect
anything, while the previous permissive OpenCV path decoded garbage and fired
phantom detections on it.
"""
from __future__ import annotations

import io

import pytest

from app.ai.best_photo import _image_size, crop_to_detection
from app.ai.detector import BoundingBox, Detection, DetectionContext, RtDetrDetector
from app.ai.imaging import configure_pillow, open_frame

Image = pytest.importorskip("PIL.Image")
np = pytest.importorskip("numpy")


def _jpeg(width: int = 320, height: int = 240) -> bytes:
    frame = Image.new("RGB", (width, height), (90, 120, 160))
    for x in range(0, width, 8):
        for y in range(0, height, 8):
            frame.putpixel((x, y), (250, 40, 40))
    buffer = io.BytesIO()
    frame.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def _truncated(image: bytes, missing: int = 40) -> bytes:
    """Drop the final bytes, exactly as observed from the edge connector."""
    return image[:-missing]


def test_a_truncated_jpeg_is_genuinely_undecodable_by_strict_pillow():
    """Guards the premise: without the flag this input really does raise."""
    import PIL.ImageFile

    original = PIL.ImageFile.LOAD_TRUNCATED_IMAGES
    PIL.ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        frame = Image.open(io.BytesIO(_truncated(_jpeg())))
        with pytest.raises(OSError):
            frame.load()
    finally:
        PIL.ImageFile.LOAD_TRUNCATED_IMAGES = original


def test_open_frame_decodes_a_truncated_jpeg():
    frame = open_frame(_truncated(_jpeg()))
    assert frame is not None
    frame.load()
    assert frame.size == (320, 240)


def test_open_frame_returns_none_for_non_image_bytes():
    """Mock providers emit placeholders; that must be 'no frame', not a crash."""
    assert open_frame(b"not-an-image-at-all") is None


def test_configure_pillow_is_idempotent():
    configure_pillow()
    configure_pillow()
    import PIL.ImageFile

    assert PIL.ImageFile.LOAD_TRUNCATED_IMAGES is True


def test_rtdetr_still_sees_a_truncated_frame():
    """The end-to-end point: a truncated snapshot must reach the model."""
    detector = RtDetrDetector.__new__(RtDetrDetector)
    detector._confidence_threshold = 0.5
    detector._input_size = 640
    captured = {}

    class _Session:
        def run(self, _outputs, feed):
            captured["tensor"] = next(iter(feed.values()))
            return [np.full((1, 300, 80), -20.0, dtype=np.float32), np.zeros((1, 300, 4), dtype=np.float32)]

    detector._session = _Session()
    detector._input_name = "pixel_values"
    result = detector.detect(_truncated(_jpeg()), DetectionContext(camera_id="dahua-channel-1"))
    assert result == []
    assert captured["tensor"].shape == (1, 3, 640, 640), "truncated frame never reached the model"


def test_rtdetr_reports_no_frame_for_non_image_bytes():
    detector = RtDetrDetector.__new__(RtDetrDetector)
    detector._confidence_threshold = 0.5
    detector._input_size = 640
    detector._input_name = "pixel_values"
    detector._session = None  # must never be reached
    assert detector.detect(b"mock-bytes", DetectionContext(camera_id="mock-garden")) == []


def test_best_photo_can_size_a_truncated_frame():
    assert _image_size(_truncated(_jpeg())) == (320, 240)


def test_best_photo_can_crop_a_truncated_frame():
    detection = Detection(label="person", confidence=0.9, bbox=BoundingBox(0.3, 0.2, 0.6, 0.9))
    cropped, did_crop, width, height = crop_to_detection(_truncated(_jpeg()), detection)
    assert did_crop is True
    assert width and height
    assert open_frame(cropped) is not None


def test_mock_placeholders_do_not_spam_warnings(caplog):
    """Mock cameras emit placeholders every poll; that is not a warning."""
    from app.ai.detector import _log_undecodable

    with caplog.at_level("DEBUG"):
        _log_undecodable("rtdetr", "mock-eufy-doorbell")
    assert [r.levelname for r in caplog.records] == ["DEBUG"]


def test_real_camera_decode_failure_still_warns(caplog):
    from app.ai.detector import _log_undecodable

    with caplog.at_level("DEBUG"):
        _log_undecodable("rtdetr", "dahua-channel-1")
    assert [r.levelname for r in caplog.records] == ["WARNING"]
