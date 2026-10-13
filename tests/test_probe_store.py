"""SQLite probe storage and the caching/concurrency service."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from threadfin.probe import InvalidProbeURLError, ProbeDetails, ProbeRun, ProbeSummary
from threadfin.probe_service import ProbeService
from threadfin.probe_store import ProbeStore

GOLDEN_DIR = Path(__file__).parent / "golden" / "probe"
T0 = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)


def make_run(url: str = "http://h/a", *, ok: bool = True, res: str = "1080p") -> ProbeRun:
    return ProbeRun(
        url=url, ok=ok, error=None if ok else "boom", duration_ms=120,
        summary=ProbeSummary(res, "25", "stereo") if ok else ProbeSummary(),
        details=ProbeDetails(width=1920, height=1080, video_codec="h264", stream_count=2)
        if ok else ProbeDetails(),
        raw_json='{"streams": []}' if ok else None,
    )  # fmt: skip


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ProbeStore(":memory:", keep_per_url=3)
        self.addCleanup(self.store.close)

    def test_round_trip(self) -> None:
        saved = self.store.record(make_run(), channel_key="chan-1", probed_at=T0)
        loaded = self.store.latest("http://h/a")
        assert loaded is not None
        self.assertEqual(loaded, saved)
        self.assertEqual(loaded.summary, ProbeSummary("1080p", "25", "stereo"))
        self.assertEqual(loaded.details.video_codec, "h264")
        self.assertEqual(loaded.probed_at, T0)
        self.assertEqual(loaded.probed_at.utcoffset(), timedelta(0))
        self.assertEqual(loaded.raw_json, '{"streams": []}')

    def test_failure_round_trip(self) -> None:
        self.store.record(make_run(ok=False), probed_at=T0)
        loaded = self.store.latest("http://h/a")
        assert loaded is not None
        self.assertFalse(loaded.ok)
        self.assertEqual(loaded.error, "boom")
        self.assertEqual(loaded.summary, ProbeSummary())

    def test_latest_is_newest_and_history_is_ordered(self) -> None:
        for i, res in enumerate(("480p", "720p", "1080p")):
            self.store.record(make_run(res=res), probed_at=T0 + timedelta(minutes=i))
        latest = self.store.latest("http://h/a")
        assert latest is not None
        self.assertEqual(latest.summary.resolution, "1080p")
        self.assertEqual(
            [r.summary.resolution for r in self.store.history("http://h/a")],
            ["1080p", "720p", "480p"],
        )
        self.assertEqual(len(self.store.history("http://h/a", limit=2)), 2)

    def test_unknown_url(self) -> None:
        self.assertIsNone(self.store.latest("http://h/none"))
        self.assertEqual(self.store.history("http://h/none"), [])

    def test_keep_per_url_prunes_oldest(self) -> None:
        for i in range(5):
            self.store.record(make_run(res=f"{i}p"), probed_at=T0 + timedelta(minutes=i))
        self.assertEqual([r.summary.resolution for r in self.store.history("http://h/a")], ["4p", "3p", "2p"])

    def test_prune_across_urls(self) -> None:
        store = ProbeStore(":memory:", keep_per_url=100)
        self.addCleanup(store.close)
        for url in ("http://h/a", "http://h/b"):
            for i in range(4):
                store.record(make_run(url), probed_at=T0 + timedelta(minutes=i))
        self.assertEqual(store.prune(keep_per_url=2), 4)
        self.assertEqual(store.count(), 4)
        self.assertEqual(len(store.history("http://h/b")), 2)

    def test_prune_older_than(self) -> None:
        self.store.record(make_run(), probed_at=T0)
        self.store.record(make_run(), probed_at=T0 + timedelta(days=2))
        self.assertEqual(self.store.prune(older_than=T0 + timedelta(days=1)), 1)
        self.assertEqual(self.store.count(), 1)

    def test_latest_many_and_channel_lookup(self) -> None:
        self.store.record(make_run("http://h/a", res="480p"), channel_key="c1", probed_at=T0)
        self.store.record(make_run("http://h/a", res="720p"), channel_key="c1", probed_at=T0)
        self.store.record(make_run("http://h/b"), channel_key="c2", probed_at=T0)
        found = self.store.latest_many(["http://h/a", "http://h/b", "http://h/zzz"])
        self.assertEqual(set(found), {"http://h/a", "http://h/b"})
        self.assertEqual(found["http://h/a"].summary.resolution, "720p")
        by_channel = self.store.latest_for_channel("c1")
        assert by_channel is not None
        self.assertEqual(by_channel.summary.resolution, "720p")
        self.assertIsNone(self.store.latest_for_channel("nope"))
        self.assertEqual(self.store.latest_many([]), {})

    def test_latest_many_handles_more_urls_than_sqlite_variable_limit(self) -> None:
        urls = [f"http://h/{i}" for i in range(1200)]
        self.store.record(make_run(urls[-1]), probed_at=T0)
        self.assertEqual(set(self.store.latest_many(urls)), {urls[-1]})

    def test_forget(self) -> None:
        self.store.record(make_run(), probed_at=T0)
        self.assertEqual(self.store.forget("http://h/a"), 1)
        self.assertIsNone(self.store.latest("http://h/a"))

    def test_sql_injection_in_url_is_just_data(self) -> None:
        url = "http://h/a'; DROP TABLE probe_results; --"
        self.store.record(make_run(url), probed_at=T0)
        self.assertIsNotNone(self.store.latest(url))
        self.assertEqual(self.store.count(), 1)

    def test_persists_across_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probes.sqlite3"
            first = ProbeStore(path)
            first.record(make_run(), probed_at=T0)
            first.close()
            second = ProbeStore(path)
            self.addCleanup(second.close)
            loaded = second.latest("http://h/a")
            assert loaded is not None
            self.assertEqual(loaded.summary.resolution, "1080p")

    def test_rejects_newer_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probes.sqlite3"
            db = sqlite3.connect(path)
            db.execute("PRAGMA user_version = 99")
            db.commit()
            db.close()
            opened: list[sqlite3.Connection] = []
            real_connect = sqlite3.connect

            def spy(*args: object, **kwargs: object) -> sqlite3.Connection:
                conn = real_connect(*args, **kwargs)  # type: ignore[arg-type]
                opened.append(conn)
                return conn

            with mock.patch("threadfin.probe_store.sqlite3.connect", spy), self.assertRaises(RuntimeError):
                ProbeStore(path)
            self.assertEqual(len(opened), 1)
            with self.assertRaises(sqlite3.ProgrammingError):  # "Cannot operate on a closed database"
                opened[0].execute("SELECT 1")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.log, self.trace = root / "calls.log", root / "trace.log"
        script = root / "fake-ffprobe"
        script.write_text(
            '#!/bin/sh\nfor a; do url="$a"; done\n'
            'echo "$url" >> "$FAKE_LOG"\necho "start $url" >> "$FAKE_TRACE"\n'
            'case "$url" in\n'
            "  *slow*) exec sleep 5 ;;\n"
            '  *fail*) echo "Connection refused" >&2; echo "end $url" >> "$FAKE_TRACE"; exit 1 ;;\n'
            '  *) cat "$FAKE_JSON"; sleep "${FAKE_DELAY:-0}" ;;\n'
            'esac\necho "end $url" >> "$FAKE_TRACE"\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.script = script
        patcher = mock.patch.dict(os.environ, {
            "FAKE_LOG": str(self.log), "FAKE_TRACE": str(self.trace),
            "FAKE_JSON": str(GOLDEN_DIR / "hd_stereo.input.json"), "FAKE_DELAY": "0",
        })  # fmt: skip
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()
        self.store = ProbeStore(":memory:")
        self.addCleanup(self.store.close)

    def service(self, **kwargs: object) -> ProbeService:
        kwargs.setdefault("clock", self.clock)
        return ProbeService(self.store, ffprobe_path=str(self.script), **kwargs)  # type: ignore[arg-type]

    def calls(self) -> list[str]:
        return self.log.read_text().split() if self.log.exists() else []

    def trace_lines(self) -> list[str]:
        return [line.split()[0] for line in self.trace.read_text().splitlines()]

    async def test_probe_stores_result(self) -> None:
        record = await self.service().probe("http://h/ok", channel_key="c1")
        self.assertTrue(record.ok)
        self.assertFalse(record.cached)
        stored = self.store.latest_for_channel("c1")
        assert stored is not None
        self.assertEqual(stored.summary, ProbeSummary("1080p", "25", "stereo"))
        self.assertEqual(stored.probed_at, T0)

    async def test_fresh_result_is_served_from_storage(self) -> None:
        svc = self.service()
        await svc.probe("http://h/ok")
        again = await svc.probe("http://h/ok")
        self.assertTrue(again.cached)
        self.assertEqual(len(self.calls()), 1)

    async def test_force_reprobes(self) -> None:
        svc = self.service()
        await svc.probe("http://h/ok")
        forced = await svc.probe("http://h/ok", force=True)
        self.assertFalse(forced.cached)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(len(self.store.history("http://h/ok")), 2)

    async def test_ok_result_expires(self) -> None:
        svc = self.service(ok_ttl=timedelta(hours=1))
        await svc.probe("http://h/ok")
        self.clock.advance(minutes=59)
        self.assertTrue((await svc.probe("http://h/ok")).cached)
        self.clock.advance(minutes=2)
        self.assertFalse((await svc.probe("http://h/ok")).cached)
        self.assertEqual(len(self.calls()), 2)

    async def test_failures_cached_briefly_then_retried(self) -> None:
        svc = self.service(failure_ttl=timedelta(minutes=5))
        first = await svc.probe("http://h/fail")
        self.assertFalse(first.ok)
        self.clock.advance(minutes=4)
        self.assertTrue((await svc.probe("http://h/fail")).cached)
        self.clock.advance(minutes=2)
        self.assertFalse((await svc.probe("http://h/fail")).cached)
        self.assertEqual(len(self.calls()), 2)

    async def test_concurrent_identical_probes_share_one_run(self) -> None:
        os.environ["FAKE_DELAY"] = "0.3"
        svc = self.service()
        records = await __import__("asyncio").gather(*(svc.probe("http://h/ok") for _ in range(5)))
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(len({r.id for r in records}), 1)

    async def test_concurrency_limit_serialises_probes(self) -> None:
        os.environ["FAKE_DELAY"] = "0.2"
        svc = self.service(max_concurrent=1)
        await svc.probe_many([("http://h/a", None), ("http://h/b", None), ("http://h/c", None)])
        self.assertEqual(self.trace_lines(), ["start", "end"] * 3)

    async def test_higher_limit_allows_overlap(self) -> None:
        os.environ["FAKE_DELAY"] = "0.4"
        svc = self.service(max_concurrent=3)
        await svc.probe_many([("http://h/a", None), ("http://h/b", None), ("http://h/c", None)])
        self.assertEqual(self.trace_lines()[:3], ["start", "start", "start"])

    async def test_timeout_is_stored_as_failure(self) -> None:
        record = await self.service(timeout=0.3).probe("http://h/slow")
        self.assertFalse(record.ok)
        self.assertIn("timed out", record.error or "")
        stored = self.store.latest("http://h/slow")
        assert stored is not None
        self.assertFalse(stored.ok)

    async def test_invalid_url_raises_and_stores_nothing(self) -> None:
        with self.assertRaises(InvalidProbeURLError):
            await self.service().probe("file:///etc/passwd")
        self.assertEqual(self.store.count(), 0)

    async def test_probe_many_reports_bad_urls_in_place(self) -> None:
        results = await self.service().probe_many(
            [("http://h/a", "c1"), ("file:///etc/passwd", "c2"), ("http://h/b", "c3")]
        )
        self.assertTrue(results[0].ok)  # type: ignore[union-attr]
        self.assertIsInstance(results[1], InvalidProbeURLError)
        self.assertTrue(results[2].ok)  # type: ignore[union-attr]

    async def test_cancelling_one_waiter_does_not_cancel_the_shared_probe(self) -> None:
        import asyncio

        os.environ["FAKE_DELAY"] = "0.3"
        svc = self.service()
        first = asyncio.create_task(svc.probe("http://h/ok"))
        second = asyncio.create_task(svc.probe("http://h/ok"))
        await asyncio.sleep(0.1)
        first.cancel()
        record = await second
        self.assertTrue(record.ok)
        self.assertEqual(len(self.calls()), 1)


if __name__ == "__main__":
    unittest.main()
