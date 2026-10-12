"""HDHomeRun emulation parity with the real Go code (tests/golden/hdhr)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from threadfin.hdhr import Device, PMSIds, capability_xml, discover, lineup_pms, lineup_status, lineup_xepg
from threadfin.playlist import StreamRegistry
from threadfin.xepg import XEPGChannel

GOLDEN = Path(__file__).parent / "golden" / "hdhr"
CASES = sorted(GOLDEN.glob("*.input.json"))
# Go sends "null" for an empty XEPG lineup / gives empty numbers without pms.json: both fixed here.
DIVERGENT = {"xepg_empty", "pms_missing_file", "unknown_source", "pms_empty"}


def run(case: dict, tmp: Path) -> dict:
    s = case["settings"]
    device = Device(case["name"], "threadfin", case["version"], s["uuid"], s["tuner"], f"http://{case['domain']}")
    registry = StreamRegistry()
    pms_path = tmp / "pms.json"
    if "pms" in case or not case.get("noPmsFile"):
        pms_path.write_text(json.dumps(case.get("pms") or {}), "utf-8")
    pms = PMSIds(pms_path)
    if s["epgSource"] == "XEPG":
        channels = {k: XEPGChannel.from_json(v) for k, v in case["xepg"].items()}
        lineup = lineup_xepg(channels, registry, device.base_url)
    elif s["epgSource"] == "PMS":
        lineup = lineup_pms(case["streams"], pms, registry, device.base_url)
    else:
        lineup = []
    return {
        "discover": discover(device),
        "status": lineup_status(bool(case["scanInProgress"])),
        "lineup": lineup,
        "capability": capability_xml(device),
        "pms": pms.ids,
        "registry": {k: v.to_json() for k, v in registry.entries.items()},
    }


class Golden(unittest.TestCase):
    def test_cases(self) -> None:
        self.assertGreaterEqual(len(CASES), 25)
        for path in CASES:
            name = path.name.removesuffix(".input.json")
            with self.subTest(name), tempfile.TemporaryDirectory() as td:
                case = json.loads(path.read_text("utf-8"))
                golden = json.loads((GOLDEN / f"{name}.golden.json").read_text("utf-8"))
                got = run(case, Path(td))
                self.assertEqual(got["discover"], json.loads(golden["discover"]))
                self.assertEqual(got["status"], json.loads(golden["lineupStatus"]))
                self.assertEqual(got["capability"], golden["capability"])
                if name in DIVERGENT:
                    continue
                want = json.loads(golden["lineup"])
                if case["settings"]["epgSource"] == "XEPG":
                    # Go listed channels in random map order, so compare as sets and check our order below
                    key = lambda e: e["GuideNumber"]  # noqa: E731
                    self.assertEqual(sorted(got["lineup"], key=key), sorted(want, key=key))
                    numbers = [float(e["GuideNumber"]) for e in got["lineup"]]
                    self.assertEqual(numbers, sorted(numbers))
                    # for URLs shared by several channels Go kept an arbitrary one
                    self.assertEqual(set(got["registry"]), set(golden["registry"]))
                else:
                    self.assertEqual(got["lineup"], want)
                    self.assertEqual(got["registry"], golden["registry"])
                self.assertEqual(got["pms"], golden.get("pms") or {})

    def test_divergences(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            case = json.loads((GOLDEN / "xepg_empty.input.json").read_text("utf-8"))
            self.assertEqual(run(case, Path(td))["lineup"], [])  # Go: null
            self.assertEqual(json.loads(json.loads((GOLDEN / "xepg_empty.golden.json").read_text("utf-8"))["lineup"]), None)
        with tempfile.TemporaryDirectory() as td:
            case = json.loads((GOLDEN / "pms_missing_file.input.json").read_text("utf-8"))
            numbers = [e["GuideNumber"] for e in run(case, Path(td))["lineup"]]
            self.assertEqual(numbers, ["id-0", "id-1"])  # Go: ["", ""]


class Units(unittest.TestCase):
    def test_pms_numbers_are_stable_and_fill_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pms.json"
            path.write_text('{"a": "id-0", "b": "id-2"}')
            ids = PMSIds(path)
            self.assertEqual(ids.number_for("c"), "id-1")
            self.assertEqual(ids.number_for("d"), "id-3")
            self.assertEqual(ids.number_for("a"), "id-0")
            self.assertEqual(PMSIds(path).ids, ids.ids)  # persisted

    def test_xepg_lineup_is_in_channel_order(self) -> None:
        def make(n: str) -> XEPGChannel:
            return XEPGChannel.from_json({"x-active": True, "x-name": n, "x-channelID": n, "tvg-chno": n,
                                          "x-epg": f"x-ID.{n}", "url": f"http://p/{n}", "_file.m3u.id": "M1"})

        channels = {f"x-ID.{n}": make(n) for n in ("10", "2", "1000", "1")}
        numbers = [e["GuideNumber"] for e in lineup_xepg(channels, StreamRegistry(), "http://h")]
        self.assertEqual(numbers, ["1", "2", "10", "1000"])

    def test_device_id(self) -> None:
        self.assertEqual(Device("n", "a", "v", "U", 1, "").device_id, "U")
        self.assertEqual(Device("n", "a", "v", "U", 4, "").device_id, "U:4")


if __name__ == "__main__":
    unittest.main()
