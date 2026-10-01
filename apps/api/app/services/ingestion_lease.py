"""Per-camera ingestion leader lease shared through the database.

Each API replica keeps its own in-memory scene cache (vehicle tracks, bin and
mailbox state) and writes it back after every frame. If two replicas ingested
the same camera, one could overwrite the other's newer state with its stale
copy. So only one replica, the lease holder, ingests a given camera. The
others stand by and take over once the holder stops renewing.

**Taking/renewing.** One conditional ``UPDATE`` matches only when this
replica still holds an unexpired lease (a renewal) or the lease has expired
(a takeover). A camera without a row is taken with an ``INSERT`` on the
primary key. The database serializes both, so at most one replica wins.

**Database time.** Every expiry comparison and every new ``expires_at`` uses
the database clock (``clock_timestamp()`` on Postgres, ``julianday('now')``
on SQLite), never the replica's own clock, so skew between replicas cannot
create two holders. Locally a replica only uses the monotonic clock to
measure how long ago it renewed (renew every TTL/2, assume lost after TTL).

**Fencing.** Every acquisition increments the lease ``epoch``; a renewal keeps
it. A frame that started under epoch *E* may be slow: detection and Foundry
checks can take longer than the TTL. So every scene-state save and every
scene-event insert first runs :func:`fence`. That ``UPDATE`` only matches
while this replica still holds epoch *E* unexpired by database time, and it
row-locks the lease until the write commits. A takeover therefore either
happens before the write, which is then discarded, or waits until it has
committed. Expiry times are epoch seconds stored as floats.
"""
from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import Float, case, cast, delete, extract, func, insert, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models.db import IngestionLease

logger = logging.getLogger(__name__)

# Local elapsed-time measurement only (patched in tests). Wall-clock time
# from this process is never compared with the database.
_monotonic = time.monotonic


class LeaseLost(Exception):
    """The fenced write's lease is no longer held: the write was discarded."""


def default_replica_id() -> str:
    if settings.ingestion_replica_id:
        return settings.ingestion_replica_id
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"[:128]


def db_now(session: AsyncSession):
    """SQL expression for the database's current time in epoch seconds."""
    if session.bind.dialect.name == "postgresql":
        return cast(extract("epoch", func.clock_timestamp()), Float)
    return (func.julianday("now") - literal(2440587.5)) * literal(86400.0)


@dataclass(frozen=True)
class LeaseToken:
    """Proof of holding ``camera_id`` at ``epoch``; checked by :func:`fence`."""

    camera_id: str
    holder: str
    epoch: int


@dataclass(frozen=True)
class LeaseStatus:
    held: bool
    # The lease was just gained, lost, or re-acquired under a new epoch.
    # The caller must drop any state cached under the previous epoch.
    changed: bool
    token: LeaseToken | None = None


@dataclass
class _Held:
    epoch: int
    renewed_at: float  # monotonic time the claim started


class LeaseKeeper:
    """One replica's view of the leases it holds."""

    def __init__(self, holder: str) -> None:
        self.holder = holder
        self._held: dict[str, _Held] = {}

    def held(self, camera_id: str) -> bool:
        """Whether this replica believes it holds ``camera_id``. A belief
        only: writes must still pass :func:`fence`."""
        entry = self._held.get(camera_id)
        return entry is not None and _monotonic() - entry.renewed_at < settings.ingestion_lease_ttl_seconds

    def token(self, camera_id: str) -> LeaseToken | None:
        entry = self._held.get(camera_id)
        if entry is None or not self.held(camera_id):
            return None
        return LeaseToken(camera_id, self.holder, entry.epoch)

    def reset(self) -> None:
        self._held.clear()

    async def ensure(self, session: AsyncSession, camera_id: str) -> LeaseStatus:
        """Hold (take, renew or keep) the lease for ``camera_id`` if possible."""
        ttl = settings.ingestion_lease_ttl_seconds
        previous = self._held.get(camera_id)
        started = _monotonic()
        if previous is not None and started - previous.renewed_at < ttl / 2:
            return LeaseStatus(True, False, LeaseToken(camera_id, self.holder, previous.epoch))
        epoch = await self._claim(session, camera_id, ttl)
        if epoch is None:
            self._held.pop(camera_id, None)
            if previous is not None:
                logger.info("ingestion lease %s: lost by %s", camera_id, self.holder)
            return LeaseStatus(False, previous is not None)
        self._held[camera_id] = _Held(epoch, started)
        changed = previous is None or previous.epoch != epoch
        if changed:
            logger.info("ingestion lease %s: acquired by %s (epoch %d)", camera_id, self.holder, epoch)
        return LeaseStatus(True, changed, LeaseToken(camera_id, self.holder, epoch))

    async def _claim(self, session: AsyncSession, camera_id: str, ttl: float) -> int | None:
        """The epoch now held for ``camera_id``, or ``None`` if another
        replica holds it."""
        now = db_now(session)
        renewal = (IngestionLease.holder == self.holder) & (IngestionLease.expires_at > now)
        result = await session.execute(
            update(IngestionLease)
            .where(IngestionLease.camera_id == camera_id, renewal | (IngestionLease.expires_at <= now))
            .values(
                epoch=case((renewal, IngestionLease.epoch), else_=IngestionLease.epoch + 1),
                acquired_at=case((renewal, IngestionLease.acquired_at), else_=now),
                holder=self.holder,
                expires_at=now + ttl,
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) == 1:
            # Still inside our transaction: the row is ours until commit.
            epoch = (
                await session.execute(select(IngestionLease.epoch).where(IngestionLease.camera_id == camera_id))
            ).scalar_one()
            await session.commit()
            return epoch
        exists = (
            await session.execute(select(IngestionLease.camera_id).where(IngestionLease.camera_id == camera_id))
        ).first()
        if exists is not None:
            await session.rollback()
            return None
        try:
            await session.execute(
                insert(IngestionLease).values(
                    camera_id=camera_id, holder=self.holder, epoch=1, expires_at=now + ttl, acquired_at=now
                )
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return None
        return 1

    async def release_all(self, session: AsyncSession) -> None:
        """Give up every lease now (clean shutdown) so a standby takes over
        immediately instead of after the TTL."""
        await session.execute(delete(IngestionLease).where(IngestionLease.holder == self.holder))
        await session.commit()
        self._held.clear()


async def fence(session: AsyncSession, token: LeaseToken) -> bool:
    """Inside the caller's transaction: whether ``token`` still holds its
    lease by database time. On success the lease row stays locked until the
    caller commits, so it cannot be taken over mid-write."""
    result = await session.execute(
        update(IngestionLease)
        .where(
            IngestionLease.camera_id == token.camera_id,
            IngestionLease.holder == token.holder,
            IngestionLease.epoch == token.epoch,
            IngestionLease.expires_at > db_now(session),
        )
        .values(holder=IngestionLease.holder)
        .execution_options(synchronize_session=False)
    )
    return (result.rowcount or 0) == 1


keeper = LeaseKeeper(default_replica_id())
