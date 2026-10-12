"""SQLite storage for channel-probe results (new in the Python port).

The Go app threw probe results away after showing them. Here every probe is
kept as a row so the UI can show the last result instantly, how a stream has
changed over time, and which streams have been failing.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .probe import ProbeDetails, ProbeRun, ProbeSummary

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE probe_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    url              TEXT    NOT NULL,
    channel_key      TEXT,
    probed_at        TEXT    NOT NULL,          -- UTC, ISO 8601
    ok               INTEGER NOT NULL,
    error            TEXT,
    duration_ms      INTEGER NOT NULL,
    resolution       TEXT    NOT NULL DEFAULT '',
    frame_rate       TEXT    NOT NULL DEFAULT '',
    audio_channels   TEXT    NOT NULL DEFAULT '',
    width            INTEGER,
    height           INTEGER,
    video_codec      TEXT,
    video_bit_rate   INTEGER,
    audio_codec      TEXT,
    audio_bit_rate   INTEGER,
    audio_sample_rate INTEGER,
    audio_channel_count INTEGER,
    stream_count     INTEGER NOT NULL DEFAULT 0,
    raw_json         TEXT
);
CREATE INDEX idx_probe_url ON probe_results (url, id DESC);
CREATE INDEX idx_probe_channel ON probe_results (channel_key, id DESC)
    WHERE channel_key IS NOT NULL;
CREATE INDEX idx_probe_time ON probe_results (probed_at);
"""


@dataclass(slots=True)
class ProbeRecord:
    url: str
    probed_at: datetime
    ok: bool
    error: str | None
    duration_ms: int
    summary: ProbeSummary
    details: ProbeDetails
    raw_json: str | None = None
    channel_key: str | None = None
    id: int | None = None
    # Set by the service when this record was served from storage, not probed now.
    cached: bool = field(default=False, compare=False)

    def age(self, now: datetime) -> float:
        return (now - self.probed_at).total_seconds()


def utcnow() -> datetime:
    return datetime.now(UTC)


