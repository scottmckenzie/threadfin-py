"""Filter parity with the real Go code (tests/golden/filters)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from threadfin.filters import (
    FilterError,
    FilterRule,
    build_filter_rules,
    check_conditions,
    filter_stream,
    go_lower,
)

GOLDEN = Path(__file__).parent / "golden" / "filters"
CASES = sorted(GOLDEN.glob("*.input.json"))


class GoldenFilterTests(unittest.TestCase):
    def test_cases_present(self) -> None:
        self.assertGreaterEqual(len(CASES), 60)

    def test_matches_go(self) -> None:
        for path in CASES:
            with self.subTest(case=path.name.removesuffix(".input.json")):
                case = json.loads(path.read_text("utf-8"))
                golden = json.loads(path.with_name(path.name.replace(".input", ".golden")).read_text("utf-8"))
                if golden["error"]:
                    with self.assertRaises(FilterError):
                        build_filter_rules(case["filters"])
                    continue
                rules = build_filter_rules(case["filters"])
                self.assertEqual(
                    [
                        {
                            "CaseSensitive": r.case_sensitive,
                            "LiveEvent": r.live_event,
                            "Rule": r.rule,
                            "Type": r.type,
                        }
                        for r in rules
                    ],
                    golden["rules"],
                )
                got = [filter_stream(s, rules) for s in case["streams"]]
                self.assertEqual([{"active": a, "liveEvent": e} for a, e in got], golden["results"])


class UnitTests(unittest.TestCase):
    def test_go_lower(self) -> None:
        self.assertEqual(go_lower("İstanbul"), "istanbul")
        self.assertEqual(go_lower("ΟΔΥΣΣΕΥΣ"), "οδυσσευσ")  # no final-sigma rule
        self.assertEqual(go_lower("ABC"), "abc")

    def test_check_conditions(self) -> None:
        self.assertTrue(check_conditions("fox news", "cnn, fox", "include"))
        self.assertFalse(check_conditions("fox news", "cnn", "include"))
        self.assertFalse(check_conditions("fox news", "fox", "exclude"))
        self.assertTrue(check_conditions("x", "a ,b", "exclude"))
        self.assertTrue(check_conditions("x", "a,", "include"))  # empty keyword matches

    def test_custom_filter_does_not_inherit_live_event(self) -> None:
        """Go leaked liveEvent from the previous group-title filter into custom filters."""
        rules = build_filter_rules(
            {
                "0": {"type": "group-title", "filter": "zzz", "liveEvent": True},
                "1": {"type": "custom-filter", "filter": "cnn"},
            }
        )
        self.assertEqual(rules[1], FilterRule("cnn", "custom-filter", False, False))
        self.assertEqual(filter_stream({"name": "CNN", "_values": "CNN"}, rules), (True, False))

    def test_numeric_key_order(self) -> None:
        rules = build_filter_rules(
            {
                "10": {"type": "custom-filter", "filter": "ten"},
                "2": {"type": "custom-filter", "filter": "two"},
            }
        )
        self.assertEqual([r.rule for r in rules], ["two", "ten"])

    def test_case_insensitive_keys_like_go(self) -> None:
        rules = build_filter_rules({"0": {"Type": "custom-filter", "FILTER": "x"}})
        self.assertEqual(rules, [FilterRule("x", "custom-filter")])

    def test_bool_is_not_a_string(self) -> None:
        with self.assertRaises(FilterError):
            build_filter_rules({"0": {"type": "custom-filter", "filter": True}})
        with self.assertRaises(FilterError):
            build_filter_rules({"0": {"type": "custom-filter", "caseSensitive": 1}})


if __name__ == "__main__":
    unittest.main()
