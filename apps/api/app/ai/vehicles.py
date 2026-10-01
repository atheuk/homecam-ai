"""Grounded vehicle make/model identification from security-camera crops."""
from __future__ import annotations

import base64
import json
import logging
import io
from collections import Counter
from dataclasses import dataclass
from typing import Protocol, Sequence

import httpx

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"
MAX_VEHICLE_FRAMES = 5
MAX_VEHICLE_IMAGE_BYTES = 4 * 1024 * 1024
MAX_VEHICLE_BATCH_BYTES = 10 * 1024 * 1024
_VEHICLE_FIELDS = ("make", "model", "year_generation", "colour", "body_type", "trim")
_NULL_VALUES = frozenset(
    {"", "-", "n/a", "na", "null", "none", "unknown", "unclear", "not visible", "not sure", "cannot tell"}
)


def crop_vehicle(frame: bytes, bbox, min_pixels: int = 320) -> bytes:
    """Tight full-resolution vehicle crop, padded and upscaled for vision."""
    try:
        from PIL import Image
        from .imaging import configure_pillow

        configure_pillow()
        with Image.open(io.BytesIO(frame)) as source:
            width, height = source.size
            pad_x = (bbox.x2 - bbox.x1) * 0.05
            pad_y = (bbox.y2 - bbox.y1) * 0.05
            box = (
                max(0, int((bbox.x1 - pad_x) * width)),
                max(0, int((bbox.y1 - pad_y) * height)),
                min(width, int((bbox.x2 + pad_x) * width)),
                min(height, int((bbox.y2 + pad_y) * height)),
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                return frame
            crop = source.crop(box).convert("RGB")
            if min(crop.size) < min_pixels:
                scale = min_pixels / min(crop.size)
                crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.LANCZOS)
            output = io.BytesIO()
            crop.save(output, format="JPEG", quality=90)
            return output.getvalue()
    except (ImportError, OSError, ValueError):
        return frame

VEHICLE_SYSTEM_PROMPT = (
    "You identify visible vehicle attributes in home security camera images. "
    "Use only visible evidence; never infer an exact year, trim, or feature that "
    "cannot be distinguished. Return the literal string \"unknown\" for every "
    "field you cannot identify. Return \"vehicle_present\": \"no\" if no vehicle "
    "is visible, and \"unknown\" if presence itself is unclear. If several frames "
    "show the same vehicle, use only details consistent with those frames. "
    "Do not infer ownership, driver identity, or intent. Return exactly the "
    "required JSON fields."
)

VEHICLE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "vehicle_present": {"type": "string", "enum": ["yes", "no", "unknown"]},
        "make": {"type": "string"},
        "make_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "model": {"type": "string"},
        "model_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "year_generation": {"type": "string"},
        "year_generation_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "colour": {"type": "string"},
        "colour_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "body_type": {"type": "string"},
        "body_type_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "trim": {"type": "string"},
        "trim_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "vehicle_present",
        "make",
        "make_confidence",
        "model",
        "model_confidence",
        "year_generation",
        "year_generation_confidence",
        "colour",
        "colour_confidence",
        "body_type",
        "body_type_confidence",
        "trim",
        "trim_confidence",
    ],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class VehicleIdentity:
    """Observable vehicle attributes, with a separate confidence per field."""

    vehicle_present: str = UNKNOWN
    make: str = UNKNOWN
    make_confidence: float = 0.0
    model: str = UNKNOWN
    model_confidence: float = 0.0
    year_generation: str = UNKNOWN
    year_generation_confidence: float = 0.0
    colour: str = UNKNOWN
    colour_confidence: float = 0.0
    body_type: str = UNKNOWN
    body_type_confidence: float = 0.0
    trim: str = UNKNOWN
    trim_confidence: float = 0.0

    def as_dict(self) -> dict[str, str | float]:
        return {
            "vehicle_present": self.vehicle_present,
            "make": self.make,
            "make_confidence": round(self.make_confidence, 4),
            "model": self.model,
            "model_confidence": round(self.model_confidence, 4),
            "year_generation": self.year_generation,
            "year_generation_confidence": round(self.year_generation_confidence, 4),
            "colour": self.colour,
            "colour_confidence": round(self.colour_confidence, 4),
            "body_type": self.body_type,
            "body_type_confidence": round(self.body_type_confidence, 4),
            "trim": self.trim,
            "trim_confidence": round(self.trim_confidence, 4),
        }


