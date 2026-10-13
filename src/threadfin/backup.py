"""Backups and restore (port of ``backup.go`` and ``zipFiles`` / ``extractZIP`` in ``compression.go``).

Archive layout, as written by the Go app: the system files at the top level, folders under their
path relative to the config folder (``data/images/logo.png``), folder entries with a trailing ``/``,
files deflated, Unix permissions kept.

- ``auto_backup``: before every scheduled / start-up update, into ``backup.path``; keeps
  ``backup.keep`` archives (``threadfin_auto_backup_YYYYMMDD_HHMM.zip``) with the uploaded images.
- ``create_backup``: the download from the UI, into the temp folder, with the whole data folder.
- ``restore_backup``: unpacks an archive into the config folder. The whole archive is checked first
  (paths, ``settings.json``, version), so a rejected archive changes nothing.
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from .jsonio import write_bytes_atomic
from .settings import Settings
from .system import SYSTEM_FILES, SystemInfo

AUTO_MARKER = "threadfin_auto_backup"
MANUAL_PREFIX = "threadfin_backup_"
STAMP_FORMAT = "%Y%m%d_%H%M"


class BackupError(ValueError):
    """An archive could not be restored (nothing was changed)."""


@dataclass(slots=True)
class AutoBackupResult:
    path: Path | None = None  # None with backup.keep 0
    deleted: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class RestoreResult:
    old_port: str
    new_port: str

    @property
    def port_changed(self) -> bool:
        return self.new_port != self.old_port

    def web_url(self, base_url: str) -> str:
        """Where the UI will be after a restart if the port changed, else ``""`` (Go ``newWebURL``)."""
        if not self.port_changed:
            return ""
        return f"{base_url}/web/".replace(f":{self.old_port}", f":{self.new_port}", 1)


def version_error(system: SystemInfo) -> str:
    return f"Invalid settings file (settings.json), file must be at least version {system.compatibility}"


def backup_folder(system: SystemInfo, settings: Settings) -> Path:
    """``backup.path`` if set (with or without a trailing slash), else ``<config>/backup``."""
    return Path(settings.backup_path) if settings.backup_path else system.folder_backup


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now()).strftime(STAMP_FORMAT)


def _write_zip(target: Path, system: SystemInfo, folder: Path) -> None:
    """System files (missing ones skipped) plus ``folder`` (if it exists), written atomically."""
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with (
            os.fdopen(fd, "wb") as handle,
            zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive,
        ):
            for name in SYSTEM_FILES:
                path = system.config / name
                if path.is_file():
                    archive.write(path, name)
            if folder.is_dir():
                for root, dirs, files in os.walk(folder):
                    dirs.sort()
                    base = Path(root)
                    archive.write(base, base.relative_to(system.config).as_posix() + "/")
                    for name in sorted(files):
                        path = base / name
                        archive.write(path, path.relative_to(system.config).as_posix())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def auto_backup(system: SystemInfo, settings: Settings, now: datetime | None = None) -> AutoBackupResult:
    """Rotate the automatic backups and write a new one (Go ``ThreadfinAutoBackup``).

    Keeps the newest ``backup.keep - 1`` old archives (by name, i.e. by time stamp) and adds one;
    ``backup.keep`` 0 deletes them all and writes none. Every ``*.zip`` whose name contains
    ``threadfin_auto_backup`` counts as an old archive.
    """
    folder = backup_folder(system, settings)
    folder.mkdir(parents=True, exist_ok=True)
    result = AutoBackupResult()

    old = sorted(p.name for p in folder.iterdir() if p.is_file() and p.suffix == ".zip" and AUTO_MARKER in p.name)
    keep = max(settings.backup_keep - 1, 0)
    for name in old[: max(len(old) - keep, 0)]:
        (folder / name).unlink(missing_ok=True)
        result.deleted.append(folder / name)
    if settings.backup_keep == 0:
        return result

    result.path = folder / f"{AUTO_MARKER}_{_stamp(now)}.zip"
    _write_zip(result.path, system, system.folder_images_upload)
    return result


def create_backup(system: SystemInfo, now: datetime | None = None) -> Path:
    """Full backup for download (Go ``ThreadfinBackup``): system files and the data folder."""
    system.folder_temp.mkdir(parents=True, exist_ok=True)
    target = system.folder_temp / f"{MANUAL_PREFIX}{_stamp(now)}.zip"
    _write_zip(target, system, system.folder_data)
    return target


def _entry_path(name: str) -> str | None:
    """Relative target of an archive entry ("" = the config folder itself), None if it escapes.

    Go joined the name onto the target folder, so a leading ``/`` stays inside and ``a/../b``
    is ``b``, but ``../x`` was written outside the config folder.
    """
    parts: list[str] = []
    for part in name.replace("\\", "/").split("/"):  # Go on Windows wrote folder entries with "\"
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    return "/".join(parts)


def _mode(info: zipfile.ZipInfo) -> int:
    """Unix permissions from the archive as Go created them under umask 022; 0644 otherwise."""
    mode = (info.external_attr >> 16) & 0o777 if info.create_system == 3 else 0
    return (mode & ~0o022) or 0o644


def restore_backup(system: SystemInfo, archive: str | os.PathLike[str], current_port: str) -> RestoreResult:
    """Unpack a backup into the config folder (Go ``ThreadfinRestore``, without the reload).

    Raises ``BackupError`` before writing anything if the archive is not a zip, has an entry
    outside the config folder, or lacks a loadable ``settings.json`` of at least
    ``system.compatibility``. The caller reloads the running state afterwards.
    """
    try:
        zf = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupError(f"not a backup archive: {exc}") from exc

    with zf:
        plan: list[tuple[zipfile.ZipInfo, str]] = []
        for info in zf.infolist():
            rel = _entry_path(info.filename)
            if rel is None:
                raise BackupError(f"archive entry outside the config folder: {info.filename}")
            plan.append((info, rel))

        settings_entry = next((i for i, rel in plan if rel == "settings.json" and not i.is_dir()), None)
        if settings_entry is None:
            raise BackupError("the archive contains no settings.json")
        try:
            raw = json.loads(zf.read(settings_entry))
        except ValueError as exc:
            raise BackupError(f"invalid settings.json in the archive: {exc}") from exc
        if not isinstance(raw, dict):
            raise BackupError("invalid settings.json in the archive: not a JSON object")
        version = raw.get("version")
        if not isinstance(version, str) or version < system.compatibility:  # Go compares the text
            raise BackupError(version_error(system))
        try:
            restored = Settings.model_validate(raw)
        except ValidationError as exc:
            raise BackupError(f"invalid settings.json in the archive: {exc}") from exc

        for info, rel in plan:
            target = system.config / rel
            if info.is_dir() or info.filename.endswith("\\"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            write_bytes_atomic(target, zf.read(info), _mode(info))

    return RestoreResult(old_port=current_port, new_port=restored.port)


__all__ = [
    "AUTO_MARKER",
    "MANUAL_PREFIX",
    "AutoBackupResult",
    "BackupError",
    "RestoreResult",
    "auto_backup",
    "backup_folder",
    "create_backup",
    "restore_backup",
    "version_error",
]
