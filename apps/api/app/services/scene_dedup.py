"""Database-atomic "at most once per window" claims for scene events.

Both API replicas run ingestion, and each keeps its own in-process scene
cache, so an in-memory "last emitted at" check cannot stop two replicas from
each emitting the same package removal. Before a deduplicated scene event is
persisted, the emitter must win :func:`claim` for its key. The claim is a
single conditional ``UPDATE`` (only succeeds when the previous claim is older
than the window) followed, for a never-seen key, by an ``INSERT`` on the
primary key. The database serializes both. On Postgres the second concurrent
``UPDATE`` re-evaluates its ``WHERE`` after the first commits, and a
concurrent ``INSERT`` hits the primary-key constraint. On SQLite the whole
database is write-locked. Exactly one caller wins per window.
"""
from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import SceneDedupClaim

logger = logging.getLogger(__name__)


async def claim(session: AsyncSession, key: str, now: float, window_seconds: float, event_id: str | None = None) -> bool:
    """Atomically claim ``key`` for ``window_seconds`` starting at ``now``.

    Returns ``True`` if this caller won and should emit the event, or
    ``False`` if another caller (on this or another replica) already
    claimed the key inside the window. Commits on success.
    """
    result = await session.execute(
        update(SceneDedupClaim)
        .where(SceneDedupClaim.key == key, SceneDedupClaim.last_at <= now - window_seconds)
        .values(last_at=now, event_id=event_id)
    )
    if (result.rowcount or 0) == 1:
        await session.commit()
        return True
    if await session.get(SceneDedupClaim, key) is not None:
        await session.rollback()
        return False
    session.add(SceneDedupClaim(key=key, last_at=now, event_id=event_id))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        logger.debug("scene dedup claim %s lost the insert race", key)
        return False
    return True
