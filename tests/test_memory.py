"""The guide and JSON writers stream to disk instead of holding the whole output in memory."""

from __future__ import annotations

import asyncio
import gc
import gzip
import io
import json
import os
import re
import tempfile
import threading
import tracemalloc
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from test_epg import channel

from threadfin.epg import EPGOptions, write_xmltv
from threadfin.jsonio import save_json, to_json
from threadfin.m3u import (
    InvalidM3UError,
    ParseResult,
    SkippedEntry,
    Stream,
    _parse_channel,
    _Skip,
    parse_m3u_file,
    parse_m3u_report,
    serialize_m3u,
    store_m3u,
)
from threadfin.providers import ProviderManager, check_xmltv, download_to, gunzip_file
from threadfin.settings import Settings
from threadfin.streams_db import StreamDatabase, build_stream_database
from threadfin.xepg import XEPGChannel

NOW = datetime(2026, 10, 12, 10, tzinfo=ZoneInfo("Australia/Sydney"))


def guide(programmes: list[tuple[str, str]]) -> str:
    """An XMLTV file from (source channel id, title) pairs; programme i starts at hour i."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<tv>"]
    base = datetime(2026, 10, 12, tzinfo=UTC)
    for i, (source, title) in enumerate(programmes):
        start = (base + timedelta(hours=i)).strftime("%Y%m%d%H%M%S +0000")
        stop = (base + timedelta(hours=i + 1)).strftime("%Y%m%d%H%M%S +0000")
        lines.append(f'<programme start="{start}" stop="{stop}" channel="{source}"><title>{title}</title></programme>')
    lines.append("</tv>")
    return "\n".join(lines) + "\n"


def mapped(number: str, source: str, file: str = "src.xml") -> XEPGChannel:
    return channel(
        **{
            "x-epg": f"x-ID.{number}",
            "x-channelID": number,
            "tvg-chno": number,
            "name": f"C{number}",
            "tvg-name": f"C{number}",
            "x-name": f"C{number}",
            "x-xmltv-file": file,
            "x-mapping": source,
        }
    )


def written(path: Path) -> list[tuple[str | None, str | None]]:
    root = ElementTree.parse(path).getroot()
    return [(p.get("channel"), p.findtext("title")) for p in root.iter("programme")]


def traced_peak(fn: object, *args: object) -> int:
    """Peak traced allocation, in bytes, while ``fn(*args)`` runs."""
    assert callable(fn)
    tracemalloc.start()
    try:
        gc.collect()
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        fn(*args)
        return tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()


INTERLEAVED = [("src.a", "A0"), ("src.b", "B0"), ("src.a", "A1"), ("src.b", "B1"), ("src.a", "A2")]


class SpooledGuide(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.folder = Path(self._td.name)
        self.out = self.folder / "out.xml"

    def test_interleaved_sources_keep_order(self) -> None:
        (self.folder / "src.xml").write_text(guide(INTERLEAVED), "utf-8")
        channels = {c.x_epg: c for c in (mapped("3", "src.a"), mapped("1", "src.a"), mapped("2", "src.b"))}
        stats = write_xmltv(self.out, channels, self.folder, EPGOptions(), NOW)
        self.assertEqual(
            written(self.out),
            [("1", "A0"), ("1", "A1"), ("1", "A2"), ("2", "B0"), ("2", "B1"), ("3", "A0"), ("3", "A1"), ("3", "A2")],
        )
        self.assertEqual(stats.programmes, 8)
        self.assertEqual(stats.errors, {})

    def test_dummy_and_real_channels_together(self) -> None:
        (self.folder / "src.xml").write_text(guide(INTERLEAVED), "utf-8")
        dummy = channel(**{"x-epg": "x-ID.0", "x-channelID": "0", "tvg-chno": "0"})
        channels = {c.x_epg: c for c in (mapped("2", "src.b"), dummy)}
        stats = write_xmltv(self.out, channels, self.folder, EPGOptions(), NOW)
        programmes = written(self.out)
        dummies = [p for p in programmes if p[0] == "0"]
        self.assertEqual(len(dummies), 4 * 24 * 2)
        self.assertEqual(programmes[len(dummies) :], [("2", "B0"), ("2", "B1")])
        self.assertEqual(stats.programmes, len(dummies) + 2)

    def test_broken_file_keeps_programmes_read_before_error(self) -> None:
        text = guide([("src.a", "A0"), ("src.a", "A1"), ("src.b", "B0")])
        self.assertTrue(text.endswith("</tv>\n"))
        (self.folder / "src.xml").write_text(text.removesuffix("</tv>\n") + "<programme", "utf-8")
        channels = {c.x_epg: c for c in (mapped("1", "src.a"), mapped("2", "src.b"))}
        stats = write_xmltv(self.out, channels, self.folder, EPGOptions(), NOW)
        self.assertIn("src.xml", stats.errors)
        self.assertEqual(written(self.out), [("1", "A0"), ("1", "A1"), ("2", "B0")])
        self.assertEqual(stats.programmes, 3)

    def test_no_temp_files_left(self) -> None:
        (self.folder / "src.xml").write_text(guide(INTERLEAVED), "utf-8")
        write_xmltv(self.out, {"x-ID.1": mapped("1", "src.a")}, self.folder, EPGOptions(), NOW)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["out.xml", "src.xml"])


class MemoryCeiling(unittest.TestCase):
    """The old writers held the whole output (several times over); these limits catch a return to that."""

    def test_write_xmltv_does_not_buffer_the_guide(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            programmes = [(f"src.{i}", "x" * 300 + str(j)) for i in range(4) for j in range(3000)]
            (folder / "src.xml").write_text(guide(programmes), "utf-8")
            channels = {c.x_epg: c for c in (mapped(str(i), f"src.{i}") for i in range(4))}
            out = folder / "out.xml"
            peak = traced_peak(write_xmltv, out, channels, folder, EPGOptions(), NOW)
            size = out.stat().st_size
            self.assertEqual(len(written(out)), 12000)
            # the spooled writer's peak is a fixed overhead (about 0.5 MB on 3.12, 0.75 MB on 3.14) whatever
            # the guide size; holding the guide, or one channel's programmes, would exceed this
            self.assertGreater(size, 5 << 20)
            self.assertLess(peak, 2 << 20, f"peak={peak} size={size}")

    def test_save_json_streams(self) -> None:
        data = {f"k{i}": {"name": "n" * 200, "i": i, "tags": ["a", "b"]} for i in range(20000)}
        expected = to_json(data).encode("utf-8")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x.json"
            peak = traced_peak(save_json, path, data)
            self.assertLess(peak, len(expected) // 4, f"peak={peak} size={len(expected)}")
            self.assertEqual(path.read_bytes(), expected)

    def test_save_json_failure_keeps_old_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x.json"
            save_json(path, {"ok": 1})
            with self.assertRaises(TypeError):
                save_json(path, {"bad": object()})
            self.assertEqual(json.loads(path.read_text("utf-8")), {"ok": 1})
            self.assertEqual([p.name for p in Path(td).iterdir()], ["x.json"])


class ProviderDownload(unittest.TestCase):
    """A provider's guide goes to disk as it downloads and is unzipped and checked from files."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.folder = Path(self._td.name)
        programmes = [(f"src.{i % 50}", "x" * 300 + str(i)) for i in range(60000)]
        self.xml = guide(programmes).encode("utf-8")
        self.assertGreater(len(self.xml), 20 << 20)

    def test_check_xmltv_drops_counted_elements(self) -> None:
        path = self.folder / "tv.xml"
        path.write_bytes(self.xml)
        peak = traced_peak(check_xmltv, path)
        self.assertLess(peak, 2 << 20, f"peak={peak}")
        self.assertEqual(check_xmltv(path), {"xmltv.channels": 0, "xmltv.programs": 60000})

    def test_gunzip_file_works_in_blocks(self) -> None:
        source, dest = self.folder / "tv.xml.gz", self.folder / "tv.xml"
        source.write_bytes(gzip.compress(self.xml, compresslevel=1))
        peak = traced_peak(gunzip_file, source, dest)
        self.assertLess(peak, 8 << 20, f"peak={peak}")
        self.assertEqual(dest.read_bytes(), self.xml)

    def test_download_to_writes_as_it_arrives(self) -> None:
        body = bytes(range(256)) * (96 << 10)  # 24 MiB

        class Big(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                view = memoryview(body)
                for i in range(0, len(view), 1 << 16):
                    self.wfile.write(view[i : i + (1 << 16)])

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Big)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        dest = self.folder / "download"

        def fetch() -> None:
            with open(dest, "wb") as out:
                self.assertEqual(download_to(f"http://127.0.0.1:{server.server_port}/tv.xml", out), "tv.xml")

        peak = traced_peak(fetch)
        self.assertLess(peak, 2 << 20, f"peak={peak} body={len(body)}")
        self.assertEqual(dest.stat().st_size, len(body))
        self.assertEqual(dest.read_bytes(), body)

    def test_adding_a_gzipped_guide(self) -> None:
        body = gzip.compress(self.xml, compresslevel=1)

        def fetch(url: str, dest: IO[bytes], **kwargs: object) -> str:
            for i in range(0, len(body), 1 << 16):
                dest.write(body[i : i + (1 << 16)])
            return "tv.xml.gz"

        data = self.folder / "data"
        data.mkdir()
        settings = Settings()
        manager = ProviderManager(settings, data, lambda: None, fetch=fetch)

        def add() -> None:
            asyncio.run(manager.add("xmltv", {"file.source": "http://prov/tv.xml.gz"}))

        peak = traced_peak(add)
        self.assertLess(peak, 10 << 20, f"peak={peak} unzipped={len(self.xml)}")
        ((pid, record),) = settings.files.xmltv.items()
        self.assertEqual((data / f"{pid}.xml").read_bytes(), self.xml)
        self.assertEqual(record["compatibility"], {"xmltv.channels": 0, "xmltv.programs": 60000})
        self.assertEqual(sorted(p.name for p in data.iterdir()), [f"{pid}.xml"])


