"""Writes tests/golden/epg/*.input.json. Run run_epg_golden.sh afterwards."""

import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

RICH = (
    '<programme start="20260101000000 +0000" stop="20260101010000 +0000" channel="{cid}">'
    '<title lang="en">  Café Show {n}  </title><title lang="fr">Émission</title>'
    '<sub-title lang="en">Sub {n}</sub-title><desc lang="en">Desc é {n}</desc>'
    '<credits><director>Dir</director><actor role="Hero">A</actor><actor>B</actor><writer>W</writer>'
    "<presenter>P</presenter><producer>Pr</producer></credits><date>2020</date>"
    '<category lang="en">Drama</category><category lang="en">Thriller</category><language>English</language>'
    '<icon src="http://x/p{n}.png" width="100" height="200"/><country lang="en">US</country>'
    '<episode-num system="xmltv_ns">0.{n}.</episode-num>'
    "<video><aspect>16:9</aspect><colour>yes</colour><present>yes</present><quality>HDTV</quality></video>"
    '<previously-shown start="20250101000000 +0000"/><premiere>First</premiere><new/><live/>'
    '<rating system="MPAA"><value>PG</value><icon src="http://r/pg.png"/></rating>'
    '<star-rating system="imdb"><value>7/10</value></star-rating></programme>'
)
PLAIN = (
    '<programme start="2026010{d}120000 +0100" stop="2026010{d}130000 +0100" channel="{cid}">'
    "<title>Plain {d}</title></programme>"
)
BARE = (
    '<programme start="bad" stop="20260101020000 +0000" channel="{cid}"><title>Bad start</title>'
    "<desc>d</desc></programme>"
)


def xmltv(channels, rich=2, plain=2, bare=False):
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv generator-info-name="t">']
    for cid in channels:
        parts.append(f'<channel id="{cid}"><display-name>{cid}</display-name></channel>')
    for cid in channels:
        parts += [RICH.format(cid=cid, n=n) for n in range(rich)]
        parts += [PLAIN.format(cid=cid, d=d + 1) for d in range(plain)]
        if bare:
            parts.append(BARE.format(cid=cid))
    parts.append("</tv>")
    return "".join(parts)


def ch(xid, chno, name="Chan", file="X1.xml", mapping=None, **kw):
    c = {
        "_file.m3u.id": "M1",
        "_file.m3u.name": "P",
        "_file.m3u.path": "/d/M1.m3u",
        "group-title": "G",
        "name": name,
        "tvg-id": f"id.{xid}",
        "tvg-logo": "http://l/logo.png",
        "tvg-name": name,
        "tvg-chno": str(chno),
        "url": f"http://p/{xid}",
        "_uuid.key": "",
        "_values": name,
        "x-active": True,
        "x-category": "",
        "x-channelID": str(chno),
        "x-epg": f"x-ID.{xid}",
        "x-group-title": "G",
        "x-mapping": mapping or f"id.{xid}",
        "x-xmltv-file": file,
        "x-ppv-extra": "",
        "x-backup-channel-1": "",
        "x-backup-channel-2": "",
        "x-backup-channel-3": "",
        "x-hide-channel": False,
        "x-name": name,
        "x-update-channel-icon": False,
        "x-update-channel-name": False,
        "x-description": "",
        "live": False,
        "is_backup_channel": False,
        "backup_channel_1": None,
        "backup_channel_2": None,
        "backup_channel_3": None,
        "channelUniqueID": "",
    }
    c.update(kw)
    return {c["x-epg"]: c}


def merge(*parts):
    d = {}
    for p in parts:
        d.update(p)
    return d


def settings(**kw):
    s = {"enableNonAscii": False, "xepg.replace.missing.images": True, "xepg.replace.channel.title": False}
    s.update(kw)
    return s


cases = {}


def case(name, xepg, xmls=None, tz="Australia/Sydney", **kw):
    cases[name] = {"tz": tz, "settings": settings(**kw), "xmltv": xmls or {}, "xepg": xepg}


