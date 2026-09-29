"""Closed, grounded questions about a candidate scene change (Foundry vision).

The local detector cannot see envelopes, and COCO has no wheelie-bin class,
so a mailbox delivery or a bin being put out is often invisible to it. The
Foundry vision deployment can look, but an open "what happened?" invites a
story. This module only ever asks *closed* questions about a specific,
already-localized candidate:

* mailbox: here is the mailbox before, during and after one person's visit.
  Was an item deposited?  ``yes`` / ``no`` / ``unknown``.
* bin: here is the bin region before and now. Is a wheeled bin present in
  each?  Was it tipped/emptied/moved?  ``yes`` / ``no`` / ``unknown``.

Answers outside the allowed vocabulary become ``unknown``; an unparseable
reply is ``None`` (no opinion), never a default "yes". It is called only on
candidate sequences the local state machines produced, never per frame.
Nothing here describes the person: no identity, gender or ethnicity.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from .appearance import _clean_confidence, _extract_json

logger = logging.getLogger(__name__)

ANSWERS = ("yes", "no", "unknown")
ITEM_TYPES = ("parcel", "mail", "unknown")

MAILBOX_PROMPT = (
    "You check a home security camera for mailbox deliveries. You get crops of "
    "ONE mailbox region: BEFORE a person's visit, DURING it, and AFTER the person "
    "left. Answer only from what is visible in these images. Do not describe the "
    "person, never guess identity, gender, age or ethnicity. A person walking past, "
    "or carrying a parcel past without leaving it, is NOT a deposit. Reply with JSON "
    'only: {"person_interacted": "yes|no|unknown", "item_deposited": '
    '"yes|no|unknown", "item_type": "parcel|mail|unknown", "confidence": 0.0-1.0, '
    '"evidence": "<=20 words about the mailbox/item only"}. Use "unknown" whenever '
    "the images do not show it clearly."
)

BIN_PROMPT = (
    "You check a home security camera for household waste bins (wheelie bins / "
    "trash or recycling containers). You get crops of ONE fixed curb/bin region: "
    "BEFORE and NOW (and optionally DURING an interaction). Answer only from what is "
    "visible. Reply with JSON only: {\"bin_present_before\": \"yes|no|unknown\", "
    '"bin_present_now": "yes|no|unknown", "bin_moved_or_tipped": "yes|no|unknown", '
    '"collection_vehicle_visible": "yes|no|unknown", "confidence": 0.0-1.0, '
    '"evidence": "<=20 words about the bin only"}. Use "unknown" whenever the '
    "images do not show it clearly. Never describe people."
)


def _answer(value: object, allowed: tuple[str, ...] = ANSWERS) -> str:
    text = str(value or "").strip().casefold()
    if text in {"true"}:
        text = "yes"
    if text in {"false"}:
        text = "no"
    return text if text in allowed else "unknown"


def _evidence(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:160]
    return text or None


def parse_mailbox_reply(reply: str | None) -> dict | None:
    parsed = _extract_json(reply) if reply else None
    if parsed is None:
        return None
    return {
        "person_interacted": _answer(parsed.get("person_interacted")),
        "item_deposited": _answer(parsed.get("item_deposited")),
        "item_type": _answer(parsed.get("item_type"), ITEM_TYPES),
        "confidence": _clean_confidence(parsed.get("confidence")),
        "evidence": _evidence(parsed.get("evidence")),
    }


def parse_bin_reply(reply: str | None) -> dict | None:
    parsed = _extract_json(reply) if reply else None
    if parsed is None:
        return None
    return {
        "bin_present_before": _answer(parsed.get("bin_present_before")),
        "bin_present_now": _answer(parsed.get("bin_present_now")),
        "bin_moved_or_tipped": _answer(parsed.get("bin_moved_or_tipped")),
        "collection_vehicle_visible": _answer(parsed.get("collection_vehicle_visible")),
        "confidence": _clean_confidence(parsed.get("confidence")),
        "evidence": _evidence(parsed.get("evidence")),
    }


class SceneVerifier(Protocol):
    name: str

    async def verify_mailbox(self, images: list[tuple[str, bytes]]) -> dict | None: ...

    async def verify_bin(self, images: list[tuple[str, bytes]]) -> dict | None: ...


@dataclass
class AzureFoundrySceneVerifier:
    endpoint: str
    api_key: str
    deployment: str
    api_version: str = "2024-10-21"
    timeout_seconds: float = 20.0
    name: str = "azure-foundry-scene"

    @property
    def _url(self) -> str:
        base = self.endpoint.rstrip("/")
        return f"{base}/openai/deployments/{self.deployment}/chat/completions?api-version={self.api_version}"

    async def _ask(self, prompt: str, images: list[tuple[str, bytes]]) -> str | None:
        content: list[dict] = []
        for label, image in images:
            content.append({"type": "text", "text": label})
            encoded = base64.b64encode(image).decode("ascii")
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}
            )
        payload = {
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": content},
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
        return (choices[0].get("message") or {}).get("content")

    async def verify_mailbox(self, images: list[tuple[str, bytes]]) -> dict | None:
        return parse_mailbox_reply(await self._ask(MAILBOX_PROMPT, images))

    async def verify_bin(self, images: list[tuple[str, bytes]]) -> dict | None:
        return parse_bin_reply(await self._ask(BIN_PROMPT, images))


_verifier: SceneVerifier | None = None
_built = False


def build_scene_verifier(settings) -> SceneVerifier | None:
    if not settings.scene_verifier_enabled:
        return None
    if not (settings.foundry_endpoint and settings.foundry_api_key):
        return None
    return AzureFoundrySceneVerifier(
        endpoint=settings.foundry_endpoint,
        api_key=settings.foundry_api_key,
        deployment=settings.foundry_vision_deployment,
        api_version=settings.foundry_vision_api_version,
        timeout_seconds=settings.foundry_timeout_seconds,
    )


def get_scene_verifier() -> SceneVerifier | None:
    global _verifier, _built
    if not _built:
        from ..config import settings

        _verifier = build_scene_verifier(settings)
        _built = True
    return _verifier


def set_scene_verifier(verifier: SceneVerifier | None) -> None:
    """Test hook: install a fake verifier (or none)."""
    global _verifier, _built
    _verifier = verifier
    _built = True


def reset_scene_verifier() -> None:
    global _verifier, _built
    _verifier = None
    _built = False
