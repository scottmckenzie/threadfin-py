"""XMLTV guide parity with the real Go code (tests/golden/epg), compared as parsed XML."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from threadfin import epg
from threadfin.epg import (
    EPGOptions,
    dummy_programmes,
    event_start,
    live_programmes,
    sort_key,
    strip_non_ascii,
    write_xmltv,
)
from threadfin.xepg import XEPGChannel

GOLDEN = Path(__file__).parent / "golden" / "epg"
CASES = sorted(GOLDEN.glob("*.input.json"))

GO_ONLY_CHANNEL_CHILDREN = {"live", "active"}
# Go's live-event parser never read the time in the channel name (always 06:00 if the name has a
# digit); ours does. In these cases the PPV programmes' start / stop differ on purpose and are checked
# in LiveEvent below; titles, descriptions and the number of programmes still have to match Go.
PARSED_PPV_TIME_CASES = {
    "ppv_01", "ppv_02", "ppv_03", "ppv_04", "ppv_05", "ppv_06", "ppv_07", "ppv_09", "ppv_10",
    "ppv_replace_title", "ppv_replace_title_non_ascii_enabled", "ppv_replace_title_without_extra",
    "ppv_uses_tvg_name_when_no_x_name", "ppv_tz_UTC", "ppv_tz_America_New_York", "ppv_tz_Pacific_Auckland",
    "mixed_guide_types",
}  # fmt: skip
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


def without_times(programmes: list[ElementTree.Element], channels: set[str]) -> list[ElementTree.Element]:
    """The programmes, those of ``channels`` without start / stop, in a stable order."""
    out = []
    for e in programmes:
        if e.get("channel") in channels:
            copy = ElementTree.Element(e.tag, {k: v for k, v in e.attrib.items() if k not in ("start", "stop")})
            copy.extend(e)
            e = copy
        out.append(e)
    return sorted(out, key=lambda e: (e.get("channel") or "", e.get("start") or "", ElementTree.tostring(e)))


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
                if name in PARSED_PPV_TIME_CASES:
                    ppv = {c["x-channelID"] for c in case["input"]["xepg"].values() if c["x-mapping"] == "PPV"}
                    self.assertTrue(ppv)
                    gp, wp = without_times(gp, ppv), without_times(wp, ppv)
                self.assertEqual(
                    [(e.get("channel"), e.get("start")) for e in gp],
                    [(e.get("channel"), e.get("start")) for e in wp],
                    "programme order",
                )
                for g, w in zip(gp, wp, strict=True):
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
            with gzip.open(Path(td) / "t.xml.gz") as unpacked:
                self.assertEqual(unpacked.read(), out.read_bytes())
            self.assertEqual([p.name for p in Path(td).glob(".*")], [])
            ElementTree.parse(out)


if __name__ == "__main__":
    unittest.main()


class LiveEvent(unittest.TestCase):
    """The event time in a PPV channel name (Go never parsed it; see PARSED_PPV_TIME_CASES)."""

    NOW = datetime(2026, 10, 13, 10, 20, tzinfo=SYD)

    def start(self, name: str, now: datetime | None = None) -> datetime | None:
        return event_start(name, now or self.NOW)

    def test_times(self) -> None:
        for name, want in [
            ("Fight Night 9PM", (2026, 10, 13, 21, 0)),
            ("6pm", (2026, 10, 13, 18, 0)),
            ("Café 9 PM", (2026, 10, 13, 21, 0)),
            ("Match 20:00", (2026, 10, 13, 20, 0)),
            ("Match 7:05", (2026, 10, 13, 7, 5)),
            ("Fight 9 p.m.", (2026, 10, 13, 21, 0)),
            ("12AM", (2026, 10, 13, 0, 0)),
            ("12PM", (2026, 10, 13, 12, 0)),
            ("UFC 300 9PM", (2026, 10, 13, 21, 0)),
            ("Event 2026 8pm", (2026, 10, 13, 20, 0)),
            ("NFL 12/25-1:00 PM", (2026, 12, 25, 13, 0)),
            ("Event 7/4 12:00 PM", (2026, 7, 4, 12, 0)),
            ("Game 3.21 3:45 AM", (2027, 3, 21, 3, 45)),  # nearer than March 2026
            ("Show 4/15 3AM", (2026, 4, 15, 3, 0)),
        ]:
            with self.subTest(name):
                self.assertEqual(self.start(name), datetime(*want, tzinfo=SYD))

    def test_not_times(self) -> None:
        for name in ["UFC 300 Main Card", "Plain Event", "Channel 5", "Ch 13:75", "13PM", "0AM", "24:00", "2/30 9PM",
                     "Score 3.5", "9 Pmax", "100PM", "Ch 9:60", "Ch 1:000 PM"]:  # fmt: skip
            with self.subTest(name):
                self.assertIsNone(self.start(name))

    def test_timezones(self) -> None:
        # Sydney 10:20 on 13 Oct is 19:20 on 12 Oct in New York: "7:30 PM ET" is that evening.
        self.assertEqual(self.start("UFC 7:30 PM ET"), datetime(2026, 10, 13, 10, 30, tzinfo=SYD))
        self.assertEqual(self.start("UFC 7:30 PM EST"), self.start("UFC 7:30 PM EDT"))
        self.assertEqual(self.start("NBA 11:59PM PST"), datetime(2026, 10, 13, 17, 59, tzinfo=SYD))
        self.assertEqual(self.start("x 8PM CT"), datetime(2026, 10, 12, 20, tzinfo=ZoneInfo("America/Chicago")))
        self.assertEqual(self.start("x 8PM MT"), datetime(2026, 10, 12, 20, tzinfo=ZoneInfo("America/Denver")))
        self.assertEqual(self.start("x 20:00 UTC"), datetime(2026, 10, 12, 20, tzinfo=UTC))
        # winter in New York: EST is UTC-5
        self.assertEqual(self.start("NFL 12/25-1:00 PM ET"), datetime(2026, 12, 25, 18, tzinfo=UTC))
        self.assertEqual(self.start("x 9PM ETC"), datetime(2026, 10, 13, 21, tzinfo=SYD))  # not a zone
        self.assertEqual(self.start("x 9PM").tzinfo, SYD)

    def test_year_rollover(self) -> None:
        self.assertEqual(self.start("12/31 11PM", datetime(2027, 1, 1, 1, tzinfo=SYD)), datetime(2026, 12, 31, 23, tzinfo=SYD))
        self.assertEqual(self.start("1/1 1AM", datetime(2026, 12, 31, 23, tzinfo=SYD)), datetime(2027, 1, 1, 1, tzinfo=SYD))

    def programmes(self, name: str, now: datetime | None = None, **options) -> list[tuple[str, str]]:
        c = channel(**{"x-mapping": "PPV", "x-name": name, "x-ppv-extra": "Main"})
        return [(p.start, p.stop) for p in live_programmes(c, "PPV", EPGOptions(**options), now or self.NOW)]

    def test_guide_today(self) -> None:
        self.assertEqual(self.programmes("Fight 9PM"), [
            ("20261013000000 +1100", "20261013210000 +1100"),
            ("20261013210000 +1100", "20261013235959 +1100"),
            ("20261014000000 +1100", "20261014235959 +1100"),
        ])  # fmt: skip

    def test_guide_fallbacks(self) -> None:
        self.assertEqual(self.programmes("UFC 300")[1][0], "20261013060000 +1100")
        self.assertEqual(self.programmes("Plain")[0], ("20261013000000 +1100", "20261013000000 +1100"))
        self.assertEqual(self.programmes("Plain")[1][0], "20261013000000 +1100")

    def test_guide_future_day(self) -> None:
        # offline from today until the event, the event to the end of its day, offline the day after
        self.assertEqual(self.programmes("NFL 12/25-1:00 PM"), [
            ("20261013000000 +1100", "20261225130000 +1100"),
            ("20261225130000 +1100", "20261225235959 +1100"),
            ("20261226000000 +1100", "20261226235959 +1100"),
        ])  # fmt: skip

    def test_guide_past_day(self) -> None:
        self.assertEqual(self.programmes("Show 10/12 3PM"), [
            ("20261012000000 +1100", "20261012150000 +1100"),
            ("20261012150000 +1100", "20261012235959 +1100"),
            ("20261013000000 +1100", "20261013235959 +1100"),
        ])  # fmt: skip

    def test_guide_late_event_runs_three_hours(self) -> None:
        self.assertEqual(self.programmes("Fight 11:30 PM")[1:], [
            ("20261013233000 +1100", "20261014023000 +1100"),
            ("20261014023000 +1100", "20261014235959 +1100"),
        ])  # fmt: skip
        self.assertEqual(self.programmes("Fight 9PM")[1][1], "20261013235959 +1100")  # exactly 3h: day end
        self.assertEqual(self.programmes("Fight 9:01PM")[1][1], "20261014000100 +1100")

    def test_guide_crosses_midnight_in_local_time(self) -> None:
        # 7:30 PM in Los Angeles on 12 Oct is 13:30 on 13 Oct in Sydney
        self.assertEqual(self.programmes("UFC 7:30 PM PT")[1][0], "20261013133000 +1100")

    def test_title_still_replaced(self) -> None:
        c = channel(**{"x-mapping": "PPV", "x-name": "Fight 9PM", "x-ppv-extra": "Main"})
        main = live_programmes(c, "PPV", EPGOptions(replace_channel_title=True), self.NOW)[1]
        self.assertEqual(main.titles, [("en", "Fight 9PM Main")])

    def test_fixed_offset_now(self) -> None:
        # the server passes datetime.now().astimezone(): a fixed offset, not a zone
        now = self.NOW.astimezone(timezone(timedelta(hours=11)))
        self.assertEqual(self.programmes("Fight 9PM", now)[1][0], "20261013210000 +1100")
