"""Web Push (RFC 8030/8291/8292) sender.

Fans out to the browser subscriptions registered by signed-in users. The
payload is text plus a deep link only - never an image - so a lock screen
shows what happened and tapping it opens the authenticated app, where the
snapshot still requires a session.

``pywebpush`` is an optional dependency: it pulls in ``cryptography``,
which has no wheel on some developer platforms. It is imported lazily so
the rest of the notification plane (and the whole API) keeps working, and
the channel simply reports itself unavailable instead of crashing.
"""

from __future__ import annotations

import asyncio
import json
import logging

from ....config import settings
from ..payload import NotificationPayload

logger = logging.getLogger(__name__)

#: Push services answer 404/410 when a subscription is permanently gone
#: (browser uninstalled, permission revoked). Those rows are deleted.
GONE_STATUSES = (404, 410)


def available() -> tuple[bool, str]:
    """Can web push actually send right now? ``(ok, reason)``."""
    if not settings.vapid_private_key or not settings.vapid_public_key:
        return False, "VAPID keys are not configured"
    try:
        import pywebpush  # noqa: F401
    except ImportError:
        return False, "pywebpush is not installed in this build"
    return True, "ok"


def _send_one(subscription: dict, body: str, timeout: float) -> int:
    """Blocking single send. Returns an HTTP-ish status for the caller.

    Runs in a worker thread because ``pywebpush`` is synchronous.
    """
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=body,
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_subject},
            timeout=timeout,
        )
        return 201
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in GONE_STATUSES:
            return status
        raise


async def send_push(
    subscriptions: list[dict], payload: NotificationPayload, timeout: float
) -> tuple[int, list[str]]:
    """Deliver to every subscription.

    Returns ``(delivered, gone_endpoints)``. Endpoints in ``gone_endpoints``
    should be deleted by the caller. Individual failures are logged and
    skipped: one dead phone must not stop the others from being alerted.
    """
    ok, reason = available()
    if not ok:
        raise RuntimeError(reason)

    body = json.dumps(
        {
            "title": payload.title,
            "body": payload.body,
            "url": payload.url,
            "severity": payload.severity,
            "incident_id": payload.incident_id,
            "tag": f"incident-{payload.incident_id}" if payload.incident_id else "homecam-test",
        }
    )
    delivered = 0
    gone: list[str] = []
    for subscription in subscriptions:
        try:
            status = await asyncio.wait_for(
                asyncio.to_thread(_send_one, subscription, body, timeout),
                timeout=timeout + 1.0,
            )
        except asyncio.TimeoutError:
            logger.warning("web push timed out for one subscription")
            continue
        except Exception:
            # The exception text can contain the endpoint (a capability
            # URL), so it is never logged verbatim.
            logger.warning("web push failed for one subscription", exc_info=False)
            continue
        if status in GONE_STATUSES:
            gone.append(subscription["endpoint"])
        else:
            delivered += 1
    return delivered, gone
