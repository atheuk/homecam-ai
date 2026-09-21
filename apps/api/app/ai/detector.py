"""Local object detection abstraction (SPEC section 13).

Two backends are selectable through ``AI_DETECTOR_BACKEND``:

``mock`` (default)
    :class:`MockDetector` — fully deterministic, zero extra dependencies, so
    tests and CI never need an ML runtime.

``onnx`` (opt-in)
    :class:`OnnxDetector` — runs a small pretrained COCO YOLO model through
    ONNX Runtime. The model file is *not* bundled in this repository; see
    ``docs/ai-pipeline.md`` for how to download one. If the runtime, numpy,
    Pillow or the model file are missing, construction raises
    :class:`DetectorUnavailableError` and :func:`get_detector` logs and falls
    back to the mock backend rather than breaking event ingestion (SPEC 43).

The detector only ever sees normalized snapshot bytes, never provider
objects, so it behaves identically for every provider.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

# SPEC section 13 categories. ``package`` has no COCO class; the ONNX backend
# maps it from carried-object heuristics only when the model exposes it, and
# the mock backend can emit it directly.
DETECTION_CLASSES: tuple[str, ...] = (
    "person",
    "car",
    "truck",
    "bicycle",
    "motorcycle",
    "dog",
    "cat",
    "package",
)

VEHICLE_CLASSES: frozenset[str] = frozenset({"car", "truck", "bicycle", "motorcycle"})
ANIMAL_CLASSES: frozenset[str] = frozenset({"dog", "cat"})


class DetectorUnavailableError(RuntimeError):
    """Raised when a detector backend cannot be constructed locally."""


@dataclass(frozen=True)
class BoundingBox:
    """Axis-aligned box in *normalized* image coordinates (0.0-1.0).

    Normalized coordinates keep the pipeline resolution-independent, which
    matters because different providers hand back different snapshot sizes.
    """

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.x1 < self.x2 <= 1.0 and 0.0 <= self.y1 < self.y2 <= 1.0):
            raise ValueError(f"invalid normalized bounding box: {self}")

    @property
    def area(self) -> float:
        return (self.x2 - self.x1) * (self.y2 - self.y1)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)

    def as_dict(self) -> dict[str, float]:
        return {"x1": self.x1, "y1": self.y1, "x2": self.x2, "y2": self.y2}

    @classmethod
    def from_dict(cls, raw: dict) -> "BoundingBox":
        return cls(float(raw["x1"]), float(raw["y1"]), float(raw["x2"]), float(raw["y2"]))


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    bbox: BoundingBox

    def as_dict(self) -> dict:
        return {"label": self.label, "confidence": round(self.confidence, 4), "bbox": self.bbox.as_dict()}


@dataclass(frozen=True)
class DetectionContext:
    """Normalized, provider-agnostic context handed to a detector."""

    camera_id: str
    camera_name: str = ""
    event_type: str | None = None


class LocalDetector(Protocol):
    """Replaceable local detection stage (SPEC section 13)."""

    name: str

    def detect(self, image: bytes, context: DetectionContext) -> list[Detection]: ...


def _digest(*parts: object) -> bytes:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(str(part).encode())
        hasher.update(b"\x00")
    return hasher.digest()


def _unit(value: int) -> float:
    return value / 255.0


# Event types that imply a specific object class. Keeps mock detections
# grounded in the event that triggered them instead of inventing activity.
_EVENT_TYPE_LABELS: dict[str, tuple[str, ...]] = {
    "person": ("person",),
    "doorbell": ("person",),
    "intrusion": ("person",),
    "vehicle": ("car",),
    "animal": ("dog",),
    "package": ("package",),
}


class MockDetector:
    """Deterministic detector used by default and in every test.

    The same ``(image, camera_id, event_type)`` triple always yields exactly
    the same detections, which makes pipeline behaviour reproducible without
    any ML runtime. Tests may pass ``script`` to pin detections for a camera.
    """

    name = "mock"

    def __init__(self, script: dict[str, list[Detection]] | None = None) -> None:
        self._script = dict(script or {})

    def set_script(self, camera_id: str, detections: list[Detection] | None) -> None:
        if detections is None:
            self._script.pop(camera_id, None)
        else:
            self._script[camera_id] = list(detections)

    def clear_script(self) -> None:
        self._script.clear()

    def detect(self, image: bytes, context: DetectionContext) -> list[Detection]:
        scripted = self._script.get(context.camera_id)
        if scripted is not None:
            return list(scripted)

        labels = _EVENT_TYPE_LABELS.get((context.event_type or "").lower())
        if labels is None:
            # A mock detector cannot actually see the frame. Rather than
            # inventing a class for a bare motion trigger (which would be
            # ungrounded activity, SPEC 15), report nothing and let the
            # event stay a plain motion event. Real recognition for
            # unlabelled motion requires the opt-in ONNX backend.
            return []

        detections: list[Detection] = []
        for index, label in enumerate(labels):
            seed = _digest(context.camera_id, context.event_type, label, index, image)
            x1 = 0.05 + _unit(seed[1]) * 0.45
            y1 = 0.05 + _unit(seed[2]) * 0.45
            width = 0.12 + _unit(seed[3]) * 0.25
            height = 0.15 + _unit(seed[4]) * 0.3
            bbox = BoundingBox(
                round(x1, 4),
                round(y1, 4),
                round(min(x1 + width, 0.999), 4),
                round(min(y1 + height, 0.999), 4),
            )
            confidence = round(0.55 + _unit(seed[5]) * 0.44, 4)
            detections.append(Detection(label=label, confidence=confidence, bbox=bbox))
        return detections


# Subset of the 80-class COCO label list relevant to HomeCam (SPEC 13).
COCO_CLASS_NAMES: dict[int, str] = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    5: "car",  # bus -> treated as a vehicle
    7: "truck",
    15: "cat",
    16: "dog",
}


class OnnxDetector:
    """Opt-in ONNX Runtime YOLO backend (SPEC section 13).

    Deliberately tolerant: any missing dependency or unreadable model raises
    :class:`DetectorUnavailableError` at construction time so the caller can
    degrade to the mock backend instead of failing an ingest request.
    """

    name = "onnx"

    def __init__(self, model_path: str, input_size: int = 640, confidence_threshold: float = 0.35) -> None:
        if not model_path:
            raise DetectorUnavailableError("AI_DETECTOR_MODEL_PATH is not set")
        try:
            import numpy  # noqa: F401
            import onnxruntime
            from PIL import Image  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on opt-in extras
            raise DetectorUnavailableError(f"onnx detector dependencies unavailable: {exc}") from exc
        try:
            self._session = onnxruntime.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        except Exception as exc:  # noqa: BLE001 - any runtime/model failure degrades to mock
            raise DetectorUnavailableError(f"could not load ONNX model '{model_path}': {exc}") from exc
        self._input_size = input_size
        self._confidence_threshold = confidence_threshold

    def detect(self, image: bytes, context: DetectionContext) -> list[Detection]:  # pragma: no cover - opt-in path
        import io

        import numpy as np
        from PIL import Image

        try:
            frame = Image.open(io.BytesIO(image)).convert("RGB").resize((self._input_size, self._input_size))
        except Exception as exc:  # noqa: BLE001
            logger.warning("onnx detector could not decode snapshot for %s: %s", context.camera_id, exc)
            return []
        tensor = np.asarray(frame, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        input_name = self._session.get_inputs()[0].name
        try:
            raw = self._session.run(None, {input_name: tensor})[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("onnx inference failed for %s: %s", context.camera_id, exc)
            return []
        return self._decode(np.squeeze(raw))

    def _decode(self, output) -> list[Detection]:  # pragma: no cover - opt-in path
        import numpy as np

        # Ultralytics YOLOv8 exports (84, N): cx, cy, w, h followed by class scores.
        if output.ndim != 2:
            return []
        if output.shape[0] < output.shape[1]:
            output = output.transpose()
        detections: list[Detection] = []
        for row in output:
            scores = row[4:]
            class_id = int(np.argmax(scores))
            confidence = float(scores[class_id])
            label = COCO_CLASS_NAMES.get(class_id)
            if label is None or confidence < self._confidence_threshold:
                continue
            cx, cy, w, h = (float(v) / self._input_size for v in row[:4])
            x1, y1 = max(cx - w / 2, 0.0), max(cy - h / 2, 0.0)
            x2, y2 = min(cx + w / 2, 1.0), min(cy + h / 2, 1.0)
            if x2 <= x1 or y2 <= y1:
                continue
            detections.append(
                Detection(label=label, confidence=confidence, bbox=BoundingBox(x1, y1, x2, y2))
            )
        return detections


_mock_detector = MockDetector()
_active_detector: LocalDetector | None = None


def mock_detector() -> MockDetector:
    """Return the process-wide mock detector (used by tests for scripting)."""
    return _mock_detector


def build_detector(backend: str, model_path: str = "") -> LocalDetector:
    """Build a detector, falling back to mock if the backend is unusable."""
    normalized = (backend or "mock").strip().lower()
    if normalized == "mock":
        return _mock_detector
    if normalized == "onnx":
        try:
            return OnnxDetector(model_path)
        except DetectorUnavailableError as exc:
            logger.warning("falling back to mock detector: %s", exc)
            return _mock_detector
    logger.warning("unknown AI_DETECTOR_BACKEND '%s'; using mock detector", backend)
    return _mock_detector


def get_detector() -> LocalDetector:
    """Return the configured detector, built lazily once per process."""
    global _active_detector
    if _active_detector is None:
        from ..config import settings

        _active_detector = build_detector(settings.ai_detector_backend, settings.ai_detector_model_path)
    return _active_detector


def reset_detector() -> None:
    """Drop the cached detector so configuration changes take effect."""
    global _active_detector
    _active_detector = None
