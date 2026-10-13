"""Writes tests/golden/admin/*.input.json. Run run_admin_golden.sh afterwards.

Each case: a settings.json (``{work}`` = the case's work folder), files/folders to create, and the raw
web-UI request (``saveSettings`` or ``saveFilter``) as the Go WebSocket handler would decode it.
"""

import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

BASE = {
    "uuid": "2026-01-ABCD-EFGHIJ",
    "ffmpeg.path": "{work}/bin/ffmpeg",
    "vlc.path": "{work}/bin/cvlc",
    "backup.path": "{work}/cfg/backup/",
    "temp.path": "{work}/tmp/",
    "ThreadfinAutoUpdate": False,
}
BINARIES = ["bin/ffmpeg", "bin/cvlc", "bin/other"]


def case(name, op, request, *, settings=None, files=(), dirs=()):
    body = {
        "op": op,
        "settings": {**BASE, **(settings or {})},
        "request": request,
        "files": [*BINARIES, *files],
        "dirs": list(dirs),
    }
    (out / f"{name}.input.json").write_text(json.dumps(body, indent=1, ensure_ascii=False) + "\n")


def settings(name, changes, **kw):
    case(name, "settings", {"cmd": "saveSettings", "settings": changes}, **kw)


def filters(name, changes, existing=None, **kw):
    case(name, "filter", {"cmd": "saveFilter", "filter": changes}, settings={"filter": existing or {}}, **kw)


# --- saveSettings ---------------------------------------------------------------------------
settings("booleans", {"api": True, "ssdp": False, "files.update": False, "dummy": True, "ignoreFilters": True})
settings("tuner", {"tuner": 3})
settings("epg_source_xepg", {"epgSource": "XEPG"})
settings(
    "strings",
    {
        "udpxy": "10.0.0.1:4022",
        "user.agent": "VLC",
        "dummyChannel": "PPV",
        "epgCategories": "Kids:kids",
        "epgCategoriesColors": "kids:red",
        "bindIpAddress": "0.0.0.0",
    },
)
settings(
    "https",
    {
        "forceHttps": True,
        "httpsPort": 8443,
        "httpsThreadfinDomain": "tv.example.com",
        "httpThreadfinDomain": "lan.example.com",
    },
)
settings("update_times", {"update": ["0300", " 12 30 ", "2359", "0000"]})
settings("update_empty", {"update": []})
for bad in ("2400", "0960", "930", "12:30", "abcd", "03000", "", "1260"):
    settings(f"update_invalid_{bad.replace(':', '_') or 'empty_string'}", {"update": ["0300", bad]})
settings("update_not_a_string", {"update": [300]})
settings("backup_path_without_slash", {"backup.path": "{work}/bk"})
settings("backup_path_many_slashes", {"backup.path": "{work}/bk///"})
settings("backup_path_nested_missing", {"backup.path": "{work}/a/b/bk/"})
settings("backup_path_under_a_file", {"backup.path": "{work}/bin/ffmpeg/bk/"})
settings("temp_path", {"temp.path": "{work}/t2"})
settings("ffmpeg_path_ok", {"ffmpeg.path": "{work}/bin/other"})
settings("ffmpeg_path_missing", {"ffmpeg.path": "{work}/bin/nope"})
settings("ffmpeg_path_directory", {"ffmpeg.path": "{work}/bin"})
settings("vlc_path_empty", {"vlc.path": ""})
settings(
    "auth_off_clears_levels",
    {
        "authentication.web": False,
        "authentication.m3u": True,
        "authentication.xml": True,
        "authentication.pms": True,
        "authentication.api": True,
    },
)
settings("auth_on", {"authentication.web": True, "authentication.m3u": True})
settings("auth_level_without_web", {"authentication.m3u": True})
settings("auth_level_with_web_on", {"authentication.xml": True}, settings={"authentication.web": True})
settings("ffmpeg_options_empty", {"ffmpeg.options": "", "vlc.options": ""})
settings("buffer_ffmpeg_ok", {"buffer": "ffmpeg"})
settings("buffer_ffmpeg_without_path", {"buffer": "ffmpeg", "ffmpeg.path": ""})
settings("buffer_vlc_without_path", {"buffer": "vlc", "vlc.path": ""})
settings("backup_keep_zero", {"backup.keep": 0})
settings("backup_keep_float", {"backup.keep": 2.5})
settings("buffer_timeout_negative", {"buffer.timeout": -5})
settings("buffer_timeout_float", {"buffer.timeout": 1.5, "buffer.size.kb": 2048})
settings("tuner_as_string", {"tuner": "5"})
settings("bool_as_string", {"api": "true"})
settings("unknown_and_fixed_keys", {"port": "1234", "uuid": "x", "language": "de", "files": {}, "nonsense": 1})
settings("exclude_stream_https", {"excludeStreamHttps": True})
settings("replace_missing_images", {"xepg.replace.missing.images": False})
settings("null_value", {"udpxy": None})

# --- saveFilter -----------------------------------------------------------------------------
GROUP = {
    "active": True,
    "caseSensitive": False,
    "description": "",
    "exclude": "",
    "filter": "News",
    "include": "",
    "liveEvent": False,
    "name": "News",
    "type": "group-title",
    "startingNumber": "1000",
    "x-category": "",
}
CUSTOM = {
    "active": True,
    "caseSensitive": True,
    "description": "d",
    "filter": "BBC {HD}",
    "name": "BBC",
    "type": "custom-filter",
}
filters("new_into_empty", {"-1": GROUP})
filters("new_after_two", {"-1": CUSTOM}, {"0": GROUP, "1": CUSTOM})
filters("new_fills_gap", {"-1": CUSTOM}, {"0": GROUP, "2": CUSTOM})
filters("edit_existing", {"1": {"name": "Renamed", "include": "HD", "liveEvent": True}}, {"0": GROUP, "1": GROUP})
filters("delete", {"0": {"delete": True}}, {"0": GROUP, "1": CUSTOM})
filters("delete_with_other_keys", {"1": {**CUSTOM, "delete": True}}, {"0": GROUP, "1": CUSTOM})
filters("new_with_empty_rule", {"-1": {**GROUP, "filter": ""}}, {"0": CUSTOM})
filters("edit_with_empty_rule", {"0": {"filter": ""}}, {"0": GROUP})
filters("edit_unknown_id", {"7": {"name": "x"}}, {"0": GROUP})
filters("new_without_rule", {"-1": {"name": "No rule", "type": "custom-filter"}})
filters("rule_not_a_string", {"-1": {**CUSTOM, "filter": 5}})
filters("edit_and_new", {"0": {"name": "Edited"}, "-1": CUSTOM}, {"0": GROUP})
filters("key_not_a_number", {"abc": {"name": "x"}}, {"0": GROUP})
