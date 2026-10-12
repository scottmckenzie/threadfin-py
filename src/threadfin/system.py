"""System information: names, versions, folders and files.

Port of the parts of ``struct-system.go`` / ``config.go`` / ``system.go`` that
decide where things live. The on-disk layout is identical to the Go app so an
existing ``~/.threadfin`` folder can be used as-is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .jsonio import save_json

NAME = "Threadfin"
DB_VERSION = "0.5.0"  # settings/data format version shared with the Go app
COMPATIBILITY = "0.1.0"

# Stock option strings the Go stream buffer used. This app never runs ffmpeg/VLC
# for streaming, but the defaults are written to settings.json for compatibility.
FFMPEG_DEFAULT_OPTIONS = (
    "-hide_banner -loglevel error -analyzeduration 1000000 -probesize 1000000 -i [URL] "
    "-map 0:v -map 0:a:0 -c:v copy -c:a aac -b:a 192k -ac 2 -c:s copy -f mpegts "
    "-fflags +genpts -movflags +faststart -copyts pipe:1"
)
VLC_DEFAULT_OPTIONS = "-I dummy [URL] --sout #std{mux=ts,access=file,dst=-}"

# Files created in the config folder on first start.
SYSTEM_FILES = ("authentication.json", "pms.json", "settings.json", "xepg.json", "urls.json")


def default_config_folder() -> Path:
    return Path.home() / f".{NAME.lower()}"


def is_running_in_container() -> bool:
    return Path("/.dockerenv").exists()


@dataclass(slots=True)
class SystemInfo:
    """Resolved folder and file locations for one running instance."""

    config: Path = field(default_factory=default_config_folder)
    name: str = NAME
    version: str = __version__
    db_version: str = DB_VERSION
    compatibility: str = COMPATIBILITY

    # Overridable for tests; by default derived from ``config`` / the OS temp dir.
    backup: Path | None = None
    temp: Path | None = None

    def __post_init__(self) -> None:
        self.config = Path(self.config).expanduser()
        if self.backup is None:
            self.backup = self.config / "backup"
        if self.temp is None:
            self.temp = Path(os.environ.get("TMPDIR", "/tmp")) / self.app_name

    @property
    def app_name(self) -> str:
        return self.name.lower()

    # --- folders -----------------------------------------------------------
    @property
    def folder_backup(self) -> Path:
        assert self.backup is not None
        return self.backup

    @property
    def folder_temp(self) -> Path:
        assert self.temp is not None
        return self.temp

    @property
    def folder_data(self) -> Path:
        return self.config / "data"

    @property
    def folder_cache(self) -> Path:
        return self.config / "cache"

    @property
    def folder_images_cache(self) -> Path:
        return self.folder_cache / "images"

    @property
    def folder_images_upload(self) -> Path:
        return self.folder_data / "images"

    # --- files -------------------------------------------------------------
    @property
    def file_settings(self) -> Path:
        return self.config / "settings.json"

    @property
    def file_authentication(self) -> Path:
        return self.config / "authentication.json"

    @property
    def file_pms(self) -> Path:
        return self.config / "pms.json"

    @property
    def file_xepg(self) -> Path:
        return self.config / "xepg.json"

    @property
    def file_urls(self) -> Path:
        return self.config / "urls.json"

    @property
    def file_probe_db(self) -> Path:
        """New in the Python port: stored channel-probe results."""
        return self.folder_data / "probes.sqlite3"

    # --- setup -------------------------------------------------------------
    def create_folders(self) -> None:
        for folder in (
            self.config,
            self.folder_backup,
            self.folder_data,
            self.folder_cache,
            self.folder_images_cache,
            self.folder_images_upload,
            self.folder_temp,
        ):
            folder.mkdir(parents=True, exist_ok=True)

    def create_files(self) -> list[Path]:
        """Create any missing system files as ``{}``; return those created."""
        created: list[Path] = []
        for name in SYSTEM_FILES:
            path = self.config / name
            if not path.exists():
                save_json(path, {})
                created.append(path)
        return created
