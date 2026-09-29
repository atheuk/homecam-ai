"""Closed-question vision checks for temporal scene events.

The local detector (RT-DETR on COCO) cannot see an envelope going into a
mailbox and has no class for a wheelie bin. The temporal engine in
:mod:`app.services.scene_state` therefore does the cheap part locally:
zones, frame-to-frame change and person/vehicle overlap. It asks the
Foundry vision deployment only when that local evidence already makes a
state change plausible, and it asks a narrow question.

The rules match :mod:`app.ai.animals`:

* The model sees labelled BEFORE / DURING / AFTER crops of one configured
  zone and must answer in a fixed JSON shape. Anything else is discarded and
  becomes ``unknown``. It never becomes a guess shown to the user.
* ``unknown`` is always an allowed answer. It is reported as such rather
  than rounded to yes or no.
* The prompts ask only about the mailbox or the bins. The model is told not
  to describe people, so nothing about who delivered the post is inferred
  here. Gender and ethnicity in particular are never requested.

Every call is wrapped (SPEC 43): a failure degrades this feature only.
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

MAILBOX_SYSTEM_PROMPT = (
    "You check home security camera crops of a mailbox for a delivery. You "
    "get up to three crops of the same mailbox: BEFORE a person reached it, "
    "DURING, and AFTER they left. Answer one closed question: did the person "
    "put an item (letter, post or parcel) INTO this mailbox or its parcel "
    "compartment? Walking past, looking, or only touching the mailbox is "
    '"no". Reply with JSON only, no prose and no code fences: {"deposited": '
    '"yes", "no" or "unknown"; "item": "letter", "parcel" or "unknown"; '
    '"confidence": a number from 0 to 1}. Use "unknown" when the crops do '
    "not show it. Do not describe the person."
)

BINS_SYSTEM_PROMPT = (
    "You check home security camera crops of an area at the kerb where "
    "wheelie bins (garbage/recycling containers) are put out for collection. "
    "You get a BEFORE crop and an AFTER crop of the same area; BEFORE may be "
    "missing. Count only wheelie bins or garbage containers, nothing else. "
    'Reply with JSON only, no prose and no code fences: {"bins_before": '
    'number of bins in BEFORE, or null if missing or you cannot tell; '
    '"bins_after": number of bins in AFTER, or null if you cannot tell; '
    '"emptied": true only if the crops show a bin being emptied or evidence '
    "of it (a collection vehicle or worker at the bins, a bin lifted, tipped "
    "or with its lid flipped open after collection), false if they show it "
    'was not, null if you cannot tell; "confidence": a number from 0 to 1}. '
    "Do not describe people."
)

DEPOSIT_ANSWERS = frozenset({"yes", "no", "unknown"})
ITEM_ANSWERS = frozenset({"letter", "parcel", "unknown"})
_ITEM_SYNONYMS = {"post": "letter", "mail": "letter", "envelope": "letter", "package": "parcel", "box": "parcel"}


@dataclass(frozen=True)
class MailboxVerdict:
    deposited: str = "unknown"
    item: str = "unknown"
    confidence: float = 0.0

    def as_dict(self) -> dict:
        return {"deposited": self.deposited, "item": self.item, "confidence": round(self.confidence, 4)}


@dataclass(frozen=True)
class BinVerdict:
    bins_before: int | None = None
    bins_after: int | None = None
    emptied: bool | None = None
    confidence: float = 0.0

    def as_dict(self) -> dict:
        return {
            "bins_before": self.bins_before,
            "bins_after": self.bins_after,
            "emptied": self.emptied,
            "confidence": round(self.confidence, 4),
        }


class TemporalVerifier(Protocol):
    name: str

    async def verify_mailbox(
        self, before: bytes | None, during: bytes, after: bytes | None
    ) -> MailboxVerdict | None: ...

    async def assess_bins(self, before: bytes | None, after: bytes) -> BinVerdict | None: ...


def _json_object(reply: str | None) -> dict | None:
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
    return payload if isinstance(payload, dict) else None


def _confidence(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _count(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 20 else None


def parse_mailbox_reply(reply: str | None) -> MailboxVerdict | None:
    payload = _json_object(reply)
    if payload is None:
        return None
    deposited = str(payload.get("deposited") or "").strip().casefold()
    if deposited not in DEPOSIT_ANSWERS:
        deposited = "unknown"
    item = str(payload.get("item") or "").strip().casefold()
    item = _ITEM_SYNONYMS.get(item, item)
    if item not in ITEM_ANSWERS or deposited != "yes":
        # An item is only meaningful for an actual deposit.
        item = "unknown"
    return MailboxVerdict(deposited=deposited, item=item, confidence=_confidence(payload.get("confidence")))


def parse_bins_reply(reply: str | None) -> BinVerdict | None:
    payload = _json_object(reply)
    if payload is None:
        return None
    emptied = payload.get("emptied")
    return BinVerdict(
        bins_before=_count(payload.get("bins_before")),
        bins_after=_count(payload.get("bins_after")),
        emptied=emptied if isinstance(emptied, bool) else None,
        confidence=_confidence(payload.get("confidence")),
    )


def _image_part(image: bytes) -> dict:
    encoded = base64.b64encode(image).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}


@dataclass
class AzureFoundryTemporalVerifier:
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

    async def _ask(self, system: str, labelled: list[tuple[str, bytes | None]]) -> str | None:
        content: list[dict] = []
        for label, image in labelled:
            if image is None:
                content.append({"type": "text", "text": f"{label}: not available."})
                continue
            content.append({"type": "text", "text": f"{label}:"})
            content.append(_image_part(image))
        payload = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "max_completion_tokens": 120,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self._url, json=payload, headers={"api-key": self.api_key, "Content-Type": "application/json"}
            )
        response.raise_for_status()
        choices = response.json().get("choices") or []
        if not choices:
            return None
        return (choices[0].get("message") or {}).get("content")

    async def verify_mailbox(
        self, before: bytes | None, during: bytes, after: bytes | None
    ) -> MailboxVerdict | None:
        reply = await self._ask(
            MAILBOX_SYSTEM_PROMPT, [("BEFORE", before), ("DURING", during), ("AFTER", after)]
        )
        return parse_mailbox_reply(reply)

    async def assess_bins(self, before: bytes | None, after: bytes) -> BinVerdict | None:
        reply = await self._ask(BINS_SYSTEM_PROMPT, [("BEFORE", before), ("AFTER", after)])
        return parse_bins_reply(reply)


_verifier: TemporalVerifier | None = None
_built = False


def build_temporal_verifier(settings) -> TemporalVerifier | None:
    if not settings.temporal_vision_enabled:
        return None
    if not (settings.foundry_endpoint and settings.foundry_api_key):
        logger.info("Foundry not configured; mailbox/bin checks run on local evidence only")
        return None
    return AzureFoundryTemporalVerifier(
        endpoint=settings.foundry_endpoint,
        api_key=settings.foundry_api_key,
        deployment=settings.foundry_vision_deployment,
        api_version=settings.foundry_vision_api_version,
        timeout_seconds=settings.foundry_timeout_seconds,
    )


def get_temporal_verifier() -> TemporalVerifier | None:
    global _verifier, _built
    if not _built:
        from ..config import settings

        _verifier = build_temporal_verifier(settings)
        _built = True
    return _verifier


def set_temporal_verifier(verifier: TemporalVerifier | None) -> None:
    """Test hook: install (or clear, with ``None``) the verifier."""
    global _verifier, _built
    _verifier = verifier
    _built = verifier is not None


def reset_temporal_verifier() -> None:
    global _verifier, _built
    _verifier = None
    _built = False
