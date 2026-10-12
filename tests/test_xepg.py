"""XEPG channel database parity with the real Go code (tests/golden/xepg)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from threadfin.xepg import (
    BackupStream,
    XEPGChannel,
    build_xepg,
    channel_hash,
    cleanup,
    go_g,
    parse_float,
    remove_duplicates,
)
from threadfin.xmltv import build_mapping, make_program_counter

GOLDEN = Path(__file__).parent / "golden" / "xepg"
CASES = sorted(GOLDEN.glob("*.input.json"))

# Cases where this port deliberately differs from Go (each is asserted separately below).
INTENTIONAL_DIVERGENCES = {
    "filter_without_starting_number_fix",
    "backup_cleared_keeps_stale_pointer_in_go",
    "provider_chno_collision_fix",
    "existing_found_by_uuid",
}


def run_case(case: dict, data: Path) -> tuple[dict[str, XEPGChannel], object, dict]:
    for name, body in case["xmltv"].items():
        (data / f"{name}.xml").write_text(body, "utf-8")
    settings = case["settings"]
    files = [(f"{pid}.xml", data / f"{pid}.xml") for pid in settings["files"]["xmltv"] if (data / f"{pid}.xml").exists()]
    mapping, _errors = build_mapping(files)
    channels = {k: XEPGChannel.from_json(v) for k, v in case["xepg"].items()}
    provider_ids = set(settings["files"]["m3u"]) | set(settings["files"]["hdhr"])
    result = build_xepg(
        channels,
        case["active"],
        mapping_first_channel=settings["mapping.first.channel"],
        filters=settings["filter"],
        xmltv_mapping=mapping,
        provider_ids=provider_ids,
        program_count=make_program_counter(data),
    )
    return channels, result, mapping


def canonical(channels: dict) -> dict:
    """Go leaves untouched channels as they were read; compare in the struct form."""
    return {k: XEPGChannel.from_json(v).to_json() for k, v in channels.items()}


class GoldenXEPGTests(unittest.TestCase):
    def test_cases_present(self) -> None:
        self.assertGreaterEqual(len(CASES), 60)

    def test_matches_go(self) -> None:
        for path in CASES:
            name = path.name.removesuffix(".input.json")
            if name in INTENTIONAL_DIVERGENCES:
                continue
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                case = json.loads(path.read_text("utf-8"))
                golden = json.loads(path.with_name(f"{name}.golden.json").read_text("utf-8"))
                channels, result, mapping = run_case(case, Path(tmp))
                self.assertEqual({k: v.to_json() for k, v in channels.items()}, canonical(golden["xepg"]))
                # Go counted channels before dropping those of unknown playlists; we count what is kept.
                kept = [c for c in golden["xepg"].values() if c["x-active"] and not c["x-hide-channel"]]
                self.assertEqual(result.xepg_count, len(kept))
                self.assertGreaterEqual(golden["xepgCount"], len(kept))
                self.assertEqual(mapping, golden["mapping"])

    def test_intentional_divergences_really_diverge(self) -> None:
        for name in sorted(INTENTIONAL_DIVERGENCES):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                path = GOLDEN / f"{name}.input.json"
                golden = json.loads((GOLDEN / f"{name}.golden.json").read_text("utf-8"))
                channels, _result, _mapping = run_case(json.loads(path.read_text("utf-8")), Path(tmp))
                self.assertNotEqual({k: v.to_json() for k, v in channels.items()}, canonical(golden["xepg"]))


def mk(xid: str, **kw: object) -> XEPGChannel:
    base = dict(file_m3u_id="M1", url=f"http://p/{xid}", tvg_id=xid, x_epg=xid, x_channel_id="1", name=xid)
    base.update(kw)
    return XEPGChannel(**base)  # type: ignore[arg-type]


def st(i: int, **kw: str) -> dict[str, str]:
    s = {"_file.m3u.id": "M1", "url": f"http://p/{i}", "tvg-id": f"id.{i}", "name": f"Ch {i}", "tvg-name": f"Ch {i}"}
    s.update(kw)
    return s


def run(channels: dict, streams: list, *, filters: dict | None = None, first: float = 1000,
        providers: set | None = None, mapping: dict | None = None, count=lambda c: 99):
    return build_xepg(
        channels, streams, mapping_first_channel=first, filters=filters or {},
        xmltv_mapping=mapping or {}, provider_ids=providers or {"M1"}, program_count=count,
    )


class DeliberateFixTests(unittest.TestCase):
    def test_url_refresh_skips_ambiguous_tvg_ids(self) -> None:
        """Go pointed every channel with that tvg-id at whichever stream came last."""
        a = mk("x-ID.0", tvg_id="same", url="http://p/a")
        b = mk("x-ID.1", tvg_id="same", url="http://p/b")
        run({"x-ID.0": a, "x-ID.1": b}, [st(1, **{"tvg-id": "same", "url": "http://p/a"}),
                                           st(2, **{"tvg-id": "same", "url": "http://p/b"})])
        self.assertEqual((a.url, b.url), ("http://p/a", "http://p/b"))

    def test_url_refresh_follows_unique_tvg_id_within_the_same_playlist(self) -> None:
        a = mk("x-ID.0", tvg_id="t", url="http://old")
        other = mk("x-ID.1", tvg_id="t", url="http://other", file_m3u_id="M2")
        channels = {"x-ID.0": a, "x-ID.1": other}
        run(channels, [st(1, **{"tvg-id": "t", "url": "http://new"})], providers={"M1", "M2"})
        self.assertEqual(channels["x-ID.0"].url, "http://new")
        self.assertEqual(other.url, "http://other")  # another playlist's channel is not touched

    def test_channel_found_by_uuid_survives_cleanup(self) -> None:
        """Go deleted it in cleanup: its hash still used the old tvg-id."""
        old = mk("x-ID.0", x_active=True, uuid_key="tvg-name", uuid_value="Ch 1", x_name="My name", x_channel_id="77")
        new_stream = st(1, url="http://p/new", **{"tvg-id": "fresh-id", "_uuid.value": "Ch 1", "_uuid.key": "tvg-name"})
        result = run({"x-ID.0": old}, [new_stream])
        self.assertEqual(list(result.channels), ["x-ID.0"])
        channel = result.channels["x-ID.0"]
        self.assertEqual((channel.url, channel.tvg_id, channel.x_name, channel.x_channel_id),
                         ("http://p/new", "fresh-id", "My name", "77"))
        self.assertEqual(result.created, [])

    def test_deactivated_channel_found_by_uuid_keeps_settings_but_follows_identity(self) -> None:
        old = mk("x-ID.0", x_active=False, uuid_key="tvg-name", uuid_value="Ch 1", x_name="Mine")
        new_stream = st(1, url="http://p/new", **{"tvg-id": "other", "_uuid.value": "Ch 1", "_uuid.key": "tvg-name"})
        result = run({"x-ID.0": old}, [new_stream])
        channel = result.channels["x-ID.0"]
        self.assertEqual((channel.x_active, channel.x_name, channel.url), (False, "Mine", "http://p/new"))

    def test_duplicate_prefers_mapped_even_with_smaller_id(self) -> None:
        same = dict(url="http://p/x", tvg_id="t", x_active=True)
        channels = {"x-ID.0": mk("x-ID.0", x_xmltv_file="X1.xml", **same), "x-ID.1": mk("x-ID.1", x_xmltv_file="-", **same)}
        remove_duplicates(channels)
        self.assertEqual(list(channels), ["x-ID.0"])

    def test_url_refresh_needs_exactly_one_stream_and_one_channel(self) -> None:
        from threadfin.xepg import _refresh_urls

        channel = mk("x-ID.0", tvg_id="t", url="http://old")
        _refresh_urls({"x-ID.0": channel}, [st(1, **{"tvg-id": "t", "url": "http://a"}),
                                            st(2, **{"tvg-id": "t", "url": "http://b"})])
        self.assertEqual(channel.url, "http://old")  # two streams share the tvg-id
        first, second = mk("x-ID.0", tvg_id="t", url="http://old1"), mk("x-ID.1", tvg_id="t", url="http://old2")
        _refresh_urls({"x-ID.0": first, "x-ID.1": second}, [st(1, **{"tvg-id": "t", "url": "http://a"})])
        self.assertEqual((first.url, second.url), ("http://old1", "http://old2"))  # two channels share it
        _refresh_urls({"x-ID.0": channel}, [st(1, **{"tvg-id": "t", "url": "http://a"})])
        self.assertEqual(channel.url, "http://a")  # unique on both sides

    def test_three_way_duplicates_keep_exactly_one(self) -> None:
        same = dict(url="http://p/x", tvg_id="t")
        channels = {
            "x-ID.0": mk("x-ID.0", x_active=False, **same),
            "x-ID.1": mk("x-ID.1", x_active=True, **same),
            "x-ID.2": mk("x-ID.2", x_active=True, **same),
        }
        removed = remove_duplicates(channels)
        self.assertEqual(sorted(removed), ["x-ID.0", "x-ID.1"])
        self.assertEqual(list(channels), ["x-ID.2"])

    def test_duplicate_choice_is_independent_of_input_order(self) -> None:
        same = dict(url="http://p/x", tvg_id="t")
        for order in (("x-ID.0", "x-ID.1"), ("x-ID.1", "x-ID.0")):
            channels = {i: mk(i, x_active=(i == "x-ID.0"), **same) for i in order}
            remove_duplicates(channels)
            self.assertEqual(list(channels), ["x-ID.0"])

    def test_provider_channel_numbers_are_reserved(self) -> None:
        result = run({}, [st(1, **{"tvg-chno": "1001"}), st(2), st(3)])
        numbers = {c.name: c.x_channel_id for c in result.channels.values()}
        self.assertEqual(numbers, {"Ch 1": "1001", "Ch 2": "1000", "Ch 3": "1002"})

    def test_blank_starting_number_does_not_renumber_from_zero(self) -> None:
        filters = {"0": {"filter": "News", "startingNumber": "", "type": "group-title"}}
        result = run({}, [st(1, **{"group-title": "News"})], filters=filters)
        self.assertEqual(next(iter(result.channels.values())).x_channel_id, "1000")

    def test_invalid_starting_number_is_ignored_and_valid_one_used(self) -> None:
        filters = {"0": {"filter": "News", "startingNumber": "x7", "type": "group-title"},
                   "1": {"filter": "Sports", "startingNumber": "40.5", "type": "group-title"}}
        result = run({}, [st(1, **{"group-title": "News"}), st(2, **{"group-title": "Sports"})], filters=filters)
        self.assertEqual([c.x_channel_id for c in result.channels.values()], ["1000", "40.5"])

    def test_removed_backup_is_cleared(self) -> None:
        a = mk("x-ID.0", x_active=True, backup_channel_1=BackupStream("M1", "http://stale"))
        result = run({"x-ID.0": a}, [st(0, url="http://p/x-ID.0", **{"tvg-id": "x-ID.0"})])
        self.assertIsNone(result.channels["x-ID.0"].backup_channel_1)

    def test_backup_found_by_name_last_match_wins(self) -> None:
        a = mk("x-ID.0", x_active=True, x_backup_channel_2=" Ch 2 ")
        result = run({"x-ID.0": a}, [st(0, url="http://p/x-ID.0", **{"tvg-id": "x-ID.0"}), st(2),
                                     st(3, **{"name": "Ch 2", "tvg-name": "Ch 2"})])
        self.assertEqual(result.channels["x-ID.0"].backup_channel_2, BackupStream("M1", "http://p/3"))

    def test_active_count_only_counts_kept_channels(self) -> None:
        a = mk("x-ID.0", x_active=True, file_m3u_id="GONE")
        b = mk("x-ID.1", x_active=True)
        result = run({"x-ID.0": a, "x-ID.1": b}, [st(0, url="http://p/x-ID.0", **{"tvg-id": "x-ID.0", "_file.m3u.id": "GONE"}),
                                                   st(1, url="http://p/x-ID.1", **{"tvg-id": "x-ID.1"})], providers={"M1"})
        self.assertEqual(result.xepg_count, 1)
        self.assertEqual(list(result.channels), ["x-ID.1"])

    def test_no_active_channels_warns(self) -> None:
        self.assertEqual(len(run({}, [st(1)]).warnings), 1)
        self.assertEqual(run({}, []).warnings, [])

    def test_streams_never_map_onto_the_dummy_guide(self) -> None:
        mapping = {"Threadfin Dummy": {"PPV": {"id": "PPV"}}}
        result = run({}, [st(1, **{"tvg-id": "PPV"})], mapping=mapping)
        channel = next(iter(result.channels.values()))
        self.assertEqual((channel.x_active, channel.x_xmltv_file), (False, "-"))

    def test_inactive_list_is_not_a_thing(self) -> None:
        """Go's isInInactiveList never matched (wrong map type), so it is not ported."""
        import threadfin.xepg as module

        self.assertFalse(hasattr(module, "is_in_inactive_list"))

    def test_new_ids_fill_the_first_gap(self) -> None:
        result = run({"x-ID.0": mk("x-ID.0", x_active=True), "x-ID.2": mk("x-ID.2", x_active=True)},
                     [st(0, url="http://p/x-ID.0", **{"tvg-id": "x-ID.0"}), st(2, url="http://p/x-ID.2", **{"tvg-id": "x-ID.2"}),
                      st(7), st(8)])
        self.assertEqual(result.created, ["x-ID.1", "x-ID.3"])


