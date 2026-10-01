"""AI-assisted incident risk summary (Foundry chat deployment).

Purely advisory, plain-language text alongside the deterministic
``Incident.summary`` (camera/zone/count/mode - always present, computed
without any AI). This module never decides whether an incident is raised or
how severe it is; those decisions are fully deterministic (see
``app.services.security_modes`` and ``app.services.incidents``). It is also
never asked to identify *who* someone is, or their gender/ethnicity/age -
only to restate the facts already given to it in plain language, the same
RAI boundary already applied to appearance analysis (:mod:`app.ai.appearance`).

Degrades to ``None`` with no import-time dependency on Foundry being
configured, exactly like ``app.ai.vision``'s captioner: disabled/absent
Foundry means every incident still has its deterministic summary, just no
AI-assisted one. Never raises: any failure (network, auth, malformed
response) is logged and treated as "no AI summary available".
"""
from __future__ import annotations

import logging

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You summarize home security incidents for a homeowner in 1-2 short, "
    "plain-language sentences. Use only the facts given to you; never invent "
    "details. Never guess or state a person's identity, name, gender, "
    "ethnicity, or age. Never recommend contacting emergency services or "
    "dispatching anyone - at most suggest the homeowner review the footage "
    "themselves. Be calm and factual, not alarmist."
)


def _configured() -> bool:
    return bool(
        settings.incident_ai_summary_enabled
        and settings.foundry_endpoint
        and settings.foundry_api_key
    )


async def summarize_incident(facts: dict) -> str | None:
    """Best-effort AI-assisted summary of ``facts`` (a small JSON-safe dict
    of deterministic incident fields). Returns ``None`` if Foundry is
    unavailable/disabled or the call fails for any reason."""
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
            {"role": "user", "content": f"Incident facts (JSON): {facts}\nWrite the 1-2 sentence summary now."},
        ],
        "max_completion_tokens": 120,
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
        if not content:
            return None
        return str(content).strip()[:1000]
    except Exception:  # noqa: BLE001 - an AI summary failure must never break incident creation
        logger.warning("incident AI summary call failed", exc_info=True)
        return None
