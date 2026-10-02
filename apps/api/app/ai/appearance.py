"""Structured description of the person in an event photo.

A cropped figure at the end of a driveway is not, on its own, useful to
someone scanning an event list. The captioner in :mod:`app.ai.vision`
already turns it into one sentence; this module turns it into *fields* the
event list can filter, badge and reason about: roughly how old they appear,
what they are wearing, what they are carrying, whether their face is
visible at all.

It also answers a question nothing else in the pipeline was asking: **is
there actually a person here?** The local detector is a HOG/SVM or YOLO
backend looking at pixels, and it fires on fence posts, bin bags and
shadows. A vision model looking at the same crop is a far better judge, so
``person_present`` becomes a second opinion that the pipeline uses to mark
a detection border verified or unverified rather than presenting every
box as fact.

What it does record (all "unknown"/null when not plainly visible)
-----------------------------------------------------------------
* hair: length, colour and style - only when the hair is visible, never
  guessed under a hood or hat;
* upper and lower clothing colour/type, headwear, footwear, accessories,
  carried items and build;
* a **broad apparent age range** (child / teenager / adult / older adult,
  each with an explicit approximate span) with a confidence that is capped
  at "medium" - it is an impression from a security crop, never an age;
* the visible action (e.g. "ringing the doorbell") and movement direction.

Attributes deliberately NOT inferred
------------------------------------
**Ethnicity/race, skin colour, gender, religion, nationality, health,
emotion, intent, criminality and identity are not classified, and must not
be added.**

* They are protected or sensitive attributes. A home security system that
  sorts callers by them and pairs that with a trust flag is a profiling
  tool, and the error rate of appearance-based classification falls
  unevenly on the people most harmed by being wrongly flagged. Skin colour
  is excluded too: it is a direct proxy for ethnicity.
* The Microsoft Enterprise AI Services Code of Conduct this deployment runs
  under prohibits inferring gender, race, nationality, religion and a
  specific age from images, while permitting age *ranges* and hair colour;
  the fields above stay on the permitted side of that line. A model asked
  anyway will often refuse, producing unparseable output; building on that
  is unreliable as well as out of policy.
* They do not serve the actual goal. "What did they look like?" is answered
  far more specifically by clothing, hair and carried items, which is what
  a witness statement would record. Who someone *is* stays a manual owner
  decision; this module never names or matches anyone.

Every free-text answer is screened: a field mentioning a prohibited
attribute is dropped rather than stored, whatever the model was told.
Failure policy (SPEC 43): every call is wrapped by the caller and a failure
degrades to no appearance data rather than losing the event.
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

# Coarse, explicitly-approximate bands. Deliberately not a numeric age:
# a number implies a precision that looking at a low-resolution security
# crop cannot support, and invites the user to treat a guess as a fact.
AGE_BANDS: tuple[str, ...] = ("child", "teenager", "adult", "older adult")

_AGE_SYNONYMS: dict[str, str] = {
    "baby": "child",
    "infant": "child",
    "toddler": "child",
    "kid": "child",
    "young child": "child",
    "teen": "teenager",
    "teenaged": "teenager",
    "adolescent": "teenager",
    "youth": "teenager",
    "young adult": "adult",
    "middle-aged": "adult",
    "middle aged": "adult",
    "grown-up": "adult",
    "elderly": "older adult",
    "senior": "older adult",
    "old": "older adult",
    "older": "older adult",
    "pensioner": "older adult",
}

# Values that mean "I cannot tell", which must become ``None`` rather than
# being stored as though they were an observation.
_NULL_VALUES = frozenset(
    {
        "",
        "-",
        "n/a",
        "na",
        "null",
        "none",
        "unknown",
        "unclear",
        "not visible",
        "not sure",
        "cannot tell",
        "can't tell",
        "unidentifiable",
        "indeterminate",
        "nothing",
        "no",
    }
)

# Explicitly approximate span shown next to each band.
AGE_RANGES: dict[str, str] = {
    "child": "approx. under 13",
    "teenager": "approx. 13-19",
    "adult": "approx. 20-64",
    "older adult": "approx. 65+",
}
# An apparent age from a security crop is an impression, never "high".
MAX_AGE_CONFIDENCE = 0.6

DIRECTIONS: tuple[str, ...] = (
    "toward camera", "away from camera", "left to right", "right to left", "stationary",
)

# Any field containing one of these is dropped rather than stored: the model
# was told not to say them, but instructions are not a guarantee.
_PROHIBITED = re.compile(
    r"\b(ethnic\w*|race|racial\w*|skin|complexion|caucasian|asian|african|hispanic|latin[oax]|"
    r"arab\w*|middle[- ]eastern|european|indian|nationality|"
    r"male|female|man|men|woman|women|boy|girl|gender\w*|masculine|feminine|"
    r"religio\w*|muslim|christian|jewish|hindu|sikh|"
    r"angry|nervous|scared|afraid|anxious|drunk|intoxicated|happy|sad|upset|emotion\w*|"
    r"disabled|disability|sick|pregnant|"
    r"suspicious|criminal|thief|burglar|dangerous|threatening|shady|sketchy|"
    r"\d+\s*(?:years?|yrs?)(?:\s*old)?)\b",
    re.I,
)

APPEARANCE_SYSTEM_PROMPT = (
    "You are describing a still frame from a homeowner's own security camera "
    "so they can recognise a caller later, like a careful witness statement. "
    "Report only what is plainly visible; use null for anything hidden, "
    "occluded, too small or unclear.\n"
    "Reply with JSON only, no prose and no code fences, with exactly these "
    "keys:\n"
    '  "person_present": true or false - is a person clearly visible?\n'
    '  "apparent_age_band": one of "child", "teenager", "adult", "older adult", '
    "or null if unclear - a broad impression, never a number.\n"
    '  "age_confidence": 0 to 1, how clear that impression is.\n'
    '  "build": short phrase for apparent height/build, or null.\n'
    '  "hair": {"length": e.g. "short"/"shoulder-length"/"long"/"shaved", '
    '"colour": e.g. "dark brown"/"blond"/"grey", "style": e.g. "ponytail"/"curly"/"braided"} '
    "- each null when the hair is covered or not visible.\n"
    '  "upper_clothing": {"colour": ..., "type": e.g. "hooded jacket"} or null.\n'
    '  "lower_clothing": {"colour": ..., "type": e.g. "jeans"} or null.\n'
    '  "headwear": e.g. "black beanie", or null.\n'
    '  "footwear": e.g. "white trainers", or null.\n'
    '  "accessories": list of visible items such as "glasses", "backpack", '
    '"face mask"; empty list if none.\n'
    '  "carrying": anything held or carried, or null.\n'
    '  "action": what they are visibly doing, e.g. "ringing the doorbell", '
    '"walking past", "delivering a parcel", or null.\n'
    '  "direction": one of "toward camera", "away from camera", "left to '
    'right", "right to left", "stationary", or null.\n'
    '  "face_visible": true or false - is the face clearly enough shown to '
    "recognise them?\n"
    '  "description": one plain sentence, under 20 words, about clothing, '
    "hair and action only.\n"
    "Rules: never state or guess ethnicity, race, skin colour, nationality, "
    "gender, religion, health, emotion, intent, a specific age or anyone's "
    "identity, and never include those anywhere. Never call anyone "
    "suspicious. Use null rather than guessing any field. If no person is "
    'visible set "person_present" to false and every other field to null.'
)


@dataclass(frozen=True)
class Appearance:
    """Observable, non-protected description of a detected person."""

    person_present: bool
    age_band: str | None = None
    age_confidence: float | None = None
    build: str | None = None
    clothing: str | None = None
    carrying: str | None = None
    face_visible: bool = False
    description: str | None = None
    hair: dict | None = None
    upper_clothing: dict | None = None
    lower_clothing: dict | None = None
    headwear: str | None = None
    footwear: str | None = None
    accessories: tuple[str, ...] = ()
    action: str | None = None
    direction: str | None = None

    @property
    def age_range(self) -> str | None:
        return AGE_RANGES.get(self.age_band) if self.age_band else None

    @property
    def age_certainty(self) -> str | None:
        if self.age_band is None or self.age_confidence is None:
            return None
        return "medium" if self.age_confidence >= 0.45 else "low"

    def as_dict(self) -> dict:
        return {
            "person_present": self.person_present,
            "age_band": self.age_band,
            "age_range": self.age_range,
            "age_confidence": round(self.age_confidence, 3) if self.age_confidence is not None else None,
            "age_certainty": self.age_certainty,
            "build": self.build,
            "clothing": self.clothing,
            "carrying": self.carrying,
            "face_visible": self.face_visible,
            "description": self.description,
            "hair": self.hair,
            "upper_clothing": self.upper_clothing,
            "lower_clothing": self.lower_clothing,
            "headwear": self.headwear,
            "footwear": self.footwear,
            "accessories": list(self.accessories),
            "action": self.action,
            "direction": self.direction,
        }

    @property
    def summary(self) -> str:
        """Short human label, e.g. "Adult (apparent) · dark jacket · carrying a parcel"."""
        parts = [
            f"{self.age_band.capitalize()} (apparent)" if self.age_band else None,
            self.clothing,
            f"carrying {self.carrying}" if self.carrying else None,
        ]
        return " · ".join(part for part in parts if part)

class AppearanceAnalyzer(Protocol):
    name: str

    async def describe_person(self, image: bytes, content_type: str) -> Appearance | None: ...


def _clean_text(value: object, limit: int = 120) -> str | None:
    """Normalize a free-text field, mapping refusals/placeholders and any
    prohibited-attribute wording to None."""
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    text = str(value).strip().strip(".").strip()
    if not text or text.casefold() in _NULL_VALUES:
        return None
    if _PROHIBITED.search(text):
        logger.info("appearance field dropped: prohibited attribute wording")
        return None
    return text[:limit]


def _safe_description(value: object) -> str | None:
    text = _clean_text(value, limit=300)
    if text is None:
        return None
    # Ages and age words belong in the explicit, hedged age field only.
    if re.search(r"\b(child|kid|teenager|teen|adult|elderly|senior|young|old|aged)\b", text, re.I):
        return None
    return text


def _clean_parts(value: object, keys: tuple[str, ...]) -> dict | None:
    if not isinstance(value, dict):
        return None
    cleaned = {key: _clean_text(value.get(key), limit=40) for key in keys}
    return cleaned if any(cleaned.values()) else None


def _clean_list(value: object, limit: int = 6) -> tuple[str, ...]:
    if isinstance(value, str):
        value = re.split(r",|;", value)
    if not isinstance(value, list):
        return ()
    items: list[str] = []
    for item in value:
        text = _clean_text(item, limit=40)
        if text and text.casefold() not in {existing.casefold() for existing in items}:
            items.append(text)
    return tuple(items[:limit])


def _clean_direction(value: object) -> str | None:
    text = _clean_text(value, limit=40)
    if text is None:
        return None
    lowered = text.casefold().replace("-", " ")
    for direction in DIRECTIONS:
        if direction in lowered:
            return direction
    if "toward" in lowered or "approach" in lowered:
        return "toward camera"
    if "away" in lowered or "leaving" in lowered:
        return "away from camera"
    return None


def _combined_clothing(upper: dict | None, lower: dict | None) -> str | None:
    pieces = [
        " ".join(part for part in (garment.get("colour"), garment.get("type")) if part)
        for garment in (upper, lower) if garment
    ]
    text = " and ".join(piece for piece in pieces if piece)
    return text or None

def normalize_age_band(value: object) -> str | None:
    """Map a model's age wording onto one of :data:`AGE_BANDS`."""
    text = _clean_text(value, limit=40)
    if text is None:
        return None
    lowered = text.casefold()
    if lowered in AGE_BANDS:
        return lowered
    if lowered in _AGE_SYNONYMS:
        return _AGE_SYNONYMS[lowered]
    # "an adult man" / "appears elderly" - find a band mentioned inside.
    for band in AGE_BANDS:
        if band in lowered:
            return band
    for synonym, band in _AGE_SYNONYMS.items():
        if synonym in lowered:
            return band
    return None


