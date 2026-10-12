"""Writes tests/golden/hdhr/*.input.json. Run run_hdhr_golden.sh afterwards."""
import json
import random
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)


def ch(n, name=None, active=True, hide=False, pid="M1", url=None, chno=None, **kw):
    name = name or f"Chan {n}"
    c = {"_file.m3u.id": pid, "_file.m3u.name": "P", "_file.m3u.path": f"/d/{pid}.m3u", "group-title": "G",
         "name": name, "tvg-id": f"id.{n}", "tvg-logo": "", "tvg-name": name, "tvg-chno": str(chno or n),
         "url": url or f"http://p/{n}", "_uuid.key": "", "_values": name, "x-active": active, "x-category": "",
         "x-channelID": str(chno or n), "x-epg": f"x-ID.{n}", "x-group-title": "G", "x-mapping": "-",
         "x-xmltv-file": "-", "x-ppv-extra": "", "x-backup-channel-1": "", "x-backup-channel-2": "",
         "x-backup-channel-3": "", "x-hide-channel": hide, "x-name": name, "x-update-channel-icon": False,
         "x-update-channel-name": False, "x-description": "", "live": False, "is_backup_channel": False,
         "backup_channel_1": None, "backup_channel_2": None, "backup_channel_3": None, "channelUniqueID": ""}
    c.update(kw)
    return c


def stream(n, name=None, uuid=None, pid="M1", url=None):
    name = name or f"Stream {n}"
    s = {"_file.m3u.id": pid, "_file.m3u.name": "P", "_file.m3u.path": f"/d/{pid}.m3u", "group-title": "G",
         "name": name, "tvg-id": f"id.{n}", "tvg-logo": "", "tvg-name": name, "tvg-chno": "",
         "url": url or f"http://s/{n}", "_uuid.key": "", "_values": name}
    if uuid is not None:
        s["_uuid.key"] = "ID-" + pid
        s["_uuid.value"] = uuid
    return s


def case(name, *, source="XEPG", tuner=1, uuid="2020-abc", xepg=(), streams=(), pms=None, scan=0,
         sname="Threadfin", no_pms_file=False, version="1.2.3", domain="localhost:34400"):
    body = {"settings": {"epgSource": source, "tuner": tuner, "uuid": uuid}, "name": sname, "version": version,
            "domain": domain, "scanInProgress": scan, "xepg": {c["x-epg"]: c for c in xepg},
            "streams": list(streams)}
    if pms is not None:
        body["pms"] = pms
    if no_pms_file:
        body["noPmsFile"] = True
    (out / f"{name}.input.json").write_text(json.dumps(body, indent=1) + "\n")


case("xepg_basic", xepg=[ch(1), ch(2), ch(3)])
case("xepg_inactive_hidden", xepg=[ch(1), ch(2, active=False), ch(3, hide=True)])
case("xepg_empty", xepg=[])
case("xepg_specials", xepg=[ch(1, name="A & B <C> \"q\" é"), ch(2, name="日本語")], sname="Thread <fin> & co")
case("xepg_duplicate_urls", xepg=[ch(1, url="http://same"), ch(2, url="http://same")])
case("xepg_two_playlists", xepg=[ch(1, url="http://same"), ch(2, url="http://same", pid="M2")])
case("tuners_3", tuner=3, xepg=[ch(1)])
case("tuners_0", tuner=0, xepg=[ch(1)])
case("scan_in_progress", scan=1, xepg=[ch(1)])
case("custom_domain", domain="tv.example.org:8443", xepg=[ch(1)], version="9.9.9-beta")
case("pms_basic", source="PMS", streams=[stream(1), stream(2), stream(3)])
case("pms_uuid_values", source="PMS", streams=[stream(1, uuid="u-1"), stream(2), stream(3, uuid="u-3")])
case("pms_existing_ids", source="PMS", streams=[stream(1), stream(2), stream(3)],
     pms={"Stream 1": "id-0", "Stream 3": "id-5"})
case("pms_id_gaps", source="PMS", streams=[stream(1), stream(2)], pms={"Other": "id-0", "Another": "id-1", "More": "id-3"})
case("pms_duplicate_names", source="PMS", streams=[stream(1, name="Same"), stream(2, name="Same", url="http://s/9")])
case("pms_missing_file", source="PMS", streams=[stream(1), stream(2)], no_pms_file=True)
case("pms_empty", source="PMS", streams=[])
case("pms_same_url_two_names", source="PMS", streams=[stream(1, url="http://x"), stream(2, url="http://x")])
case("unknown_source", source="", xepg=[ch(1)], streams=[stream(1)])

rng = random.Random(11)
for k in range(10):
    n = rng.randint(1, 15)
    if rng.random() < 0.5:
        xs = [ch(i, active=rng.random() > 0.3, hide=rng.random() > 0.9, url=f"http://p/{rng.randint(1, 8)}",
                 pid=rng.choice(["M1", "M2"])) for i in range(1, n + 1)]
        case(f"random_xepg_{k:02d}", xepg=xs, tuner=rng.randint(1, 4))
    else:
        ss = [stream(i, name=f"N{rng.randint(1, 8)}", uuid=rng.choice([None, None, f"u{i}"]),
                     url=f"http://s/{rng.randint(1, 8)}", pid=rng.choice(["M1", "M2"])) for i in range(1, n + 1)]
        case(f"random_pms_{k:02d}", source="PMS", streams=ss, pms=rng.choice([None, {"N1": "id-0", "N5": "id-2"}]))
