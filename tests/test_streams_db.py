"""Stream database parity with the real Go buildDatabaseDVR (tests/golden/streams)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from threadfin.providers import EXTENSIONS, ProviderManager
from threadfin.settings import Settings
from threadfin.streams_db import GroupCount, build_stream_database

GOLDEN = Path(__file__).parent / "golden" / "streams"
CASES = sorted(GOLDEN.glob("*.input.json"))


def comparable(streams: list[dict[str, str]]) -> list[dict[str, str]]:
    """Paths differ per run; Go visits providers in random order, so sort by provider."""
    out = [{**s, "_file.m3u.path": Path(s["_file.m3u.path"]).name} for s in streams]
    return sorted(out, key=lambda s: s["_file.m3u.id"])  # stable: keeps per-provider order


def setup(case: dict, data: Path) -> tuple[Settings, ProviderManager]:
    settings = Settings()
    settings.filter = case["filters"]
    settings.ignore_filters = case["ignoreFilters"]
    for p in case["providers"]:
        getattr(settings.files, p["kind"])[p["id"]] = {"name": p["name"]}
        if not p["missing"]:
            (data / (p["id"] + EXTENSIONS[p["kind"]])).write_text(p["content"], "utf-8")
    return settings, ProviderManager(settings, data, lambda: None)


class GoldenStreamDatabaseTests(unittest.TestCase):
    def test_cases_present(self) -> None:
        self.assertGreaterEqual(len(CASES), 25)

    def test_matches_go(self) -> None:
        for path in CASES:
            name = path.name.removesuffix(".input.json")
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                case = json.loads(path.read_text("utf-8"))
                golden = json.loads(path.with_name(f"{name}.golden.json").read_text("utf-8"))
                settings, manager = setup(case, Path(tmp))
                db = build_stream_database(manager, settings)

                self.assertEqual(comparable(db.all), comparable(golden["all"]))
                self.assertEqual(comparable(db.active), comparable(golden["active"]))
                self.assertEqual(comparable(db.inactive), comparable(golden["inactive"]))
                self.assertEqual(db.preview_active, golden["previewActive"])
                self.assertEqual(db.preview_inactive, golden["previewInactive"])
                self.assertEqual([g.name for g in db.groups], golden["groupsValue"])
                self.assertEqual([g.label for g in db.groups], golden["groupsText"])
                self.assertEqual(db.compatibility, golden["compatibility"])
                # Go returns an error whenever the last provider it visited had a bad copy;
                # we report each bad provider instead.
                missing = [p["id"] for p in case["providers"] if p["missing"]]
                self.assertEqual(sorted(db.errors), sorted(missing))
                # and the compatibility counts were stored on the provider records
                for pid, record in settings.files.m3u.items():
                    self.assertEqual(record["compatibility"], golden["compatibility"][pid])


class BehaviourTests(unittest.TestCase):
    def test_warnings(self) -> None:
        case = json.loads((GOLDEN / "no_filters_over_limit.input.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            settings, manager = setup(case, Path(tmp))
            db = build_stream_database(manager, settings)
            self.assertEqual(len(db.warnings), 1)
            self.assertIn("more than 480 streams", db.warnings[0])
            self.assertEqual((len(db.active), len(db.inactive)), (0, 500))
            db = build_stream_database(manager, settings, unfiltered_limit=1000, plex_limit=10)
            self.assertEqual((len(db.active), len(db.inactive)), (500, 0))
            self.assertIn("Plex can not handle more than 10 streams", db.warnings[0])

    def test_limit_boundaries(self) -> None:
        def run(count: int, **kw: object):
            content = "#EXTM3U\n" + "".join(f'#EXTINF:-1 tvg-id="i{i}",C{i}\nhttp://s/{i}\n' for i in range(count))
            case = {
                "filters": {},
                "ignoreFilters": bool(kw.get("ignore")),
                "providers": [{"kind": "m3u", "id": "M1", "name": "P", "content": content, "missing": False}],
            }
            with tempfile.TemporaryDirectory() as tmp:
                settings, manager = setup(case, Path(tmp))
                return build_stream_database(manager, settings)

        at, over = run(480), run(481)
        self.assertEqual((len(at.active), at.warnings), (480, []))
        self.assertEqual((len(over.active), len(over.inactive), len(over.warnings)), (0, 481, 1))
        self.assertEqual(run(480, ignore=True).warnings, [])
        self.assertEqual(len(run(481, ignore=True).warnings), 2)  # Plex limit and "no filter" (both, as in Go)

    def test_hdhr_sources_are_ignored(self) -> None:
        # Go's HDHomeRun tuner sources (files.hdhr) are not supported: kept in settings, never read.
        lineup = json.dumps([{"GuideNumber": "2.1", "GuideName": "WXYZ", "URL": "http://t/2.1"}])
        case = {"filters": {}, "ignoreFilters": False, "providers": [
            {"kind": "m3u", "id": "M1", "name": "P", "content": '#EXTM3U\n#EXTINF:-1,A\nhttp://s/1\n', "missing": False},
        ]}  # fmt: skip
        with tempfile.TemporaryDirectory() as tmp:
            settings, manager = setup(case, Path(tmp))
            settings.files.hdhr["H1"] = {"name": "Tuner"}
            (Path(tmp) / "H1.json").write_text(lineup, "utf-8")
            db = build_stream_database(manager, settings)
            self.assertEqual([s["_file.m3u.id"] for s in db.all], ["M1"])
            self.assertEqual((db.errors, db.compatibility.keys()), ({}, {"M1"}))
            self.assertEqual(settings.files.hdhr, {"H1": {"name": "Tuner"}})

    def test_error_names_the_provider_and_other_providers_still_load(self) -> None:
        case = json.loads((GOLDEN / "two_providers.input.json").read_text("utf-8"))
        case["providers"][0]["missing"] = True
        with tempfile.TemporaryDirectory() as tmp:
            settings, manager = setup(case, Path(tmp))
            db = build_stream_database(manager, settings)
            self.assertEqual({s["_file.m3u.id"] for s in db.all}, {"M2"})
            self.assertIn("M1: Local copy of the file no longer exists", db.errors["M1"])

    def test_invalid_copy_is_reported_not_raised(self) -> None:
        case = {
            "filters": {},
            "ignoreFilters": False,
            "providers": [{"kind": "m3u", "id": "M1", "name": "Bad", "content": "<html>", "missing": False}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings, manager = setup(case, Path(tmp))
            db = build_stream_database(manager, settings)
            self.assertIn("Invalid M3U", db.errors["M1"])

    def test_skipped_entries_are_surfaced(self) -> None:
        content = '#EXTM3U\n#EXTINF:-1 tvg-id="x",\nhttp://s/1\n#EXTINF:-1,Named\nhttp://s/2\n'
        case = {
            "filters": {},
            "ignoreFilters": False,
            "providers": [{"kind": "m3u", "id": "M1", "name": "P", "content": content, "missing": False}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            settings, manager = setup(case, Path(tmp))
            db = build_stream_database(manager, settings)
            self.assertEqual(len(db.all), 1)
            self.assertEqual([e.reason for e in db.skipped["M1"]], ["no name"])

    def test_group_labels(self) -> None:
        self.assertEqual(GroupCount("News", 3).label, "News (3)")


if __name__ == "__main__":
    unittest.main()
