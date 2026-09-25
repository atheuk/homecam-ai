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
import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

# Species HomeCam reports. ``other`` is a real answer, not a failure: the
# requirement is "a dog, a bird, or a cat, or something else".
ANIMAL_SPECIES: tuple[str, ...] = ("dog", "cat", "bird", "other")

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
    'animal is visible; "breed": the specific breed or kind if you can '
    "genuinely see it, otherwise null; \"confidence\": a number from 0 to 1 "
    'for how sure you are of the breed; "description": one short sentence '
    "describing the animal, under 20 words}. Never guess a breed you cannot "
    "actually see -- return null instead. Judge only what is visible."
)


@dataclass(frozen=True)
class AnimalIdentity:
    """What was seen, at the level of certainty it was actually seen at."""

    species: str
    breed: str | None = None
    confidence: float = 0.0
    description: str | None = None

    @property
    def kind(self) -> str:
        """Best available name for the animal, breed first."""
        return self.breed or self.species

    def as_dict(self) -> dict:
        return {
            "species": self.species,
            "breed": self.breed,
            "confidence": round(self.confidence, 4),
            "description": self.description,
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
    return AnimalIdentity(
        species=species,
        breed=breed,
        # A breed we chose not to report cannot carry breed confidence.
        confidence=_clean_confidence(payload.get("confidence")) if breed else 0.0,
        description=description[:300] if description else None,
    )


def describe_animal(identity: AnimalIdentity, camera_name: str, zone_name: str | None = None) -> str:
    """One-line event description, e.g. "A Border Collie (dog) was seen..."."""
    where = f" in the {zone_name}" if zone_name else ""
    if identity.breed:
        subject = f"A {identity.breed} ({identity.species})"
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
                        {"type": "text", "text": "Identify the animal in this security camera crop."},
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