def _clean_confidence(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if number > 1.0:  # a model answering in percent
        number /= 100.0
    return max(0.0, min(1.0, number))


def _coerce_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().casefold() in {"true", "yes", "1"}


def _extract_json(reply: str) -> dict | None:
    """Pull a JSON object out of a reply that may be fenced or chatty."""
    text = reply.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text.lstrip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def parse_appearance_reply(reply: str | None) -> Appearance | None:
    """Turn a model reply into an :class:`Appearance`, or ``None``.

    Unparseable output yields ``None`` rather than a default-shaped record,
    so the pipeline can tell "the model said nothing is there" apart from
    "the model could not be understood" - only the first is evidence.
    """
    if not reply:
        return None
    parsed = _extract_json(reply)
    if parsed is None:
        logger.debug("appearance reply was not JSON: %s", reply[:200])
        return None

    present = _coerce_bool(parsed.get("person_present"), default=True)
    if not present:
        return Appearance(person_present=False)

    band = normalize_age_band(parsed.get("apparent_age_band") or parsed.get("age_band"))
    confidence = None
    if band is not None:
        confidence = min(MAX_AGE_CONFIDENCE, _clean_confidence(parsed.get("age_confidence")))
        if confidence <= 0.0:
            band, confidence = None, None
    upper = _clean_parts(parsed.get("upper_clothing"), ("colour", "type"))
    lower = _clean_parts(parsed.get("lower_clothing"), ("colour", "type"))
    return Appearance(
        person_present=True,
        age_band=band,
        age_confidence=confidence,
        build=_clean_text(parsed.get("build"), limit=80),
        clothing=_clean_text(parsed.get("clothing"), limit=160) or _combined_clothing(upper, lower),
        carrying=_clean_text(parsed.get("carrying"), limit=120),
        face_visible=_coerce_bool(parsed.get("face_visible")),
        description=_safe_description(parsed.get("description")),
        hair=_clean_parts(parsed.get("hair"), ("length", "colour", "style")),
        upper_clothing=upper,
        lower_clothing=lower,
        headwear=_clean_text(parsed.get("headwear"), limit=60),
        footwear=_clean_text(parsed.get("footwear"), limit=60),
        accessories=_clean_list(parsed.get("accessories")),
        action=_clean_text(parsed.get("action"), limit=80),
        direction=_clean_direction(parsed.get("direction")),
    )

@dataclass
class AzureFoundryAppearanceAnalyzer:
    """Structured appearance analysis via a Foundry vision chat deployment.

    Shares the account and deployment used for captioning and animal
    identification; no extra Azure resource is required.
    """

    endpoint: str
    api_key: str
    deployment: str
    api_version: str = "2024-10-21"
    timeout_seconds: float = 20.0
    name: str = "azure-foundry-appearance"

    @property
    def _url(self) -> str:
        base = self.endpoint.rstrip("/")
        return (
            f"{base}/openai/deployments/{self.deployment}"
            f"/chat/completions?api-version={self.api_version}"
        )

    async def describe_person(
        self, image: bytes, content_type: str = "image/jpeg"
    ) -> Appearance | None:
        encoded = base64.b64encode(image).decode("ascii")
        payload = {
            "messages": [
                {"role": "system", "content": APPEARANCE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this security camera crop as JSON."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{content_type};base64,{encoded}"},
                        },
                    ],
                },
            ],
            "max_completion_tokens": 500,
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
        return parse_appearance_reply((choices[0].get("message") or {}).get("content"))


_analyzer: AppearanceAnalyzer | None = None


def build_appearance_analyzer(settings) -> AppearanceAnalyzer | None:
    if not settings.appearance_analysis_enabled:
        return None
    if not (settings.foundry_endpoint and settings.foundry_api_key):
        logger.info("Foundry not configured; appearance analysis is unavailable")
        return None
    return AzureFoundryAppearanceAnalyzer(
        endpoint=settings.foundry_endpoint,
        api_key=settings.foundry_api_key,
        deployment=settings.foundry_vision_deployment,
        api_version=settings.foundry_vision_api_version,
        timeout_seconds=settings.foundry_timeout_seconds,
    )


def get_appearance_analyzer() -> AppearanceAnalyzer | None:
    global _analyzer
    if _analyzer is None:
        from ..config import settings

        _analyzer = build_appearance_analyzer(settings)
    return _analyzer


def reset_appearance_analyzer() -> None:
    """Test hook: drop the cached analyzer singleton."""
    global _analyzer
    _analyzer = None
