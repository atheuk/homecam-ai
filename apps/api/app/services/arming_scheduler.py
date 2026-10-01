"""Background applier for automatic arming schedules.

Thin on purpose: all of the logic lives in
:mod:`app.services.arming_schedules`, and this is the timer that calls it.
Safe to run on every replica - the transition itself is claimed with a
conditional UPDATE, so a tick that loses the race is a no-op rather than a
duplicate mode change.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from ..config import settings
from ..db import SessionLocal
from . import arming_schedules

logger = logging.getLogger(__name__)


class ArmingScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None or not settings.arming_scheduler_enabled:
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
                await asyncio.sleep(settings.arming_scheduler_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
                logger.exception("arming scheduler tick failed")
                await asyncio.sleep(settings.arming_scheduler_interval_seconds)

    async def tick(self, now: datetime | None = None) -> dict | None:
        async with SessionLocal() as session:
            applied = await arming_schedules.apply_due_transition(session, now)
        if applied and applied["changed"]:
            logger.info(
                "arming schedule set mode %s (was %s) at boundary %s",
                applied["mode"],
                applied["previous_mode"],
                applied["boundary"].isoformat(),
            )
        return applied


arming_scheduler = ArmingScheduler()
