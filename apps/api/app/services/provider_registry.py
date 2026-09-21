"""Provider registry: aggregates all configured camera providers.

Iterates providers defensively so a single provider raising
``ProviderUnavailableError`` (e.g. a simulated HomeBase outage) never
prevents cameras from other providers from being listed (SPEC 2.5 / 43).
"""
from __future__ import annotations

import logging

from ..providers.base import CameraNotFoundError, ProviderUnavailableError
from ..providers.mock import PROVIDERS, MockCameraProvider

logger = logging.getLogger(__name__)


def all_providers() -> list[MockCameraProvider]:
    return list(PROVIDERS)


def find_provider_for_camera(camera_id: str) -> MockCameraProvider | None:
    for provider in PROVIDERS:
        if provider.has_camera(camera_id):
            return provider
    return None


async def discover_all_cameras() -> list[dict]:
    """Return cameras from every healthy provider. A single failing
    provider is logged and skipped rather than raised, so the endpoint
    keeps serving cameras from the remaining providers."""
    cameras: list[dict] = []
    for provider in PROVIDERS:
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
    for provider in PROVIDERS:
        health.append(await provider.get_health())
    return health
