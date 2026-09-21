"""Admin API for runtime Dahua/Eufy provider configuration.

Auth note: this phase treats any authenticated user as admin (HomeCam is a
single-user local system, see ``app/auth`` and SPEC section 27). This is a
deliberate scope limitation, not silent privilege expansion: if HomeCam ever
supports multiple accounts, these routes must gain a real role check before
that happens.

Every response here is built from :func:`app.services.provider_configs.to_out`
which never includes ``secret_encrypted``/plaintext secrets, so config
list/get responses are safe to expose to the frontend as-is.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..db import get_db
from ..models.db import ProviderConfig, User
from ..schemas_admin import (
    DahuaProviderConfigIn,
    DahuaProviderConfigUpdate,
    DahuaTestIn,
    EufyProviderConfigIn,
    EufyProviderConfigUpdate,
    EufyTestIn,
    ProviderConfigOut,
    ProviderEnabledIn,
    ProviderTestResult,
)
from ..services import provider_configs as service

router = APIRouter(prefix="/api/v1/admin/providers", tags=["admin"])


async def _get_config_or_404(session: AsyncSession, config_id: str, provider_type: str | None = None) -> ProviderConfig:
    config = await service.get_config(session, config_id)
    if config is None or (provider_type is not None and config.provider_type != provider_type):
        raise HTTPException(404, "Provider configuration not found")
    return config


@router.get("", response_model=list[ProviderConfigOut])
async def list_provider_configs(
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    configs = await service.list_configs(session)
    return [service.to_out(config) for config in configs]


@router.post("/dahua", response_model=ProviderConfigOut, status_code=201)
async def create_dahua_config(
    payload: DahuaProviderConfigIn,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    config = await service.create_dahua(session, payload)
    return service.to_out(config)


@router.put("/dahua/{config_id}", response_model=ProviderConfigOut)
async def update_dahua_config(
    config_id: str,
    payload: DahuaProviderConfigUpdate,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    config = await _get_config_or_404(session, config_id, service.DAHUA)
    config = await service.update_dahua(session, config, payload)
    return service.to_out(config)


@router.post("/eufy", response_model=ProviderConfigOut, status_code=201)
async def create_eufy_config(
    payload: EufyProviderConfigIn,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    config = await service.create_eufy(session, payload)
    return service.to_out(config)


@router.put("/eufy/{config_id}", response_model=ProviderConfigOut)
async def update_eufy_config(
    config_id: str,
    payload: EufyProviderConfigUpdate,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    config = await _get_config_or_404(session, config_id, service.EUFY)
    config = await service.update_eufy(session, config, payload)
    return service.to_out(config)


@router.post("/{config_id}/enabled", response_model=ProviderConfigOut)
async def set_provider_config_enabled(
    config_id: str,
    payload: ProviderEnabledIn,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    config = await _get_config_or_404(session, config_id)
    config = await service.set_enabled(session, config, payload.enabled)
    return service.to_out(config)


@router.delete("/{config_id}", status_code=204)
async def delete_provider_config(
    config_id: str,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    config = await _get_config_or_404(session, config_id)
    await service.delete_config(session, config)
    return None


@router.post("/dahua/test", response_model=ProviderTestResult)
async def test_dahua_config(
    payload: DahuaTestIn,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    try:
        settings, config = await service.resolve_dahua_test_settings(session, payload)
    except LookupError as exc:
        raise HTTPException(404, "Provider configuration not found") from exc
    result = await service.test_dahua_connection(settings)
    await service.record_test_result(session, config, result)
    return result


@router.post("/eufy/test", response_model=ProviderTestResult)
async def test_eufy_config(
    payload: EufyTestIn,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    try:
        settings, config = await service.resolve_eufy_test_settings(session, payload)
    except LookupError as exc:
        raise HTTPException(404, "Provider configuration not found") from exc
    result = await service.test_eufy_connection(settings)
    await service.record_test_result(session, config, result)
    return result
