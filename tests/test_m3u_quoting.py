"""Quotes, commas and ``=`` inside channel names (Go parsed attributes inside names too).

Go removed every ``key="value"``-looking piece from the whole ``#EXTINF`` line, including the
name, so ``The "Best" Channel`` became ``The  Channel`` and a fully quoted name left the
channel unnamed. Here attributes are only read before the name, which starts at the first
comma outside a quoted attribute value.
"""

from __future__ import annotations

import hashlib
import json
import random
import unittest
from pathlib import Path

from threadfin.m3u import parse_m3u

GOLDEN_DIR = Path(__file__).parent / "golden" / "m3u"


def one(line: str) -> dict[str, str]:
    (stream,) = parse_m3u(f"#EXTM3U\n{line}\nhttp://x/1\n")
    return stream


class NameQuotingTests(unittest.TestCase):
    def test_fixture(self) -> None:
        streams = parse_m3u((GOLDEN_DIR / "double_quotes_in_name.m3u").read_bytes())
        self.assertEqual([s["name"] for s in streams], ['The "Best" Channel', '"Quoted Name"'])
        self.assertEqual([s["group-title"] for s in streams], ["G", "G"])

    def test_go_output_lost_text(self) -> None:
        """Documents the Go bug (Go golden = corrupted data)."""
        go = json.loads((GOLDEN_DIR / "double_quotes_in_name.golden.json").read_text("utf-8"))
        self.assertEqual(go["streams"][0]["name"], "The  Channel")
        self.assertNotIn("name", go["streams"][1])  # fully quoted name: channel left unnamed

    def test_attribute_lookalikes_in_name_are_not_attributes(self) -> None:
        stream = one('#EXTINF:-1 tvg-id="a",Show title="Pilot" HD')
        self.assertEqual(stream["name"], 'Show title="Pilot" HD')
        self.assertNotIn("title", stream)
        self.assertEqual(one("#EXTINF:-1 tvg-id=\"a\",Level='5' Channel")["name"], "Level='5' Channel")

    def test_commas_inside_attribute_values_do_not_start_the_name(self) -> None:
        stream = one('#EXTINF:-1 group-title="News, Weather" tvg-name="A, B",Real Name')
        self.assertEqual(
            (stream["group-title"], stream["tvg-name"], stream["name"]),
            ("News, Weather", "A, B", "Real Name"),
        )

    def test_commas_and_quotes_in_name(self) -> None:
        self.assertEqual(one('#EXTINF:-1 tvg-id="a",Say "hi", world')["name"], 'Say "hi", world')

    def test_no_attributes(self) -> None:
        self.assertEqual(one('#EXTINF:-1,"Quoted"')["name"], '"Quoted"')

    def test_unbalanced_quote_in_attributes_behaves_like_go(self) -> None:
        # No closing quote: nothing matches as an attribute, so the first comma starts the name.
        stream = one('#EXTINF:-1 tvg-name="Foo,Bar')
        self.assertEqual(stream["name"], "Bar")
        self.assertNotIn("tvg-name", stream)


# --- randomized property test: build playlists from a model, expect exact recovery -----------

ATTR_KEYS = ["tvg-id", "tvg-name", "tvg-logo", "group-title", "tvg-chno"]
ALPHABET = "abcXYZ019 ,=#:/&éあ"  # no "-" so ":-1" can never appear; no newlines


def text(rnd: random.Random, extra: str = "", low: int = 0, high: int = 14) -> str:
    return "".join(rnd.choice(ALPHABET + extra) for _ in range(rnd.randint(low, high)))


def build(rnd: random.Random, i: int) -> tuple[str, dict[str, str] | None]:
    """One #EXTINF entry plus the stream we expect it to produce (None = skipped)."""
    attrs: dict[str, str] = {}
    parts = []
    for key in rnd.sample(ATTR_KEYS, rnd.randint(0, len(ATTR_KEYS))):
        if rnd.random() < 0.25:  # single-quoted: value may hold double quotes, never apostrophes
            value = text(rnd, '"')
            parts.append(f"{key}='{value}'")
        else:  # double-quoted: value may hold apostrophes, never double quotes
            value = text(rnd, "'")
            parts.append(f'{key}="{value}"')
        attrs[key] = value
    name = text(rnd, "'\"=,", 0, 18)  # names may contain anything
    sep = rnd.choice([",", ", "])
    line = "#EXTINF:" + rnd.choice(["-1", "0", "123"]) + "".join(" " + p for p in parts) + sep + name
    url = f"http://h/{i}"

    shown = name.rstrip(" ") if name else ""
    if sep == ", ":
        shown = (" " + name).rstrip(" ")
    display = shown or attrs.get("tvg-name", "").rstrip(" ")
    entry = "\n".join([line, url])
    if not display:
        return entry, None

    stream = {"url": url}
    values = []
    for key, value in attrs.items():
        stream[key] = value
        if "://" not in value and value:
            values.append(value + " ")
    if stream.get("tvg-id", "") == "":
        stream["tvg-id"] = "threadfin-" + hashlib.md5(url.encode()).hexdigest()
    stream["name"] = display
    stream["_values"] = "".join(values) + display
    if "tvg-name" in attrs:
        stream["_uuid.key"], stream["_uuid.value"] = "tvg-name", attrs["tvg-name"]
    return entry, stream


class PropertyTests(unittest.TestCase):
    def test_random_entries_are_recovered_exactly(self) -> None:
        rnd = random.Random(7)
        for round_ in range(40):
            built = [build(rnd, round_ * 100 + n) for n in range(rnd.randint(1, 12))]
            eol = rnd.choice(["\n", "\r\n"])
            playlist = eol.join(["#EXTM3U"] + [e for e, _ in built]) + eol
            expected = [s for _, s in built if s is not None]
            with self.subTest(round=round_):
                self.assertEqual(parse_m3u(playlist), expected)


if __name__ == "__main__":
    unittest.main()
