"""The log shown in the web UI (Go ``WebScreenLog``): the last ``log.entries.ram`` messages in memory,
with error and warning counters, cleared from the log page."""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import override


@dataclass(frozen=True, slots=True)
class LogEntry:
    time: datetime
    level: str  # INFO, WARNING, ERROR, ...
    message: str


class LogBuffer(logging.Handler):
    def __init__(self, size: int = 500) -> None:
        super().__init__(logging.INFO)
        self._lock_entries = threading.Lock()
        self._entries: deque[LogEntry] = deque(maxlen=max(size, 1))
        self.errors = 0
        self.warnings = 0

    def resize(self, size: int) -> None:
        with self._lock_entries:
            self._entries = deque(self._entries, maxlen=max(size, 1))

    @override
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                message += f": {record.exc_info[1]}"
        except Exception:
            message = str(record.msg)
        entry = LogEntry(datetime.fromtimestamp(record.created), record.levelname, message)
        with self._lock_entries:
            self._entries.append(entry)
            if record.levelno >= logging.ERROR:
                self.errors += 1
            elif record.levelno >= logging.WARNING:
                self.warnings += 1

    def entries(self) -> list[LogEntry]:
        with self._lock_entries:
            return list(self._entries)

    def clear(self) -> None:
        with self._lock_entries:
            self._entries.clear()
            self.errors = self.warnings = 0


__all__ = ["LogBuffer", "LogEntry"]
