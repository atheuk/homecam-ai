"""Request/response models for the notification admin plane.

Note what is *not* here: there is no field that returns a channel secret.
The only secret-shaped field is write-only (``secret`` on create/update);
responses expose ``has_secret`` instead, exactly like provider configs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ChannelType = Literal["webpush", "ntfy", "telegram", "webhook"]
Severity = Literal["low", "medium", "high", "critical"]
HHMM = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class ChannelIn(BaseModel):
    type: ChannelType
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = False
    config: dict = Field(default_factory=dict)
    # Write-only. Omit to leave an existing secret untouched; send "" to clear.
    secret: str | None = Field(default=None, max_length=1024)
    attach_images: bool = False
    min_severity: Severity = "low"


class ChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    config: dict | None = None
    secret: str | None = Field(default=None, max_length=1024)
    attach_images: bool | None = None
    min_severity: Severity | None = None


class ChannelOut(BaseModel):
    id: str
    type: str
    name: str
    enabled: bool
    config: dict
    has_secret: bool
    attach_images: bool
    min_severity: str
    last_status: str | None = None
    last_message: str | None = None
    last_sent_at: str | None = None
    created_at: str
    updated_at: str


class NotificationSettingsIn(BaseModel):
    enabled: bool | None = None
    quiet_hours_enabled: bool | None = None
    quiet_hours_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    quiet_hours_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    quiet_hours_override_severity: Severity | None = None
    min_severity: Severity | None = None
    max_per_hour: int | None = Field(default=None, ge=1, le=500)


class NotificationSettingsOut(BaseModel):
    enabled: bool
    quiet_hours_enabled: bool
    quiet_hours_start: str
    quiet_hours_end: str
    quiet_hours_override_severity: str
    min_severity: str
    max_per_hour: int
    updated_at: str


class NotificationStatusOut(BaseModel):
    """Everything the admin UI needs to explain the current state."""

    notifications_enabled: bool
    web_push_available: bool
    web_push_detail: str
    vapid_public_key: str | None
    deep_links_configured: bool
    channel_count: int
    enabled_channel_count: int
    subscription_count: int


class TestResultOut(BaseModel):
    status: str
    detail: str | None = None


class PushSubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=10, max_length=500)
    p256dh: str = Field(min_length=1, max_length=255)
    auth: str = Field(min_length=1, max_length=255)


class PushSubscriptionDeleteIn(BaseModel):
    endpoint: str = Field(min_length=10, max_length=500)


class PushSubscriptionOut(BaseModel):
    id: str
    endpoint_hint: str
    user_agent: str | None = None
    created_at: str
    last_used_at: str | None = None
