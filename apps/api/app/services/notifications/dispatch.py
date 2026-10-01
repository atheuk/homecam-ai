"""Dispatch: incident -> enabled channels, without ever blocking ingestion.

:func:`notify_incident` is the only thing the incident pipeline calls. It
is synchronous, takes a plain ``incidents.to_dict()`` snapshot (no ORM
object, so there is nothing to lazy-load on a background task and no way
to trip a detached-session error), and schedules the real work on the
running event loop. If there is no loop, or the task fails, the incident
is still recorded and broadcast - alerting is best-effort by design.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...models.db import Camera, Event, EventPhoto, NotificationChannel
from . import store
from .payload import NotificationPayload, build_payload, severity_rank
from .policy import should_notify
from .senders import SENDERS, ChannelSpec, NotificationError
from .senders import webpush as webpush_sender

logger = logging.getLogger(__name__)

#: Strong references to in-flight dispatch tasks. Without this the event
#: loop may garbage-collect a running task mid-send.
_tasks: set[asyncio.Task] = set()


def notify_incident(incident: dict, *, reason: str) -> None:
    """Fire-and-forget: schedule notifications for ``incident``.

    Safe to call from anywhere in the incident pipeline. Never raises, never
    awaits a network call, and does nothing at all when notifications are
    disabled or the snapshot has no id.
    """
    if not settings.notifications_enabled or not incident.get("id"):
        return
    if store.no_channels_configured():
        # Nothing is set up: do not even open a database session from the
        # ingestion path.
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop (sync context/tests): alerting is best-effort, so skip
        # rather than spin up a loop inside the ingestion path.
        return
    task = loop.create_task(_safe_dispatch(incident, reason))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _safe_dispatch(incident: dict, reason: str) -> None:
    try:
        await dispatch_incident(incident, reason=reason)
    except Exception:
        logger.warning("notification dispatch failed for incident %s", incident.get("id"), exc_info=True)


async def _camera_name(session: AsyncSession, camera_id: str | None) -> str:
    if not camera_id:
        return "Unknown camera"
    camera = await session.get(Camera, camera_id)
    return camera.name if camera else camera_id


async def _snapshot(session: AsyncSession, incident: dict) -> bytes | None:
    """Most recent stored photo for this incident, if there is one.

    Only ever handed to a channel that both opted in and delivers
    privately; see ``ChannelSpec.supports_images``.
    """
    event_ids = list(incident.get("event_ids") or [])
    if not event_ids:
        return None
    result = await session.execute(
        select(EventPhoto.image)
        .join(Event, Event.id == EventPhoto.event_id)
        .where(EventPhoto.event_id.in_(event_ids))
        .order_by(Event.start_time.desc())
        .limit(1)
    )
    return result.scalars().first()


async def _send_to_channel(
    session: AsyncSession,
    spec: ChannelSpec,
    payload: NotificationPayload,
    image: bytes | None,
) -> tuple[str, str | None]:
    """Perform one channel's delivery. Returns ``(status, detail)``."""
    timeout = settings.notification_timeout_seconds
    if spec.type == "webpush":
        subscriptions = [
            {"endpoint": row.endpoint, "p256dh": row.p256dh, "auth": row.auth}
            for row in await store.all_subscriptions(session)
        ]
        if not subscriptions:
            return "skipped", "no push subscriptions registered"
        delivered, gone = await webpush_sender.send_push(subscriptions, payload, timeout)
        await store.drop_dead_subscriptions(session, gone)
        if delivered == 0:
            return "failed", "no subscription accepted the push"
        return "sent", f"delivered to {delivered} device(s)"

    sender = SENDERS.get(spec.type)
    if sender is None:
        return "failed", f"unsupported channel type: {spec.type}"
    await asyncio.wait_for(
        sender(spec, payload, image if spec.supports_images() else None, timeout),
        timeout=timeout + 2.0,
    )
    return "sent", None


