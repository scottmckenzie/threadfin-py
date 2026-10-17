"""M3U playlist parity with the real Go buildM3U (tests/golden/playlist)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from threadfin.playlist import (
    PlaylistOptions,
    StreamRegistry,
    build_playlist,
    expected_group_counts,
    stream_id,
)
from threadfin.xepg import XEPGChannel

GOLDEN = Path(__file__).parent / "golden" / "playlist"
CASES = sorted(GOLDEN.glob("*.input.json"))


def run(case: dict) -> tuple[str, StreamRegistry]:
    channels = {k: XEPGChannel.from_json(v) for k, v in case["xepg"].items()}
    registry = StreamRegistry()
    options = PlaylistOptions(base_url="http://localhost:34400", group_labels=tuple(case["groupLabels"]))
    return build_playlist(channels, options, registry, case["groups"]), registry


class Golden(unittest.TestCase):
    def test_cases(self) -> None:
        self.assertGreaterEqual(len(CASES), 35)
        for path in CASES:
            name = path.name.removesuffix(".input.json")
            with self.subTest(name):
                case = json.loads(path.read_text("utf-8"))
                golden = json.loads((GOLDEN / f"{name}.golden.json").read_text("utf-8"))
                got, registry = run(case)
                self.assertEqual(got, golden["m3u"])
                self.assertEqual({k: v.to_json() for k, v in registry.entries.items()}, golden["registry"])


class Units(unittest.TestCase):
    def test_stream_id(self) -> None:
        self.assertEqual(stream_id("M1", "http://a"), __import__("hashlib").md5(b"M1-http://a").hexdigest())
        self.assertEqual(len(stream_id("M1", "http://a")), 32)
        self.assertNotEqual(stream_id("M1", "http://a"), stream_id("M2", "http://a"))

    def test_label_parsing(self) -> None:
        self.assertEqual(expected_group_counts(["A (3)", "B (x)", "C", "D (+2)", "E (1"]), {"A": 3, "D": 2})

    def test_ordered_by_the_mapped_channel_number(self) -> None:
        # released Go (1.2.37) sorted by x-channelID, the number the playlist shows; main (1.2.38+)
        # sorts by the provider's tvg-chno, which puts renumbered channels out of order
        def make(number: str, provider_number: str) -> XEPGChannel:
            return XEPGChannel.from_json(
                {
                    "x-active": True,
                    "x-name": f"C{number}",
                    "x-channelID": number,
                    "tvg-chno": provider_number,
                    "x-epg": f"x-ID.{provider_number}",
                    "url": f"http://p/{number}",
                    "_file.m3u.id": "M1",
                }
            )

        channels = {c.x_epg: c for c in (make("3", "1"), make("1", "20"), make(" 2 ", "3"), make("x", "0"))}
        text = build_playlist(channels, PlaylistOptions(), StreamRegistry())
        numbers = [line.split('tvg-chno="')[1].split('"')[0] for line in text.splitlines()[1::2]]
        self.assertEqual(numbers, ["1", " 2 ", "3", "x"])

    def test_quotes_never_break_a_line(self) -> None:
        channel = XEPGChannel.from_json(
            {
                "x-active": True,
                "x-name": 'Say "hi"\nnow',
                "x-channelID": "1",
                "x-epg": "x-ID.1",
                "url": "http://p/1",
                "_file.m3u.id": "M1",
            }
        )
        text = build_playlist({"x-ID.1": channel}, PlaylistOptions(), StreamRegistry())
        self.assertEqual(len(text.splitlines()), 3)
        self.assertIn("Say 'hi' now", text)

    def test_registry_roundtrip_and_refresh(self) -> None:
        channel = XEPGChannel.from_json(
            {
                "x-active": True,
                "x-name": "One",
                "x-channelID": "1",
                "x-epg": "x-ID.1",
                "url": "http://p/1\r\n",
                "_file.m3u.id": "M1",
            }
        )
        with tempfile.TemporaryDirectory() as td:
            registry = StreamRegistry(Path(td) / "urls.json")
            uid = registry.register(channel)
            channel.x_name = "Renamed"
            registry.register(channel)
            registry.save()
            again = StreamRegistry(Path(td) / "urls.json")
            info = again.lookup(uid)
            assert info is not None
            self.assertEqual((info.name, info.url), ("Renamed", "http://p/1"))
            self.assertIsNone(again.lookup("nope"))
            again.prune([])
            self.assertEqual(again.entries, {})


if __name__ == "__main__":
    unittest.main()
