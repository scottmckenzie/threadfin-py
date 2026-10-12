"""Writes tests/golden/xepg/*.input.json. Run run_xepg_golden.sh afterwards."""
import copy
import json
import random
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
rng = random.Random(2024)


def stream(i, group="News", fid="M1", tvg_id=None, name=None, url=None, chno="", live="false",
           uuid=True, logo="", tvg_name=None):
    name = name or f"Channel {i}"
    s = {"_file.m3u.id": fid, "_file.m3u.name": f"Provider {fid}", "_file.m3u.path": f"/data/{fid}.m3u",
         "group-title": group, "name": name, "tvg-id": tvg_id if tvg_id is not None else f"id.{i}",
         "tvg-logo": logo, "tvg-name": tvg_name if tvg_name is not None else name, "url": url or f"http://p/{fid}/{i}",
         "_values": f"{group} {name}", "liveEvent": live}
    if chno:
        s["tvg-chno"] = chno
    if uuid:
        s["_uuid.key"] = "tvg-name"
        s["_uuid.value"] = s["tvg-name"]
    return s


def channel(xid, st, chno=None, **kw):
    """An existing XEPG channel as Go would have stored it."""
    c = {"_file.m3u.id": st["_file.m3u.id"], "_file.m3u.name": st["_file.m3u.name"],
         "_file.m3u.path": st["_file.m3u.path"], "group-title": st["group-title"], "name": st["name"],
         "tvg-id": st["tvg-id"], "tvg-logo": st["tvg-logo"], "tvg-name": st["tvg-name"],
         "tvg-chno": chno or "1000", "url": st["url"], "_uuid.key": st.get("_uuid.key", ""),
         "_uuid.value": st.get("_uuid.value", ""), "_values": st["_values"], "x-active": True,
         "x-category": "", "x-channelID": chno or "1000", "x-epg": xid, "x-group-title": st["group-title"],
         "x-mapping": "-", "x-xmltv-file": "-", "x-ppv-extra": "", "x-backup-channel-1": "",
         "x-backup-channel-2": "", "x-backup-channel-3": "", "x-hide-channel": False,
         "x-name": st["name"], "x-update-channel-icon": False, "x-update-channel-name": False,
         "x-description": "", "live": False, "is_backup_channel": False, "backup_channel_1": None,
         "backup_channel_2": None, "backup_channel_3": None, "channelUniqueID": ""}
    c.update(kw)
    return c


def xmltv(channels, programmes_per_channel=5):
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv generator-info-name="t">']
    for cid in channels:
        parts.append(f'<channel id="{cid}"><display-name>{cid} name</display-name><icon src="http://i/{cid}.png"/></channel>')
    for cid in channels:
        for n in range(programmes_per_channel):
            parts.append(f'<programme start="2026010{n+1}000000 +0000" stop="2026010{n+1}010000 +0000" channel="{cid}"><title>{cid} show {n}</title></programme>')
    parts.append("</tv>")
    return "".join(parts)


def base(**kw):
    s = {"mapping.first.channel": 1000, "files": {"m3u": {"M1": {"name": "Provider M1"}}, "hdhr": {},
                                                    "xmltv": {"X1": {"name": "EPG one"}}}, "filter": {}}
    s.update(kw)
    return s


def F(group, start="", category="", type_="group-title"):
    return {"type": type_, "filter": group, "startingNumber": start, "x-category": category, "active": True,
            "caseSensitive": False, "liveEvent": False, "include": "", "exclude": "", "name": group}


S = [stream(i) for i in range(1, 6)]
X5 = xmltv([f"id.{i}" for i in (1, 3)])
cases = {}


def case(name, active, xepg=None, settings=None, xml=None, inactive=None):
    cases[name] = {"settings": settings or base(), "xmltv": {"X1": xml} if xml else {}, "xepg": xepg or {},
                   "active": active, "inactive": inactive or []}


