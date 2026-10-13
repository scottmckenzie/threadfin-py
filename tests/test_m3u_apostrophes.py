"""Apostrophes: the one place this parser intentionally differs from the Go parser.

Go rewrote every ``'`` in the file to ``"`` before parsing (to support single-quoted
attributes). That mangled any name, group, URL or attribute value containing an
apostrophe: ``Joe's Pizza`` became the name ``Joe`` and lost its group-title, and
``http://host/it's`` became ``http://host/it"s``. Here, apostrophes are plain text, while
single-quoted attributes (``tvg-id='x'``) still work. Go's output for these fixtures is
kept in ``tests/golden/m3u/*.golden.json`` as documentation of the bug.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from threadfin.m3u import parse_m3u, parse_m3u_report

GOLDEN_DIR = Path(__file__).parent / "golden" / "m3u"


def load(name: str) -> list[dict[str, str]]:
    return parse_m3u((GOLDEN_DIR / f"{name}.m3u").read_bytes())


class ApostropheTests(unittest.TestCase):
    def test_exact_output_for_double_quoted_attributes(self) -> None:
        self.assertEqual(
            load("apostrophes")[0],
            {
                "url": "http://example.com/ap1",
                "tvg-id": "ap.1",
                "tvg-name": "Joe's Pizza",
                "group-title": "Food",
                "name": "Joe's Pizza",
                "_values": "ap.1 Joe's Pizza Food Joe's Pizza",
                "_uuid.key": "tvg-name",
                "_uuid.value": "Joe's Pizza",
            },
        )

    def test_exact_output_for_single_quoted_attributes_and_apostrophe_name(self) -> None:
        self.assertEqual(
            load("apostrophes")[1],
            {
                "url": "http://example.com/ap2",
                "tvg-id": "ap.2",
                "tvg-name": "Single",
                "group-title": "G",
                "name": "Rock'n'Roll Radio",
                "_values": "ap.2 Single G Rock'n'Roll Radio",
                "_uuid.key": "tvg-name",
                "_uuid.value": "Single",
            },
        )

    def test_each_entry(self) -> None:
        # (url, name, tvg-name, group-title)
        expected = [
            ("http://example.com/ap1", "Joe's Pizza", "Joe's Pizza", "Food"),
            ("http://example.com/ap2", "Rock'n'Roll Radio", "Single", "G"),
            ("http://example.com/ap3", "Don't Stop", None, None),
            ("http://example.com/ap4", "Kids Zone", None, "Children's TV"),
            ("http://example.com/ap5", "'Allo 'Allo", None, "G"),
            ("http://example.com/ap6", "L'Équipe 21", "L'Équipe", "Sport"),
            ("http://example.com/it's/stream.ts?title=don't", "Apostrophe In URL", None, "G"),
            ("http://example.com/ap8", "Mixed Quotes", 'He said "hi"', "G"),
        ]
        streams = load("apostrophes")
        self.assertEqual(len(streams), len(expected))
        for stream, (url, name, tvg_name, group) in zip(streams, expected, strict=True):
            with self.subTest(name=name):
                self.assertEqual(stream["url"], url)
                self.assertEqual(stream["name"], name)
                self.assertEqual(stream.get("tvg-name"), tvg_name)
                self.assertEqual(stream.get("group-title"), group)

    def test_go_output_was_corrupted(self) -> None:
        """Documents the Go bug these fixtures capture (Go golden = corrupted data)."""
        go = json.loads((GOLDEN_DIR / "apostrophes.golden.json").read_text("utf-8"))["streams"]
        self.assertEqual(go[0]["name"], "Joe")
        self.assertNotIn("group-title", go[0])
        self.assertEqual(go[3]["group-title"], "Children")
        self.assertEqual(go[6]["url"], 'http://example.com/it"s/stream.ts?title=don"t')

    def test_global_replacements_fixture(self) -> None:
        report = parse_m3u_report((GOLDEN_DIR / "global_replacements.m3u").read_bytes())
        go = json.loads((GOLDEN_DIR / "global_replacements.golden.json").read_text("utf-8"))["streams"]
        self.assertEqual(
            report.streams[0],
            {
                "url": "http://example.com/g1",
                "tvg-id": "g.1",
                "tvg-name": "Rock'n'Roll",
                "group-title": "Music",
                "name": "Rock'n'Roll Radio",
                "_values": "g.1 Rock'n'Roll Music Rock'n'Roll Radio",
                "_uuid.key": "tvg-name",
                "_uuid.value": "Rock'n'Roll",
            },
        )
        # The second entry has no apostrophes, so it still matches Go exactly.
        self.assertEqual(report.streams[1], go[1])
        self.assertEqual(len(report.streams), 2)
        # The third entry mentions "#EXTINF" inside its name, which splits it in two (as in Go):
        # a first half without a URL and a second half without a name. Both are reported now.
        self.assertEqual([(e.line, e.reason) for e in report.skipped], [(6, "no url"), (6, "no name")])

    def test_single_quoted_attributes_still_parse(self) -> None:
        text = "#EXTM3U\n#EXTINF:-1 tvg-id='a.1' tvg-name='Alpha' group-title='News',Alpha\nhttp://x/1\n"
        (stream,) = parse_m3u(text)
        self.assertEqual((stream["tvg-id"], stream["tvg-name"], stream["group-title"]), ("a.1", "Alpha", "News"))


if __name__ == "__main__":
    unittest.main()
