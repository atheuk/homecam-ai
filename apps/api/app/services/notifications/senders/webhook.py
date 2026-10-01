"""Generic HTTPS webhook sender.

Posts the same structured payload the rest of the system uses. When a
secret is configured it is used as an HMAC-SHA256 key over the exact body
bytes (``X-HomeCam-Signature: sha256=<hex>``) so the receiver can verify
the call really came from this deployment. The secret itself is never
transmitted.

Never carries an image: a webhook endpoint is operator-defined and its
transport privacy cannot be reasoned about here.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from ..payload import NotificationPayload


def signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


async def send(spec, payload: NotificationPayload, image: bytes | None, timeout: float) -> str:
    from . import NotificationError, scrub

    url = spec.config.get("url") or ""
    body = json.dumps({"event": "incident_notification", **payload.as_dict()}).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "homecam-ai"}
    if spec.secret:
        headers["X-HomeCam-Signature"] = signature(body, spec.secret)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(url, content=body, headers=headers)
        if response.status_code >= 400:
            raise NotificationError(f"webhook returned HTTP {response.status_code}")
    except httpx.HTTPError as exc:
        raise NotificationError(scrub(f"webhook request failed: {exc}", spec.secret)) from None
    return "sent"