class VehicleIdentifier(Protocol):
    """Public identifier interface; image bytes may already be a detector crop."""

    name: str

    async def identify_vehicle(
        self, image: bytes, content_type: str = "image/jpeg"
    ) -> VehicleIdentity | None: ...

    async def identify_vehicle_frames(
        self, frames: Sequence[bytes | tuple[bytes, str]]
    ) -> VehicleIdentity | None: ...


def _clean_value(value: object, limit: int = 80) -> str:
    if not isinstance(value, str):
        return UNKNOWN
    text = " ".join(value.strip().strip(".").split())
    if text.casefold() in _NULL_VALUES:
        return UNKNOWN
    return text[:limit]


def _clean_confidence(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if number > 1.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


def parse_vehicle_reply(reply: str | None) -> VehicleIdentity | None:
    """Parse strict-schema JSON, rejecting malformed output rather than guessing."""
    if not reply:
        return None
    try:
        payload = json.loads(reply)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    present = str(payload.get("vehicle_present") or "").strip().casefold()
    if present not in {"yes", "no", UNKNOWN}:
        return None
    values = {field: _clean_value(payload.get(field)) for field in _VEHICLE_FIELDS}
    confidences = {
        field: _clean_confidence(payload.get(f"{field}_confidence"))
        if values[field] != UNKNOWN and present != "no"
        else 0.0
        for field in _VEHICLE_FIELDS
    }
    if present == "no":
        values = {field: UNKNOWN for field in _VEHICLE_FIELDS}
    return VehicleIdentity(
        vehicle_present=present,
        **{
            key: value
            for field in _VEHICLE_FIELDS
            for key, value in (
                (field, values[field]),
                (f"{field}_confidence", confidences[field]),
            )
        },
    )


def _validate_image(image: bytes) -> None:
    if not isinstance(image, bytes) or not image:
        raise ValueError("vehicle identification requires non-empty image bytes")
    if len(image) > MAX_VEHICLE_IMAGE_BYTES:
        raise ValueError(f"vehicle image exceeds {MAX_VEHICLE_IMAGE_BYTES} bytes")


def _consensus(identities: Sequence[VehicleIdentity]) -> VehicleIdentity:
    if not identities:
        return VehicleIdentity()

    presence_counts = Counter(item.vehicle_present for item in identities)
    max_presence = max(presence_counts.values())
    presence_winners = sorted(value for value, count in presence_counts.items() if count == max_presence)
    present = presence_winners[0] if len(presence_winners) == 1 else UNKNOWN
    if present == "no":
        return VehicleIdentity(vehicle_present="no")
    fields: dict[str, str | float] = {"vehicle_present": present}

    for field in _VEHICLE_FIELDS:
        confidence_field = f"{field}_confidence"
        votes = Counter(
            getattr(item, field).casefold()
            for item in identities
            if getattr(item, field) != UNKNOWN
        )
        if not votes:
            fields[field] = UNKNOWN
            fields[confidence_field] = 0.0
            continue
        top_count = max(votes.values())
        winners = sorted(value for value, count in votes.items() if count == top_count)
        if len(winners) != 1:
            fields[field] = UNKNOWN
            fields[confidence_field] = 0.0
            continue
        winner = winners[0]
        agreeing = [item for item in identities if getattr(item, field).casefold() == winner]
        fields[field] = getattr(agreeing[0], field)
        fields[confidence_field] = (
            sum(getattr(item, confidence_field) for item in agreeing) / len(agreeing)
        ) * (len(agreeing) / len(identities))
    return VehicleIdentity(**fields)


@dataclass
class AzureFoundryVehicleIdentifier:
    """Vehicle identification using a Foundry vision chat-completions endpoint."""

    endpoint: str
    api_key: str
    deployment: str
    api_version: str = "2024-10-21"
    timeout_seconds: float = 20.0
    name: str = "azure-foundry-vehicle"

    @property
    def _url(self) -> str:
        base = self.endpoint.rstrip("/")
        return f"{base}/openai/deployments/{self.deployment}/chat/completions?api-version={self.api_version}"

    async def _identify_one(self, image: bytes, content_type: str) -> VehicleIdentity | None:
        _validate_image(image)
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError(f"unsupported vehicle image content type: {content_type}")
        encoded = base64.b64encode(image).decode("ascii")
        payload = {
            "messages": [
                {"role": "system", "content": VEHICLE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Identify the visible vehicle attributes in this camera crop."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{content_type};base64,{encoded}"},
                        },
                    ],
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "vehicle_identification",
                    "strict": True,
                    "schema": VEHICLE_JSON_SCHEMA,
                },
            },
            "max_completion_tokens": 300,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self._url,
                json=payload,
                headers={"api-key": self.api_key, "Content-Type": "application/json"},
            )
        response.raise_for_status()
        choices = response.json().get("choices") or []
        if not choices:
            return None
        return parse_vehicle_reply((choices[0].get("message") or {}).get("content"))

    async def identify_vehicle(
        self, image: bytes, content_type: str = "image/jpeg"
    ) -> VehicleIdentity | None:
        return await self._identify_one(image, content_type)

    async def identify_vehicle_frames(
        self, frames: Sequence[bytes | tuple[bytes, str]]
    ) -> VehicleIdentity | None:
        normalized: list[tuple[bytes, str]] = []
        for frame in frames:
            image, content_type = frame if isinstance(frame, tuple) else (frame, "image/jpeg")
            _validate_image(image)
            normalized.append((image, content_type))
        if not normalized:
            return None
        if len(normalized) > MAX_VEHICLE_FRAMES:
            raise ValueError(f"vehicle identification accepts at most {MAX_VEHICLE_FRAMES} frames")
        if sum(len(image) for image, _ in normalized) > MAX_VEHICLE_BATCH_BYTES:
            raise ValueError(f"vehicle frame batch exceeds {MAX_VEHICLE_BATCH_BYTES} bytes")
        results = []
        for image, content_type in normalized:
            identity = await self._identify_one(image, content_type)
            if identity is not None:
                results.append(identity)
        return _consensus(results) if results else None