case("new_no_xmltv", S)
case("new_with_xmltv_match", S, xml=X5)
case("new_chno_provided", [stream(1, chno="5"), stream(2), stream(3, chno="7.5"), stream(4)])
case("first_channel_custom", S, settings=base(**{"mapping.first.channel": 2000.5}))
case("filter_starting_number_and_category",
     [stream(1, "News"), stream(2, "Sports"), stream(3, "News"), stream(4, "Sports")],
     settings=base(filter={"0": F("News", "300", "News"), "1": F("Sports", "700", "Sports")}), xml=X5)
case("provider_chno_collision_fix", [stream(1, chno="1001"), stream(2), stream(3)])
case("filter_without_starting_number_fix", [stream(1, "News")], settings=base(filter={"0": F("News", "", "")}))
case("existing_unchanged", S[:3], xepg={f"x-ID.{i}": channel(f"x-ID.{i}", S[i], str(1000 + i)) for i in range(3)})
st_changed = copy.deepcopy(S[:3]); st_changed[1]["url"] = "http://p/M1/moved"
case("existing_url_changed_same_tvgid", st_changed, xepg={f"x-ID.{i}": channel(f"x-ID.{i}", S[i], str(1000 + i)) for i in range(3)})
st_uuid = copy.deepcopy(S[:3]); st_uuid[1]["url"] = "http://p/M1/new"; st_uuid[1]["tvg-id"] = "threadfin-new"
case("existing_found_by_uuid", st_uuid, xepg={f"x-ID.{i}": channel(f"x-ID.{i}", S[i], str(1000 + i)) for i in range(3)})
case("deactivated_channel_not_updated", S[:2], xepg={
    "x-ID.0": channel("x-ID.0", S[0], "1000"),
    "x-ID.1": channel("x-ID.1", S[1], "1001", **{"x-active": False, "x-name": "Custom", "url": "http://old"})})
case("stream_removed_channel_deleted", S[:2], xepg={f"x-ID.{i}": channel(f"x-ID.{i}", S[i], str(1000 + i)) for i in range(4)})
case("provider_removed_channels_deleted", S[:3], xepg={f"x-ID.{i}": channel(f"x-ID.{i}", S[i], str(1000 + i)) for i in range(3)},
     settings=base(files={"m3u": {"M9": {"name": "other"}}, "hdhr": {}, "xmltv": {"X1": {"name": "e"}}}))
case("empty_channel_number_deleted", S[:2], xepg={"x-ID.0": channel("x-ID.0", S[0], "1000"),
                                                  "x-ID.1": channel("x-ID.1", S[1], "1001", **{"x-channelID": ""})})
case("live_event_new_without_epg", [stream(1, live="true", name="PPV Fight 9PM"), stream(2)])
case("live_event_new_with_full_epg", [stream(1, live="true")], xml=xmltv(["id.1"], 6))
case("live_event_new_with_thin_epg", [stream(1, live="true")], xml=xmltv(["id.1"], 2))
live_ch = channel("x-ID.0", stream(1), "1000", **{"x-xmltv-file": "X1.xml", "x-mapping": "id.1"})
case("live_event_existing_gets_dummy", [stream(1, live="true")], xepg={"x-ID.0": live_ch}, xml=xmltv(["id.1"], 2))
case("name_update_with_flag_and_uuid", [stream(1, name="New Name", tvg_name="New Name")],
     xepg={"x-ID.0": channel("x-ID.0", stream(1), "1000", **{"x-update-channel-name": True})}, xml=xmltv(["id.1"], 5))
case("name_not_updated_without_flag", [stream(1, name="New Name", tvg_name="Old")],
     xepg={"x-ID.0": channel("x-ID.0", stream(1, tvg_name="Old"), "1000")})
