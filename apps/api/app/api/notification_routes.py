"""Notification admin + Web Push subscription API.

Every route requires a session, like the rest of the authenticated plane
(same single-user-is-admin scope limitation documented in
``app/api/admin_routes.py``). Channel configuration changes are
audit-logged with the acting user, and no response ever contains a channel
secret or a full push endpoint.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..config import settings
from ..db import get_db
from ..models.db import NotificationChannel, User
from ..schemas_notifications import (
    ChannelIn,
    ChannelOut,
    ChannelUpdate,
    NotificationSettingsIn,
    NotificationSettingsOut,
    NotificationStatusOut,
    PushSubscriptionDeleteIn,
    PushSubscriptionIn,
    PushSubscriptionOut,
    TestResultOut,
)
from ..services import audit as audit_service
from ..services.notifications import dispatch, store
from ..services.notifications.senders import webpush as webpush_sender

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


async def _get_channel(session: AsyncSession, channel_id: str) -> NotificationChannel:
    channel = await session.get(NotificationChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="channel not found")
    return channel


@router.get("/status", response_model=NotificationStatusOut)
async def get_status(session: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)):
    channels = await store.list_channels(session)
    available, detail = webpush_sender.available()
    return NotificationStatusOut(
        notifications_enabled=settings.notifications_enabled,
        web_push_available=available,
        web_push_detail=detail,
        # The VAPID *public* key is public by design (RFC 8292): the browser
        # needs it to create a subscription. The private key never leaves
        # the server.
        vapid_public_key=settings.vapid_public_key if available else None,
        deep_links_configured=bool(settings.web_app_base_url),
        channel_count=len(channels),
        enabled_channel_count=sum(1 for channel in channels if channel.enabled),
        subscription_count=len(await store.all_subscriptions(session)),
    )


@router.get("/settings", response_model=NotificationSettingsOut)
async def get_settings(session: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)):
    return store.settings_to_out(await store.get_settings(session))


@router.put("/settings", response_model=NotificationSettingsOut)
async def put_settings(
    payload: NotificationSettingsIn,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = payload.model_dump(exclude_none=True)
    row = await store.update_settings(session, data, user_id=user.id)
    await audit_service.record(
        session,
        "notification.settings.updated",
        actor_user_id=user.id,
        target_type="notification_settings",
        target_id=store.SETTINGS_ID,
        details=data,
    )
    return store.settings_to_out(row)


@router.get("/channels", response_model=list[ChannelOut])
async def list_channels(session: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)):
    return [store.to_out(channel) for channel in await store.list_channels(session)]


@router.post("/channels", response_model=ChannelOut, status_code=201)
async def create_channel(
    payload: ChannelIn,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        channel = await store.create_channel(session, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await audit_service.record(
        session,
        "notification.channel.created",
        actor_user_id=user.id,
        target_type="notification_channel",
        target_id=channel.id,
        # Config is non-secret by construction; the secret is never recorded.
        details={"type": channel.type, "name": channel.name, "enabled": channel.enabled},
    )
    return store.to_out(channel)


@router.patch("/channels/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: str,
    payload: ChannelUpdate,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    channel = await _get_channel(session, channel_id)
    data = payload.model_dump(exclude_unset=True)
    try:
        channel = await store.update_channel(session, channel, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await audit_service.record(
        session,
        "notification.channel.updated",
        actor_user_id=user.id,
        target_type="notification_channel",
        target_id=channel.id,
        details={"fields": sorted(k for k in data if k != "secret"), "secret_changed": "secret" in data},
    )
    return store.to_out(channel)


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: str,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    channel = await _get_channel(session, channel_id)
    await store.delete_channel(session, channel)
    await audit_service.record(
        session,
        "notification.channel.deleted",
        actor_user_id=user.id,
        target_type="notification_channel",
        target_id=channel_id,
        details={"type": channel.type},
    )
    return None


@router.post("/channels/{channel_id}/test", response_model=TestResultOut)
async def test_channel(
    channel_id: str,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    channel = await _get_channel(session, channel_id)
    status, detail = await dispatch.send_test(session, channel)
    await audit_service.record(
        session,
        "notification.channel.tested",
        actor_user_id=user.id,
        target_type="notification_channel",
        target_id=channel.id,
        details={"status": status},
    )
    return TestResultOut(status=status, detail=detail)


@router.get("/push/subscriptions", response_model=list[PushSubscriptionOut])
async def list_subscriptions(
    session: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
):
    rows = await store.list_subscriptions(session, user.id)
    return [store.subscription_to_out(row) for row in rows]


@router.post("/push/subscriptions", response_model=PushSubscriptionOut, status_code=201)
async def create_subscription(
    payload: PushSubscriptionIn,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not payload.endpoint.startswith("https://"):
        raise HTTPException(status_code=400, detail="endpoint must be https")
    row = await store.upsert_subscription(
        session,
        user_id=user.id,
        endpoint=payload.endpoint,
        p256dh=payload.p256dh,
        auth=payload.auth,
        user_agent=request.headers.get("user-agent"),
    )
    return store.subscription_to_out(row)


@router.post("/push/subscriptions/remove", status_code=204)
async def remove_subscription(
    payload: PushSubscriptionDeleteIn,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Unsubscribe one browser.

    A POST rather than a DELETE because the endpoint URL is too long and
    too sensitive to put in a path segment (it would land in access logs).
    Scoped to the caller's own subscriptions.
    """
    removed = await store.delete_subscription(session, user_id=user.id, endpoint=payload.endpoint)
    if not removed:
        raise HTTPException(status_code=404, detail="subscription not found")
    return None
