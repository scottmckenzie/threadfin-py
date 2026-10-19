"""Backup / restore parity with the real Go code (tests/golden/backup).

Each case builds the same file tree as the Go harness, runs the operation and compares the result,
the error and the whole tree afterwards (archives are compared entry by entry: name, mode,
compression method and content). The time stamp of the run is normalised to ``{ts}``.
"""

from __future__ import annotations

import collections
import io
import json
import os
import stat
import struct
import tempfile
import unittest
import unittest.mock
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from threadfin import backup
from threadfin.backup import (
    BackupError,
    auto_backup,
    create_backup,
    restore_backup,
    version_error,
)
from threadfin.settings import Settings
from threadfin.system import SystemInfo

GOLDEN = Path(__file__).parent / "golden" / "backup"
CASES = sorted(GOLDEN.glob("*.input.json"))
NOW = datetime(2026, 3, 4, 5, 6, 7)
STAMP = "20260304_0506"

# Go behaviour that is fixed on purpose; each is asserted in Divergences below.
DIVERGENT = {
    "auto_path_without_slash",  # Go wrote "<path>threadfin_auto_backup_...zip" next to the folder
    "auto_path_without_slash_missing",  # Go failed: the folder was never created
    "auto_missing_system_file",  # Go silently stopped at the first missing file
    "manual_missing_system_file",
    "restore_zip_slip",  # Go wrote "../evil.txt" outside the config folder
    "restore_nested_without_dir_entries",  # Go failed: folders without their own entry were not created
    "restore_executable_mode",  # Go restored data/run.sh as 0755 (executable)
}
# Go panicked; here a clean error / result, with the same files afterwards.
PANICS = {
    "restore_without_version": version_error(SystemInfo(config=Path("/x"))),
    "restore_numeric_version": version_error(SystemInfo(config=Path("/x"))),
    "restore_without_port": "",  # restored; the port falls back to the default 34400 (unchanged)
}


def dump_zip(data: bytes, stamp: str) -> list[dict[str, Any]] | None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None
    entries = []
    with archive:
        for info in archive.infolist():
            entry: dict[str, Any] = {"name": info.filename.replace(stamp, "{ts}"), "method": info.compress_type}
            if info.is_dir():
                entry["dir"] = True
            else:
                entry["mode"] = f"{(info.external_attr >> 16) & 0o777:04o}"
                entry["content"] = archive.read(info).decode(errors="surrogateescape")
            entries.append(entry)
    return sorted(entries, key=lambda e: e["name"])


def dump_tree(work: Path, stamp: str) -> dict[str, Any]:
    tree: dict[str, Any] = {}
    for root, dirs, files in os.walk(work):
        for name in dirs:
            tree[(Path(root) / name).relative_to(work).as_posix().replace(stamp, "{ts}")] = {"dir": True}
        for name in files:
            path = Path(root) / name
            entry: dict[str, Any] = {"mode": f"{stat.S_IMODE(path.stat().st_mode):04o}"}
            data = path.read_bytes()
            entries = dump_zip(data, stamp) if name.endswith(".zip") else None
            if entries is not None:
                entry["zip"] = entries
            else:
                entry["content"] = data.decode(errors="surrogateescape")
            tree[path.relative_to(work).as_posix().replace(stamp, "{ts}")] = entry
    return tree


