"""The guide and JSON writers stream to disk instead of holding the whole output in memory."""

from __future__ import annotations

import asyncio
import gc
import gzip
import json
import tempfile
import tracemalloc
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from test_epg import channel

from threadfin.epg import EPGOptions, write_xmltv
from threadfin.jsonio import save_json, to_json
from threadfin.providers import ProviderManager, check_xmltv, gunzip_file
from threadfin.settings import Settings
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
