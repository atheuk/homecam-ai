"""ntfy (https://ntfy.sh) sender.

Text alerts are a plain POST to the topic URL. A snapshot is only ever
published when the channel has a token *and* an admin enabled image
attachments - an unauthenticated ntfy topic is world-readable to anyone
who guesses its name.
"""

from __future__ import annotations

import httpx

from ..payload import NotificationPayload

#: ntfy priority 1..5; mapped from incident severity so a critical alert
#: can break through a phone's do-not-disturb while a low one cannot.
_PRIORITY = {"low": "2", "medium": "3", "high": "4", "critical": "5"}


def _headers(spec, payload: NotificationPayload) -> dict:
    headers = {
        "Title": payload.title,
        "Priority": _PRIORITY.get(payload.severity, "3"),
        "Tags": "rotating_light" if payload.severity in ("high", "critical") else "bell",
    }
    if payload.url:
        headers["Click"] = payload.url
    if spec.secret:
        headers["Authorization"] = f"Bearer {spec.secret}"
    return headers


async def send(spec, payload: NotificationPayload, image: bytes | None, timeout: float) -> str:
    from . import NotificationError, scrub

    url = f"{spec.config.get('server', 'https://ntfy.sh')}/{spec.config.get('topic', '')}"
    headers = _headers(spec, payload)
    content: bytes
    if image and spec.supports_images():
        headers["Filename"] = f"incident-{payload.incident_id or 'test'}.jpg"
        headers["Message"] = payload.body
        content = image
    else:
        content = payload.body.encode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(url, content=content, headers=headers)
        if response.status_code >= 400:
            raise NotificationError(f"ntfy returned HTTP {response.status_code}")
    except httpx.HTTPError as exc:
        raise NotificationError(scrub(f"ntfy request failed: {exc}", spec.secret)) from None
    return "sent"