def normalise_go(tree: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for path, entry in tree.items():
        entry = dict(entry)
        if "zip" in entry:
            entry["zip"] = [{k: v for k, v in z.items() if k in ("name", "method") or v} for z in entry["zip"]]
        out[path] = entry
    return out


def write_archive(path: Path, entries: list[dict[str, Any]]) -> None:
    """Same archive as the Go harness writes."""
    with zipfile.ZipFile(path, "w") as archive:
        for e in entries:
            info = zipfile.ZipInfo(e["name"], date_time=(2026, 1, 1, 0, 0, 0))
            if e["dir"]:
                info.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
                archive.writestr(info, b"", zipfile.ZIP_STORED)
            else:
                info.external_attr = (stat.S_IFREG | int(e["mode"] or "0644", 8)) << 16
                archive.writestr(info, e["content"].encode(), zipfile.ZIP_DEFLATED)


class Case:
    """One golden case set up in a temporary work folder."""

    def __init__(self, name: str, work: Path) -> None:
        self.name, self.work = name, work
        self.spec = json.loads((GOLDEN / f"{name}.input.json").read_text())
        self.golden = json.loads((GOLDEN / f"{name}.golden.json").read_text())
        for folder in self.spec["dirs"]:
            (work / folder).mkdir(parents=True, exist_ok=True)
        for rel, content in self.spec["files"].items():
            path = work / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(int(self.spec["modes"].get(rel, "0644"), 8))
        self.system = SystemInfo(config=work / "cfg", temp=work / "tmp")
        self.settings = Settings.model_validate(
            {"backup.keep": self.spec["keep"], "backup.path": self.spec["path"].replace("{work}", str(work)),
             "port": self.spec["port"]}
        )  # fmt: skip

    def run(self) -> tuple[str, str]:
        """(result, error) like the Go harness."""
        spec = self.spec
        try:
            if spec["op"] == "auto":
                auto_backup(self.system, self.settings, NOW)
                return "", ""
            if spec["op"] == "manual":
                return create_backup(self.system, NOW).name.replace(STAMP, "{ts}"), ""
            archive = self.work / "upload.zip"
            write_archive(archive, spec["archive"])
            result = restore_backup(self.system, archive, spec["port"])
            return result.web_url(spec["urlBase"]), ""
        except (BackupError, OSError) as exc:
            return "", str(exc) or type(exc).__name__

    def trees(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """(ours, Go's); restore cases leave out tmp/, where Go left its unpacked copy behind."""
        ours, theirs = dump_tree(self.work, STAMP), normalise_go(self.golden["tree"])
        if self.spec["op"] == "restore":
            keep = lambda p: p != "tmp" and not p.startswith("tmp/")  # noqa: E731
            ours = {p: e for p, e in ours.items() if keep(p)}
            theirs = {p: e for p, e in theirs.items() if keep(p)}
        return ours, theirs


class Base(unittest.TestCase):
    def setUp(self) -> None:
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)

    def case(self, name: str) -> Case:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Case(name, Path(tmp.name))


class Goldens(Base):
    def test_there_are_cases(self) -> None:
        self.assertGreaterEqual(len(CASES), 30)

    def test_cases(self) -> None:
        for path in CASES:
            name = path.name.removesuffix(".input.json")
            if name in DIVERGENT:
                continue
            with self.subTest(name):
                case = self.case(name)
                result, err = case.run()
                golden = case.golden
                if name in PANICS:
                    self.assertTrue(golden["panic"])
                    self.assertEqual(err, PANICS[name])
                    self.assertEqual(result, "")
                else:
                    self.assertEqual(result, golden["result"])
                    if golden["err"].startswith("Invalid settings file"):
                        self.assertEqual(err, golden["err"])
                    else:
                        self.assertEqual(bool(err), bool(golden["err"]), err or golden["err"])
                ours, theirs = case.trees()
                self.assertEqual(ours, theirs)

    def test_divergent_cases_really_differ(self) -> None:
        for name in DIVERGENT:
            with self.subTest(name):
                case = self.case(name)
                case.run()
                ours, theirs = case.trees()
                self.assertNotEqual(ours, theirs)


class Divergences(Base):
    def files(self, case: Case, archive: str) -> list[str]:
        with zipfile.ZipFile(case.work / archive) as zf:
            return sorted(zf.namelist())

    def test_path_without_trailing_slash_is_a_folder(self) -> None:
        case = self.case("auto_path_without_slash")
        case.run()
        backups = sorted(p.name for p in (case.work / "bk").iterdir())
        self.assertEqual(backups, [f"threadfin_auto_backup_{STAMP}.zip"])  # keep 1: the old one went
        self.assertFalse(any(p.name.startswith("bkthreadfin") for p in case.work.iterdir()))

    def test_missing_backup_folder_without_slash_is_created(self) -> None:
        case = self.case("auto_path_without_slash_missing")
        self.assertEqual(case.run(), ("", ""))
        self.assertTrue((case.work / "bk" / f"threadfin_auto_backup_{STAMP}.zip").is_file())

    def test_missing_system_file_is_skipped_not_fatal(self) -> None:
        case = self.case("auto_missing_system_file")
        case.run()
        names = self.files(case, f"cfg/backup/threadfin_auto_backup_{STAMP}.zip")
        self.assertNotIn("pms.json", names)
        self.assertIn("xepg.json", names)
        self.assertIn("data/images/sub/inner.png", names)
        case = self.case("manual_missing_system_file")
        case.run()
        names = self.files(case, f"tmp/threadfin_backup_{STAMP}.zip")
        self.assertNotIn("xepg.json", names)
        self.assertIn("urls.json", names)
        self.assertIn("data/M123.m3u", names)

    def test_zip_slip_is_refused_and_nothing_changes(self) -> None:
        case = self.case("restore_zip_slip")
        before = dump_tree(case.work, STAMP)
        _, err = case.run()
        self.assertIn("outside the config folder: ../evil.txt", err)
        after = dump_tree(case.work, STAMP)
        after.pop("upload.zip")
        self.assertEqual(after, before)

    def test_folders_without_entries_are_created(self) -> None:
        case = self.case("restore_nested_without_dir_entries")
        self.assertEqual(case.run(), ("http://192.168.1.2:34500/web/", ""))
        self.assertEqual((case.work / "cfg/data/new/deep/x.txt").read_text(), "x")

    def test_executable_entries_are_restored_without_execute_bits(self) -> None:
        case = self.case("restore_executable_mode")
        self.assertEqual(case.run(), (case.golden["result"], ""))
        ours, theirs = case.trees()
        self.assertEqual(theirs["cfg/data/run.sh"]["mode"], "0755")
        self.assertEqual(ours.pop("cfg/data/run.sh"), {**theirs.pop("cfg/data/run.sh"), "mode": "0644"})
        self.assertEqual(ours, theirs)  # everything else as in Go

    def test_other_escapes_are_refused(self) -> None:
        for name in ("a/../../x", "/../x", "..", "..\\x", "data/../../../etc/passwd"):
            with self.subTest(name):
                case = self.case("restore_settings_only")
                archive = case.work / "bad.zip"
                with zipfile.ZipFile(archive, "w") as zf:
                    zf.writestr("settings.json", json.dumps({"version": "0.5.0", "port": "1"}))
                    zf.writestr(name, "x")
                with self.assertRaises(BackupError):
                    restore_backup(case.system, archive, "34400")
                self.assertEqual(json.loads((case.work / "cfg/settings.json").read_text())["port"], "34400")


class Restore(Base):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name)
        self.system = SystemInfo(config=self.work / "cfg", temp=self.work / "tmp")
        self.system.create_folders()

    def archive(self, entries: dict[str, str | bytes]) -> Path:
        path = self.work / "a.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        return path

    def test_round_trip(self) -> None:
        cfg = self.system.config
        (cfg / "settings.json").write_text(json.dumps({"version": "0.5.0", "port": "34400"}))
        (cfg / "authentication.json").write_text('{"users": {}}')
        (cfg / "authentication.json").chmod(0o600)
        (cfg / "data" / "images" / "logo.png").write_bytes(b"\x89PNG\x00\xff")
        (cfg / "data" / "M1.m3u").write_text("#EXTM3U")
        archive = create_backup(self.system, NOW)
        files = lambda tree: {p: e for p, e in tree.items() if not e.get("dir")}  # noqa: E731
        before = files(dump_tree(cfg, STAMP))
        self.assertEqual(len(before), 4)

        other = SystemInfo(config=self.work / "other", temp=self.work / "tmp")
        result = restore_backup(other, archive, "34400")
        self.assertFalse(result.port_changed)
        self.assertEqual(result.web_url("http://h:34400"), "")
        self.assertEqual(files(dump_tree(other.config, STAMP)), before)

    def test_port_change(self) -> None:
        archive = self.archive({"settings.json": json.dumps({"version": "0.5.0", "port": "8080"})})
        result = restore_backup(self.system, archive, "34400")
        self.assertTrue(result.port_changed)
        self.assertEqual(result.web_url("http://192.168.1.2:34400"), "http://192.168.1.2:8080/web/")

    def test_not_a_zip(self) -> None:
        path = self.work / "junk.zip"
        path.write_bytes(b"not a zip")
        with self.assertRaisesRegex(BackupError, "not a backup archive"):
            restore_backup(self.system, path, "34400")
        with self.assertRaisesRegex(BackupError, "not a backup archive"):
            restore_backup(self.system, self.work / "missing.zip", "34400")

    def test_settings_that_cannot_be_loaded_are_refused(self) -> None:
        for settings in ('["a"]', json.dumps({"version": "0.5.0", "port": 34400}),
                         json.dumps({"version": "0.5.0", "tuner": "many"})):  # fmt: skip
            with self.subTest(settings):
                with self.assertRaisesRegex(BackupError, "invalid settings.json"):
                    restore_backup(self.system, self.archive({"settings.json": settings, "xepg.json": "{}"}), "1")
                self.assertFalse((self.system.config / "xepg.json").exists())

    def test_windows_separators(self) -> None:
        archive = self.archive({
            "settings.json": json.dumps({"version": "0.5.0", "port": "1"}),
            "data\\images\\": b"", "data\\images\\a.png": b"A",
        })  # fmt: skip
        restore_backup(self.system, archive, "1")
        self.assertEqual((self.system.folder_images_upload / "a.png").read_bytes(), b"A")

    def test_entry_without_unix_mode(self) -> None:
        path = self.work / "a.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("settings.json", json.dumps({"version": "0.5.0", "port": "1"}))
            info = zipfile.ZipInfo("pms.json")
            info.create_system, info.external_attr = 0, 0
            zf.writestr(info, "{}")
        restore_backup(self.system, path, "1")
        self.assertEqual(stat.S_IMODE((self.system.config / "pms.json").stat().st_mode), 0o644)

    def test_read_write_modes_are_kept_and_execute_bits_dropped(self) -> None:
        for archived, restored in [(0o600, 0o600), (0o640, 0o640), (0o755, 0o644), (0o700, 0o600),
                                   (0o4755, 0o644), (0o111, 0o644), (0o000, 0o644)]:  # fmt: skip
            with self.subTest(f"{archived:04o}"):
                path = self.work / "a.zip"
                with zipfile.ZipFile(path, "w") as zf:
                    zf.writestr("settings.json", json.dumps({"version": "0.5.0", "port": "1"}))
                    info = zipfile.ZipInfo("x/ffprobe")
                    info.external_attr = (stat.S_IFREG | archived) << 16
                    zf.writestr(info, "#!/bin/sh\n")
                restore_backup(self.system, path, "1")
                self.assertEqual(stat.S_IMODE((self.system.config / "x" / "ffprobe").stat().st_mode), restored)

    def test_group_and_world_write_bits_are_dropped(self) -> None:
        path = self.work / "a.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("settings.json", json.dumps({"version": "0.5.0", "port": "1"}))
            info = zipfile.ZipInfo("pms.json")
            info.external_attr = (stat.S_IFREG | 0o666) << 16
            zf.writestr(info, "{}")
        restore_backup(self.system, path, "1")
        self.assertEqual(stat.S_IMODE((self.system.config / "pms.json").stat().st_mode), 0o644)

    def test_total_over_limit_refused(self) -> None:
        (self.system.config / "settings.json").write_text(json.dumps({"version": "0.5.0", "port": "1"}))
        archive = self.archive(
            {
                "settings.json": json.dumps({"version": "0.5.0", "port": "1"}),
                "data/a.bin": b"x" * 1000,
                "data/b.bin": b"x" * 1000,
            }
        )
        before = dump_tree(self.work, STAMP)
        with (
            self.assertRaisesRegex(BackupError, "archive contents exceed"),
            unittest.mock.patch.object(backup, "MAX_RESTORE_TOTAL", 1500),
        ):
            restore_backup(self.system, archive, "1")
        after = dump_tree(self.work, STAMP)
        self.assertEqual(after, before)

    def test_large_settings_refused(self) -> None:
        (self.system.config / "settings.json").write_text(json.dumps({"version": "0.5.0", "port": "1"}))
        archive = self.archive(
            {
                "settings.json": json.dumps({"version": "0.5.0", "port": "1"}),
                "xepg.json": "{}",
            }
        )
        before = dump_tree(self.work, STAMP)
        with self.assertRaises(BackupError), unittest.mock.patch.object(backup, "MAX_SETTINGS_BYTES", 10):
            restore_backup(self.system, archive, "1")
        after = dump_tree(self.work, STAMP)
        self.assertEqual(after, before)

    def test_not_enough_disk_space_refused(self) -> None:
        (self.system.config / "settings.json").write_text(json.dumps({"version": "0.5.0", "port": "1"}))
        archive = self.archive(
            {
                "settings.json": json.dumps({"version": "0.5.0", "port": "1"}),
                "data/x.bin": b"x" * 1000,
            }
        )
        usage = collections.namedtuple("usage", "total used free")(100, 90, 10)
        before = dump_tree(self.work, STAMP)
        with (
            self.assertRaisesRegex(BackupError, "not enough free space"),
            unittest.mock.patch.object(backup.shutil, "disk_usage", return_value=usage),
        ):
            restore_backup(self.system, archive, "1")
        after = dump_tree(self.work, STAMP)
        self.assertEqual(after, before)

    def test_understated_entry_refused_before_writing(self) -> None:
        (self.system.config / "settings.json").write_text(json.dumps({"version": "0.5.0", "port": "1"}))
        entries = {
            "settings.json": json.dumps({"version": "0.5.0", "port": "1"}),
            "data/x.bin": bytes(range(256)) * 40,
        }
        archive_path = self.work / "a.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        raw = bytearray(archive_path.read_bytes())
        local_idx = raw.index(b"data/x.bin") - 30
        struct.pack_into("<I", raw, local_idx + 22, 1000)
        central_idx = raw.rindex(b"data/x.bin") - 46
        struct.pack_into("<I", raw, central_idx + 24, 1000)
        archive_path.write_bytes(raw)
        before = dump_tree(self.system.config, STAMP)
        with self.assertRaises(BackupError):
            restore_backup(self.system, archive_path, "1")
        after = dump_tree(self.system.config, STAMP)
        self.assertEqual(after, before)

    def test_bad_settings_crc_refused(self) -> None:
        (self.system.config / "settings.json").write_text(json.dumps({"version": "0.5.0", "port": "1"}))
        entries = {"settings.json": json.dumps({"version": "0.5.0", "port": "1"})}
        archive_path = self.work / "a.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        raw = bytearray(archive_path.read_bytes())
        local_idx = raw.index(b"settings.json") - 30
        data_start = local_idx + 30 + len(b"settings.json") + 1
        raw[data_start] ^= 0xFF
        archive_path.write_bytes(raw)
        before = dump_tree(self.system.config, STAMP)
        with self.assertRaises(BackupError):
            restore_backup(self.system, archive_path, "1")
        after = dump_tree(self.system.config, STAMP)
        self.assertEqual(after, before)

    def test_corrupt_deflate_stream_refused(self) -> None:
        (self.system.config / "settings.json").write_text(json.dumps({"version": "0.5.0", "port": "1"}))
        entries = {
            "settings.json": json.dumps({"version": "0.5.0", "port": "1"}),
            "data/x.bin": (bytes(range(256)) * 40)[:10000],
        }
        archive_path = self.work / "a.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        raw = bytearray(archive_path.read_bytes())
        central_idx = raw.rindex(b"data/x.bin") - 46
        compressed_size = struct.unpack_from("<I", raw, central_idx + 20)[0]
        local_idx = struct.unpack_from("<I", raw, central_idx + 42)[0]
        data_start = local_idx + 30 + len(b"data/x.bin")
        middle = data_start + compressed_size // 2
        raw[middle : middle + 8] = b"\xff" * 8
        archive_path.write_bytes(raw)
        before = dump_tree(self.system.config, STAMP)
        with self.assertRaises(BackupError):
            restore_backup(self.system, archive_path, "1")
        after = dump_tree(self.system.config, STAMP)
        self.assertEqual(after, before)

    def test_unknown_compression_refused(self) -> None:
        (self.system.config / "settings.json").write_text(json.dumps({"version": "0.5.0", "port": "1"}))
        entries = {"settings.json": json.dumps({"version": "0.5.0", "port": "1"})}
        archive_path = self.work / "a.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        raw = bytearray(archive_path.read_bytes())
        local_idx = raw.index(b"settings.json") - 30
        struct.pack_into("<H", raw, local_idx + 8, 99)
        central_idx = raw.rindex(b"settings.json") - 46
        struct.pack_into("<H", raw, central_idx + 10, 99)
        archive_path.write_bytes(raw)
        before = dump_tree(self.system.config, STAMP)
        with self.assertRaises(BackupError):
            restore_backup(self.system, archive_path, "1")
        after = dump_tree(self.system.config, STAMP)
        self.assertEqual(after, before)

    def test_too_many_entries_refused(self) -> None:
        (self.system.config / "settings.json").write_text(json.dumps({"version": "0.5.0", "port": "1"}))
        archive = self.archive(
            {
                "settings.json": json.dumps({"version": "0.5.0", "port": "1"}),
                "data/a.bin": b"1",
                "data/b.bin": b"2",
                "data/c.bin": b"3",
                "data/d.bin": b"4",
            }
        )
        before = dump_tree(self.work, STAMP)
        with (
            self.assertRaisesRegex(BackupError, "too many entries"),
            unittest.mock.patch.object(backup, "MAX_ENTRIES", 3),
        ):
            restore_backup(self.system, archive, "1")
        after = dump_tree(self.work, STAMP)
        self.assertEqual(after, before)

    def test_large_central_directory_refused(self) -> None:
        (self.system.config / "settings.json").write_text(json.dumps({"version": "0.5.0", "port": "1"}))
        archive = self.archive(
            {
                "settings.json": json.dumps({"version": "0.5.0", "port": "1"}),
                "data/a.bin": b"1",
                "data/b.bin": b"2",
                "data/c.bin": b"3",
                "data/d.bin": b"4",
            }
        )
        before = dump_tree(self.work, STAMP)
        with (
            self.assertRaisesRegex(BackupError, "too many entries"),
            unittest.mock.patch.object(backup, "MAX_CENTRAL_DIRECTORY", 100),
        ):
            restore_backup(self.system, archive, "1")
        after = dump_tree(self.work, STAMP)
        self.assertEqual(after, before)

    def test_zip64_locator_refused(self) -> None:
        (self.system.config / "settings.json").write_text(json.dumps({"version": "0.5.0", "port": "1"}))
        archive = self.archive(
            {
                "settings.json": json.dumps({"version": "0.5.0", "port": "1"}),
                "data/a.bin": b"1",
                "data/b.bin": b"2",
                "data/c.bin": b"3",
                "data/d.bin": b"4",
            }
        )
        data = archive.read_bytes()
        eocd_idx = data.rfind(b"PK\x05\x06")
        data = data[:eocd_idx] + b"PK\x06\x07" + b"\x00" * 16 + data[eocd_idx:]
        self.assertIn(b"PK\x06\x07", data)
        self.assertLess(
            struct.unpack_from("<I", data, eocd_idx + 20 + 12)[0],
            backup.MAX_CENTRAL_DIRECTORY,
        )
        (self.work / "modified.zip").write_bytes(data)
        before = dump_tree(self.work, STAMP)
        with (
            self.assertRaisesRegex(BackupError, "too many entries"),
            unittest.mock.patch.object(zipfile, "ZipFile", side_effect=AssertionError),
        ):
            restore_backup(self.system, self.work / "modified.zip", "1")
        after = dump_tree(self.work, STAMP)
        self.assertEqual(after, before)

    def test_entries_are_streamed(self) -> None:
        (self.system.config / "settings.json").write_text(json.dumps({"version": "0.5.0", "port": "1"}))
        archive = self.archive(
            {
                "settings.json": json.dumps({"version": "0.5.0", "port": "1"}),
                "xepg.json": "{}",
                "data/x.bin": b"abc",
            }
        )
        original_read = zipfile.ZipFile.read

        def guarded_read(zf: zipfile.ZipFile, name: str | zipfile.ZipInfo, pwd: bytes | None = None) -> bytes:
            name = getattr(name, "filename", name)
            if name not in ("settings.json",):
                raise AssertionError(f"Unexpected read of {name}")
            return original_read(zf, name, pwd)

        with unittest.mock.patch.object(zipfile.ZipFile, "read", guarded_read):
            restore_backup(self.system, archive, "1")
        self.assertEqual((self.system.config / "data" / "x.bin").read_bytes(), b"abc")


