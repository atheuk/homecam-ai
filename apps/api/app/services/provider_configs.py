"""Runtime provider configuration CRUD + connection testing (admin plane).

This is the DB-backed counterpart to the env-var Dahua/Eufy settings in
``app/config.py``. Enabling a config here takes precedence over the process
env vars (see ``app/services/provider_registry.py``); env vars remain only
an optional local seed/default.

Enforcement rule: at most one *enabled* config exists per provider type at a
time (this is a single-NVR / single-adapter local admin plane). Enabling a
config automatically disables any other enabled config of the same type.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import SecretDecryptionError, decrypt_secret, encrypt_secret
from ..models.db import ProviderConfig
from ..providers.base import ProviderUnavailableError
from ..providers.dahua import DahuaProvider, DahuaSettings
from ..providers.eufy import EufyEdgeProvider, EufySettings
from ..schemas_admin import (
    DahuaProviderConfigIn,
    DahuaProviderConfigUpdate,
    DahuaTestIn,
    EufyProviderConfigIn,
    EufyProviderConfigUpdate,
    EufyTestIn,
    ProviderConfigOut,
    ProviderTestResult,
)

DAHUA = "dahua"
EUFY = "eufy"

# The "test connection" action is a quick interactive admin probe, not a
# long-running discovery loop: use a short timeout and no retries so a
# misconfigured/unreachable host fails back to the UI quickly instead of
# blocking on the longer defaults used for steady-state provider polling.
TEST_TIMEOUT_SECONDS = 3.0
TEST_RETRIES = 0


def to_out(config: ProviderConfig) -> ProviderConfigOut:
    return ProviderConfigOut(
        id=config.id,
        provider_type=config.provider_type,
        name=config.name,
        enabled=config.enabled,
        scheme=config.scheme,
        host=config.host,
        port=config.port,
        username=config.username,
        channels=config.channels,
        adapter_url=config.adapter_url,
        has_secret=bool(config.secret_encrypted),
        last_test_status=config.last_test_status,
        last_test_message=config.last_test_message,
        last_test_at=config.last_test_at,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


async def list_configs(session: AsyncSession) -> list[ProviderConfig]:
    result = await session.execute(select(ProviderConfig).order_by(ProviderConfig.created_at))
    return list(result.scalars().all())


async def get_config(session: AsyncSession, config_id: str) -> ProviderConfig | None:
    return await session.get(ProviderConfig, config_id)


async def _disable_other_enabled(session: AsyncSession, provider_type: str, keep_id: str | None) -> None:
    result = await session.execute(
        select(ProviderConfig).where(ProviderConfig.provider_type == provider_type, ProviderConfig.enabled.is_(True))
    )
    for other in result.scalars().all():
        if other.id != keep_id:
            other.enabled = False
            other.updated_at = datetime.now(timezone.utc)


async def create_dahua(session: AsyncSession, payload: DahuaProviderConfigIn) -> ProviderConfig:
    now = datetime.now(timezone.utc)
    config = ProviderConfig(
        id=str(uuid.uuid4()),
        provider_type=DAHUA,
        name=payload.name,
        enabled=payload.enabled,
        scheme=payload.scheme,
        host=payload.host,
        port=payload.port,
        username=payload.username,
        channels=payload.channels,
        secret_encrypted=encrypt_secret(payload.password) if payload.password else None,
        created_at=now,
        updated_at=now,
    )
    session.add(config)
    if payload.enabled:
        await session.flush()
        await _disable_other_enabled(session, DAHUA, config.id)
    await session.commit()
    await session.refresh(config)
    return config


async def update_dahua(session: AsyncSession, config: ProviderConfig, payload: DahuaProviderConfigUpdate) -> ProviderConfig:
    if payload.name is not None:
        config.name = payload.name
    if payload.scheme is not None:
        config.scheme = payload.scheme
    if payload.host is not None:
        config.host = payload.host
    if payload.port is not None:
        config.port = payload.port
    if payload.username is not None:
        config.username = payload.username
    if payload.channels is not None:
        config.channels = payload.channels
    if payload.password:
        config.secret_encrypted = encrypt_secret(payload.password)
    if payload.enabled is not None:
        config.enabled = payload.enabled
    config.updated_at = datetime.now(timezone.utc)
    if config.enabled:
        await _disable_other_enabled(session, DAHUA, config.id)
    await session.commit()
    await session.refresh(config)
    return config


async def create_eufy(session: AsyncSession, payload: EufyProviderConfigIn) -> ProviderConfig:
    now = datetime.now(timezone.utc)
    config = ProviderConfig(
        id=str(uuid.uuid4()),
        provider_type=EUFY,
        name=payload.name,
        enabled=payload.enabled,
        adapter_url=payload.adapter_url,
        secret_encrypted=encrypt_secret(payload.adapter_token) if payload.adapter_token else None,
        created_at=now,
        updated_at=now,
    )
    session.add(config)
    if payload.enabled:
        await session.flush()
        await _disable_other_enabled(session, EUFY, config.id)
    await session.commit()
    await session.refresh(config)
    return config


async def update_eufy(session: AsyncSession, config: ProviderConfig, payload: EufyProviderConfigUpdate) -> ProviderConfig:
    if payload.name is not None:
        config.name = payload.name
    if payload.adapter_url is not None:
        config.adapter_url = payload.adapter_url
    if payload.adapter_token:
        config.secret_encrypted = encrypt_secret(payload.adapter_token)
    if payload.enabled is not None:
        config.enabled = payload.enabled
    config.updated_at = datetime.now(timezone.utc)
    if config.enabled:
        await _disable_other_enabled(session, EUFY, config.id)
    await session.commit()
    await session.refresh(config)
    return config


async def set_enabled(session: AsyncSession, config: ProviderConfig, enabled: bool) -> ProviderConfig:
    config.enabled = enabled
    config.updated_at = datetime.now(timezone.utc)
    if enabled:
        await _disable_other_enabled(session, config.provider_type, config.id)
    await session.commit()
    await session.refresh(config)
    return config


async def delete_config(session: AsyncSession, config: ProviderConfig) -> None:
    await session.delete(config)
    await session.commit()


def _decrypt_or_none(secret_encrypted: str | None) -> str | None:
    if not secret_encrypted:
        return None
    try:
        return decrypt_secret(secret_encrypted)
    except SecretDecryptionError:
        return None


def dahua_settings_from_config(config: ProviderConfig) -> DahuaSettings:
    return DahuaSettings(
        scheme=config.scheme or "http",
        host=config.host,
        port=config.port or 80,
        username=config.username,
        password=_decrypt_or_none(config.secret_encrypted),
        channels=config.channels or "",
    )


def eufy_settings_from_config(config: ProviderConfig) -> EufySettings:
    return EufySettings(
        adapter_url=config.adapter_url,
        adapter_token=_decrypt_or_none(config.secret_encrypted),
    )


async def resolve_dahua_test_settings(session: AsyncSession, payload: DahuaTestIn) -> tuple[DahuaSettings, ProviderConfig | None]:
    config: ProviderConfig | None = None
    base = DahuaSettings()
    if payload.config_id:
        config = await get_config(session, payload.config_id)
        if config is None or config.provider_type != DAHUA:
            raise LookupError(payload.config_id)
        base = dahua_settings_from_config(config)
    resolved_password = payload.password if payload.password is not None else base.password
    return (
        DahuaSettings(
            scheme=payload.scheme or base.scheme,
            host=payload.host if payload.host is not None else base.host,
            port=payload.port if payload.port is not None else base.port,
            username=payload.username if payload.username is not None else base.username,
            password=resolved_password,
            channels=payload.channels if payload.channels is not None else base.channels,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            retries=TEST_RETRIES,
        ),
        config,
    )


async def resolve_eufy_test_settings(session: AsyncSession, payload: EufyTestIn) -> tuple[EufySettings, ProviderConfig | None]:
    config: ProviderConfig | None = None
    base = EufySettings()
    if payload.config_id:
        config = await get_config(session, payload.config_id)
        if config is None or config.provider_type != EUFY:
            raise LookupError(payload.config_id)
        base = eufy_settings_from_config(config)
    return (
        EufySettings(
            adapter_url=payload.adapter_url if payload.adapter_url is not None else base.adapter_url,
            adapter_token=payload.adapter_token if payload.adapter_token is not None else base.adapter_token,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            retries=TEST_RETRIES,
        ),
        config,
    )


async def _run_health_check(provider) -> ProviderTestResult:
    try:
        health = await provider.get_health()
    except ProviderUnavailableError as exc:
        return ProviderTestResult(success=False, status="OFFLINE", message=str(exc))
    success = health["status"] == "ONLINE"
    return ProviderTestResult(success=success, status=health["status"], message=health["message"])


async def test_dahua_connection(settings: DahuaSettings) -> ProviderTestResult:
    return await _run_health_check(DahuaProvider(settings))


async def test_eufy_connection(settings: EufySettings) -> ProviderTestResult:
    return await _run_health_check(EufyEdgeProvider(settings))


async def record_test_result(session: AsyncSession, config: ProviderConfig | None, result: ProviderTestResult) -> None:
    if config is None:
        return
    config.last_test_status = result.status
    config.last_test_message = result.message
    config.last_test_at = datetime.now(timezone.utc)
    await session.commit()
