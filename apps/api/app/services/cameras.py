"""Camera persistence (SPEC section 31 / 6).

Cameras discovered from providers are upserted into the database so their
current state (online/offline/degraded, battery level, capabilities) is
durable and queryable, rather than living only inside the in-process mock
provider objects.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import Camera


async def upsert_camera(session: AsyncSession, camera: dict) -> Camera:
    existing = await session.get(Camera, camera["id"])
    if existing is None:
        existing = Camera(id=camera["id"])
        session.add(existing)
    existing.provider_id = camera["provider_id"]
    existing.name = camera["name"]
    existing.type = camera["type"]
    existing.model = camera["model"]
    existing.online = camera["online"]
    existing.status = camera["status"]
    existing.battery_level = camera["battery_level"]
    existing.capabilities = camera["capabilities"]
    await session.commit()
    await session.refresh(existing)
    return existing


async def sync_cameras(session: AsyncSession, cameras: list[dict]) -> list[Camera]:
    return [await upsert_camera(session, camera) for camera in cameras]


async def list_cameras(session: AsyncSession) -> list[Camera]:
    result = await session.execute(select(Camera).order_by(Camera.name))
    return list(result.scalars().all())


async def get_camera(session: AsyncSession, camera_id: str) -> Camera | None:
    return await session.get(Camera, camera_id)