class AutoBackup(Base):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.system = SystemInfo(config=Path(tmp.name) / "cfg")
        self.system.create_folders()
        self.system.create_files()

    def test_rotation_over_several_runs(self) -> None:
        settings = Settings.model_validate({"backup.keep": 3})
        for minute in range(6):
            result = auto_backup(self.system, settings, NOW.replace(minute=minute))
            assert result.path is not None
        names = sorted(p.name for p in self.system.folder_backup.iterdir())
        self.assertEqual(names, [f"threadfin_auto_backup_20260304_05{m:02d}.zip" for m in (3, 4, 5)])
        self.assertEqual([p.name for p in result.deleted], ["threadfin_auto_backup_20260304_0502.zip"])

    def test_keep_zero_writes_nothing(self) -> None:
        settings = Settings.model_validate({"backup.keep": 0})
        result = auto_backup(self.system, settings, NOW)
        self.assertIsNone(result.path)
        self.assertEqual(list(self.system.folder_backup.iterdir()), [])

    def test_no_temporary_file_left_on_failure(self) -> None:
        settings = Settings.model_validate({"backup.keep": 2})
        self.system.file_pms.chmod(0)
        if os.access(self.system.file_pms, os.R_OK):
            self.skipTest("running as root")
        self.addCleanup(self.system.file_pms.chmod, 0o644)
        with self.assertRaises(PermissionError):
            auto_backup(self.system, settings, NOW)
        self.assertEqual(list(self.system.folder_backup.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