class ProbeStore:
    """Thread-safe SQLite store. Use ``":memory:"`` for tests."""

    def __init__(self, path: str | Path = ":memory:", *, keep_per_url: int = 20) -> None:
        self._keep_per_url = keep_per_url
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            with self._lock:
                if str(path) != ":memory:":
                    self._db.execute("PRAGMA journal_mode=WAL")
                self._migrate()
        except BaseException:
            self._db.close()
            raise

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self._db.executescript(_SCHEMA)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._db.commit()
        elif version > SCHEMA_VERSION:
            raise RuntimeError(
                f"probe database is from a newer version (schema {version} > {SCHEMA_VERSION})"
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- writing -----------------------------------------------------------
    def record(
        self,
        run: ProbeRun,
        *,
        channel_key: str | None = None,
        probed_at: datetime | None = None,
    ) -> ProbeRecord:
        probed_at = probed_at or utcnow()
        d, s = run.details, run.summary
        with self._lock:
            cursor = self._db.execute(
                """INSERT INTO probe_results
                   (url, channel_key, probed_at, ok, error, duration_ms,
                    resolution, frame_rate, audio_channels,
                    width, height, video_codec, video_bit_rate,
                    audio_codec, audio_bit_rate, audio_sample_rate, audio_channel_count,
                    stream_count, raw_json)
                   VALUES (?,?,?,?,?,?, ?,?,?, ?,?,?,?, ?,?,?,?, ?,?)""",
                (
                    run.url, channel_key, probed_at.astimezone(UTC).isoformat(),
                    int(run.ok), run.error, run.duration_ms,
                    s.resolution, s.frame_rate, s.audio_channels,
                    d.width, d.height, d.video_codec, d.video_bit_rate,
                    d.audio_codec, d.audio_bit_rate, d.audio_sample_rate, d.audio_channel_count,
                    d.stream_count, run.raw_json,
                ),
            )  # fmt: skip
            row_id = int(cursor.lastrowid or 0)
            self._prune_url(run.url, self._keep_per_url)
            self._db.commit()
        return ProbeRecord(
            id=row_id, url=run.url, channel_key=channel_key, probed_at=probed_at.astimezone(UTC),
            ok=run.ok, error=run.error, duration_ms=run.duration_ms,
            summary=s, details=d, raw_json=run.raw_json,
        )  # fmt: skip

    def _prune_url(self, url: str, keep: int) -> None:
        self._db.execute(
            """DELETE FROM probe_results WHERE url = ? AND id NOT IN
               (SELECT id FROM probe_results WHERE url = ? ORDER BY id DESC LIMIT ?)""",
            (url, url, keep),
        )

    def prune(self, *, keep_per_url: int | None = None, older_than: datetime | None = None) -> int:
        """Delete old rows; returns how many were removed."""
        removed = 0
        with self._lock:
            if keep_per_url is not None:
                cursor = self._db.execute(
                    """DELETE FROM probe_results WHERE id NOT IN (
                         SELECT id FROM (
                           SELECT id, ROW_NUMBER() OVER (PARTITION BY url ORDER BY id DESC) AS n
                           FROM probe_results) WHERE n <= ?)""",
                    (keep_per_url,),
                )
                removed += cursor.rowcount
            if older_than is not None:
                cursor = self._db.execute(
                    "DELETE FROM probe_results WHERE probed_at < ?",
                    (older_than.astimezone(UTC).isoformat(),),
                )
                removed += cursor.rowcount
            self._db.commit()
        return removed

    def forget(self, url: str) -> int:
        """Remove every stored result for ``url``."""
        with self._lock:
            cursor = self._db.execute("DELETE FROM probe_results WHERE url = ?", (url,))
            self._db.commit()
            return cursor.rowcount

    # --- reading -----------------------------------------------------------
    def latest(self, url: str) -> ProbeRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM probe_results WHERE url = ? ORDER BY id DESC LIMIT 1", (url,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def latest_for_channel(self, channel_key: str) -> ProbeRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM probe_results WHERE channel_key = ? ORDER BY id DESC LIMIT 1",
                (channel_key,),
            ).fetchone()
        return _row_to_record(row) if row else None

    def latest_many(self, urls: list[str]) -> dict[str, ProbeRecord]:
        """Most recent result for each URL that has one (one query, for list views)."""
        found: dict[str, ProbeRecord] = {}
        with self._lock:
            for start in range(0, len(urls), 500):  # stay under SQLite's variable limit
                chunk = urls[start : start + 500]
                marks = ",".join("?" * len(chunk))
                rows = self._db.execute(
                    f"""SELECT * FROM probe_results WHERE id IN
                        (SELECT MAX(id) FROM probe_results WHERE url IN ({marks}) GROUP BY url)""",
                    chunk,
                ).fetchall()
                for row in rows:
                    found[row["url"]] = _row_to_record(row)
        return found

    def history(self, url: str, limit: int = 20) -> list[ProbeRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM probe_results WHERE url = ? ORDER BY id DESC LIMIT ?", (url, limit)
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM probe_results").fetchone()[0])


def _row_to_record(row: sqlite3.Row) -> ProbeRecord:
    return ProbeRecord(
        id=row["id"],
        url=row["url"],
        channel_key=row["channel_key"],
        probed_at=datetime.fromisoformat(row["probed_at"]),
        ok=bool(row["ok"]),
        error=row["error"],
        duration_ms=row["duration_ms"],
        summary=ProbeSummary(row["resolution"], row["frame_rate"], row["audio_channels"]),
        details=ProbeDetails(
            width=row["width"],
            height=row["height"],
            video_codec=row["video_codec"],
            video_bit_rate=row["video_bit_rate"],
            audio_codec=row["audio_codec"],
            audio_bit_rate=row["audio_bit_rate"],
            audio_sample_rate=row["audio_sample_rate"],
            audio_channel_count=row["audio_channel_count"],
            stream_count=row["stream_count"],
        ),
        raw_json=row["raw_json"],
    )
