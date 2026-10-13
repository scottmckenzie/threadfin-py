"""Writes tests/golden/streams/*.input.json (needs PYTHONPATH=src). Run run_streams_golden.sh afterwards."""
import json
import random
import sys
from pathlib import Path

from threadfin.m3u import serialize_m3u

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
rng = random.Random(99)


def stream(i, group, name=None, tvg_id=None, chno=""):
    name = name or f"Channel {i}"
    return {"name": name, "url": f"http://p/{i}", "tvg-id": tvg_id if tvg_id is not None else f"id.{i}",
            "tvg-name": name, "tvg-chno": chno, "tvg-logo": "", "group-title": group}


def playlist(streams):
    return serialize_m3u(streams)


def F(type_, filter_, **kw):
    d = {"type": type_, "filter": filter_, "include": "", "exclude": "", "caseSensitive": False,
         "liveEvent": False}
    d.update(kw)
    return d


def P(pid, content, kind="m3u", name=None, missing=False):
    return {"kind": kind, "id": pid, "name": name or pid, "content": content, "missing": missing}


groups = ["News", "Sports", "Movies", "Ünïcode", "sports", "A", "A-b", "A b", "", "Kids (HD)"]
base = [stream(i, groups[i % len(groups)], tvg_id="" if i % 7 == 0 else None, chno="5" if i % 3 == 0 else "")
        for i in range(60)]
big = [stream(i, groups[i % len(groups)]) for i in range(500)]

cases = {
    "no_filters_all_active": {"providers": [P("M1", playlist(base))]},
    "no_filters_over_limit": {"providers": [P("M1", playlist(big))]},
    "group_filter": {"filters": {"0": F("group-title", "News")}, "providers": [P("M1", playlist(base))]},
    "custom_filter": {"filters": {"0": F("custom-filter", "channel 1")}, "providers": [P("M1", playlist(base))]},
    "filter_matching_nothing": {"filters": {"0": F("custom-filter", "zzz")}, "providers": [P("M1", playlist(base))]},
    "ignore_filters": {"ignoreFilters": True, "filters": {"0": F("custom-filter", "zzz")},
                       "providers": [P("M1", playlist(big))]},
    "live_event": {"filters": {"0": F("group-title", "Sports", liveEvent=True)}, "providers": [P("M1", playlist(base))]},
    "group_include_exclude": {"filters": {"0": F("group-title", "News", include="1", exclude="11,12")},
                              "providers": [P("M1", playlist(base))]},
    "two_disjoint_group_filters": {"filters": {"0": F("group-title", "News"), "1": F("group-title", "Sports")},
                                   "providers": [P("M1", playlist(base))]},
    "two_providers": {"filters": {"0": F("group-title", "News")},
                      "providers": [P("M1", playlist(base[:30])), P("M2", playlist(base[30:]), name="Second")]},
    "empty_playlist": {"providers": [P("M1", "#EXTM3U\n")]},
    "missing_local_copy": {"providers": [P("M1", "", missing=True)]},
    "no_providers": {"providers": []},
}
for n in range(15):
    pls = []
    for p in range(rng.randint(1, 3)):
        ss = [stream(i, rng.choice(groups), name=rng.choice(["ESPN", "BBC", "CNN", "Arte", "Ünï", "x y"]) + f" {i}",
                     tvg_id=rng.choice(["", "a", "b"])) for i in range(rng.randint(1, 80))]
        pls.append(P(f"M{p}", playlist(ss)))
    fl = {}
    # Go walks Settings.Filter in random map order, so only single filters give stable output here
    # (multi-filter ordering is covered deterministically by the filters golden tests).
    for i in range(rng.randint(0, 1)):
        t = rng.choice(["custom-filter", "group-title"])
        fl[str(i)] = F(t, rng.choice(["espn", "News", "sports", "ünï", "Arte", "5"]), include=rng.choice(["", "1", "2,3"]),
                       exclude=rng.choice(["", "bbc", "9"]), caseSensitive=rng.random() < 0.3, liveEvent=rng.random() < 0.3)
    cases[f"random_{n:02d}"] = {"filters": fl, "ignoreFilters": rng.random() < 0.1, "providers": pls}

for name, case in cases.items():
    case.setdefault("filters", {})
    case.setdefault("ignoreFilters", False)
    (out / f"{name}.input.json").write_text(json.dumps(case, ensure_ascii=False, indent=1), "utf-8")
print(len(cases), "cases")