case("name_update_generated_tvgid", [stream(1, tvg_id="threadfin-abc", name="Renamed", tvg_name="Orig")],
     xepg={"x-ID.0": channel("x-ID.0", stream(1, tvg_id="threadfin-abc", tvg_name="Orig"), "1000")})
case("live_event_name_update", [stream(1, name="Fight 2", live="true", tvg_name="Fight 2")],
     xepg={"x-ID.0": channel("x-ID.0", stream(1, tvg_name="Fight 1"), "1000")}, xml=xmltv(["id.1"], 1))
case("icon_update_flag", [stream(1, logo="http://l/new.png")],
     xepg={"x-ID.0": channel("x-ID.0", stream(1, logo="http://l/old.png"), "1000", **{"x-update-channel-icon": True})})
case("backup_channels", S[:3], xepg={"x-ID.0": channel("x-ID.0", S[0], "1000", **{"x-backup-channel-1": "Channel 2", "x-backup-channel-3": " Channel 3 "}),
                                       "x-ID.1": channel("x-ID.1", S[1], "1001"), "x-ID.2": channel("x-ID.2", S[2], "1002")})
case("backup_cleared_keeps_stale_pointer_in_go", S[:2], xepg={"x-ID.0": channel("x-ID.0", S[0], "1000", **{"backup_channel_1": {"PlaylistID": "M1", "URL": "http://stale"}}),
                                                               "x-ID.1": channel("x-ID.1", S[1], "1001")})
case("hidden_channel_changes_not_saved", S[:2], xepg={"x-ID.0": channel("x-ID.0", S[0], "1000", **{"x-hide-channel": True, "x-backup-channel-1": "Channel 2"}),
                                                       "x-ID.1": channel("x-ID.1", S[1], "1001")})
dup_a = channel("x-ID.0", S[0], "1000"); dup_b = channel("x-ID.1", S[0], "1001", **{"x-xmltv-file": "X1.xml", "x-mapping": "id.1"})
case("duplicate_pair_prefers_mapped", S[:1], xepg={"x-ID.0": dup_a, "x-ID.1": dup_b}, xml=X5)
case("duplicate_pair_prefers_active", S[:1], xepg={"x-ID.0": channel("x-ID.0", S[0], "1000", **{"x-active": False}), "x-ID.1": dup_a | {"x-epg": "x-ID.1", "x-channelID": "1001"}})
case("identical_streams_make_one_channel", [stream(1), stream(1)])
case("number_collision_avoidance", S, xepg={"x-ID.0": channel("x-ID.0", S[0], "1000"), "x-ID.1": channel("x-ID.1", S[1], "1001"),
                                            "x-ID.7": channel("x-ID.7", S[2], "1005")})
case("new_ids_fill_gaps", S, xepg={"x-ID.0": channel("x-ID.0", S[0], "1000"), "x-ID.2": channel("x-ID.2", S[1], "1001")})
case("inactive_channel_gets_auto_mapped", [stream(1)], xml=xmltv(["id.1"], 5),
     xepg={"x-ID.0": channel("x-ID.0", stream(1), "1000", **{"x-active": False, "x-xmltv-file": "", "x-mapping": ""})},
     settings=base(filter={"0": F("News", "", "Kids")}))
case("active_channel_empty_mapping_defaults", [stream(1)], xepg={"x-ID.0": channel("x-ID.0", stream(1), "1000", **{"x-xmltv-file": "", "x-mapping": ""})})
case("active_channel_category_filled", [stream(1)], xml=xmltv(["id.1"], 5),
     xepg={"x-ID.0": channel("x-ID.0", stream(1), "1000", **{"x-xmltv-file": "X1.xml", "x-mapping": "id.1"})},
     settings=base(filter={"0": F("News", "", "Sports")}))
case("mapping_to_missing_xmltv_is_kept", [stream(1)],
     xepg={"x-ID.0": channel("x-ID.0", stream(1), "1000", **{"x-xmltv-file": "GONE.xml", "x-mapping": "zzz"})})
