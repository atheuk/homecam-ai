"""Animal identification: which species, and which breed (SPEC section 13).

The local detector answers "there is an animal here, and roughly where".
That is deliberately all it answers: the bundled/opt-in box detectors are
COCO-class models, so they can separate a dog from a cat from a bird but
they have no notion of *breed*, and for anything outside their class list
they can only say ``animal``.

Breed is therefore asked of the same Azure AI Foundry vision deployment
already used to caption person crops. Two rules make that safe to show to a
user:

* the model is required to answer in a fixed JSON shape, so a chatty reply
  is discarded rather than displayed as if it were a classification;
* it must return ``null`` for a breed it cannot actually see. "Probably a
  Labrador" on a blurry night frame is worse than no answer, because the
  user has no way to tell a guess from an observation.

It may also answer ``species: "none"``, which is used exactly like
:data:`app.ai.vision.NO_PERSON_CAPTION` — a second opinion that catches a
false positive from the box detector.

Failure policy matches the rest of the pipeline (SPEC 43): every call is
wrapped, and a failure degrades this feature only.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

# Species HomeCam reports. ``other`` is a real answer, not a failure: the
# requirement is "a dog, a bird, or a cat, or something else".
ANIMAL_SPECIES: tuple[str, ...] = ("dog", "cat", "bird", "other")
TAXONOMIC_GROUPS: tuple[str, ...] = ("bird", "mammal", "reptile", "amphibian", "insect")

# The model's way of saying the crop does not actually contain an animal.
NO_ANIMAL = "none"

# Everyday words for the species we track, so a model that answers "puppy"
# or "kitten" is still understood instead of being demoted to ``other``.
_SPECIES_SYNONYMS: dict[str, str] = {
    "dog": "dog",
    "puppy": "dog",
    "canine": "dog",
    "cat": "cat",
    "kitten": "cat",
    "feline": "cat",
    "bird": "bird",
    "fowl": "bird",
    "other": "other",
    "unknown": "other",
    "animal": "other",
}

# Values that mean "I could not tell", which must become a null breed
# rather than being shown to the user as a classification.
_NULL_ANSWERS = frozenset({"", "unknown", "unsure", "none", "null", "n/a", "na", "not sure", "undetermined"})

ANIMAL_SYSTEM_PROMPT = (
    "You identify animals in still frames from a home security camera. "
    "Reply with JSON only, no prose and no code fences, using exactly these "
    'keys: {"species": one of "dog", "cat", "bird", "other", or "none" if no '
    'animal is visible; "breed": the specific breed or kind if you can genuinely '
    'see it, otherwise null; "common_name": the most specific common name; '
    '"scientific_name": the binomial; "genus": a genus; "family": a family; '
    '"taxonomic_group": one of "bird", "mammal", "reptile", "amphibian", '
    '"insect", or null; "confidence": a number from 0 to 1 for the most '
    'specific name supplied; "description": one short sentence describing the '
    "animal, under 20 words}. Return null for names you cannot see. "
    "Judge only what is visible."
)


@dataclass(frozen=True)
class AnimalIdentity:
    """What was seen, at the level of certainty it was actually seen at."""

    species: str
    breed: str | None = None
    confidence: float = 0.0
    description: str | None = None
    common_name: str | None = None
    scientific_name: str | None = None
    taxonomic_group: str | None = None
    genus: str | None = None
    family: str | None = None

    @property
    def kind(self) -> str:
        """Best available fine-grained name, falling back to the group."""
        return (
            self.common_name
            or self.breed
            or self.scientific_name
            or self.genus
            or self.family
            or self.taxonomic_group
            or self.species
        )

    def as_dict(self) -> dict:
        return {
            "species": self.species,
            "breed": self.breed,
            "confidence": round(self.confidence, 4),
            "description": self.description,
            "common_name": self.common_name,
            "scientific_name": self.scientific_name,
            "taxonomic_group": self.taxonomic_group,
            "genus": self.genus,
            "family": self.family,
        }


class AnimalIdentifier(Protocol):
    name: str

    async def identify_animal(self, image: bytes, content_type: str) -> AnimalIdentity | None: ...


def normalize_species(value: object) -> str | None:
    """Map a model's species word onto :data:`ANIMAL_SPECIES`.

    Returns ``None`` when the model reported no animal at all, so callers
    can treat that as a false-positive signal rather than as ``other``.
    """
    text = str(value or "").strip().casefold()
    if not text or text == NO_ANIMAL:
        return None
    return _SPECIES_SYNONYMS.get(text, "other")


def _clean_breed(value: object) -> str | None:
    text = str(value or "").strip().strip(".")
    if text.casefold() in _NULL_ANSWERS:
        return None
    return text[:80]


def _clean_confidence(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _clean_name(value: object) -> str | None:
    text = str(value or "").strip().strip(".")
    if text.casefold() in _NULL_ANSWERS:
        return None
    return text[:120] or None


def _clean_group(value: object) -> str | None:
    group = str(value or "").strip().casefold()
    return group if group in TAXONOMIC_GROUPS else None


def parse_animal_reply(reply: str | None) -> AnimalIdentity | None:
    """Turn a model reply into an :class:`AnimalIdentity`, or ``None``.

    Tolerates the two things vision deployments do even when told not to:
    wrapping JSON in a ``` fence, and adding a sentence around it.
    """
    if not reply:
        return None
    text = reply.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    if not text.startswith("{"):
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        text = match.group(0)
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    species = normalize_species(payload.get("species"))
    if species is None:
        return None
    description = str(payload.get("description") or "").strip() or None
    breed = _clean_breed(payload.get("breed"))
    common_name = _clean_name(payload.get("common_name"))
    scientific_name = _clean_name(payload.get("scientific_name"))
    genus = _clean_name(payload.get("genus"))
    family = _clean_name(payload.get("family"))
    taxonomic_group = _clean_group(payload.get("taxonomic_group"))
    confidence = _clean_confidence(payload.get("confidence"))
    # Confidence must describe a displayed name, never an unsupported guess.
    if not (breed or common_name or scientific_name or genus or family or taxonomic_group):
        confidence = 0.0
    return AnimalIdentity(
        species=species,
        breed=breed,
        confidence=confidence,
        description=description[:300] if description else None,
        common_name=common_name,
        scientific_name=scientific_name,
        taxonomic_group=taxonomic_group,
        genus=genus,
        family=family,
    )


def prepare_animal_crop(image: bytes, bbox=None, padding: float = 0.08, min_pixels: int = 256) -> bytes:
    """Make a tight padded crop and upscale tiny subjects for vision."""
    if bbox is None:
        return image
    try:
        from PIL import Image
        from .imaging import configure_pillow

        configure_pillow()
        with Image.open(io.BytesIO(image)) as frame:
            width, height = frame.size
            if isinstance(bbox, (tuple, list)):
                left, top, right, bottom = bbox
            elif isinstance(bbox, dict):
                left, top, right, bottom = (bbox[k] for k in ("x1", "y1", "x2", "y2"))
            else:
                left, top, right, bottom = (getattr(bbox, k) for k in ("x1", "y1", "x2", "y2"))
            pad_x, pad_y = (right - left) * padding, (bottom - top) * padding
            left, top = max(0, int((left - pad_x) * width)), max(0, int((top - pad_y) * height))
            right, bottom = min(width, int((right + pad_x) * width)), min(height, int((bottom + pad_y) * height))
            if right <= left or bottom <= top:
                return image
            crop = frame.crop((left, top, right, bottom)).convert("RGB")
            if min(crop.size) < min_pixels:
                scale = min_pixels / min(crop.size)
                crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.LANCZOS)
            output = io.BytesIO()
            crop.save(output, format="JPEG", quality=90)
            return output.getvalue()
    except (ImportError, OSError, TypeError, ValueError, KeyError):
        return image


def vote_animal_identities(identities: list[AnimalIdentity | None]) -> AnimalIdentity | None:
    """Return the strongest result from the largest name consensus."""
    valid = [identity for identity in identities if identity is not None]
    if not valid:
        return None
    groups: dict[tuple[str, str], list[AnimalIdentity]] = {}
    for identity in valid:
        name = (
            identity.common_name
            or identity.breed
            or identity.scientific_name
            or identity.genus
            or identity.family
            or identity.taxonomic_group
            or ""
        ).casefold()
        groups.setdefault((identity.species, name), []).append(identity)
    candidates = max(groups.values(), key=lambda group: (len(group), max(item.confidence for item in group)))
    return max(candidates, key=lambda item: item.confidence)


async def identify_animal_frames(identifier: AnimalIdentifier, frames: list[bytes], content_type: str = "image/jpeg") -> AnimalIdentity | None:
    """Identify multiple frames; one weak result cannot replace a consensus."""
    results: list[AnimalIdentity | None] = []
    for frame in frames:
        try:
            results.append(await identifier.identify_animal(frame, content_type))
        except Exception as exc:  # noqa: BLE001 - frame failures are non-fatal
            logger.warning("animal frame identification failed: %s", exc)
    return vote_animal_identities(results)


def describe_animal(identity: AnimalIdentity, camera_name: str, zone_name: str | None = None) -> str:
    """One-line event description, e.g. "A Border Collie (dog) was seen..."."""
    where = f" in the {zone_name}" if zone_name else ""
    if identity.kind != identity.species:
        subject = f"A {identity.kind} ({identity.species})"
    elif identity.species == "other":
        subject = "An animal"
    else:
        subject = f"A {identity.species}"
    return f"{subject} was seen{where} at {camera_name}."


@dataclass
class AzureFoundryAnimalIdentifier:
    """Species + breed identification via a Foundry vision deployment."""

    endpoint: str
    api_key: str
    deployment: str
    api_version: str = "2024-10-21"
    timeout_seconds: float = 20.0
    region_hint: str | None = None
    name: str = "azure-foundry-vision"

    @property
    def _url(self) -> str:
        base = self.endpoint.rstrip("/")
        return f"{base}/openai/deployments/{self.deployment}/chat/completions?api-version={self.api_version}"

    async def identify_animal(
        self, image: bytes, content_type: str = "image/jpeg"
    ) -> AnimalIdentity | None:
        encoded = base64.b64encode(image).decode("ascii")
        payload = {
            "messages": [
                {"role": "system", "content": ANIMAL_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Identify the animal in this security camera crop."
                                + (f" The camera region is {self.region_hint}; use it only as a weak hint." if self.region_hint else "")
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{content_type};base64,{encoded}"},
                        },
                    ],
                },
            ],
            "max_completion_tokens": 200,
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
        return parse_animal_reply((choices[0].get("message") or {}).get("content"))


_identifier: AnimalIdentifier | None = None


def build_animal_identifier(settings) -> AnimalIdentifier | None:
    if not settings.animal_identification_enabled:
        return None
    if not (settings.foundry_endpoint and settings.foundry_api_key):
        logger.info("Foundry not configured; animal events report species only, no breed")
        return None
    return AzureFoundryAnimalIdentifier(
        endpoint=settings.foundry_endpoint,
        api_key=settings.foundry_api_key,
        deployment=settings.foundry_vision_deployment,
        api_version=settings.foundry_vision_api_version,
        timeout_seconds=settings.foundry_timeout_seconds,
        # HOME_REGION is intentionally read here until the settings model is
        # extended; deployment-specific config must not be hard-coded.
        region_hint=getattr(settings, "home_region", None) or os.getenv("HOME_REGION"),
    )


def get_animal_identifier() -> AnimalIdentifier | None:
    global _identifier
    if _identifier is None:
        from ..config import settings

        _identifier = build_animal_identifier(settings)
    return _identifier


def reset_animal_identifier() -> None:
    """Test hook: drop the cached identifier singleton."""
    global _identifier
    _identifier = None
