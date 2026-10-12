"""XMLTV guide parity with the real Go code (tests/golden/epg), compared as parsed XML."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from threadfin import epg
from threadfin.epg import EPGOptions, dummy_programmes, live_programmes, sort_key, strip_non_ascii, write_xmltv
from threadfin.xepg import XEPGChannel

GOLDEN = Path(__file__).parent / "golden" / "epg"
CASES = sorted(GOLDEN.glob("*.input.json"))

GO_ONLY_CHANNEL_CHILDREN = {"live", "active"}
EMPTY_PROGRAMME_CHILDREN = {"credits", "video", "date"}


def normalise(element: ElementTree.Element, is_root: bool = False) -> tuple:
    """Canonical form; drops what Go emits but carries no information (see epg.py docstring)."""
    attrs = {k: v for k, v in element.attrib.items() if v != ""}
    children = []
    for child in element:
        if is_root and child.tag in ("generator", "source"):
            attrs["generator-info-name" if child.tag == "generator" else "source-info-name"] = (child.text or "").strip()
            continue
        if element.tag == "channel" and child.tag in GO_ONLY_CHANNEL_CHILDREN:
            continue
        canon = normalise(child)
        if element.tag == "programme" and child.tag in EMPTY_PROGRAMME_CHILDREN and canon[1:] == ({}, "", ()):
            continue
        children.append(canon)
    text = (element.text or "").strip() if not children else ""
    if element.tag == "icon" and not attrs and not children:
        text = ""
    # Go's output order is struct order, ours is DTD order: compare per tag, keeping order within a tag.
    grouped = tuple(sorted(children, key=lambda c: c[0]))
    return (element.tag, attrs, text, grouped)



def expected_programme_channels(root: ElementTree.Element) -> list:
    return [normalise(e) for e in root if e.tag == "programme"]


def render(case: dict, tmp: Path) -> tuple[str, datetime]:
    from threadfin.xepg import XEPGChannel as C

    for name, body in case["input"]["xmltv"].items():
        (tmp / name).write_text(body, "utf-8")
    s = case["input"]["settings"]
    options = EPGOptions(
        enable_non_ascii=s["enableNonAscii"],
        replace_missing_images=s["xepg.replace.missing.images"],
        replace_channel_title=s["xepg.replace.channel.title"],
        generator="Threadfin",
        source="Threadfin - 1.0.0",
    )
    now = datetime.fromisoformat(case["golden"]["generatedAt"][:26] + case["golden"]["generatedAt"][-6:])
    now = now.astimezone(ZoneInfo(case["input"]["tz"]))
    channels = {k: C.from_json(v) for k, v in case["input"]["xepg"].items()}
    out = tmp / "threadfin.xml"
    write_xmltv(out, channels, tmp, options, now)
    return out.read_text("utf-8"), now


def channel_map(root: ElementTree.Element) -> dict:
    return {e.get("id"): normalise(e) for e in root if e.tag == "channel"}


class GoldenGuide(unittest.TestCase):
    def test_all_cases(self) -> None:
        self.assertGreaterEqual(len(CASES), 50)
        for path in CASES:
            name = path.name.removesuffix(".input.json")
            with self.subTest(name), tempfile.TemporaryDirectory() as td:
                case = {
                    "input": json.loads(path.read_text("utf-8")),
                    "golden": json.loads((GOLDEN / f"{name}.golden.json").read_text("utf-8")),
                }
                got_xml, _ = render(case, Path(td))
                got = ElementTree.fromstring(got_xml)
                want = ElementTree.fromstring(case["golden"]["xml"])
                self.assertEqual(normalise(got, True)[1], normalise(want, True)[1], "tv attributes")
                self.assertEqual(channel_map(got), channel_map(want), "channels")
                self.assertEqual(
                    [e.get("id") for e in got if e.tag == "channel"],
                    [e.get("id") for e in want if e.tag == "channel"],
                    "channel order",
                )
                gp = [e for e in got if e.tag == "programme"]
                wp = [e for e in want if e.tag == "programme"]
                self.assertEqual(len(gp), len(wp), "programme count")
                self.assertEqual(
                    [(e.get("channel"), e.get("start")) for e in gp],
                    [(e.get("channel"), e.get("start")) for e in wp],
                    "programme order",
                )
                for g, w in zip(gp, wp):
                    self.assertEqual(normalise(g), normalise(w))


def channel(**kw) -> XEPGChannel:
    base = {
        "_file.m3u.id": "M1", "_file.m3u.name": "P", "_file.m3u.path": "/d/M1.m3u", "group-title": "G",
        "name": "Alpha", "tvg-id": "a", "tvg-logo": "", "tvg-name": "Alpha", "tvg-chno": "1", "url": "http://p/1",
        "x-active": True, "x-channelID": "1", "x-epg": "x-ID.1", "x-mapping": "30_Minutes",
        "x-xmltv-file": "Threadfin Dummy", "x-name": "Alpha", "x-ppv-extra": "", "x-category": "",
    }
    base.update(kw)
    return XEPGChannel.from_json(base)


SYD = ZoneInfo("Australia/Sydney")


class Units(unittest.TestCase):
    def test_strip_non_ascii(self) -> None:
        self.assertEqual(strip_non_ascii("Café Ünï 1"), "Caf n 1")

    def test_dummy_count_and_layout(self) -> None:
        now = datetime(2026, 10, 12, 10, 20, tzinfo=SYD)
        progs = dummy_programmes(channel(), EPGOptions(), now)
        self.assertEqual(len(progs), 4 * 24 * 2)
        self.assertEqual(progs[0].titles[0][1].split(" (")[0], "Alpha")

    def test_live_digit_changes_start(self) -> None:
        now = datetime(2026, 10, 12, 10, 20, tzinfo=SYD)
        a = channel(**{"x-mapping": "PPV", "x-name": "Fight 7", "x-ppv-extra": "x"})
        b = channel(**{"x-mapping": "PPV", "x-name": "Fight", "x-ppv-extra": "x"})
        pa = live_programmes(a, "1", EPGOptions(), now)
        pb = live_programmes(b, "1", EPGOptions(), now)
        self.assertNotEqual([p.start for p in pa], [p.start for p in pb])

    def test_sort_key(self) -> None:
        cs = [channel(**{"tvg-chno": c, "x-epg": f"x-ID.{i}"}) for i, c in enumerate(["10", "2", "abc", "1.5"])]
        self.assertEqual([c.tvg_chno for c in sorted(cs, key=sort_key)], ["1.5", "2", "10", "abc"])

    def test_clean_replaces_control_chars(self) -> None:
        self.assertEqual(epg._clean("a\x00b\x0bc퟿d"), "a�b�c퟿d")
        self.assertEqual(epg._clean("tab\tok\n"), "tab\tok\n")
        self.assertEqual(epg._clean("x￾y￿"), "x�y�")
        self.assertEqual(epg._clean("😀"), "😀")

    def test_gzip_and_atomic(self) -> None:
        import gzip

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "t.xml"
            write_xmltv(out, {"x-ID.1": channel()}, td, EPGOptions(), datetime(2026, 10, 12, 10, tzinfo=SYD),
                        gzip_path=Path(td) / "t.xml.gz")
            self.assertEqual(gzip.open(Path(td) / "t.xml.gz").read(), out.read_bytes())
            self.assertEqual([p.name for p in Path(td).glob(".*")], [])
            ElementTree.parse(out)


if __name__ == "__main__":
    unittest.main()