case("dummy_mapping_kept", [stream(1)], xepg={"x-ID.0": channel("x-ID.0", stream(1), "1000", **{"x-xmltv-file": "Threadfin Dummy", "x-mapping": "60_Minutes"})})
case("new_channel_matching_dummy_id", [stream(1, tvg_id="PPV")])
case("hdhr_style_streams", [{"_file.m3u.id": "H1", "_file.m3u.name": "Tuner", "_file.m3u.path": "/d/H1.json", "group-title": "Tuner",
                              "name": "WXYZ", "tvg-id": "WXYZ", "url": "http://t/2.1", "ID-H1": "2.1", "_uuid.key": "ID-H1",
                              "_values": "Tuner WXYZ", "liveEvent": "false"}],
     settings=base(files={"m3u": {}, "hdhr": {"H1": {"name": "Tuner"}}, "xmltv": {"X1": {"name": "e"}}}), xml=xmltv(["WXYZ"], 5))
case("inactive_list_is_ignored", [stream(1, url="http://p/dup")], inactive=[stream(2, url="http://p/dup")])

# Randomised: provider channel numbers stay below any automatic number, filters always have a
# starting number (both are deliberate divergences, covered by their own cases above); unique tvg-ids across the run (Go's tvg-id keyed URL refresh misbehaves on collisions,
# see PARITY_NOTES), a single XMLTV file, at most one filter, no triple duplicates.
for n in range(30):
    count = rng.randint(1, 25)
    ids = rng.sample(range(1, 200), count)
    active = []
    for i in ids:
        active.append(stream(i, group=rng.choice(["News", "Sports", "Kids", ""]), fid=rng.choice(["M1", "M2"]),
                             tvg_id=rng.choice([f"id.{i}", f"id.{i}", f"threadfin-{i}"]),
                             live=rng.choice(["false", "false", "false", "true"]),
                             chno=rng.choice(["", "", "", str(rng.randint(1, 200))]), logo=rng.choice(["", f"http://l/{i}.png"]),
                             uuid=rng.random() < 0.8))
    xepg = {}
    next_id = 0
    for st in rng.sample(active, rng.randint(0, len(active))):
        old = copy.deepcopy(st)
        if rng.random() < 0.3:
            st["url"] += "/moved"
        if rng.random() < 0.2:
            st["name"] = st["name"] + " v2"
        xid = f"x-ID.{next_id}"; next_id += rng.randint(1, 2)
        xepg[xid] = channel(xid, old, str(1000 + next_id), **{
            "x-active": rng.random() < 0.7, "x-hide-channel": rng.random() < 0.1,
            "x-update-channel-name": rng.random() < 0.3, "x-update-channel-icon": rng.random() < 0.3,
            "x-xmltv-file": rng.choice(["-", "X1.xml", ""]), "x-mapping": rng.choice(["-", "", f"id.{rng.randint(1, 50)}"]),
            "live": rng.random() < 0.2})
    for _ in range(rng.randint(0, 2)):  # channels of streams that no longer exist
        xid = f"x-ID.{next_id}"; next_id += 1
        ghost = stream(500 + next_id)
        xepg[xid] = channel(xid, ghost, str(1000 + next_id))
    wanted = [f"id.{i}" for i in rng.sample(ids, min(len(ids), 6))] + ["id.1", "id.2"]
    settings = base(filter={"0": F(rng.choice(["News", "Sports"]), "300", rng.choice(["", "Kids"]))} if rng.random() < 0.5 else {})
    case(f"random_{n:02d}", active, xepg=xepg, settings=settings,
         xml=xmltv(sorted(set(wanted)), rng.choice([1, 2, 5])) if rng.random() < 0.7 else None)

for name, c in cases.items():
    (out / f"{name}.input.json").write_text(json.dumps(c, ensure_ascii=False, indent=1), "utf-8")
print(len(cases), "cases")
