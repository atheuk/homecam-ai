"""Opt-in background digest generator.

Off by default (``digest_scheduler_enabled``): the digest is always
available on demand from ``GET /api/v1/digest``, and a background timer is
only worth running when somebody wants yesterday's summary waiting for
them in the morning. Leaving it off also keeps tests and dev runs free of
a stray timer task.

Safe to run on every replica: :func:`app.services.digest.generate` is
idempotent at the database level (``daily_digests`` is keyed on the date),
so two replicas ticking at the same moment produce one row, not two.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..config import settings
from ..db import SessionLocal
from . import digest as digest_service

logger = logging.getLogger(__name__)


class DigestScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None or not settings.digest_scheduler_enabled:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown must not raise
            pass

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(settings.digest_scheduler_interval_seconds)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
                logger.exception("digest scheduler tick failed")

    async def tick(self, now: datetime | None = None) -> None:
        """Refresh today's digest and settle yesterday's."""
        now = now or datetime.now(timezone.utc)
        async with SessionLocal() as session:
            await digest_service.generate(session, now.date(), refresh=True)
            await digest_service.generate(session, (now - timedelta(days=1)).date())


digest_scheduler = DigestScheduler()