class MockVehicleIdentifier:
    """Deterministic no-vision fallback; unknown is safer than a fabricated car."""

    name = "mock-vehicle"

    async def identify_vehicle(
        self, image: bytes, content_type: str = "image/jpeg"
    ) -> VehicleIdentity:
        return VehicleIdentity()

    async def identify_vehicle_frames(
        self, frames: Sequence[bytes | tuple[bytes, str]]
    ) -> VehicleIdentity:
        return VehicleIdentity()


@dataclass
class FallbackVehicleIdentifier:
    """Keep vision failures isolated and return deterministic unknown values."""

    primary: VehicleIdentifier
    fallback: VehicleIdentifier

    @property
    def name(self) -> str:
        return self.primary.name

    async def identify_vehicle(
        self, image: bytes, content_type: str = "image/jpeg"
    ) -> VehicleIdentity | None:
        try:
            return await self.primary.identify_vehicle(image, content_type)
        except Exception as exc:  # noqa: BLE001 - identification must not break event processing
            logger.warning("vehicle identification failed; returning unknown attributes: %s", exc)
            return await self.fallback.identify_vehicle(image, content_type)

    async def identify_vehicle_frames(
        self, frames: Sequence[bytes | tuple[bytes, str]]
    ) -> VehicleIdentity | None:
        try:
            return await self.primary.identify_vehicle_frames(frames)
        except Exception as exc:  # noqa: BLE001 - identification must not break event processing
            logger.warning("vehicle frame identification failed; returning unknown attributes: %s", exc)
            return await self.fallback.identify_vehicle_frames(frames)


def build_vehicle_identifier(settings) -> VehicleIdentifier:
    """Build Foundry identification when configured, otherwise deterministic mock."""
    fallback = MockVehicleIdentifier()
    if not (settings.foundry_endpoint and settings.foundry_api_key):
        return fallback
    primary = AzureFoundryVehicleIdentifier(
        endpoint=settings.foundry_endpoint,
        api_key=settings.foundry_api_key,
        deployment=settings.foundry_vision_deployment,
        api_version=settings.foundry_vision_api_version,
        timeout_seconds=settings.foundry_timeout_seconds,
    )
    return FallbackVehicleIdentifier(primary=primary, fallback=fallback)
