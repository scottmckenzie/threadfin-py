"""Probe orchestration: caching, de-duplication and a concurrency limit.

IPTV providers usually cap simultaneous connections per account, and a probe
opens a connection to the stream. So probes run through a semaphore (default:
one at a time), identical in-flight probes are shared, and recent results are
served from storage instead of hitting the provider again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from .probe import run_ffprobe, validate_probe_url
from .probe_store import ProbeRecord, ProbeStore, utcnow


class ProbeService:
    def __init__(
        self,
        store: ProbeStore,
        *,
        ffprobe_path: str,
        user_agent: str = "",
        timeout: float = 20.0,
        max_concurrent: int = 1,
        ok_ttl: timedelta = timedelta(hours=24),
        failure_ttl: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = utcnow,
        allow_local: bool = False,
    ) -> None:
        self._store = store
        self._ffprobe = ffprobe_path
        self._user_agent = user_agent
        self._timeout = timeout
        self._ok_ttl = ok_ttl
        self._failure_ttl = failure_ttl
        self._clock = clock
        self._allow_local = allow_local
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._inflight: dict[str, asyncio.Task[ProbeRecord]] = {}

    @property
    def store(self) -> ProbeStore:
        return self._store

    def _is_fresh(self, record: ProbeRecord) -> bool:
        ttl = self._ok_ttl if record.ok else self._failure_ttl
        return record.age(self._clock()) < ttl.total_seconds()

    async def probe(self, url: str, *, channel_key: str | None = None, force: bool = False) -> ProbeRecord:
        """Return a probe result for ``url``: stored if fresh, otherwise a new probe.

        Failed probes are cached for a shorter time than successful ones so a
        dead stream is not hammered, but is retried soon. ``force`` always probes.
        Raises :class:`~threadfin.probe.InvalidProbeURLError` for unacceptable URLs.
        """
        url = validate_probe_url(url, allow_local=self._allow_local)

        if not force:
            cached = await asyncio.to_thread(self._store.latest, url)
            if cached is not None and self._is_fresh(cached):
                cached.cached = True
                return cached

        task = self._inflight.get(url)
        if task is None:
            task = asyncio.create_task(self._probe_and_store(url, channel_key))
            self._inflight[url] = task
            task.add_done_callback(lambda _task: self._inflight.pop(url, None))
        # shield: one waiter being cancelled must not kill the probe others are awaiting
        return await asyncio.shield(task)

    async def _probe_and_store(self, url: str, channel_key: str | None) -> ProbeRecord:
        async with self._semaphore:
            run = await run_ffprobe(
                url,
                ffprobe=self._ffprobe,
                timeout=self._timeout,
                user_agent=self._user_agent,
                allow_local=self._allow_local,
            )
        return await asyncio.to_thread(self._store.record, run, channel_key=channel_key, probed_at=self._clock())

    async def probe_many(
        self, items: Iterable[tuple[str, str | None]], *, force: bool = False
    ) -> list[ProbeRecord | BaseException]:
        """Probe several ``(url, channel_key)`` pairs; one bad URL does not stop the rest.

        Failures (e.g. an invalid URL) are returned in place of that item's record.
        """
        return await asyncio.gather(
            *(self.probe(url, channel_key=key, force=force) for url, key in items),
            return_exceptions=True,
        )
