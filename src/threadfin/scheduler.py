"""Scheduled updates (port of ``maintenance.go``).

The ``update`` setting lists local times as ``HHMM``. At each of them the service makes an
automatic backup, downloads every provider and rebuilds (``Service.scheduled_update``). A time that
comes up while a download or build is already running is skipped, as in Go.

Go checked once every 60 s plus the time the update took, so a time could be skipped, and listed
twice it ran twice. Here the loop wakes at each minute boundary and runs at most once per minute.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from .service import Service

log = logging.getLogger(__name__)

MINUTE_FORMAT = "%H%M"


def is_due(times: list[str], now: datetime) -> bool:
    return now.strftime(MINUTE_FORMAT) in times


def seconds_to_next_minute(now: datetime) -> float:
    following = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return (following - now).total_seconds()


class Scheduler:
    def __init__(
        self,
        service: Service,
        *,
        clock: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self.service = service
        self._clock = clock
        self._sleep = sleep
        self._last_minute: datetime | None = None

    async def tick(self, now: datetime) -> bool:
        """Run the scheduled update if one is due at ``now``; return whether it ran."""
        minute = now.replace(second=0, microsecond=0)
        if minute == self._last_minute or not is_due(self.service.settings.update, now):
            return False
        self._last_minute = minute
        if self.service.busy:
            log.info("scheduled update %s skipped: an update is already running", minute.strftime(MINUTE_FORMAT))
            return False
        log.info("scheduled update %s", minute.strftime(MINUTE_FORMAT))
        try:
            report = await self.service.scheduled_update()
        except Exception:
            log.exception("scheduled update failed")
            return True
        if not report.ok:
            log.warning("scheduled update failed: %s", report.error)
        return True

    async def run(self) -> None:
        """Check every minute until cancelled."""
        while True:
            await self.tick(self._clock())
            # A second into the minute, so a slightly early wake-up still lands in the right one.
            await self._sleep(seconds_to_next_minute(self._clock()) + 1)


__all__ = ["Scheduler", "is_due", "seconds_to_next_minute"]