M3U_GOLDEN = Path(__file__).parent / "golden" / "m3u"


def playlist(count: int) -> bytes:
    """An extended M3U playlist with ``count`` streams of realistic length."""
    lines = ["#EXTM3U"]
    for i in range(count):
        lines.append(
            f'#EXTINF:-1 tvg-id="ch{i}.example" tvg-name="Channel {i}" tvg-logo="http://logo.example/{i}.png" '
            f'group-title="Group {i % 40}",Channel {i}'
        )
        lines.append(f"http://stream.example/live/user/pass/{i}.ts")
    return ("\n".join(lines) + "\n").encode("utf-8")


def whole_text_parse(content: bytes) -> ParseResult:
    """The parser as it was before it read a line at a time: the whole text at once (the oracle)."""
    original = content.decode("utf-8", errors="replace")
    if "#EXT-X-TARGETDURATION" in original or "#EXT-X-MEDIA-SEQUENCE" in original or "#EXTM3U" not in original:
        raise InvalidM3UError("invalid")
    text = original.replace(":-1", "")
    streams: list[Stream] = []
    skipped: list[SkippedEntry] = []
    starts = [m.start() for m in re.finditer("#EXTINF", text)]
    line, previous = 1, 0
    for i, start in enumerate(starts):
        line += text.count("\n", previous, start)
        previous = start
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        outcome = _parse_channel(text[start + len("#EXTINF") : end])
        if isinstance(outcome, _Skip):
            source = original.split("\n")[line - 1].strip()[:200]
            skipped.append(SkippedEntry(line, outcome.reason, outcome.url, source))
        else:
            streams.append(outcome)
    return ParseResult(streams, skipped)


