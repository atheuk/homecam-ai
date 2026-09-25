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

Attributes deliberately NOT inferred
------------------------------------
**Ethnicity/race and gender are not classified, and must not be added.**

* They are protected attributes. A home security system that sorts callers
  by race and pairs that with a trust flag is a profiling tool, and the
  false-positive rate of appearance-based classification falls unevenly on
  the people most harmed by being wrongly flagged.
* Microsoft retired exactly these inferences (gender, age, race) from Azure
  Face in 2022 under its Responsible AI Standard, and the Azure AI Services
  terms this deployment runs under prohibit using the service to infer
  them. A model asked anyway will often refuse, producing unparseable
  output; building on that is unreliable as well as out of policy.
* They do not serve the actual goal. "Was this the same caller as
  Tuesday?" is answered by the embedding matcher in
  :mod:`app.services.persons`, and "what did they look like?" is answered
  far more specifically by clothing and carried items, which is what a
  witness statement would record.

Apparent age band *is* captured, coarsely and explicitly as an estimate,
because "a child is at the front door" and "an adult is at the front door"
are genuinely different events for a homeowner to act on. It is reported
with its own confidence and is always allowed to be ``None``.

Failure policy (SPEC 43): every call is wrapped by the caller and a failure
degrades to no appearance data rather than losing the event.
"""
from __future__ import annotations

import base64
import json
import logging
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

APPEARANCE_SYSTEM_PROMPT = (
    "You are describing a still frame from a homeowner's own security camera "
    "so they can recognise a caller later. Report only what is plainly "
    "visible.\n"
    "Reply with JSON only, no prose and no code fences, with exactly these "
    "keys:\n"
    '  "person_present": true or false - is a person clearly visible?\n'
    '  "age_band": one of "child", "teenager", "adult", "older adult", or '
    "null if you cannot tell from the image.\n"
    '  "age_confidence": 0.0-1.0, how sure you are of the age band.\n'
    '  "build": short phrase for apparent height/build, or null.\n'
    '  "clothing": colours and garments visible, or null.\n'
    '  "carrying": anything held or carried, or null.\n'
    '  "face_visible": true or false - is the face clearly enough shown to '
    "recognise them?\n"
    '  "description": one plain sentence, under 20 words, for the event '
    "list.\n"
    "Rules: never state or guess the person's ethnicity, race, nationality, "
    "gender or identity, and never include those in the description. Never "
    "guess intent. Use null rather than guessing any field. If no person is "
    'visible set "person_present" to false and every other field to null.'
)


@dataclass(frozen=True)
class Appearance:
    """Observable, non-protected description of a detected person."""

    person_present: bool
    age_band: str | None = None
    age_confidence: float = 0.0
    build: str | None = None
    clothing: str | None = None
    carrying: str | None = None
    face_visible: bool = False
    description: str | None = None

    def as_dict(self) -> dict:
        return {
            "person_present": self.person_present,
            "age_band": self.age_band,
            "age_confidence": round(self.age_confidence, 3),
            "build": self.build,
            "clothing": self.clothing,
            "carrying": self.carrying,
            "face_visible": self.face_visible,
            "description": self.description,
        }

    @property
    def summary(self) -> str:
        """Short human label, e.g. "Adult · dark jacket · carrying a parcel"."""
        parts = [
            self.age_band.capitalize() if self.age_band else None,
            self.clothing,
            f"carrying {self.carrying}" if self.carrying else None,
        ]
        return " · ".join(part for part in parts if part)


class AppearanceAnalyzer(Protocol):
    name: str

    async def describe_person(self, image: bytes, content_type: str) -> Appearance | None: ...


def _clean_text(value: object, limit: int = 120) -> str | None:
    """Normalize a free-text field, mapping refusals/placeholders to None."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().strip(".").strip()
    if not text or text.casefold() in _NULL_VALUES:
        return None
    return text[:limit]


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

    age_band = normalize_age_band(parsed.get("age_band"))
    # Confidence describes the age band, so it is meaningless without one.
    age_confidence = _clean_confidence(parsed.get("age_confidence")) if age_band else None
    return Appearance(
        person_present=True,
        age_band=age_band,
        age_confidence=age_confidence,
        build=_clean_text(parsed.get("build"), limit=80),
        clothing=_clean_text(parsed.get("clothing"), limit=160),
        carrying=_clean_text(parsed.get("carrying"), limit=120),
        face_visible=_coerce_bool(parsed.get("face_visible")),
        description=_clean_text(parsed.get("description"), limit=300),
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
