"""Camera zone CRUD (admin plane).

Zones are user-defined labelled regions in normalized image coordinates —
a rectangle, or a polygon drawn on a still from the camera. Validation
lives in the Pydantic schema; this module only persists and reads them back
and converts rows into the pipeline's :class:`~app.ai.zones.Zone`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from ..ai.zones import Zone
from ..models.db import CameraZone


class ZoneNameConflict(ValueError):
    """Another zone on the same camera already uses this name."""


async def zone_attribute(
    session: AsyncSession, camera_id: str, zone_name: str | None, column: ColumnElement
) -> object | None:
    """One column of the zone called ``zone_name`` on ``camera_id``.

    Zone names are *not* unique per camera - nothing in the schema or (until
    now) the admin plane stopped a household from having two zones called
    "driveway" on the same camera. Every enrichment stage used to read this
    with ``scalar_one_or_none()``, so a duplicate name raised
    ``MultipleResultsFound`` on live events and took down the whole stage:
    signals, notification priority and incident routing all silently failed
    and real alerts were lost.

    Ambiguous configuration must degrade to a stable answer, never to a lost
    alert, so resolve duplicates deterministically to the oldest matching
    zone instead of raising. ``create_zone``/``update_zone`` now reject new
    duplicates, but existing ones must keep working.
    """
    if not zone_name:
        return None
    result = await session.execute(
        select(column)
        .where(CameraZone.camera_id == camera_id, CameraZone.name == zone_name)
        .order_by(CameraZone.created_at.asc(), CameraZone.id.asc())
        .limit(1)
    )
    return result.scalars().first()


async def _name_taken(
    session: AsyncSession, camera_id: str, name: str, *, exclude_id: str | None = None
) -> bool:
    statement = select(CameraZone.id).where(
        CameraZone.camera_id == camera_id, CameraZone.name == name
    )
    if exclude_id is not None:
        statement = statement.where(CameraZone.id != exclude_id)
    return (await session.execute(statement.limit(1))).scalars().first() is not None


async def list_zones(session: AsyncSession, camera_id: str | None = None) -> list[CameraZone]:
    statement = select(CameraZone).order_by(CameraZone.camera_id, CameraZone.name)
    if camera_id is not None:
        statement = statement.where(CameraZone.camera_id == camera_id)
    result = await session.execute(statement)
    return list(result.scalars().all())


async def get_zone(session: AsyncSession, zone_id: str) -> CameraZone | None:
    return await session.get(CameraZone, zone_id)


async def create_zone(session: AsyncSession, camera_id: str, payload) -> CameraZone:
    if await _name_taken(session, camera_id, payload.name):
        raise ZoneNameConflict(f"camera already has a zone named {payload.name!r}")
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
        points=getattr(payload, "points", None) or None,
        dwell_seconds=getattr(payload, "dwell_seconds", None),
        alerts_enabled=payload.alerts_enabled,
        created_at=now,
        updated_at=now,
    )
    session.add(zone)
    await session.commit()
    await session.refresh(zone)
    _invalidate(camera_id)
    return zone


def _invalidate(camera_id: str) -> None:
    from . import scene_state

    scene_state.invalidate_zone_cache(camera_id)


async def update_zone(session: AsyncSession, zone: CameraZone, payload) -> CameraZone:
    points = getattr(payload, "points", None)
    new_name = getattr(payload, "name", None)
    if new_name is not None and new_name != zone.name and await _name_taken(
        session, zone.camera_id, new_name, exclude_id=zone.id
    ):
        raise ZoneNameConflict(f"camera already has a zone named {new_name!r}")
    for field in ("name", "kind", "x1", "y1", "x2", "y2", "dwell_seconds"):
        value = getattr(payload, field, None)
        if value is not None:
            setattr(zone, field, value)
    if payload.alerts_enabled is not None:
        zone.alerts_enabled = payload.alerts_enabled
    if points is not None:
        # An explicit empty list turns a polygon back into its rectangle.
        zone.points = points or None
    if zone.x1 >= zone.x2 or zone.y1 >= zone.y2:
        raise ValueError("zone must satisfy x1 < x2 and y1 < y2")
    zone.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(zone)
    _invalidate(zone.camera_id)
    return zone


async def delete_zone(session: AsyncSession, zone: CameraZone) -> None:
    await session.delete(zone)
    await session.commit()
    _invalidate(zone.camera_id)


async def zones_for_camera(session: AsyncSession, camera_id: str) -> list[Zone]:
    """Return pipeline-ready zones; malformed rows are skipped, not fatal."""
    zones: list[Zone] = []
    for row in await list_zones(session, camera_id):
        try:
            zones.append(Zone.from_row(row))
        except ValueError:
            continue
    return zones
