"""Writes tests/golden/playlist/*.input.json. Run run_playlist_golden.sh afterwards."""
import json
import random
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)


def ch(n, chno="1", name=None, group="G", cat="", active=True, hide=False, url=None, pid="M1", **kw):
    name = name or f"Chan {n}"
    c = {"_file.m3u.id": pid, "_file.m3u.name": "P", "_file.m3u.path": f"/d/{pid}.m3u", "group-title": group,
         "name": name, "tvg-id": f"id.{n}", "tvg-logo": f"http://l/{n}.png", "tvg-name": name,
         "tvg-chno": str(chno), "url": url or f"http://p/{n}", "_uuid.key": "", "_values": name,
         "x-active": active, "x-category": cat, "x-channelID": str(chno), "x-epg": f"x-ID.{n}",
         "x-group-title": group, "x-mapping": "-", "x-xmltv-file": "-", "x-ppv-extra": "",
         "x-backup-channel-1": "", "x-backup-channel-2": "", "x-backup-channel-3": "",
         "x-hide-channel": hide, "x-name": name, "x-update-channel-icon": False,
         "x-update-channel-name": False, "x-description": "", "live": False, "is_backup_channel": False,
         "backup_channel_1": None, "backup_channel_2": None, "backup_channel_3": None, "channelUniqueID": ""}
    c.update(kw)
    return c


def case(name, channels, labels=(), groups=(), settings=None):
    xepg = {c["x-epg"]: c for c in channels}
    body = {"settings": settings or {}, "groupLabels": list(labels), "groups": list(groups), "xepg": xepg}
    (out / f"{name}.input.json").write_text(json.dumps(body, indent=1) + "\n")


case("basic", [ch(1, 3), ch(2, 1), ch(3, 2)])
case("numeric_sort", [ch(i, c) for i, c in enumerate(["10", "9", "2.5", "100", "1e1", "0"], 1)])
case("mixed_sort", [ch(i, c) for i, c in enumerate(["b", "10", "a", "2", "B"], 1)])
case("inactive_and_hidden", [ch(1, 1), ch(2, 2, active=False), ch(3, 3, hide=True), ch(4, 4)])
case("group_filter", [ch(1, 1, group="A"), ch(2, 2, group="B"), ch(3, 3, group="A", cat="C")], groups=["A"])
case("group_filter_category_quirk", [ch(1, 1, group="A", cat="Z"), ch(2, 2, group="B")], groups=["A", "B"])
case("category_wins", [ch(1, 1, group="A", cat="Sport"), ch(2, 2, group="A")])
case("empty_group_falls_back", [ch(1, 1, group="", cat="", **{"x-group-title": ""}), ch(2, 2, group="Fallback", **{"x-group-title": ""})])
case("duplicate_urls_same_group", [ch(1, 1, url="http://same"), ch(2, 2, url="http://same")])
case("duplicate_urls_other_group", [ch(1, 1, url="http://same", group="A"), ch(2, 2, url="http://same", group="B")])
case("same_url_other_playlist", [ch(1, 1, url="http://same"), ch(2, 2, url="http://same", pid="M2")])
case("cap_by_label", [ch(i, i) for i in range(1, 6)], labels=["G (3)"])
case("cap_minus_deactivated", [ch(1, 1), ch(2, 2), ch(3, 3), ch(4, 4, active=False), ch(5, 5, hide=True)], labels=["G (4)"])
case("cap_zero_floor", [ch(1, 1), ch(2, 2, active=False), ch(3, 3, active=False)], labels=["G (1)"])
case("cap_other_group_unaffected", [ch(1, 1, group="A"), ch(2, 2, group="A"), ch(3, 3, group="B"), ch(4, 4, group="B")], labels=["A (1)"])
case("labels_malformed", [ch(1, 1), ch(2, 2)], labels=["G", "G (x)", " (3)", "G ()", "G (1"])
case("label_with_parens_in_name", [ch(1, 1, group="News (HD)"), ch(2, 2, group="News (HD)")], labels=["News (HD) (1)"])
case("label_plus_sign", [ch(1, 1), ch(2, 2)], labels=["G (+1)"])
case("no_logo", [ch(1, 1, **{"tvg-logo": ""})])
case("name_specials", [ch(1, 1, name="A & B <C> é"), ch(2, 2, name="Plain")])
case("tvg_name_ignored_x_name_used", [ch(1, 1, **{"x-name": "Shown", "tvg-name": "Other"})])
case("empty_playlist", [])
case("backup_channels", [ch(1, 1, backup_channel_1={"PlaylistID": "M2", "URL": "http://b/1"}, is_backup_channel=False,
                              **{"x-backup-channel-1": "x"})])
case("https_flags_off", [ch(1, 1)], settings={"forceHttps": False, "httpsThreadfinDomain": "x.example"})

rng = random.Random(7)
for k in range(15):
    n = rng.randint(1, 25)
    groups = ["A", "B", "C", ""]
    chans = []
    for i in range(1, n + 1):
        chans.append(ch(i, rng.choice([str(rng.randint(0, 30)), str(rng.randint(0, 99)) + ".5", "x" + str(rng.randint(0, 3))]),
                        group=rng.choice(groups), cat=rng.choice(["", "", "K"]), active=rng.random() > 0.2,
                        hide=rng.random() > 0.9, url=f"http://p/{rng.randint(1, max(2, n // 2))}", pid=rng.choice(["M1", "M2"]),
                        **{"x-group-title": rng.choice(groups)}))
    # unique channel numbers: Go's sort is unstable so ties would make the golden arbitrary
    seen = set()
    for c in chans:
        while c["tvg-chno"] in seen:
            c["tvg-chno"] = c["tvg-chno"] + "0"
        seen.add(c["tvg-chno"])
        c["x-channelID"] = c["tvg-chno"]
    labels = [f"{g} ({rng.randint(0, 6)})" for g in groups[:3] if rng.random() > 0.4]
    case(f"random_{k:02d}", chans, labels=labels, groups=rng.choice([[], [], ["A"], ["A", "B"]]))
