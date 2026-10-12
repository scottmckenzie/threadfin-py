"""Parity tests: the Python parser must match the Go parser's output exactly.

Golden files are generated from the real Go code by
``tools/go_golden/run_m3u_golden.sh``.

Written with ``unittest`` so they run with no extra dependencies
(``PYTHONPATH=src python3 -m unittest discover -s tests``) and under pytest.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from threadfin.m3u import NO_NAME, InvalidM3UError, parse_m3u, parse_m3u_report

GOLDEN_DIR = Path(__file__).parent / "golden" / "m3u"
FIXTURES = sorted(GOLDEN_DIR.glob("*.m3u"))

# Fixtures where this port deliberately differs from Go beyond skipping unnamed entries:
# Go corrupts apostrophes (test_m3u_apostrophes.py) and text in double quotes inside names
# (test_m3u_quoting.py). Their correct output is asserted there.
INTENTIONAL_DIVERGENCES = {"apostrophes", "global_replacements", "double_quotes_in_name"}


def named_only(streams: list[dict[str, str]]) -> list[dict[str, str]]:
    """Go emits entries without a name as partial streams (no ``name`` key); we skip them."""
    return [s for s in streams if "name" in s]


class GoldenParityTests(unittest.TestCase):
    def test_fixtures_present(self) -> None:
        self.assertGreaterEqual(len(FIXTURES), 15)
        for fixture in FIXTURES:
            self.assertTrue(
                fixture.with_suffix(".golden.json").exists(),
                f"missing golden for {fixture.name}",
            )

    def test_matches_go_output(self) -> None:
        for fixture in FIXTURES:
            if fixture.stem in INTENTIONAL_DIVERGENCES:
                continue
            with self.subTest(fixture=fixture.stem):
                golden = json.loads(fixture.with_suffix(".golden.json").read_text("utf-8"))
                content = fixture.read_bytes()
                if golden["error"] is not None:
                    with self.assertRaises(InvalidM3UError) as ctx:
                        parse_m3u(content)
                    self.assertEqual(str(ctx.exception), golden["error"])
                else:
                    self.assertEqual(parse_m3u(content), named_only(golden["streams"]))

    def test_every_go_blank_channel_is_reported_as_skipped(self) -> None:
        """Nothing is dropped silently: Go's nameless partial streams == our NO_NAME reports."""
        for fixture in FIXTURES:
            if fixture.stem in INTENTIONAL_DIVERGENCES:
                continue
            with self.subTest(fixture=fixture.stem):
                golden = json.loads(fixture.with_suffix(".golden.json").read_text("utf-8"))
                if golden["error"] is not None:
                    continue
                blank = [s for s in golden["streams"] if "name" not in s]
                report = parse_m3u_report(fixture.read_bytes())
                skipped = [e for e in report.skipped if e.reason == NO_NAME]
                self.assertEqual([e.url for e in skipped], [s["url"] for s in blank])

    def test_intentional_divergences_really_diverge(self) -> None:
        """Keeps the exemption list honest: a listed fixture must still differ from Go."""
        for name in sorted(INTENTIONAL_DIVERGENCES):
            with self.subTest(fixture=name):
                golden = json.loads((GOLDEN_DIR / f"{name}.golden.json").read_text("utf-8"))
                ours = parse_m3u((GOLDEN_DIR / f"{name}.m3u").read_bytes())
                self.assertNotEqual(ours, golden["streams"])


class BehaviourTests(unittest.TestCase):
    def test_accepts_str_and_bytes(self) -> None:
        text = '#EXTM3U\n#EXTINF:-1 tvg-id="a" tvg-name="A",A\nhttp://x/1\n'
        self.assertEqual(parse_m3u(text), parse_m3u(text.encode()))

    def test_large_playlist_is_parsed(self) -> None:
        """The Go app's >10 MB code path returns zero channels; ours must not."""
        entry = '#EXTINF:-1 tvg-id="c.{i}" tvg-name="C{i}" group-title="G",Channel {i}\nhttp://x/{i}\n'
        text = "#EXTM3U\n" + "".join(entry.format(i=i) for i in range(120_000))
        self.assertGreater(len(text), 10 * 1024 * 1024)
        streams = parse_m3u(text)
        self.assertEqual(len(streams), 120_000)
        self.assertEqual(streams[-1]["name"], "Channel 119999")


if __name__ == "__main__":
    unittest.main()
