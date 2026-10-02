"""Background scheduler: poll on an interval; manual trigger support."""

from __future__ import annotations

import asyncio
import logging
import time

from .sync import SyncEngine

log = logging.getLogger("pihole_sync.scheduler")


class Scheduler:
    def __init__(self, engine: SyncEngine, interval_seconds: int):
        self.engine = engine
        self.interval = max(5, int(interval_seconds))
        self.manual_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.next_run_at: float = 0.0

    def request_sync(self) -> bool:
        """Trigger an immediate manual (forced) sync. False if already running."""
        if self.engine.running:
            return False
        self.manual_event.set()
        return True

    async def run(self) -> None:
        # Initial sync shortly after startup
        await asyncio.sleep(2)
        while not self.stop_event.is_set():
            started = time.time()
            reason = "manual" if self.manual_event.is_set() else "interval"
            forced = reason == "manual"
            self.manual_event.clear()
            try:
                await self.engine.run_cycle(reason=reason, force=forced)
            except Exception:  # noqa: BLE001 - never kill the scheduler
                log.exception("Sync cycle crashed")
            elapsed = time.time() - started
            sleep_for = max(5.0, self.interval - elapsed)
            self.next_run_at = time.time() + sleep_for
            try:
                await asyncio.wait_for(self.manual_event.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return

    async def stop(self) -> None:
        self.stop_event.set()
        self.manual_event.set()