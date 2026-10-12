"""``settings.json`` model, defaults, loading and saving.

Port of ``SettingsStruct`` (struct-system.go) and ``loadSettings`` /
``saveSettings`` (system.go). The JSON keys, defaults and normalisation rules
match the Go app; ``tests/golden/settings`` proves it against the real Go code.

Settings that only the Go stream buffer used (``buffer*``, ``ffmpeg.options``,
``vlc.*``, ``storeBufferInRAM`` ...) are kept so a config folder can move
between the two apps, but this app does not act on them.
"""

from __future__ import annotations

import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .jsonio import JSONFileError, load_json_object, save_json
from .system import FFMPEG_DEFAULT_OPTIONS, VLC_DEFAULT_OPTIONS, SystemInfo, is_running_in_container

_UUID_ALPHABET = "AB1CD2EF3GH4IJ5KL6MN7OP8QR9ST0UVWXYZ"

# Fields Go marshals with ``omitempty``: dropped from the file when empty.
_OMIT_EMPTY = ("git.branch", "key", "update.url", "file", "xmltv")


class SettingsError(ValueError):
    """settings.json could not be loaded or does not match the schema."""


class SettingsFiles(BaseModel):
    hdhr: dict[str, Any] = Field(default_factory=dict)
    m3u: dict[str, Any] = Field(default_factory=dict)
    xmltv: dict[str, Any] = Field(default_factory=dict)


class Settings(BaseModel):
    """Contents of settings.json (field order = Go struct order = file order)."""

    # Unknown keys (e.g. settings added by newer versions) round-trip untouched.
    # The Go app silently drops them on save.
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    api: bool = False
    authentication_api: bool = Field(False, alias="authentication.api")
    authentication_m3u: bool = Field(False, alias="authentication.m3u")
    authentication_pms: bool = Field(False, alias="authentication.pms")
    authentication_web: bool = Field(False, alias="authentication.web")
    authentication_xml: bool = Field(False, alias="authentication.xml")
    backup_keep: int = Field(10, alias="backup.keep")
    backup_path: str = Field("", alias="backup.path")
    branch: str = Field("", alias="git.branch")
    buffer: str = "-"
    buffer_size: int = Field(1024, alias="buffer.size.kb")
    buffer_timeout: float = Field(500, alias="buffer.timeout")
    cache_images: bool = Field(False, alias="cache.images")
    epg_source: str = Field("PMS", alias="epgSource")
    ffmpeg_options: str = Field("", alias="ffmpeg.options")
    ffmpeg_path: str = Field("", alias="ffmpeg.path")
    ffmpeg_force_http: bool = Field(False, alias="ffmpeg.forceHttp")
    vlc_options: str = Field("", alias="vlc.options")
    vlc_path: str = Field("", alias="vlc.path")
    file_m3u: list[str] = Field(default_factory=list, alias="file")
    file_xmltv: list[str] = Field(default_factory=list, alias="xmltv")
    files: SettingsFiles = Field(default_factory=SettingsFiles)
    files_update: bool = Field(True, alias="files.update")
    filter: dict[str, Any] = Field(default_factory=dict)
    key: str = ""
    language: str = "en"
    log_entries_ram: int = Field(500, alias="log.entries.ram")
    m3u8_adaptive_bandwidth_mbps: int = Field(10, alias="m3u8.adaptive.bandwidth.mbps")
    mapping_first_channel: float = Field(1000, alias="mapping.first.channel")
    port: str = "34400"
    ssdp: bool = True
    temp_path: str = Field("", alias="temp.path")
    tuner: int = 1
    update: list[str] = Field(default_factory=lambda: ["0000"])
    update_url: str = Field("", alias="update.url")
    user_agent: str = Field("", alias="user.agent")
    uuid: str = ""
    udpxy: str = ""
    version: str = ""
    xepg_replace_missing_images: bool = Field(True, alias="xepg.replace.missing.images")
    xepg_replace_channel_title: bool = Field(False, alias="xepg.replace.channel.title")
    threadfin_auto_update: bool = Field(True, alias="ThreadfinAutoUpdate")
    store_buffer_in_ram: bool = Field(True, alias="storeBufferInRAM")
    force_https: bool = Field(False, alias="forceHttps")
    exclude_stream_https: bool = Field(False, alias="excludeStreamHttps")
    https_port: int = Field(443, alias="httpsPort")
    bind_ip_address: str = Field("", alias="bindIpAddress")
    https_threadfin_domain: str = Field("", alias="httpsThreadfinDomain")
    http_threadfin_domain: str = Field("", alias="httpThreadfinDomain")
    enable_non_ascii: bool = Field(False, alias="enableNonAscii")
    epg_categories: str = Field(
        "Kids:kids|News:news|Movie:movie|Series:series|Sports:sports", alias="epgCategories"
    )
    epg_categories_colors: str = Field(
        "kids:mediumpurple|news:tomato|movie:royalblue|series:gold|sports:yellowgreen",
        alias="epgCategoriesColors",
    )
    dummy: bool = False
    dummy_channel: str = Field("", alias="dummyChannel")
    ignore_filters: bool = Field(False, alias="ignoreFilters")

    @model_validator(mode="before")
    @classmethod
    def _null_means_zero_value(cls, data: Any) -> Any:
        """Go decodes JSON ``null`` into the field's zero value; do the same."""
        if not isinstance(data, dict):
            return data
        zero: dict[Any, Any] = {str: "", bool: False, int: 0, float: 0.0}
        by_alias = {(f.alias or name): f for name, f in cls.model_fields.items()}
        out = dict(data)
        for key, value in data.items():
            if value is not None or key not in by_alias:
                continue
            annotation = by_alias[key].annotation
            origin = getattr(annotation, "__origin__", annotation)
            if origin in (list, dict) or (isinstance(origin, type) and issubclass(origin, BaseModel)):
                out[key] = [] if origin is list else {}
            else:
                out[key] = zero.get(origin)
        return out

    def to_json_dict(self) -> dict[str, Any]:
        """Dict in file form: JSON key names, Go's ``omitempty`` fields dropped when empty."""
        data = self.model_dump(by_alias=True, mode="json")
        for key in ("buffer.timeout", "mapping.first.channel"):
            value = data.get(key)
            if isinstance(value, float) and value.is_integer():
                data[key] = int(value)  # Go writes 500, not 500.0
        for key in _OMIT_EMPTY:
            if key in data and not data[key]:
                del data[key]
        return data