X = {"X1.xml": xmltv(["id.1", "id.2", "id.3"], bare=True)}
case("rich_programmes", ch(1, 1, "Alpha"), X)
case("non_ascii_enabled", ch(1, 1, "Alpéha"), X, enableNonAscii=True)
case("non_ascii_disabled_channel_name", ch(1, 1, "Alpéha ☃"), X)
case("replace_images_off", ch(1, 1, "Alpha"), X, **{"xepg.replace.missing.images": False})
case("category_series", ch(1, 1, "Alpha", **{"x-category": "Sports"}), X)
case("category_movie", ch(1, 1, "Alpha", **{"x-category": "Movie"}), X)
case(
    "quality_from_name",
    merge(
        ch(1, 1, "Alpha HD"),
        ch(2, 2, "Beta UHD"),
        ch(3, 3, "Gamma 4K"),
        ch(4, 4, "Delta FHD", mapping="id.1"),
        ch(5, 5, "NoSpaceHD", mapping="id.2"),
    ),
    X,
)
case(
    "sorting",
    merge(
        ch(1, "10", "Ten"),
        ch(2, "9", "Nine"),
        ch(3, "2.5", "Two five"),
        ch(4, "abc", "Alpha text"),
        ch(5, "Abd", "Alpha text 2"),
        ch(6, "1000", "Thousand"),
    ),
    X,
)
case(
    "hidden_and_inactive_excluded",
    merge(ch(1, 1, "Shown"), ch(2, 2, "Hidden", **{"x-hide-channel": True}), ch(3, 3, "Off", **{"x-active": False})),
    X,
)
case("channel_without_programmes", ch(1, 1, "Empty", mapping="nope"), X)
case("missing_xmltv_file", ch(1, 1, "Alpha", file="GONE.xml"), X)
case("two_channels_share_a_guide", merge(ch(1, 1, "One", mapping="id.1"), ch(2, 2, "Two", mapping="id.1")), X)
case("empty_names_fall_back", ch(1, 1, "", **{"x-name": "", "tvg-name": "Fallback"}), X)
case("name_empty_everywhere_skipped", ch(1, 1, "", **{"x-name": "", "tvg-name": ""}), X)
case("no_logo", ch(1, 1, "Alpha", **{"tvg-logo": ""}), X)
for mins in ("30", "60", "90", "120", "180", "240", "360"):
    case(f"dummy_{mins}", ch(1, 1, "Dummy Chan", file="Threadfin Dummy", mapping=f"{mins}_Minutes"))
case(
    "dummy_with_description_and_movie",
    ch(
        1,
        1,
        "Dummy Chan",
        file="Threadfin Dummy",
        mapping="60_Minutes",
        **{"x-description": "Custom é text", "x-category": "Movie"},
    ),
)
case(
    "dummy_non_ascii_enabled",
    ch(1, 1, "Dummy é", file="Threadfin Dummy", mapping="60_Minutes", **{"x-description": "dé"}),
    enableNonAscii=True,
)
case(
    "dummy_no_images",
    ch(1, 1, "Dummy Chan", file="Threadfin Dummy", mapping="120_Minutes"),
    **{"xepg.replace.missing.images": False},
)
case("dummy_unknown_mapping", ch(1, 1, "Dummy Chan", file="Threadfin Dummy", mapping="weird"))
for tz in ("UTC", "America/New_York", "Asia/Kolkata", "Pacific/Auckland", "Europe/London"):
    case(
        "dummy_tz_" + tz.replace("/", "_"), ch(1, 1, "Dummy Chan", file="Threadfin Dummy", mapping="180_Minutes"), tz=tz
    )
PPV = dict(file="Threadfin Dummy", mapping="PPV", live=True)
for i, name in enumerate(
    [
        "UFC 300 Main Card",
        "Fight Night 9PM",
        "UFC 7:30 PM ET",
        "NFL 12/25-1:00 PM",
        "Event 7/4 12:00 PM",
        "Game 3.21 3:45 AM",
        "Show 4/15 3AM",
        "NBA 11:59PM PST",
        "Plain Event",
        "6PM",
        "Café 9PM",
    ]
):
    case(f"ppv_{i:02d}", ch(1, 1, name, **PPV))
case(
    "ppv_replace_title",
    ch(1, 1, "UFC 300 9PM", **{**PPV, "x-ppv-extra": "Main Card"}),
    **{"xepg.replace.channel.title": True},
)
case(
    "ppv_replace_title_non_ascii_enabled",
    ch(1, 1, "UFC é 9PM", **{**PPV, "x-ppv-extra": "xé"}),
    enableNonAscii=True,
    **{"xepg.replace.channel.title": True},
)
case("ppv_replace_title_without_extra", ch(1, 1, "Fight 8PM", **PPV), **{"xepg.replace.channel.title": True})
case("ppv_uses_tvg_name_when_no_x_name", ch(1, 1, "Fight 8PM", **{**PPV, "x-name": ""}))
for tz in ("UTC", "America/New_York", "Pacific/Auckland"):
    case("ppv_tz_" + tz.replace("/", "_"), ch(1, 1, "UFC 7:30 PM ET", **PPV), tz=tz)
case(
    "mixed_guide_types",
    merge(
        ch(1, 1, "Real"), ch(2, 2, "Dummy", file="Threadfin Dummy", mapping="360_Minutes"), ch(3, 3, "Fight 9PM", **PPV)
    ),
    X,
)

for name, c in cases.items():
    (out / f"{name}.input.json").write_text(json.dumps(c, ensure_ascii=False, indent=1), "utf-8")
print(len(cases), "cases")
