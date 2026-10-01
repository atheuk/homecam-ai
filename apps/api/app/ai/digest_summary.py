"""AI-assisted daily digest narration (Foundry chat deployment).

Same contract as :mod:`app.ai.incident_summary`: the deterministic,
template-built digest is always produced first, and this module only ever
*rewrites it in nicer prose* from facts that were already computed in SQL.
Returns ``None`` when Foundry is not configured or the call fails, so the
digest degrades to the template rather than to nothing - which is also
what makes the whole feature deterministic under test.
"""
from __future__ import annotations

import logging

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You write a short daily home-security digest for a homeowner in 2-3 calm, "
    "plain-language sentences. Use only the counts and facts given to you; never "
    "invent events. Never guess or state anyone's identity, name, gender, ethnicity "
    "or age. Never recommend contacting emergency services or dispatching anyone."
)


def _configured() -> bool:
    return bool(settings.digest_enabled and settings.foundry_endpoint and settings.foundry_api_key)


async def summarize_day(facts: dict) -> str | None:
    """Best-effort narration of one day's aggregate ``facts``."""
    if not _configured():
        return None
    base = settings.foundry_endpoint.rstrip("/")
    url = (
        f"{base}/openai/deployments/{settings.foundry_vision_deployment}"
        f"/chat/completions?api-version={settings.foundry_vision_api_version}"
    )
    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Day facts (JSON): {facts}\nWrite the digest now."},
        ],
        "max_completion_tokens": 200,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.foundry_timeout_seconds) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"api-key": settings.foundry_api_key, "Content-Type": "application/json"},
            )
        response.raise_for_status()
        choices = response.json().get("choices") or []
        if not choices:
            return None
        content = (choices[0].get("message") or {}).get("content")
        return str(content).strip()[:1000] if content else None
    except Exception:  # noqa: BLE001 - the digest must never depend on the provider
        logger.warning("digest AI summary call failed", exc_info=True)
        return None
