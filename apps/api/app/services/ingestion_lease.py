"""Per-camera ingestion leader lease shared through the database.

Each API replica keeps its own in-memory scene cache (vehicle tracks, bin and
mailbox state) and writes it back unconditionally. If two replicas ingest the
same camera, one can overwrite the other's newer state with its stale copy.
So only one replica, the lease holder, ingests a given camera. The others
stand by and take over once the holder stops renewing.

A lease row is taken or renewed with one conditional ``UPDATE`` that only
matches when this replica already holds it or the previous lease has expired.
A camera without a row is taken with an ``INSERT`` on the primary key. The
database serializes both, so at most one replica wins.

The holder renews once half the TTL has passed, so a healthy holder never
lets the lease lapse. Clock skew between replicas of up to ``ttl / 2`` is
tolerated. Expiry times are epoch seconds, compared as plain floats on SQLite
and Postgres alike.
"""
from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import case, delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models.db import IngestionLease

logger = logging.getLogger(__name__)

_now = time.time


def default_replica_id() -> str:
    if settings.ingestion_replica_id:
        return settings.ingestion_replica_id
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"[:128]


@dataclass(frozen=True)
class LeaseStatus:
    held: bool
    # ``held`` differs from the previous call for this camera: the lease was
    # just acquired (or taken over), or just lost.
    changed: bool


class LeaseKeeper:
    """One replica's view of the leases it holds."""

    def __init__(self, holder: str) -> None:
        self.holder = holder
        self._expires: dict[str, float] = {}

    def held(self, camera_id: str, now: float | None = None) -> bool:
        now = _now() if now is None else now
        return self._expires.get(camera_id, 0.0) > now

    def reset(self) -> None:
        self._expires.clear()

    async def ensure(
        self, session: AsyncSession, camera_id: str, now: float | None = None
    ) -> LeaseStatus:
        """Hold (take, renew or keep) the lease for ``camera_id`` if possible."""
        now = _now() if now is None else now
        ttl = settings.ingestion_lease_ttl_seconds
        # Whether we believed we held it, even if it has since lapsed: losing
        # it must still be reported so the caller drops its cached state.
        was_held = camera_id in self._expires
        if self._expires.get(camera_id, 0.0) - now > ttl / 2:
            return LeaseStatus(True, False)
        held = await self._claim(session, camera_id, now, ttl)
        if held:
            self._expires[camera_id] = now + ttl
        else:
            self._expires.pop(camera_id, None)
        if held != was_held:
            logger.info(
                "ingestion lease %s: %s %s",
                camera_id,
                "acquired by" if held else "lost by",
                self.holder,
            )
        return LeaseStatus(held, held != was_held)

    async def _claim(self, session: AsyncSession, camera_id: str, now: float, ttl: float) -> bool:
        result = await session.execute(
            update(IngestionLease)
            .where(
                IngestionLease.camera_id == camera_id,
                (IngestionLease.holder == self.holder) | (IngestionLease.expires_at <= now),
            )
            .values(
                acquired_at=case(
                    (IngestionLease.holder == self.holder, IngestionLease.acquired_at), else_=now
                ),
                holder=self.holder,
                expires_at=now + ttl,
            )
        )
        if (result.rowcount or 0) == 1:
            await session.commit()
            return True
        if await session.get(IngestionLease, camera_id) is not None:
            await session.rollback()
            return False
        session.add(
            IngestionLease(camera_id=camera_id, holder=self.holder, expires_at=now + ttl, acquired_at=now)
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return False
        return True

    async def release_all(self, session: AsyncSession) -> None:
        """Give up every lease now (clean shutdown) so a standby takes over
        immediately instead of after the TTL."""
        await session.execute(delete(IngestionLease).where(IngestionLease.holder == self.holder))
        await session.commit()
        self._expires.clear()


keeper = LeaseKeeper(default_replica_id())
