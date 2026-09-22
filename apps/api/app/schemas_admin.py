"""Admin provider-configuration schemas (SPEC admin plane).

Secrets (``password`` / ``adapter_token`` / ``edge_token``) are write-only:
they are accepted on create/update requests but never appear on any output
model. Omitting the secret field on an update preserves the previously
stored value.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DahuaProviderConfigIn(BaseModel):
    name: str = "Dahua NVR"
    # "direct": raw LAN HTTP/RTSP (existing behavior). "edge": talk to a
    # Home Assistant/Raspberry Pi edge connector over a private VPN/overlay
    # (Tailscale recommended) instead — see docs/edge-connector.md.
    mode: Literal["direct", "edge"] = "direct"
    scheme: str = Field(default="http", pattern="^(http|https)$")
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int = Field(default=80, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = Field(default=None, max_length=512)
    channels: str = ""
    edge_base_url: str | None = Field(default=None, min_length=1, max_length=255)
    edge_token: str | None = Field(default=None, max_length=512)
    enabled: bool = True

    @model_validator(mode="after")
    def check_mode_requirements(self) -> "DahuaProviderConfigIn":
        if self.mode == "direct":
            if not self.host or not self.username:
                raise ValueError("mode 'direct' requires 'host' and 'username'")
        elif self.mode == "edge":
            if not self.edge_base_url:
                raise ValueError("mode 'edge' requires 'edge_base_url'")
        return self


class DahuaProviderConfigUpdate(BaseModel):
    """Partial update; omitted fields (including secrets) keep their
    previously stored value."""

    name: str | None = None
    mode: Literal["direct", "edge"] | None = None
    scheme: str | None = Field(default=None, pattern="^(http|https)$")
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = Field(default=None, max_length=512)
    channels: str | None = None
    edge_base_url: str | None = Field(default=None, min_length=1, max_length=255)
    edge_token: str | None = Field(default=None, max_length=512)
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
    mode: str | None = None
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
    (including secrets) falls back to the stored configuration so a saved
    config can be re-tested without re-entering the password/token. If
    ``mode`` is omitted, it falls back to the stored config's mode (or
    "direct" for an ad-hoc test with no ``config_id``)."""

    config_id: str | None = None
    mode: Literal["direct", "edge"] | None = None
    scheme: str | None = Field(default=None, pattern="^(http|https)$")
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    channels: str | None = None
    edge_base_url: str | None = None
    edge_token: str | None = None


class EufyTestIn(BaseModel):
    config_id: str | None = None
    adapter_url: str | None = None
    adapter_token: str | None = None


class ProviderTestResult(BaseModel):
    success: bool
    status: str
    message: str


class CameraZoneIn(BaseModel):
    """Named rectangle in normalized (0..1) image coordinates.

    Zone geometry is validated here so the pipeline can assume every stored
    zone is a well-formed box.
    """

    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(default="other", max_length=32)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)
    x2: float = Field(ge=0.0, le=1.0)
    y2: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def check_box(self) -> "CameraZoneIn":
        if self.x1 >= self.x2 or self.y1 >= self.y2:
            raise ValueError("zone must satisfy x1 < x2 and y1 < y2")
        return self


class CameraZoneUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    kind: str | None = Field(default=None, max_length=32)
    x1: float | None = Field(default=None, ge=0.0, le=1.0)
    y1: float | None = Field(default=None, ge=0.0, le=1.0)
    x2: float | None = Field(default=None, ge=0.0, le=1.0)
    y2: float | None = Field(default=None, ge=0.0, le=1.0)


class CameraZoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    camera_id: str
    name: str
    kind: str
    x1: float
    y1: float
    x2: float
    y2: float
    created_at: datetime
    updated_at: datetime
