"""Admin provider-configuration schemas (SPEC admin plane).

Secrets (``password`` / ``adapter_token``) are write-only: they are accepted
on create/update requests but never appear on any output model. Omitting the
secret field on an update preserves the previously stored value.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DahuaProviderConfigIn(BaseModel):
    name: str = "Dahua NVR"
    scheme: str = Field(default="http", pattern="^(http|https)$")
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=80, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=120)
    password: str | None = Field(default=None, max_length=512)
    channels: str = ""
    enabled: bool = True


class DahuaProviderConfigUpdate(BaseModel):
    """Partial update; omitted fields (including ``password``) keep their
    previously stored value."""

    name: str | None = None
    scheme: str | None = Field(default=None, pattern="^(http|https)$")
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = Field(default=None, max_length=512)
    channels: str | None = None
    enabled: bool | None = None


class EufyProviderConfigIn(BaseModel):
    name: str = "Eufy Adapter"
    adapter_url: str = Field(min_length=1, max_length=255)
    adapter_token: str | None = Field(default=None, max_length=512)
    enabled: bool = True


class EufyProviderConfigUpdate(BaseModel):
    name: str | None = None
    adapter_url: str | None = Field(default=None, min_length=1, max_length=255)
    adapter_token: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None


class ProviderConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    provider_type: str
    name: str
    enabled: bool
    scheme: str | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    channels: str | None = None
    adapter_url: str | None = None
    has_secret: bool
    last_test_status: str | None = None
    last_test_message: str | None = None
    last_test_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ProviderEnabledIn(BaseModel):
    enabled: bool


class DahuaTestIn(BaseModel):
    """Test-connection input. If ``config_id`` is set, any omitted field
    (including ``password``) falls back to the stored configuration so a
    saved config can be re-tested without re-entering the password."""

    config_id: str | None = None
    scheme: str | None = Field(default=None, pattern="^(http|https)$")
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    channels: str | None = None


class EufyTestIn(BaseModel):
    config_id: str | None = None
    adapter_url: str | None = None
    adapter_token: str | None = None


class ProviderTestResult(BaseModel):
    success: bool
    status: str
    message: str
