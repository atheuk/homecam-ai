"""Camera zone CRUD (admin plane).

Zones are user-defined labelled rectangles in normalized image coordinates.
Validation lives in the Pydantic schema; this module only persists and reads
them back and converts rows into the pipeline's :class:`~app.ai.zones.Zone`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.zones import Zone
from ..models.db import CameraZone


async def list_zones(session: AsyncSession, camera_id: str | None = None) -> list[CameraZone]:
    statement = select(CameraZone).order_by(CameraZone.camera_id, CameraZone.name)
    if camera_id is not None:
        statement = statement.where(CameraZone.camera_id == camera_id)
    result = await session.execute(statement)
    return list(result.scalars().all())


async def get_zone(session: AsyncSession, zone_id: str) -> CameraZone | None:
    return await session.get(CameraZone, zone_id)


async def create_zone(session: AsyncSession, camera_id: str, payload) -> CameraZone:
    now = datetime.now(timezone.utc)
    zone = CameraZone(
        id=str(uuid.uuid4()),
        camera_id=camera_id,
        name=payload.name,
        kind=payload.kind,
        x1=payload.x1,
        y1=payload.y1,
        x2=payload.x2,
        y2=payload.y2,
        created_at=now,
        updated_at=now,
    )
    session.add(zone)
    await session.commit()
    await session.refresh(zone)
    return zone


async def update_zone(session: AsyncSession, zone: CameraZone, payload) -> CameraZone:
    for field in ("name", "kind", "x1", "y1", "x2", "y2"):
        value = getattr(payload, field, None)
        if value is not None:
            setattr(zone, field, value)
    if zone.x1 >= zone.x2 or zone.y1 >= zone.y2:
        raise ValueError("zone must satisfy x1 < x2 and y1 < y2")
    zone.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(zone)
    return zone


async def delete_zone(session: AsyncSession, zone: CameraZone) -> None:
    await session.delete(zone)
    await session.commit()


async def zones_for_camera(session: AsyncSession, camera_id: str) -> list[Zone]:
    """Return pipeline-ready zones; malformed rows are skipped, not fatal."""
    zones: list[Zone] = []
    for row in await list_zones(session, camera_id):
        try:
            zones.append(Zone.from_row(row))
        except ValueError:
            continue
    return zones
