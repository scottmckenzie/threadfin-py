"""Writes tests/golden/backup/*.input.json. Run run_backup_golden.sh afterwards.

Paths are relative to an empty work folder; the config folder is ``cfg/``, the temp folder ``tmp/``.
``{work}`` in ``path`` is replaced by the work folder.
"""
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

SETTINGS = {"port": "34400", "version": "0.5.0", "backup.keep": 10}
CONFIG = {
    "cfg/authentication.json": '{"users": {}}',
    "cfg/pms.json": '{"a": 1}',
    "cfg/settings.json": json.dumps(SETTINGS),
    "cfg/xepg.json": '{"x-1": {}}',
    "cfg/urls.json": '{"id": {}}',
    "cfg/data/images/logo.png": "PNG",
    "cfg/data/images/sub/inner.png": "INNER",
    "cfg/data/threadfin.xml": "<tv/>",
    "cfg/data/M123.m3u": "#EXTM3U",
}
MODES = {"cfg/authentication.json": "0600", "cfg/data/images/logo.png": "0640"}
DEFAULT = "{work}/cfg/backup/"


def old(*stamps, folder="cfg/backup"):
    return {f"{folder}/threadfin_auto_backup_{s}.zip": "old " + s for s in stamps}


def without(files, *names):
    return {k: v for k, v in files.items() if k not in names}


def case(name, op, *, files=CONFIG, modes=MODES, dirs=(), keep=10, path=DEFAULT, archive=(), port="34400"):
    body = {
        "op": op, "files": files, "modes": modes, "dirs": list(dirs), "keep": keep, "path": path,
        "port": port, "urlBase": f"http://192.168.1.2:{port}", "archive": list(archive),
    }
    (out / f"{name}.input.json").write_text(json.dumps(body, indent=1, ensure_ascii=False) + "\n")


# --- automatic backups (scheduler / start-up) ------------------------------------------------
case("auto_first", "auto", dirs=["cfg/backup"])
case("auto_creates_folder", "auto")
case("auto_nested_folder", "auto", path="{work}/a/b/bk/")
case("auto_empty_path", "auto", path="", dirs=["cfg/backup"])
case("auto_rotation_keep3", "auto", keep=3, files={
    **CONFIG, **old("20250101_0000", "20250102_0000", "20250103_0000", "20250104_0000", "20250105_0000"),
    "cfg/backup/other.zip": "other", "cfg/backup/threadfin_auto_backup_notes.txt": "notes",
    "cfg/backup/threadfin_backup_20250101_0000.zip": "manual", "cfg/backup/my_threadfin_auto_backup.zip": "mine",
})
case("auto_keep1", "auto", keep=1, files={**CONFIG, **old("20250101_0000", "20250102_0000")})
case("auto_keep0", "auto", keep=0, files={**CONFIG, **old("20250101_0000", "20250102_0000", "20250103_0000")})
case("auto_fewer_than_keep", "auto", keep=5, files={**CONFIG, **old("20250101_0000", "20250102_0000")})
case("auto_path_without_slash", "auto", path="{work}/bk", files={**CONFIG, **old("20250101_0000", folder="bk")}, keep=1)
case("auto_path_without_slash_missing", "auto", path="{work}/bk")
case("auto_missing_system_file", "auto", dirs=["cfg/backup"], files=without(CONFIG, "cfg/pms.json"))
case("auto_no_images", "auto", dirs=["cfg/backup"],
     files=without(CONFIG, "cfg/data/images/logo.png", "cfg/data/images/sub/inner.png"))
case("auto_empty_images", "auto", dirs=["cfg/backup", "cfg/data/images"],
     files=without(CONFIG, "cfg/data/images/logo.png", "cfg/data/images/sub/inner.png"))

# --- manual backups (download from the UI) ---------------------------------------------------
case("manual_full", "manual", dirs=["tmp"])
case("manual_creates_temp", "manual")
case("manual_no_data", "manual", files={k: v for k, v in CONFIG.items() if "/data/" not in k}, modes={})
case("manual_missing_system_file", "manual", files=without(CONFIG, "cfg/xepg.json"))


# --- restore ---------------------------------------------------------------------------------
def entry(name, content="", mode="", dir=False):
    return {"name": name, "content": content, "mode": mode, "dir": dir}


def settings_entry(**changes):
    data = {**SETTINGS, **changes}
    return entry("settings.json", json.dumps({k: v for k, v in data.items() if v is not None}))


BACKUP = [
    entry("authentication.json", '{"restored": true}', "0600"),
    entry("pms.json", '{"b": 2}'),
    entry("xepg.json", '{"x-2": {}}'),
    entry("urls.json", "{}"),
    entry("data/", dir=True),
    entry("data/images/", dir=True),
    entry("data/images/new.png", "NEW", "0644"),
    entry("data/M999.m3u", "#EXTM3U restored"),
]


def restore(name, *entries, files=CONFIG):
    case(name, "restore", files=files, archive=entries)


restore("restore_port_changed", settings_entry(port="34500"), *BACKUP)
restore("restore_into_empty_config", settings_entry(port="34500"), *BACKUP, files={})
restore("restore_settings_only", settings_entry(port="8080"))
restore("restore_executable_mode", settings_entry(port="1"), entry("data/", dir=True), entry("data/run.sh", "#!/bin/sh", "0755"))
restore("restore_version_too_old", settings_entry(version="0.0.9", port="34500"), *BACKUP)
restore("restore_version_compared_as_text", settings_entry(version="0.10.0", port="34500"))
restore("restore_version_newer", settings_entry(version="2.0.0", port="34500"))
restore("restore_without_settings", *BACKUP)
restore("restore_invalid_settings", entry("settings.json", "{not json"), *BACKUP)
restore("restore_without_version", settings_entry(version=None, port="34500"), *BACKUP)
restore("restore_numeric_version", settings_entry(version=5, port="34500"), *BACKUP)
restore("restore_without_port", settings_entry(port=None), *BACKUP)
restore("restore_zip_slip", settings_entry(port="34500"), entry("../evil.txt", "evil"), entry("data/../inside.txt", "ok"))
restore("restore_leading_slash", settings_entry(port="34500"), entry("/data/", dir=True), entry("/data/abs.txt", "abs"))
restore("restore_nested_without_dir_entries", settings_entry(port="34500"), entry("data/new/deep/x.txt", "x"))
