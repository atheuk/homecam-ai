"""Generic camera provider contract (SPEC section 5).

HomeCam Core must depend only on this contract, never on a specific vendor
implementation. Concrete providers (mock, and later Dahua/Eufy/ONVIF) live
under ``app/providers/<name>/`` and implement the same shape so the frontend
and orchestration code can treat every camera uniformly.
"""
from enum import Enum
from dataclasses import dataclass
from typing import Protocol, TypedDict


class CapabilityStatus(str, Enum):
    """Per-capability support status advertised by a provider for a camera."""

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class ProviderStatus(str, Enum):
    """Overall provider connectivity/health status."""

    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


class CameraStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class ProviderUnavailableError(RuntimeError):
    """Raised by a provider when it cannot currently serve requests.

    Callers (route handlers, aggregation services) must catch this per
    provider so one provider failing never breaks cameras served by another
    provider (SPEC section 2.5 / 43).
    """


class CameraNotFoundError(LookupError):
    """Raised when a provider does not recognize a requested camera id."""


class CameraOfflineError(RuntimeError):
    """Raised when an operation (snapshot/live) cannot run because the
    specific camera is currently offline, distinct from a whole-provider
    outage."""


class ProviderConfigurationError(RuntimeError):
    """Raised when a provider is enabled without the required local settings."""


@dataclass(frozen=True)
class ProviderRequestError(RuntimeError):
    """Structured upstream provider failure safe to expose without secrets."""

    provider_id: str
    operation: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return f"{self.provider_id} {self.operation} failed: {self.message}"


class CameraRecord(TypedDict):
    id: str
    provider_id: str
    name: str
    type: str
    model: str
    online: bool
    status: str
    battery_level: int | None
    capabilities: dict[str, str]


class ProviderInfo(TypedDict):
    id: str
    name: str
    manufacturer: str


class ProviderHealth(TypedDict):
    provider_id: str
    status: str
    message: str
    camera_count: int
    online_camera_count: int


class CameraProvider(Protocol):
    """Conceptual provider contract from SPEC section 5."""

    id: str

    async def get_provider_info(self) -> ProviderInfo: ...

    async def discover_devices(self) -> list[CameraRecord]: ...

    async def get_capabilities(self, camera_id: str) -> dict[str, str]: ...

    async def get_snapshot(self, camera_id: str) -> bytes: ...

    async def get_live_stream(self, camera_id: str) -> str: ...

    async def get_health(self) -> ProviderHealth: ...

    def has_camera(self, camera_id: str) -> bool: ...