def traced_transient(fn: Callable[[], object]) -> int:
    """Peak traced allocation while ``fn()`` runs, less what is still allocated after it."""
    tracemalloc.start()
    try:
        gc.collect()
        tracemalloc.reset_peak()
        fn()
        current, peak = tracemalloc.get_traced_memory()
        return peak - current
    finally:
        tracemalloc.stop()


class PlaylistLines(unittest.TestCase):
    """Parsing a line at a time gives exactly what parsing the whole text gave."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.folder = Path(self._td.name)

    def parse_both(self, content: bytes) -> tuple[ParseResult | type[Exception], ParseResult | type[Exception]]:
        path = self.folder / "list.m3u"
        path.write_bytes(content)
        results: list[ParseResult | type[Exception]] = []
        for parse in (
            lambda: whole_text_parse(content),
            lambda: parse_m3u_report(content),
            lambda: parse_m3u_file(path),
        ):
            try:
                results.append(parse())
            except InvalidM3UError as exc:
                results.append(type(exc))
        self.assertEqual(results[1], results[0], "parse_m3u_report")
        return results[0], results[2]

    def test_file_parser_matches_goldens(self) -> None:
        fixtures = sorted(M3U_GOLDEN.glob("*.m3u"))
        self.assertGreater(len(fixtures), 10)
        for fixture in fixtures:
            with self.subTest(fixture.name):
                whole, by_line = self.parse_both(fixture.read_bytes())
                self.assertEqual(by_line, whole)

    def test_markers_anywhere_in_a_line(self) -> None:
        cases = {
            "two entries on one line": b'#EXTM3U\n#EXTINF:-1,A #EXTINF:-1 tvg-name="B",B\nhttp://x/1\n',
            "header after the entries": b"#EXTINF:-1,A\nhttp://x/1\n#EXTM3U\n",
            "no newline at the end": b"#EXTM3U\n#EXTINF:-1,A\nhttp://x/1",
            "crlf": b"#EXTM3U\r\n#EXTINF:-1,A\r\nhttp://x/1\r\n#EXTINF:-1,\r\nhttp://x/2\r\n",
            "bare cr": b"#EXTM3U\r#EXTINF:-1,A\rhttp://x/1\r",
            "removing :-1 joins a marker": b"#EXTM3U\n#EXT:-1INF,A\nhttp://x/1\n",
            "hls tag late": b"#EXTM3U\n#EXTINF:-1,A\nhttp://x/1\n#EXT-X-MEDIA-SEQUENCE:1\n",
            "broken utf-8 before a newline": b"#EXTM3U\n#EXTINF:-1,A\xe2\x82\nhttp://x/\xff1\n",
            "skipped entry line numbers": b"#EXTM3U\n\n#EXTINF:-1,\nhttp://x/1\n#EXTINF:-1,B\n",
            "empty": b"",
            "header inside a name": b'#EXTINF:-1 tvg-name="#EXTM3U",A\nhttp://x/1\n',
        }
        for name, content in cases.items():
            with self.subTest(name):
                whole, by_line = self.parse_both(content)
                self.assertEqual(by_line, whole)

    def test_attribute_names_are_shared(self) -> None:
        first, second = parse_m3u_report(playlist(2)).streams
        for key in ("tvg-id", "tvg-name", "tvg-logo", "group-title"):
            self.assertIs(next(k for k in first if k == key), next(k for k in second if k == key))

    def test_store_writes_the_serialized_copy(self) -> None:
        for fixture in sorted(M3U_GOLDEN.glob("*.m3u")):
            with self.subTest(fixture.name):
                out = io.StringIO()
                try:
                    whole = parse_m3u_report(fixture.read_bytes())
                except InvalidM3UError:
                    with self.assertRaises(InvalidM3UError):
                        store_m3u(fixture, out)
                    continue
                count, skipped = store_m3u(fixture, out)
                self.assertEqual(out.getvalue(), serialize_m3u(whole.streams))
                self.assertEqual((count, skipped), (len(whole.streams), whole.skipped))


class PlaylistMemory(unittest.TestCase):
    """A playlist is parsed from its file and copied entry by entry, never held as a whole."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.folder = Path(self._td.name)
        self.m3u = playlist(100_000)
        self.assertGreater(len(self.m3u), 15 << 20)

    def test_store_m3u_does_not_buffer_the_playlist(self) -> None:
        source = self.folder / "list.m3u"
        source.write_bytes(self.m3u)
        with open(self.folder / "copy.m3u", "w", encoding="utf-8", newline="") as out:
            peak = traced_peak(store_m3u, source, out)
        self.assertLess(peak, 2 << 20, f"peak={peak} playlist={len(self.m3u)}")
        self.assertEqual(
            (self.folder / "copy.m3u").read_text(encoding="utf-8"),
            serialize_m3u(parse_m3u_report(self.m3u).streams),
        )

    def test_parse_m3u_file_does_not_read_the_whole_file(self) -> None:
        source = self.folder / "list.m3u"
        source.write_bytes(self.m3u)
        kept: list[ParseResult] = []
        # the streams stay, so only what is freed again counts: never the file text
        transient = traced_transient(lambda: kept.append(parse_m3u_file(source)))
        self.assertLess(transient, 2 << 20, f"transient={transient} playlist={len(self.m3u)}")
        self.assertEqual(len(kept[0].streams), 100_000)

    def test_stream_database_reads_the_copy_a_line_at_a_time(self) -> None:
        data = self.folder / "data"
        data.mkdir()
        settings = Settings()
        settings.files.m3u["p1"] = {"name": "P"}
        (data / "p1.m3u").write_bytes(self.m3u)
        manager = ProviderManager(settings, data, lambda: None)
        kept: list[StreamDatabase] = []
        transient = traced_transient(lambda: kept.append(build_stream_database(manager, settings)))
        self.assertLess(transient, 4 << 20, f"transient={transient} playlist={len(self.m3u)}")
        self.assertEqual(len(kept[0].all), 100_000)

    def test_adding_a_playlist(self) -> None:
        body = self.m3u

        def fetch(url: str, dest: IO[bytes], **kwargs: object) -> str:
            for i in range(0, len(body), 1 << 16):
                dest.write(body[i : i + (1 << 16)])
            return "list.m3u"

        data = self.folder / "data"
        data.mkdir()
        settings = Settings()
        manager = ProviderManager(settings, data, lambda: None, fetch=fetch)

        def add() -> None:
            asyncio.run(manager.add("m3u", {"file.source": "http://prov/list.m3u", "name": "P"}))

        old_umask = os.umask(0o077)  # the copy must be 644 whatever the umask
        try:
            peak = traced_peak(add)
        finally:
            os.umask(old_umask)
        self.assertLess(peak, 4 << 20, f"peak={peak} playlist={len(body)}")
        ((pid, _),) = settings.files.m3u.items()
        stored = data / f"{pid}.m3u"
        self.assertEqual(stored.read_text(encoding="utf-8"), serialize_m3u(parse_m3u_report(body).streams))
        self.assertEqual(stored.stat().st_mode & 0o777, 0o644)
        self.assertEqual(sorted(p.name for p in data.iterdir()), [f"{pid}.m3u"])

    def test_a_bad_refresh_keeps_the_old_copy(self) -> None:
        bodies = [self.m3u[:4096] + b"\n", b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:-1,A\nhttp://x/1\n"]

        def fetch(url: str, dest: IO[bytes], **kwargs: object) -> str:
            dest.write(bodies.pop(0))
            return "list.m3u"

        data = self.folder / "data"
        data.mkdir()
        settings = Settings()
        manager = ProviderManager(settings, data, lambda: None, fetch=fetch)
        asyncio.run(manager.add("m3u", {"file.source": "http://prov/list.m3u", "name": "P"}))
        ((pid, _),) = settings.files.m3u.items()
        before = (data / f"{pid}.m3u").read_bytes()
        (result,) = asyncio.run(manager.refresh("m3u", pid))
        self.assertFalse(result.ok)
        self.assertEqual((data / f"{pid}.m3u").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in data.iterdir()), [f"{pid}.m3u"])
