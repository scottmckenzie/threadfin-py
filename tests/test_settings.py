"""Parity tests for settings loading against the real Go ``loadSettings``.

Golden files come from ``tools/go_golden/run_settings_golden.sh``.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from threadfin.jsonio import JSONFileError, load_json_object, save_json
from threadfin.settings import Settings, SettingsError, create_uuid, load_settings
from threadfin.system import SYSTEM_FILES, SystemInfo

GOLDEN_DIR = Path(__file__).parent / "golden" / "settings"
CASES = sorted(p.name.removesuffix(".input.json") for p in GOLDEN_DIR.glob("*.input.json"))
UUID_RE = re.compile(r"^\d{4}-\d{2}-[A-Z0-9]{4}-[A-Z0-9]{6}$")


def make_system(root: Path) -> SystemInfo:
    # The Go golden run used these folders for the backup/temp defaults.
    return SystemInfo(
        config=root, backup=Path("/tmp/tfgolden/backup"), temp=Path("/tmp/tfgolden/tmp")
    )


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.system = make_system(self.root)


class GoldenSettingsTests(TempDirCase):
    def test_cases_present(self) -> None:
        self.assertGreaterEqual(len(CASES), 6)

    def test_matches_go_output(self) -> None:
        for case in CASES:
            with self.subTest(case=case):
                input_path = GOLDEN_DIR / f"{case}.input.json"
                golden = json.loads((GOLDEN_DIR / f"{case}.golden.json").read_text("utf-8"))
                shutil.copy(input_path, self.system.file_settings)

                if golden.get("__error__"):
                    with self.assertRaises(SettingsError):
                        load_settings(self.system)
                    continue

                input_data = json.loads(input_path.read_text("utf-8"))
                load_settings(self.system)
                ours = json.loads(self.system.file_settings.read_text("utf-8"))

                # Volatile values: compare shape / environment, not the literal.
                if "uuid" not in input_data:
                    self.assertRegex(ours["uuid"], UUID_RE)
                    self.assertRegex(golden["uuid"], UUID_RE)
                    ours["uuid"] = golden["uuid"] = "<generated>"
                for key, exe in (("ffmpeg.path", "ffmpeg"), ("vlc.path", "cvlc")):
                    if key not in input_data:
                        self.assertEqual(ours[key], shutil.which(exe) or "")
                        ours[key] = golden[key] = "<which>"

                # Every key Go writes has the same value, in the same order.
                self.assertEqual({k: ours.get(k) for k in golden}, golden)
                self.assertEqual([k for k in ours if k in golden], list(golden))
                # Anything extra must be a key the input carried (Go would drop it).
                for key in set(ours) - set(golden):
                    self.assertIn(key, input_data)


class SettingsBehaviourTests(TempDirCase):
    def write(self, data: object) -> None:
        save_json(self.system.file_settings, data)

    def test_unknown_keys_round_trip(self) -> None:
        self.write({"my.new.setting": {"a": 1}})
        load_settings(self.system)
        self.assertEqual(load_json_object(self.system.file_settings)["my.new.setting"], {"a": 1})

    def test_load_is_idempotent(self) -> None:
        self.write({"uuid": "2024-01-ABCD-123456"})
        load_settings(self.system, flag_port="9999")
        first = self.system.file_settings.read_text("utf-8")
        load_settings(self.system)
        self.assertEqual(self.system.file_settings.read_text("utf-8"), first)
        self.assertEqual(json.loads(first)["port"], "9999")  # CLI override persists, like Go

    def test_null_becomes_zero_value(self) -> None:
        self.write({"port": None, "tuner": None, "files": None, "ssdp": None})
        s = load_settings(self.system)
        self.assertEqual((s.port, s.tuner, s.ssdp), ("", 0, False))
        self.assertEqual(s.files.m3u, {})

    def test_missing_file_is_an_error(self) -> None:
        with self.assertRaises(SettingsError):
            load_settings(self.system)

    def test_invalid_json_is_an_error(self) -> None:
        self.system.file_settings.write_text("{not json", encoding="utf-8")
        with self.assertRaises(SettingsError):
            load_settings(self.system)

    def test_whole_numbers_written_without_fraction(self) -> None:
        self.write({})
        load_settings(self.system)
        text = self.system.file_settings.read_text("utf-8")
        self.assertIn('"buffer.timeout": 500,', text)
        self.assertIn('"mapping.first.channel": 1000,', text)

    def test_model_accepts_field_names(self) -> None:
        self.assertEqual(Settings(port="1", tuner=3).tuner, 3)


class SystemAndJsonTests(TempDirCase):
    def test_create_folders_and_files(self) -> None:
        sys_info = SystemInfo(config=self.root / "cfg", temp=self.root / "tmp")
        sys_info.create_folders()
        created = sys_info.create_files()
        self.assertEqual([p.name for p in created], list(SYSTEM_FILES))
        self.assertTrue(sys_info.folder_images_cache.is_dir())
        self.assertTrue(sys_info.folder_images_upload.is_dir())
        self.assertEqual(sys_info.create_files(), [])  # second run creates nothing
        self.assertEqual(load_json_object(sys_info.file_xepg), {})

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        save_json(self.root / "a.json", {"x": 1})
        self.assertEqual([p.name for p in self.root.iterdir()], ["a.json"])

    def test_load_json_object_rejects_non_objects(self) -> None:
        path = self.root / "list.json"
        path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(JSONFileError):
            load_json_object(path)

    def test_uuid_format(self) -> None:
        self.assertRegex(create_uuid(), UUID_RE)


if __name__ == "__main__":
    unittest.main()