async def dispatch_incident(incident: dict, *, reason: str, session_factory=None) -> dict:
    """Evaluate policy and deliver ``incident`` to every eligible channel.

    Returns a small per-channel result map, which the tests assert on and
    which keeps the whole decision path observable without logging payload
    contents.
    """
    from ...db import SessionLocal

    factory = session_factory or SessionLocal
    results: dict[str, str] = {}
    severity = (incident.get("severity") or "low").lower()
    incident_id = str(incident.get("id"))

    async with factory() as session:
        policy = await store.get_settings(session)
        allowed, why = should_notify(
            severity=severity,
            now=datetime.now(timezone.utc),
            enabled=policy.enabled,
            min_severity=policy.min_severity,
            quiet_hours_enabled=policy.quiet_hours_enabled,
            quiet_hours_start=policy.quiet_hours_start,
            quiet_hours_end=policy.quiet_hours_end,
            quiet_hours_override_severity=policy.quiet_hours_override_severity,
        )
        if not allowed:
            logger.info("incident %s not notified: %s", incident_id, why)
            return {"_policy": why}

        channels = await store.enabled_channels(session)
        if not channels:
            return {"_policy": "no_enabled_channels"}

        # Flatten every channel up front. The loop below commits (claiming
        # a delivery), which expires ORM objects, and a lazily-refreshed
        # attribute would then try to do IO from a plain attribute access.
        specs = [store.spec_for(channel) for channel in channels]
        max_per_hour = policy.max_per_hour

        camera_name = await _camera_name(session, incident.get("camera_id"))
        payload = build_payload(incident, camera_name=camera_name, reason=reason)
        image = None
        if any(spec.supports_images() for spec in specs):
            image = await _snapshot(session, incident)

        # One notification per incident per reason; an escalation is keyed
        # by level so the second escalation can still alert.
        suffix = incident.get("escalation_level") if reason == "escalated" else ""
        dedupe_key = f"{incident_id}:{reason}:{suffix}"

        for spec in specs:
            if severity_rank(severity) < severity_rank(spec.min_severity):
                results[spec.id] = "below_channel_min_severity"
                continue
            if await store.recent_delivery_count(session, spec.id) >= max_per_hour:
                results[spec.id] = "rate_limited"
                logger.warning("channel %s rate-limited", spec.id)
                continue
            if not await store.claim_delivery(
                session,
                channel_id=spec.id,
                dedupe_key=dedupe_key,
                incident_id=incident_id,
                reason=reason,
            ):
                results[spec.id] = "duplicate"
                continue
            try:
                status, detail = await _send_to_channel(session, spec, payload, image)
            except (NotificationError, asyncio.TimeoutError, RuntimeError) as exc:
                status, detail = "failed", str(exc)[:400]
                logger.warning("notification channel %s failed: %s", spec.id, detail)
            except Exception:
                status, detail = "failed", "unexpected error"
                logger.warning("notification channel %s raised", spec.id, exc_info=False)
            await store.finish_delivery(
                session, channel_id=spec.id, dedupe_key=dedupe_key, status=status, detail=detail
            )
            await store.record_result(session, spec.id, status=status, message=detail)
            results[spec.id] = status
    return results


async def send_test(session: AsyncSession, channel: NotificationChannel) -> tuple[str, str | None]:
    """Deliver a clearly-labelled test notification to one channel.

    Bypasses dedupe and quiet hours (the operator is asking for it right
    now) but still goes through the real sender and the real timeout, so a
    successful test means the channel genuinely works.
    """
    payload = build_payload(
        {
            "id": "",
            "kind": "intrusion",
            "severity": "low",
            "summary": "Test notification from HomeCam AI. If you can read this, alerts work.",
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        camera_name="Test",
        reason="test",
    )
    spec = store.spec_for(channel)
    channel_id = channel.id
    try:
        status, detail = await _send_to_channel(session, spec, payload, None)
    except (NotificationError, asyncio.TimeoutError, RuntimeError) as exc:
        status, detail = "failed", str(exc)[:400]
    except Exception:
        status, detail = "failed", "unexpected error"
    with contextlib.suppress(Exception):
        await store.record_result(session, channel_id, status=status, message=detail)
    return status, detail
