"""Settings page and filter changes against the real Go code (tests/golden/admin).

Each case loads the same settings.json as the Go harness, applies the web UI's ``saveSettings`` /
``saveFilter`` request and compares settings.json afterwards (and the folders created). Where Go
failed to decode the request, refused it or panicked, nothing may change here either.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from threadfin.admin import (
    FFMPEG_NOT_FOUND,
    INVALID_FILTER_RULE,
    VLC_NOT_FOUND,
    AdminError,
    apply_channel_changes,
    apply_filter_changes,
    bulk_channel_changes,
    channel_changes,
    check_mapping,
    logo_filename,
    provider_changes,
    update_settings,
)
from threadfin.settings import load_settings, save_settings
from threadfin.system import SystemInfo
from threadfin.xepg import XEPGChannel

GOLDEN = Path(__file__).parent / "golden" / "admin"
CASES = sorted(GOLDEN.glob("*.input.json"))

# Go texts we keep; other errors only have to happen.
EXACT_ERRORS = {FFMPEG_NOT_FOUND, VLC_NOT_FOUND, INVALID_FILTER_RULE}
# Go accepted these (unknown keys ignored); refused here, with the same file afterwards.
REFUSED_HERE = {"unknown_and_fixed_keys"}
# Different file on purpose; asserted in Divergences.
DIVERGENT = {
    "exclude_stream_https",  # Go's request struct misspelled the key, so it could never be saved
    "rule_not_a_string",  # Go saved the broken filter (and every later build failed)
}


def normalise_filters(settings: dict[str, Any]) -> dict[str, Any]:
    """Go nested the whole request ({id: data, ...}) in a new filter; that copy is dropped here."""
    filters = settings.get("filter")
    if isinstance(filters, dict):
        settings = {**settings, "filter": {k: {kk: vv for kk, vv in v.items() if not kk.lstrip("-").isdigit()}
                                           for k, v in filters.items()}}  # fmt: skip
    return settings


class Case:
    def __init__(self, name: str, work: Path) -> None:
        self.name = name
        text = (GOLDEN / f"{name}.input.json").read_text().replace("{work}", str(work))
        self.spec = json.loads(text)
        self.golden = json.loads((GOLDEN / f"{name}.golden.json").read_text())
        self.work = work
        for folder in self.spec["dirs"]:
            (work / folder).mkdir(parents=True, exist_ok=True)
        for rel in self.spec["files"]:
            path = work / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
        self.system = SystemInfo(config=work / "cfg", temp=work / "tmp")
        self.system.create_folders()
        self.system.file_settings.write_text(json.dumps(self.spec["settings"]))
        self.settings = load_settings(self.system)

    def run(self) -> str:
        """Apply the request; return the error ('' if accepted)."""
        request = self.spec["request"]
        try:
            if self.spec["op"] == "settings":
                new, _ = update_settings(self.settings, request["settings"])
                save_settings(new, self.system)
            else:
                filters, _ = apply_filter_changes(self.settings.filter, request["filter"])
                self.settings.filter = filters
                save_settings(self.settings, self.system)
        except AdminError as exc:
            return str(exc) or "error"
        return ""

    def saved(self) -> dict[str, Any]:
        text = self.system.file_settings.read_text().replace(str(self.work), "{work}")
        return normalise_filters(json.loads(text))

    def dirs(self) -> list[str]:
        return sorted(p.relative_to(self.work).as_posix() for p in self.work.rglob("*") if p.is_dir())


class Base(unittest.TestCase):
    def case(self, name: str) -> Case:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Case(name, Path(tmp.name))


class Goldens(Base):
    def test_there_are_cases(self) -> None:
        self.assertGreaterEqual(len(CASES), 50)

    def test_cases(self) -> None:
        for path in CASES:
            name = path.name.removesuffix(".input.json")
            if name in DIVERGENT:
                continue
            with self.subTest(name):
                case = self.case(name)
                err = case.run()
                golden = case.golden
                go_err = golden["err"] or golden["unmarshalErr"] or golden["panic"]
                if name in REFUSED_HERE:
                    self.assertTrue(err)
                elif go_err in EXACT_ERRORS:
                    self.assertEqual(err, go_err)
                else:
                    self.assertEqual(bool(err), bool(go_err), err or go_err)
                self.assertEqual(case.saved(), normalise_filters(golden["settings"]))
                self.assertEqual(case.dirs(), golden["dirs"])

    def test_divergent_cases_really_differ(self) -> None:
        for name in DIVERGENT:
            with self.subTest(name):
                case = self.case(name)
                case.run()
                self.assertNotEqual(case.saved(), normalise_filters(case.golden["settings"]))


class Divergences(Base):
    def test_exclude_stream_https_can_be_saved(self) -> None:
        case = self.case("exclude_stream_https")
        self.assertEqual(case.run(), "")
        self.assertIs(case.saved()["excludeStreamHttps"], True)

    def test_filter_rule_must_be_text(self) -> None:
        case = self.case("rule_not_a_string")
        self.assertIn("invalid value 5", case.run())
        self.assertEqual(case.saved()["filter"], {})

    def test_new_filter_is_not_nested(self) -> None:
        filters, ids = apply_filter_changes({}, {"-1": {"name": "A", "type": "custom-filter", "filter": "x"}})
        self.assertEqual((filters, ids), ({"0": {"name": "A", "type": "custom-filter", "filter": "x"}}, ["0"]))

    def test_refused_request_changes_nothing(self) -> None:
        """Go applied a bad buffer setting in memory and saved it with the next change."""
        case = self.case("buffer_ffmpeg_without_path")
        before = case.settings.to_json_dict()
        with self.assertRaises(AdminError):
            update_settings(case.settings, {"buffer": "ffmpeg", "ffmpeg.path": ""})
        self.assertEqual(case.settings.to_json_dict(), before)

    def test_changed_keys(self) -> None:
        case = self.case("tuner")
        new, changed = update_settings(case.settings, {"tuner": 4, "api": False, "update": ["0300"]})
        self.assertEqual(changed, {"tuner", "update"})
        self.assertEqual((new.tuner, case.settings.tuner), (4, 1))

    def test_choices(self) -> None:
        case = self.case("tuner")
        for change in ({"epgSource": "XML"}, {"buffer": "mplayer"}):
            with self.subTest(change), self.assertRaisesRegex(AdminError, "must be one of"):
                update_settings(case.settings, change)

    def test_filter_types(self) -> None:
        for data in ({"type": "regex", "filter": "x"}, {"caseSensitive": "yes"}, {"name": 1}):
            with self.subTest(data), self.assertRaises(AdminError):
                apply_filter_changes({}, {"-1": data})


class Providers(unittest.TestCase):
    def test_fields(self) -> None:
        self.assertEqual(
            provider_changes("m3u", {"name": "A", "file.source": " http://x/a.m3u ", "tuner": "3"}, new=True),
            {"name": "A", "file.source": "http://x/a.m3u", "tuner": 3},
        )
        self.assertEqual(provider_changes("xmltv", {"description": "d"}, new=False), {"description": "d"})

    def test_refused(self) -> None:
        for kind, data, new in (
            ("m3u", {"name": "A"}, True),  # no source
            ("m3u", {"file.source": ""}, False),
            ("m3u", {"tuner": 0}, False),
            ("m3u", {"tuner": 101}, False),
            ("m3u", {"tuner": True}, False),
            ("xmltv", {"tuner": 1}, False),  # guides have no tuners
            ("m3u", {"buffer": "x"}, False),
            ("m3u", {"file.m3u.path": "/etc"}, False),  # not editable
            ("m3u", {"name": 5}, False),
            ("dvb", {"name": "A"}, False),
        ):
            with self.subTest(kind=kind, data=data), self.assertRaises(AdminError):
                provider_changes(kind, data, new=new)


XMLTV_FILES: dict[str, dict[str, Any]] = {"X1.xml": {"bbc": {}, "itv": {}}, "Threadfin Dummy": {"30_Minutes": {}}}


class Channels(unittest.TestCase):
    FILES = XMLTV_FILES

    def test_changes(self) -> None:
        changes = channel_changes(
            {"x-name": "BBC One", "x-channelID": "101", "x-active": True, "x-xmltv-file": "X1.xml",
             "x-mapping": "bbc"}, self.FILES)  # fmt: skip
        channel = apply_channel_changes(XEPGChannel(x_channel_id="1000"), changes)
        self.assertEqual((channel.x_name, channel.x_channel_id, channel.x_mapping), ("BBC One", "101", "bbc"))
        check_mapping(channel, self.FILES)

    def test_refused(self) -> None:
        for data in ({"x-channelID": "abc"}, {"x-channelID": "0"}, {"x-active": "yes"}, {"url": "http://x"},
                     {"x-xmltv-file": "nope.xml"}, {"x-name": None}):  # fmt: skip
            with self.subTest(data), self.assertRaises(AdminError):
                channel_changes(data, self.FILES)

    def test_mapping_needs_a_file(self) -> None:
        with self.assertRaisesRegex(AdminError, "choose an XMLTV file"):
            check_mapping(XEPGChannel(x_xmltv_file="-", x_mapping="bbc"), self.FILES)

    def test_mapping_checks(self) -> None:
        bad = XEPGChannel(x_xmltv_file="X1.xml", x_mapping="cnn")
        with self.assertRaisesRegex(AdminError, "no channel"):
            check_mapping(bad, self.FILES)
        with self.assertRaisesRegex(AdminError, "needs an XMLTV"):
            check_mapping(XEPGChannel(x_active=True, x_xmltv_file="-", x_mapping="-"), self.FILES)
        unmapped = apply_channel_changes(XEPGChannel(x_xmltv_file="X1.xml", x_mapping="bbc"),
                                         channel_changes({"x-xmltv-file": "-"}, self.FILES))  # fmt: skip
        self.assertEqual(unmapped.x_mapping, "-")

    def test_bulk(self) -> None:
        self.assertEqual(bulk_channel_changes({"x-active": False, "x-channels-start": "200"}),
                         {"x_active": False, "x-channels-start": 200.0})  # fmt: skip
        with self.assertRaises(AdminError):
            bulk_channel_changes({"x-name": "same for all"})


class Logos(unittest.TestCase):
    def test_names(self) -> None:
        self.assertEqual(logo_filename("../../etc/My Logo.PNG"), "My_Logo.PNG")
        self.assertEqual(logo_filename("C:\\pics\\a.svg"), "a.svg")
        for bad in ("logo.exe", "noext", ".png"):
            with self.subTest(bad), self.assertRaises(AdminError):
                logo_filename(bad)


if __name__ == "__main__":
    unittest.main()
