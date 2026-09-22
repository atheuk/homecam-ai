"""Provider registry: aggregates all configured camera providers.

Iterates providers defensively so a single provider raising
``ProviderUnavailableError`` (e.g. a simulated HomeBase outage) never
prevents cameras from other providers from being listed (SPEC 2.5 / 43).

DB-backed provider configuration (``ProviderConfig``, see
``app/services/provider_configs.py``) takes precedence over the env-var
settings in ``app/config.py``: if an *enabled* DB config exists for a
provider type, it is used and the corresponding env-var provider is not
also constructed. Env vars remain an optional local seed/default that is
only used when nothing has been configured at runtime yet. A single bad DB
config (e.g. it fails to decrypt) is caught and dropped so it degrades to
"not configured" rather than breaking every other provider.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models.db import ProviderConfig
from ..providers.base import CameraNotFoundError, CameraProvider, ProviderUnavailableError
from ..providers.dahua import DahuaEdgeProvider, DahuaEdgeSettings, DahuaProvider, DahuaSettings
from ..providers.eufy import EufyEdgeProvider, EufySettings
from ..providers.mock import PROVIDERS as MOCK_PROVIDERS
from ..providers.mock import MockCameraProvider
from . import provider_configs as provider_config_service

logger = logging.getLogger(__name__)


async def _enabled_db_config(provider_type: str) -> ProviderConfig | None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(ProviderConfig).where(
                ProviderConfig.provider_type == provider_type, ProviderConfig.enabled.is_(True)
            )
        )
        return result.scalars().first()


async def _configured_real_providers() -> list[CameraProvider]:
    providers: list[CameraProvider] = []

    try:
        dahua_config = await _enabled_db_config(provider_config_service.DAHUA)
    except Exception:  # noqa: BLE001 - a DB/decryption failure must not break other providers
        logger.exception("failed to load Dahua provider config from database")
        dahua_config = None
    if dahua_config is not None:
        try:
            if dahua_config.mode == provider_config_service.DAHUA_MODE_EDGE:
                providers.append(
                    DahuaEdgeProvider(provider_config_service.dahua_edge_settings_from_config(dahua_config))
                )
            else:
                providers.append(DahuaProvider(provider_config_service.dahua_settings_from_config(dahua_config)))
        except Exception:  # noqa: BLE001
            logger.exception("failed to build Dahua provider from stored config")
    elif settings.dahua_mode == provider_config_service.DAHUA_MODE_EDGE and settings.dahua_edge_url:
        providers.append(
            DahuaEdgeProvider(
                DahuaEdgeSettings(
                    base_url=settings.dahua_edge_url,
                    token=settings.dahua_edge_token,
                    timeout_seconds=settings.dahua_edge_timeout_seconds,
                    retries=settings.dahua_edge_retries,
                )
            )
        )
    elif settings.dahua_enabled:
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

    try:
        eufy_config = await _enabled_db_config(provider_config_service.EUFY)
    except Exception:  # noqa: BLE001
        logger.exception("failed to load Eufy provider config from database")
        eufy_config = None
    if eufy_config is not None:
        try:
            providers.append(EufyEdgeProvider(provider_config_service.eufy_settings_from_config(eufy_config)))
        except Exception:  # noqa: BLE001
            logger.exception("failed to build Eufy provider from stored config")
    elif settings.eufy_enabled:
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


async def all_providers() -> list[CameraProvider]:
    return [*MOCK_PROVIDERS, *await _configured_real_providers()]


def mock_providers() -> list[MockCameraProvider]:
    return list(MOCK_PROVIDERS)


async def find_provider_for_camera(camera_id: str) -> CameraProvider | None:
    for provider in await all_providers():
        if provider.has_camera(camera_id):
            return provider
    return None


async def discover_all_cameras() -> list[dict]:
    """Return cameras from every healthy provider. A single failing
    provider is logged and skipped rather than raised, so the endpoint
    keeps serving cameras from the remaining providers."""
    cameras: list[dict] = []
    for provider in await all_providers():
        try:
            cameras.extend(await provider.discover_devices())
        except ProviderUnavailableError as exc:
            logger.warning("provider %s unavailable: %s", provider.id, exc)
    return cameras


async def get_camera_or_raise(camera_id: str) -> dict:
    provider = await find_provider_for_camera(camera_id)
    if provider is None:
        raise CameraNotFoundError(camera_id)
    cameras = await provider.discover_devices()
    for camera in cameras:
        if camera["id"] == camera_id:
            return camera
    raise CameraNotFoundError(camera_id)


async def get_all_provider_health() -> list[dict]:
    health: list[dict] = []
    for provider in await all_providers():
        try:
            health.append(await provider.get_health())
        except ProviderUnavailableError as exc:
            health.append(
                {
                    "provider_id": provider.id,
                    "status": "OFFLINE",
                    "message": str(exc),
                    "camera_count": 0,
                    "online_camera_count": 0,
                }
            )
    return health