class HelperTests(unittest.TestCase):
    def test_go_g(self) -> None:
        for value, text in [(1000.0, "1000"), (1000.5, "1000.5"), (1e6, "1e+06"), (999999.0, "999999"),
                            (1234567.0, "1.234567e+06"), (0.0001, "0.0001"), (0.00001, "1e-05"), (-3.0, "-3"), (0.0, "0")]:
            self.assertEqual(go_g(value), text)

    def test_parse_float(self) -> None:
        self.assertEqual(parse_float("1000"), 1000.0)
        self.assertEqual(parse_float("7.5"), 7.5)
        for bad in ("", " 5", "5 ", "x", "inf", "nan", "1e999"):
            self.assertIsNone(parse_float(bad), bad)

    def test_hash_prefers_tvg_id_when_present(self) -> None:
        self.assertNotEqual(channel_hash("u", "id", "M1"), channel_hash("u", "", "M1"))
        self.assertEqual(channel_hash("u", "", "M1"), channel_hash("u", "", "M1"))

    def test_json_round_trip_keeps_go_key_order_and_omits_empty_uuid_value(self) -> None:
        channel = XEPGChannel.from_json({"url": "u", "x-active": True, "unknown": 1, "backup_channel_2": {"PlaylistID": "M", "URL": "x"}})
        data = channel.to_json()
        self.assertEqual(list(data)[:3], ["_file.m3u.id", "_file.m3u.name", "_file.m3u.path"])
        self.assertNotIn("_uuid.value", data)
        self.assertNotIn("unknown", data)
        self.assertEqual(data["backup_channel_2"], {"PlaylistID": "M", "URL": "x"})
        self.assertIsNone(data["backup_channel_1"])
        self.assertEqual(XEPGChannel.from_json(data).to_json(), data)

    def test_wrong_types_decode_to_zero_values(self) -> None:
        channel = XEPGChannel.from_json({"url": 5, "x-active": "yes", "name": None})
        self.assertEqual((channel.url, channel.x_active, channel.name), ("", False, ""))

    def test_cleanup_function_directly(self) -> None:
        keep = mk("x-ID.0", x_active=True)
        channels = {"x-ID.0": keep, "x-ID.1": mk("x-ID.1")}
        removed, count = cleanup(channels, [channel_hash(keep.url, keep.tvg_id, keep.file_m3u_id)], {"M1"})
        self.assertEqual((removed, count), (["x-ID.1"], 1))


if __name__ == "__main__":
    unittest.main()
