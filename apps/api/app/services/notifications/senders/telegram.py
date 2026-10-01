"""Telegram bot sender.

The bot token is the channel secret and appears in the request *path*, so
every error string is scrubbed before it is logged or stored. Delivery is
to one chat id: a private chat with the household, which is why Telegram
is allowed to carry a snapshot when an admin turns that on.
"""

from __future__ import annotations

import httpx

from ..payload import NotificationPayload

_API = "https://api.telegram.org"


def _text(payload: NotificationPayload) -> str:
    lines = [payload.title, payload.body]
    if payload.url:
        lines.append(payload.url)
    return "\n".join(lines)


async def send(spec, payload: NotificationPayload, image: bytes | None, timeout: float) -> str:
    from . import NotificationError, scrub

    token = spec.secret
    if not token:
        raise NotificationError("telegram channel has no bot token configured")
    chat_id = str(spec.config.get("chat_id") or "")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            if image and spec.supports_images():
                response = await client.post(
                    f"{_API}/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": _text(payload)[:1024]},
                    files={"photo": (f"incident-{payload.incident_id or 'test'}.jpg", image, "image/jpeg")},
                )
            else:
                response = await client.post(
                    f"{_API}/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": _text(payload)[:4000],
                        "disable_web_page_preview": True,
                    },
                )
        if response.status_code >= 400:
            raise NotificationError(f"telegram returned HTTP {response.status_code}")
    except httpx.HTTPError as exc:
        raise NotificationError(scrub(f"telegram request failed: {exc}", token)) from None
    return "sent"
