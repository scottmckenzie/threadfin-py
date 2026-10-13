"""Entries that cannot become a stream are skipped *and reported*, never silently dropped.

Go emitted nameless entries as blank-named channels (visible in the UI, written to the
re-saved playlist copy, and into Plex). Here they are skipped, and the report says what
was skipped, where, and why, so the UI/log can show "N entries ignored".
"""

from __future__ import annotations

import unittest
from pathlib import Path

from threadfin.m3u import NO_NAME, NO_URL, SkippedEntry, parse_m3u, parse_m3u_report

GOLDEN_DIR = Path(__file__).parent / "golden" / "m3u"


def report(name: str):
    return parse_m3u_report((GOLDEN_DIR / f"{name}.m3u").read_bytes())


class SkippedEntryTests(unittest.TestCase):
    def test_unnamed_entries_are_skipped_with_details(self) -> None:
        result = report("names")
        self.assertEqual(
            [s["tvg-id"] for s in result.streams], ["n.1", "n.2", "n.3"]
        )  # n.4 (no comma, no tvg-name) and n.5 (empty name) are gone
        self.assertEqual(
            result.skipped,
            [
                SkippedEntry(8, NO_NAME, "http://example.com/n4",
                             '#EXTINF:-1 tvg-id="n.4" group-title="G"'),
                SkippedEntry(10, NO_NAME, "http://example.com/n5", '#EXTINF:-1 tvg-id="n.5",'),
            ],
        )  # fmt: skip
        self.assertEqual(result.entries, 5)

    def test_tvg_name_still_rescues_an_empty_name(self) -> None:
        stream = report("names").streams[0]
        self.assertEqual((stream["tvg-id"], stream["name"]), ("n.1", "FromTvgName"))

    def test_entry_without_url_is_reported(self) -> None:
        result = report("directives")
        self.assertEqual([s["tvg-id"] for s in result.streams], ["d.1", "d.2"])
        (entry,) = result.skipped
        self.assertEqual((entry.line, entry.reason, entry.url), (12, NO_URL, None))
        self.assertIn('tvg-id="d.3"', entry.extinf)

    def test_nothing_skipped_in_a_clean_playlist(self) -> None:
        for name in ("basic", "crlf", "unicode", "missing_ids", "duplicates", "quotes_and_case"):
            with self.subTest(name=name):
                self.assertEqual(report(name).skipped, [])

    def test_counts_add_up(self) -> None:
        for path in sorted(GOLDEN_DIR.glob("*.m3u")):
            text = path.read_bytes().decode("utf-8", errors="replace")
            if "#EXTM3U" not in text or "#EXT-X-" in text:
                continue
            with self.subTest(fixture=path.stem):
                result = parse_m3u_report(text)
                self.assertEqual(result.entries, text.count("#EXTINF"))

    def test_whitespace_only_name_counts_as_no_name(self) -> None:
        text = '#EXTM3U\n#EXTINF:-1 tvg-id="a",   \nhttp://x/1\n'
        result = parse_m3u_report(text)
        self.assertEqual((result.streams, [e.reason for e in result.skipped]), ([], [NO_NAME]))

    def test_line_numbers_with_crlf_and_blank_lines(self) -> None:
        text = (
            '#EXTM3U\r\n\r\n#EXTINF:-1 tvg-id="a",Named\r\nhttp://x/1\r\n\r\n#EXTINF:-1 tvg-id="b",\r\nhttp://x/2\r\n'
        )
        result = parse_m3u_report(text)
        self.assertEqual([s["name"] for s in result.streams], ["Named"])
        self.assertEqual([(e.line, e.url) for e in result.skipped], [(6, "http://x/2")])
        self.assertEqual(result.skipped[0].extinf, '#EXTINF:-1 tvg-id="b",')  # CR trimmed

    def test_extinf_preview_is_truncated(self) -> None:
        text = '#EXTM3U\n#EXTINF:-1 tvg-id="a" tvg-logo="' + "x" * 500 + '"\nhttp://x/1\n'
        (entry,) = parse_m3u_report(text).skipped
        self.assertEqual(len(entry.extinf), 200)

    def test_parse_m3u_returns_the_same_streams(self) -> None:
        content = (GOLDEN_DIR / "names.m3u").read_bytes()
        self.assertEqual(parse_m3u(content), parse_m3u_report(content).streams)

    def test_report_for_invalid_input_raises(self) -> None:
        from threadfin.m3u import InvalidM3UError

        with self.assertRaises(InvalidM3UError):
            parse_m3u_report("no header here")


if __name__ == "__main__":
    unittest.main()
