"""Writes tests/golden/filters/*.input.json (run run_filter_golden.sh afterwards)."""

import json
import random
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)


def F(type_, filter_, **kw):
    d = {
        "active": True,
        "type": type_,
        "filter": filter_,
        "include": "",
        "exclude": "",
        "caseSensitive": False,
        "liveEvent": False,
        "name": "n",
        "description": "",
        "startingNumber": "1",
        "x-category": "",
    }
    d.update(kw)
    return d


def S(name, group="", values=None):
    s = {"name": name}
    if group:
        s["group-title"] = group
    s["_values"] = values if values is not None else f"{group} {name}".strip()
    return s


streams = [
    S("ESPN HD", "Sports"),
    S("espn 2", "sports"),
    S("BBC One", "UK"),
    S("BBC Two DEU", "UK"),
    S("Sky Sports DE", "Sports DE"),
    S("Arte", "DEU"),
    S("CNN", "News"),
    S("Fox News HD", "News"),
    S("Ünïcode Kanal", "Größe"),
    S("İstanbul TV", "TR"),
    S("ΟΔΥΣΣΕΥΣ", "GR"),
    S("CR\rLF", "Odd", "Odd CR\rLF"),
    S("No group"),
    S("Dotted.Name", "a.b"),
]
cases = {
    "carriage_returns_removed": {"filters": {"0": F("custom-filter", "crlf")}, "streams": streams},
    "no_filters": {"filters": {}, "streams": streams},
    "custom_basic": {"filters": {"0": F("custom-filter", "bbc")}, "streams": streams},
    "custom_case_sensitive": {"filters": {"0": F("custom-filter", "BBC", caseSensitive=True)}, "streams": streams},
    "custom_case_sensitive_miss": {"filters": {"0": F("custom-filter", "bbc", caseSensitive=True)}, "streams": streams},
    "group_basic": {"filters": {"0": F("group-title", "News")}, "streams": streams},
    "group_case_sensitive": {"filters": {"0": F("group-title", "sports", caseSensitive=True)}, "streams": streams},
    "group_include": {"filters": {"0": F("group-title", "UK", include="two")}, "streams": streams},
    "group_exclude": {"filters": {"0": F("group-title", "UK", exclude="two")}, "streams": streams},
    "group_include_exclude": {
        "filters": {"0": F("group-title", "News", include="fox, cnn", exclude="hd")},
        "streams": streams,
    },
    "comma_lists_spacing": {"filters": {"0": F("group-title", "News", include="fox ,cnn , hd")}, "streams": streams},
    "trailing_comma_matches_all": {"filters": {"0": F("group-title", "UK", include="zzz,")}, "streams": streams},
    "custom_with_braces": {"filters": {"0": F("custom-filter", "bbc {two} !{deu}")}, "streams": streams},
    "custom_with_exclude_only": {"filters": {"0": F("custom-filter", "bbc !{two}")}, "streams": streams},
    "empty_rule_skipped": {
        "filters": {"0": F("custom-filter", ""), "1": F("custom-filter", "cnn")},
        "streams": streams,
    },
    "empty_group_rule": {"filters": {"0": F("group-title", "")}, "streams": streams},
    "dot_in_include_breaks": {"filters": {"0": F("group-title", "UK", include="a.b")}, "streams": streams},
    "dot_in_rule": {"filters": {"0": F("group-title", "a.b")}, "streams": streams},
    "multiple_bangs": {"filters": {"0": F("custom-filter", "bbc !!{two}")}, "streams": streams},
    "multiple_braces": {"filters": {"0": F("custom-filter", "bbc {{two}}")}, "streams": streams},
    "unicode_lowering": {
        "filters": {
            "0": F("custom-filter", "ünï"),
            "1": F("custom-filter", "i̇stanbul"),
            "2": F("custom-filter", "οδυσσευς"),
            "3": F("group-title", "GRÖSSE"),
        },
        "streams": streams,
    },
    "first_match_decides_exclude": {
        "filters": {"0": F("group-title", "UK", exclude="two"), "1": F("custom-filter", "bbc")},
        "streams": streams,
    },
    "first_match_decides_include": {
        "filters": {"0": F("custom-filter", "bbc"), "1": F("group-title", "UK", exclude="two")},
        "streams": streams,
    },
    "numeric_key_order": {
        "filters": {"10": F("group-title", "News", liveEvent=True), "2": F("custom-filter", "news")},
        "streams": streams,
    },
    "live_event_flag": {
        "filters": {"0": F("group-title", "News", liveEvent=True), "1": F("group-title", "Sports")},
        "streams": streams,
    },
    "live_event_no_match_last_flag": {
        "filters": {"0": F("group-title", "zzz", liveEvent=True), "1": F("group-title", "yyy", liveEvent=False)},
        "streams": streams,
    },
    "unknown_type_ignored": {"filters": {"0": {**F("other", "bbc")}}, "streams": streams},
    "null_fields": {
        "filters": {"0": {"type": "custom-filter", "filter": "cnn", "include": None, "caseSensitive": None}},
        "streams": streams,
    },
    "wrong_type_error": {"filters": {"0": {"type": "custom-filter", "filter": 5}}, "streams": streams},
    "wrong_bool_type_error": {
        "filters": {"0": {"type": "custom-filter", "filter": "a", "caseSensitive": "yes"}},
        "streams": streams,
    },
}

# Randomised: many filters and streams drawn from a small vocabulary so matches are common.
rng = random.Random(1234)
words = ["abc", "Abc", "ABC", "de", "DE", "x", "über", "Ünï", "news", "sport", "hd", "4k", "a b", "z.z", ""]
for n in range(40):
    fl = {}
    for i in range(rng.randint(1, 4)):
        t = rng.choice(["custom-filter", "group-title"])
        rule = rng.choice(words)
        if t == "custom-filter" and rng.random() < 0.4:
            rule += rng.choice(
                [
                    " {" + rng.choice(words) + "}",
                    " !{" + rng.choice(words) + "}",
                    " {" + rng.choice(words) + "} !{" + rng.choice(words) + "}",
                ]
            )
        fl[str(i)] = F(
            t,
            rule,
            include=",".join(rng.sample(words, rng.randint(0, 2))),
            exclude=",".join(rng.sample(words, rng.randint(0, 2))),
            caseSensitive=rng.random() < 0.4,
            liveEvent=rng.random() < 0.3,
        )
    ss = []
    for _ in range(60):
        name = " ".join(rng.sample(words, rng.randint(1, 3)))
        grp = rng.choice([*words, "", ""])
        vals = " ".join(w for w in [grp, rng.choice(words), name] if w)
        ss.append(S(name, grp, vals))
    cases[f"random_{n:02d}"] = {"filters": fl, "streams": ss}

for name, case in cases.items():
    (out / f"{name}.input.json").write_text(json.dumps(case, ensure_ascii=False, indent=1), "utf-8")
print(len(cases), "cases")
