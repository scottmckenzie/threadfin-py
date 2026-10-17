"""Backup-channel failover detection: notices a player asking for the same stream again and again.

``/stream/<id>`` answers with an HTTP redirect to the provider, so this server cannot see that the
redirected stream failed. Players do notice: they retry a dead stream every few seconds. When one
client asks for the same stream ``requests`` times within ``seconds`` seconds, the stream is treated
as failed and the client is moved on to the next backup channel; when no backup is left the burst is
reported once as ``stuck``. A client that stays quiet for ``seconds`` seconds goes back to the main stream.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Decision:
    step: int  # 0 = main stream, n = the n-th usable backup
    switched: bool  # this request moved the client on
    stuck: bool  # this request completed a burst with nowhere left to go (first time per burst)


@dataclass(slots=True)
class _Entry:
    last: float
    step: int = 0
    warned: bool = False
    times: list[float] = field(default_factory=list[float])


class Failover:
    """In-memory request tracker, one entry per (client, stream id)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[tuple[str, str], _Entry] = {}

    def request(self, client: str, stream_id: str, *, backups: int, requests: int, seconds: int) -> Decision:
        """Record one request and say which stream (0 = main, n = n-th backup) the client gets."""
        if requests < 2:
            return Decision(0, False, False)
        seconds = max(seconds, 1)
        with self._lock:
            now = self._clock()
            self._state = {k: e for k, e in self._state.items() if now - e.last < seconds}
            entry = self._state.setdefault((client, stream_id), _Entry(last=now))
            entry.last = now
            entry.step = min(entry.step, backups)
            entry.times = [t for t in entry.times if now - t <= seconds]
            entry.times.append(now)
            if len(entry.times) < requests:
                return Decision(entry.step, False, False)
            entry.times.clear()
            if entry.step < backups:
                entry.step += 1
                entry.warned = False
                return Decision(entry.step, True, False)
            stuck = not entry.warned
            entry.warned = True
            return Decision(entry.step, False, stuck)
