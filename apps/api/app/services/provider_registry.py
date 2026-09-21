"""Provider registry: aggregates all configured camera providers.

Iterates providers defensively so a single provider raising
``ProviderUnavailableError`` (e.g. a simulated HomeBase outage) never
prevents cameras from other providers from being listed (SPEC 2.5 / 43).
"""
from __future__ import annotations

import logging

from ..config import settings
from ..providers.base import CameraNotFoundError, CameraProvider, ProviderUnavailableError
from ..providers.dahua import DahuaProvider, DahuaSettings
from ..providers.eufy import EufyEdgeProvider, EufySettings
from ..providers.mock import PROVIDERS as MOCK_PROVIDERS
from ..providers.mock import MockCameraProvider

logger = logging.getLogger(__name__)


def _configured_real_providers() -> list[CameraProvider]:
    providers: list[CameraProvider] = []
    if settings.dahua_enabled:
        providers.append(
            DahuaProvider(
                DahuaSettings(
                    scheme=settings.dahua_scheme,
                    host=settings.dahua_host,
                    port=settings.dahua_port,
                    username=settings.dahua_username,
                    password=settings.dahua_password,
                    serial=settings.dahua_serial,
                    channels=settings.dahua_channels,
                    timeout_seconds=settings.dahua_timeout_seconds,
                    retries=settings.dahua_retries,
                )
            )
        )
    if settings.eufy_enabled:
        providers.append(
            EufyEdgeProvider(
                EufySettings(
                    adapter_url=settings.eufy_adapter_url,
                    adapter_token=settings.eufy_adapter_token,
                    timeout_seconds=settings.eufy_timeout_seconds,
                    retries=settings.eufy_retries,
                )
            )
        )
    return providers


def all_providers() -> list[CameraProvider]:
    return [*MOCK_PROVIDERS, *_configured_real_providers()]


def mock_providers() -> list[MockCameraProvider]:
    return list(MOCK_PROVIDERS)


def find_provider_for_camera(camera_id: str) -> CameraProvider | None:
    for provider in all_providers():
        if provider.has_camera(camera_id):
            return provider
    return None


async def discover_all_cameras() -> list[dict]:
    """Return cameras from every healthy provider. A single failing
    provider is logged and skipped rather than raised, so the endpoint
    keeps serving cameras from the remaining providers."""
    cameras: list[dict] = []
    for provider in all_providers():
        try:
            cameras.extend(await provider.discover_devices())
        except ProviderUnavailableError as exc:
            logger.warning("provider %s unavailable: %s", provider.id, exc)
    return cameras


async def get_camera_or_raise(camera_id: str) -> dict:
    provider = find_provider_for_camera(camera_id)
    if provider is None:
        raise CameraNotFoundError(camera_id)
    cameras = await provider.discover_devices()
    for camera in cameras:
        if camera["id"] == camera_id:
            return camera
    raise CameraNotFoundError(camera_id)


async def get_all_provider_health() -> list[dict]:
    health: list[dict] = []
    for provider in all_providers():
        health.append(await provider.get_health())
    return health