def create_uuid() -> str:
    """``YYYY-MM-XXXX-XXXXXX`` identifier, as generated by the Go app."""
    rand = lambda n: "".join(secrets.choice(_UUID_ALPHABET) for _ in range(n))  # noqa: E731
    return f"{time.strftime('%Y-%m')}-{rand(4)}-{rand(6)}"


def _find_executable(name: str) -> str:
    return shutil.which(name) or ""


def load_settings(
    system: SystemInfo,
    *,
    flag_port: str = "",
    flag_branch: str = "",
) -> Settings:
    """Load settings.json, fill in defaults for missing keys, normalise and save.

    An explicit value in the file always wins over a default, even if it is
    empty or zero (``port: ""`` stays empty), exactly like the Go app.
    """
    try:
        raw = load_json_object(system.file_settings)
    except JSONFileError as exc:
        raise SettingsError(str(exc)) from exc

    # Defaults that depend on the environment (static ones live on the model).
    dynamic_defaults: dict[str, Any] = {
        "backup.path": str(system.folder_backup) + "/",
        "ffmpeg.options": FFMPEG_DEFAULT_OPTIONS,
        "vlc.options": VLC_DEFAULT_OPTIONS,
        "git.branch": "",
        "user.agent": system.name,
        "uuid": create_uuid(),
        "version": system.db_version,
        "ThreadfinAutoUpdate": not is_running_in_container(),
        "temp.path": str(system.folder_temp) + "/",
    }
    for key, value in dynamic_defaults.items():
        raw.setdefault(key, value)

    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        raise SettingsError(f"invalid settings in {system.file_settings}: {exc}") from exc

    # Command-line overrides (persisted to the file, as in the Go app).
    if flag_port:
        settings.port = flag_port
    if flag_branch:
        settings.branch = flag_branch

    if not settings.ffmpeg_path:
        settings.ffmpeg_path = _find_executable("ffmpeg")
    if not settings.vlc_path:
        settings.vlc_path = _find_executable("cvlc")

    settings.version = system.db_version
    save_settings(settings, system)
    return settings


def save_settings(settings: Settings, system: SystemInfo) -> None:
    """Normalise and write settings.json (port of ``saveSettings``)."""
    if settings.backup_keep == 0:
        settings.backup_keep = 10
    if not settings.backup_path:
        settings.backup_path = str(system.folder_backup) + "/"
    if settings.buffer_timeout < 0:
        settings.buffer_timeout = 0

    path: Path = system.file_settings
    save_json(path, settings.to_json_dict())
