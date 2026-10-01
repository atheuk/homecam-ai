"""Channel/settings/subscription storage for the notification plane.

Secret handling mirrors ``app/services/provider_configs.py``: the single
per-channel secret is encrypted at rest, decrypted only in memory for the
duration of one send, and the API surface exposes ``has_secret`` instead of
the value. A secret is never logged, never returned, and never echoed back
in an error message.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...crypto import SecretDecryptionError, decrypt_secret, encrypt_secret
from ...models.db import (
    NotificationChannel,
    NotificationDelivery,
    NotificationSetting,
    PushSubscription,
)
from .senders import ChannelSpec, requires_secret, validate_config

SETTINGS_ID = "default"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def to_out(channel: NotificationChannel) -> dict:
    """Public representation of a channel - secret-free by construction."""
    return {
        "id": channel.id,
        "type": channel.type,
        "name": channel.name,
        "enabled": channel.enabled,
        "config": dict(channel.config or {}),
        "has_secret": bool(channel.secret_encrypted),
        "attach_images": channel.attach_images,
        "min_severity": channel.min_severity,
        "last_status": channel.last_status,
        "last_message": channel.last_message,
        "last_sent_at": channel.last_sent_at.isoformat() if channel.last_sent_at else None,
        "created_at": channel.created_at.isoformat(),
        "updated_at": channel.updated_at.isoformat(),
    }


def settings_to_out(row: NotificationSetting) -> dict:
    return {
        "enabled": row.enabled,
        "quiet_hours_enabled": row.quiet_hours_enabled,
        "quiet_hours_start": row.quiet_hours_start,
        "quiet_hours_end": row.quiet_hours_end,
        "quiet_hours_override_severity": row.quiet_hours_override_severity,
        "min_severity": row.min_severity,
        "max_per_hour": row.max_per_hour,
        "updated_at": row.updated_at.isoformat(),
    }


def spec_for(channel: NotificationChannel) -> ChannelSpec:
    """Flatten a stored channel into a sendable spec.

    A secret that cannot be decrypted (for example after ``SECRET_KEY`` was
    rotated) is treated as absent rather than fatal, exactly like provider
    credentials: the send then fails with a clear message instead of a
    stack trace that might echo ciphertext.
    """
    secret = None
    if channel.secret_encrypted:
        try:
            secret = decrypt_secret(channel.secret_encrypted)
        except SecretDecryptionError:
            secret = None
    return ChannelSpec(
        id=channel.id,
        type=channel.type,
        name=channel.name,
        config=dict(channel.config or {}),
        secret=secret,
        attach_images=channel.attach_images,
        min_severity=channel.min_severity,
    )


async def get_settings(session: AsyncSession) -> NotificationSetting:
    """Fetch (lazily creating) the single household policy row."""
    row = await session.get(NotificationSetting, SETTINGS_ID)
    if row is None:
        row = NotificationSetting(id=SETTINGS_ID, updated_at=_now())
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def update_settings(session: AsyncSession, data: dict, *, user_id: str | None) -> NotificationSetting:
    row = await get_settings(session)
    for field in (
        "enabled",
        "quiet_hours_enabled",
        "quiet_hours_start",
        "quiet_hours_end",
        "quiet_hours_override_severity",
        "min_severity",
        "max_per_hour",
    ):
        if data.get(field) is not None:
            setattr(row, field, data[field])
    row.updated_by = user_id
    row.updated_at = _now()
    await session.commit()
    await session.refresh(row)
    return row


async def list_channels(session: AsyncSession) -> list[NotificationChannel]:
    result = await session.execute(select(NotificationChannel).order_by(NotificationChannel.created_at.asc()))
    return list(result.scalars().all())


async def enabled_channels(session: AsyncSession) -> list[NotificationChannel]:
    result = await session.execute(
        select(NotificationChannel).where(NotificationChannel.enabled.is_(True))
    )
    channels = list(result.scalars().all())
    global _no_channels_until
    # Remember "nothing is configured" briefly so the common case - alerting
    # not set up - costs the ingestion path no database work at all. The TTL
    # bounds how long another replica's new channel stays unnoticed.
    _no_channels_until = None if channels else _now() + timedelta(seconds=_NO_CHANNEL_TTL_SECONDS)
    return channels


#: See :func:`enabled_channels` / :func:`no_channels_configured`.
_NO_CHANNEL_TTL_SECONDS = 60
_no_channels_until: datetime | None = None


def no_channels_configured() -> bool:
    """True when a recent lookup found no enabled channel at all."""
    return _no_channels_until is not None and _now() < _no_channels_until


def invalidate_channel_cache() -> None:
    global _no_channels_until
    _no_channels_until = None


async def create_channel(session: AsyncSession, data: dict) -> NotificationChannel:
    channel_type = data["type"]
    config = validate_config(channel_type, data.get("config"))
    secret = (data.get("secret") or "").strip() or None
    if requires_secret(channel_type) and not secret:
        raise ValueError(f"{channel_type} requires a secret")
    now = _now()
    channel = NotificationChannel(
        id=_new_id(),
        type=channel_type,
        name=(data.get("name") or channel_type).strip()[:120],
        enabled=bool(data.get("enabled", False)),
        config=config,
        secret_encrypted=encrypt_secret(secret) if secret else None,
        attach_images=bool(data.get("attach_images", False)),
        min_severity=data.get("min_severity") or "low",
        created_at=now,
        updated_at=now,
    )
    session.add(channel)
    await session.commit()
    await session.refresh(channel)
    invalidate_channel_cache()
    return channel


async def update_channel(session: AsyncSession, channel: NotificationChannel, data: dict) -> NotificationChannel:
    if data.get("config") is not None:
        channel.config = validate_config(channel.type, data["config"])
    if data.get("name"):
        channel.name = data["name"].strip()[:120]
    if data.get("enabled") is not None:
        channel.enabled = bool(data["enabled"])
    if data.get("attach_images") is not None:
        channel.attach_images = bool(data["attach_images"])
    if data.get("min_severity"):
        channel.min_severity = data["min_severity"]
    if "secret" in data and data["secret"] is not None:
        # An empty string clears the secret; omitting the field leaves it
        # untouched, so a UI round-trip never has to resend it.
        secret = data["secret"].strip()
        channel.secret_encrypted = encrypt_secret(secret) if secret else None
    if requires_secret(channel.type) and not channel.secret_encrypted:
        raise ValueError(f"{channel.type} requires a secret")
    channel.updated_at = _now()
    await session.commit()
    await session.refresh(channel)
    invalidate_channel_cache()
    return channel


async def delete_channel(session: AsyncSession, channel: NotificationChannel) -> None:
    await session.execute(
        delete(NotificationDelivery).where(NotificationDelivery.channel_id == channel.id)
    )
    await session.delete(channel)
    await session.commit()
    invalidate_channel_cache()


async def record_result(
    session: AsyncSession, channel: NotificationChannel | str, *, status: str, message: str | None
) -> None:
    """Store the last outcome so the admin UI can show a channel's health.

    Accepts an id as well as an instance: the dispatcher commits between
    steps, which expires ORM objects, and re-fetching by id keeps that
    bookkeeping off the hot path.
    """
    row = channel
    if isinstance(channel, str):
        row = await session.get(NotificationChannel, channel)
        if row is None:
            return
    row.last_status = status
    row.last_message = message[:500] if message else None
    if status == "sent":
        row.last_sent_at = _now()
    row.updated_at = _now()
    await session.commit()


async def claim_delivery(
    session: AsyncSession, *, channel_id: str, dedupe_key: str, incident_id: str | None, reason: str
) -> bool:
    """Reserve the right to send exactly once.

    The unique ``(channel_id, dedupe_key)`` constraint means a second API
    replica racing on the same incident loses the insert and skips the
    send, so the household gets one notification rather than two.
    """
    # A Core INSERT (rather than an ORM add+flush) keeps the failure path
    # to a single statement, so losing the race is a clean rollback.
    try:
        await session.execute(
            insert(NotificationDelivery).values(
                id=_new_id(),
                channel_id=channel_id,
                dedupe_key=dedupe_key[:160],
                incident_id=incident_id,
                reason=reason,
                status="pending",
                created_at=_now(),
            )
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True


async def finish_delivery(
    session: AsyncSession, *, channel_id: str, dedupe_key: str, status: str, detail: str | None
) -> None:
    result = await session.execute(
        select(NotificationDelivery).where(
            NotificationDelivery.channel_id == channel_id,
            NotificationDelivery.dedupe_key == dedupe_key[:160],
        )
    )
    row = result.scalars().first()
    if row is not None:
        row.status = status
        row.detail = detail[:500] if detail else None
        await session.commit()


async def recent_delivery_count(
    session: AsyncSession, channel_id: str, *, window_minutes: int = 60, exclude_dedupe_key: str | None = None
) -> int:
    """How many sends this channel has claimed in the trailing window.

    ``exclude_dedupe_key`` lets the dispatcher claim first and then count,
    so two replicas racing on different incidents each see the other's
    claim instead of both reading a stale under-the-limit count.
    """
    since = _now() - timedelta(minutes=window_minutes)
    conditions = [NotificationDelivery.channel_id == channel_id, NotificationDelivery.created_at >= since]
    if exclude_dedupe_key is not None:
        conditions.append(NotificationDelivery.dedupe_key != exclude_dedupe_key[:160])
    result = await session.execute(select(func.count()).select_from(NotificationDelivery).where(*conditions))
    return int(result.scalar() or 0)


async def list_subscriptions(session: AsyncSession, user_id: str) -> list[PushSubscription]:
    result = await session.execute(
        select(PushSubscription).where(PushSubscription.user_id == user_id).order_by(PushSubscription.created_at.asc())
    )
    return list(result.scalars().all())


async def all_subscriptions(session: AsyncSession) -> list[PushSubscription]:
    result = await session.execute(select(PushSubscription))
    return list(result.scalars().all())


async def upsert_subscription(
    session: AsyncSession, *, user_id: str, endpoint: str, p256dh: str, auth: str, user_agent: str | None
) -> PushSubscription:
    """Register (or re-register) one browser.

    Keyed on the endpoint, which is what the push service hands out, so
    re-subscribing the same browser updates the row instead of growing a
    pile of stale duplicates that all fail later.
    """
    result = await session.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    row = result.scalars().first()
    now = _now()
    if row is None:
        row = PushSubscription(
            id=_new_id(),
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=(user_agent or "")[:255] or None,
            created_at=now,
        )
        session.add(row)
    else:
        row.user_id = user_id
        row.p256dh = p256dh
        row.auth = auth
        row.user_agent = (user_agent or "")[:255] or None
        row.failure_count = 0
    await session.commit()
    await session.refresh(row)
    return row


async def delete_subscription(session: AsyncSession, *, user_id: str, endpoint: str) -> bool:
    result = await session.execute(
        select(PushSubscription).where(
            PushSubscription.endpoint == endpoint, PushSubscription.user_id == user_id
        )
    )
    row = result.scalars().first()
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def drop_dead_subscriptions(session: AsyncSession, endpoints: list[str]) -> None:
    """Remove subscriptions the push service reported as permanently gone."""
    if not endpoints:
        return
    await session.execute(delete(PushSubscription).where(PushSubscription.endpoint.in_(endpoints)))
    await session.commit()


def subscription_to_out(row: PushSubscription) -> dict:
    """Owner-facing view. The endpoint is a capability URL, so only a short
    fingerprint is returned - enough to tell two devices apart, not enough
    to push to them."""
    return {
        "id": row.id,
        "endpoint_hint": row.endpoint[-12:],
        "user_agent": row.user_agent,
        "created_at": row.created_at.isoformat(),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
    }
