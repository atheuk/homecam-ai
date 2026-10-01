"""Background retention purge.

Off by default (``retention_enabled``), and dry-run by default
(``retention_dry_run``) even once enabled: the first thing a deployment
should see is a log line saying exactly what the policy *would* delete.
Turning the dry run off is a separate, deliberate decision.

Safe on every replica - see :mod:`app.services.retention` for why the
statements are idempotent and bounded.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from ..config import settings
from ..db import SessionLocal
from . import retention

logger = logging.getLogger(__name__)


class RetentionScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None or not settings.retention_enabled:
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
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
                logger.exception("retention purge tick failed")
            await asyncio.sleep(settings.retention_interval_seconds)

    async def tick(self, now: datetime | None = None) -> dict:
        async with SessionLocal() as session:
            report = await retention.run(session, now=now)
        payload = report.to_dict()
        logger.info(
            "retention pass (%s): %s%s",
            "dry run" if report.dry_run else "purge",
            payload["counts"],
            " [truncated, continuing next run]" if report.truncated else "",
        )
        return payload


retention_scheduler = RetentionScheduler()
