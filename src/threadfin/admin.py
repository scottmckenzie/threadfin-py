"""Changes made from the web UI / REST API (port of the save functions in ``data.go``).

Everything is validated before anything changes: a rejected request leaves the settings, filters,
providers and channels exactly as they were (Go applied some of a request in memory before failing,
and then saved it with the next change).

- ``update_settings``: ``updateServerSettings`` (the settings page). ``tests/golden/admin`` compares it
  with the real Go code.
- ``apply_filter_changes``: ``saveFilter``. Also golden-tested.
- ``provider_changes``, ``channel_changes``, ``bulk_channel_changes``: what the provider, mapping and
  bulk-edit dialogs may change, with type checks (Go stored whatever the browser sent).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .jsonio import write_bytes_atomic
from .settings import Settings
from .system import FFMPEG_DEFAULT_OPTIONS, VLC_DEFAULT_OPTIONS, SystemInfo
from .xepg import XEPGChannel, parse_float


class AdminError(ValueError):
    """A change was refused; nothing was changed."""


# Go error texts (screen.go), kept where the UI shows them.
FFMPEG_NOT_FOUND = "FFmpeg binary was not found. Check the FFmpeg binary path in the Threadfin settings."
VLC_NOT_FOUND = "VLC binary was not found. Check the VLC path binary in the Threadfin settings."
INVALID_FILTER_RULE = "Invalid filter rule"

# --- settings --------------------------------------------------------------------------------

# Settings the settings page may change, by JSON key: the keys of Go's request struct that exist in
# settings.json, plus ``excludeStreamHttps`` (Go's request struct spelled it ``excludeStreamsHttps``,
# so it could never be saved). Port, UUID, providers, filters etc. are changed elsewhere or not at all.
SETTABLE: dict[str, str] = {
    "api": "bool",
    "ssdp": "bool",
    "authentication.api": "bool",
    "authentication.m3u": "bool",
    "authentication.pms": "bool",
    "authentication.web": "bool",
    "authentication.xml": "bool",
    "backup.keep": "int",
    "backup.path": "folder",
    "buffer": "str",
    "buffer.size.kb": "int",
    "buffer.timeout": "float",
    "cache.images": "bool",
    "epgSource": "str",
    "ffmpeg.options": "str",
    "ffmpeg.path": "file",
    "ffmpeg.forceHttp": "bool",
    "vlc.options": "str",
    "vlc.path": "file",
    "files.update": "bool",
    "temp.path": "folder",
    "tuner": "int",
    "udpxy": "str",
    "update": "times",
    "user.agent": "str",
    "xepg.replace.missing.images": "bool",
    "xepg.replace.channel.title": "bool",
    "ThreadfinAutoUpdate": "bool",
    "storeBufferInRAM": "bool",
    "forceHttps": "bool",
    "excludeStreamHttps": "bool",
    "httpsPort": "int",
    "httpsThreadfinDomain": "str",
    "httpThreadfinDomain": "str",
    "bindIpAddress": "str",
    "enableNonAscii": "bool",
    "epgCategories": "str",
    "epgCategoriesColors": "str",
    "dummy": "bool",
    "dummyChannel": "str",
    "ignoreFilters": "bool",
}
CHOICES: dict[str, tuple[str, ...]] = {"epgSource": ("PMS", "XEPG"), "buffer": ("-", "ffmpeg", "vlc")}

# Changing one of these rewrites the channel database / guide / playlist (Go rebuilt for some of them,
# or regenerated the files in the background).
REBUILD_KEYS = frozenset({
    "epgSource", "xepg.replace.missing.images", "xepg.replace.channel.title", "enableNonAscii",
    "epgCategories", "dummy", "dummyChannel", "ignoreFilters", "cache.images",
})  # fmt: skip

_AUTH_LEVELS = ("authentication.api", "authentication.m3u", "authentication.pms", "authentication.xml")
_TIME = re.compile(r"([01]\d|2[0-3])[0-5]\d")


def _check_type(key: str, kind: str, value: Any) -> Any:
    if kind == "bool":
        ok = isinstance(value, bool)
    elif kind == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "float":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif kind == "times":
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
    else:
        ok = isinstance(value, str)
    if not ok:
        raise AdminError(f"{key}: invalid value {value!r}")
    if key in CHOICES and value not in CHOICES[key]:
        raise AdminError(f"{key}: must be one of {', '.join(CHOICES[key])}")
    return value


def parse_update_times(values: list[str]) -> list[str]:
    """Spaces removed, each ``HHMM`` from 0000 to 2359 (Go ``time.Parse("1504")``)."""
    times = []
    for value in values:
        value = value.replace(" ", "")
        if not _TIME.fullmatch(value):
            raise AdminError(f"Invalid formatting of the time: {value!r} (use HHMM, 0000-2359)")
        times.append(value)
    return times


def _folder(key: str, value: str) -> str:
    """Go ``checkFolder`` + ``checkFilePermission``: created if missing, must be writable."""
    value = value.rstrip(os.sep) + os.sep
    path = Path(value)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / "permission.test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise AdminError(f"{key}: {exc}") from exc
    return value


def update_settings(settings: Settings, changes: Mapping[str, Any]) -> tuple[Settings, set[str]]:
    """Validate ``changes`` (JSON keys) and return the new settings and the keys that changed.

    ``settings`` itself is not modified; the caller stores and saves the result. ``null`` values are
    ignored, as Go did. Unknown keys and keys this page cannot change are refused (Go ignored them).
    """
    data = settings.to_json_dict()
    for key, value in changes.items():
        if value is None:
            continue
        kind = SETTABLE.get(key)
        if kind is None:
            raise AdminError(f"unknown setting: {key}")
        value = _check_type(key, kind, value)
        if kind == "times":
            value = parse_update_times(value)
        elif kind == "folder":
            value = _folder(key, value)
        elif kind == "file" and value and not Path(value).is_file():
            raise AdminError(f"{value}: File not found")
        data[key] = value

    if not data["authentication.web"]:
        for key in _AUTH_LEVELS:
            data[key] = False
    if not data["ffmpeg.options"]:
        data["ffmpeg.options"] = FFMPEG_DEFAULT_OPTIONS
    if not data["vlc.options"]:
        data["vlc.options"] = VLC_DEFAULT_OPTIONS
    if data["buffer"] == "ffmpeg" and not data["ffmpeg.path"]:
        raise AdminError(FFMPEG_NOT_FOUND)
    if data["buffer"] == "vlc" and not data["vlc.path"]:
        raise AdminError(VLC_NOT_FOUND)

    new = Settings.model_validate(data)
    old = settings.to_json_dict()
    changed = {k for k, v in new.to_json_dict().items() if old.get(k) != v}
    return new, changed


def copy_settings(source: Settings, target: Settings) -> None:
    """Make ``target`` equal to ``source`` in place (the provider manager and web app hold it)."""
    for name in Settings.model_fields:
        setattr(target, name, getattr(source, name))
    target.__pydantic_extra__ = dict(source.__pydantic_extra__ or {})


# --- filters ---------------------------------------------------------------------------------

FILTER_TYPES = ("group-title", "custom-filter")
# Go FilterStruct: a filter with another type for one of these failed every later build.
FILTER_FIELDS: dict[str, type] = {
    "active": bool, "liveEvent": bool, "caseSensitive": bool, "description": str, "exclude": str,
    "filter": str, "include": str, "name": str, "type": str, "startingNumber": str, "x-category": str,
}  # fmt: skip
NEW_FILTER = "-1"


def _check_filter(data: Mapping[str, Any]) -> None:
    for key, value in data.items():
        expected = FILTER_FIELDS.get(key)
        if expected is not None and not isinstance(value, expected):
            raise AdminError(f"filter {key}: invalid value {value!r}")
    if "filter" in data and not data["filter"]:
        raise AdminError(INVALID_FILTER_RULE)
    if "type" in data and data["type"] not in FILTER_TYPES:
        raise AdminError(f"filter type must be one of {', '.join(FILTER_TYPES)}")


def apply_filter_changes(
    current: Mapping[str, Any], changes: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """Go ``saveFilter``: ``{id: data}`` with id ``"-1"`` for a new filter; ``delete`` removes one.

    Returns the new filter map and the ids of new filters. New filters get the lowest free number;
    changes to an unknown id are ignored. Go stored a new filter with the whole request nested in
    it (under ``"-1"``); here it is just the filter's data.
    """
    filters = {k: dict(v) if isinstance(v, dict) else v for k, v in current.items()}
    new_ids = []
    for filter_id, data in changes.items():
        if not isinstance(data, Mapping):
            raise AdminError(f"filter {filter_id}: expected an object")
        if not (filter_id == NEW_FILTER or filter_id.isdigit()):
            raise AdminError(f"invalid filter id {filter_id!r}")
        if "delete" in data:
            filters.pop(filter_id, None)
            continue
        _check_filter(data)
        if filter_id == NEW_FILTER:
            number = 0
            while str(number) in filters:
                number += 1
            filter_id = str(number)
            filters[filter_id] = {}
            new_ids.append(filter_id)
        existing = filters.get(filter_id)
        if isinstance(existing, dict):
            existing.update(data)
    return filters, new_ids


# --- providers -------------------------------------------------------------------------------

PROVIDER_KINDS = ("m3u", "hdhr", "xmltv")
_PROVIDER_FIELDS: dict[str, dict[str, str]] = {
    "m3u": {
        "name": "str", "description": "str", "file.source": "str", "tuner": "tuner", "buffer": "str",
        "http_proxy.ip": "str", "http_proxy.port": "str",
        "http_headers.origin": "str", "http_headers.referer": "str",
    },
    "hdhr": {
        "name": "str", "description": "str", "file.source": "str", "tuner": "tuner", "buffer": "str",
        "http_proxy.ip": "str", "http_proxy.port": "str",
    },
    "xmltv": {
        "name": "str", "description": "str", "file.source": "str",
        "http_proxy.ip": "str", "http_proxy.port": "str",
    },
}  # fmt: skip


def provider_changes(kind: str, changes: Mapping[str, Any], *, new: bool) -> dict[str, Any]:
    """The fields the provider dialogs edit, type-checked; ``file.source`` is required when new."""
    if kind not in PROVIDER_KINDS:
        raise AdminError(f"unknown provider type {kind!r}")
    fields = _PROVIDER_FIELDS[kind]
    out: dict[str, Any] = {}
    for key, value in changes.items():
        field = fields.get(key)
        if field is None:
            raise AdminError(f"{kind} providers have no field {key!r}")
        if field == "tuner":
            if isinstance(value, str) and value.isdigit():
                value = int(value)
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
                raise AdminError("tuner: must be a number from 1 to 100")
        elif not isinstance(value, str):
            raise AdminError(f"{key}: invalid value {value!r}")
        elif key == "buffer" and value not in CHOICES["buffer"]:
            raise AdminError(f"buffer: must be one of {', '.join(CHOICES['buffer'])}")
        out[key] = value.strip() if key == "file.source" else value
    if new and not out.get("file.source"):
        raise AdminError("file.source is required")
    if "file.source" in out and not out["file.source"]:
        raise AdminError("file.source must not be empty")
    return out


# --- channels (mapping editor) ---------------------------------------------------------------

_CHANNEL_FIELDS: dict[str, str] = {
    "x-active": "bool",
    "x-hide-channel": "bool",
    "x-name": "str",
    "x-description": "str",
    "x-update-channel-name": "bool",
    "tvg-logo": "str",
    "x-update-channel-icon": "bool",
    "x-category": "str",
    "x-group-title": "str",
    "x-xmltv-file": "str",
    "x-mapping": "str",
    "x-ppv-extra": "str",
    "x-backup-channel-1": "str",
    "x-backup-channel-2": "str",
    "x-backup-channel-3": "str",
    "x-channelID": "number",
}
# JSON key -> XEPGChannel attribute
CHANNEL_ATTRIBUTES = {
    "x-active": "x_active", "x-hide-channel": "x_hide_channel", "x-name": "x_name",
    "x-description": "x_description", "x-update-channel-name": "x_update_channel_name",
    "tvg-logo": "tvg_logo", "x-update-channel-icon": "x_update_channel_icon",
    "x-category": "x_category", "x-group-title": "x_group_title", "x-xmltv-file": "x_xmltv_file",
    "x-mapping": "x_mapping", "x-ppv-extra": "x_ppv_extra", "x-backup-channel-1": "x_backup_channel_1",
    "x-backup-channel-2": "x_backup_channel_2", "x-backup-channel-3": "x_backup_channel_3",
    "x-channelID": "x_channel_id",
}  # fmt: skip
BULK_FIELDS = ("x-active", "x-hide-channel", "x-category", "x-group-title")


def _number_text(value: Any) -> str:
    text = str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""
    number = parse_float(text)
    if number is None or number <= 0:
        raise AdminError(f"x-channelID: {value!r} is not a channel number")
    return text


def channel_changes(changes: Mapping[str, Any], xmltv_files: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Validate a mapping-dialog change: ``{JSON key: value}`` -> ``{attribute: value}``.

    ``xmltv_files`` is ``{file name: {channel id: ...}}`` (the XMLTV mapping, incl. the dummy file):
    ``x-xmltv-file`` must be one of them (or ``-``), and ``x-mapping`` a channel of that file.
    """
    out: dict[str, Any] = {}
    for key, value in changes.items():
        kind = _CHANNEL_FIELDS.get(key)
        if kind is None:
            raise AdminError(f"channels have no editable field {key!r}")
        if kind == "number":
            value = _number_text(value)
        elif (kind == "bool" and not isinstance(value, bool)) or (kind == "str" and not isinstance(value, str)):
            raise AdminError(f"{key}: invalid value {value!r}")
        out[CHANNEL_ATTRIBUTES[key]] = value
    file = out.get("x_xmltv_file")
    if file is not None and file != "-" and file not in xmltv_files:
        raise AdminError(f"unknown XMLTV file {file!r}")
    return out


def apply_channel_changes(channel: XEPGChannel, changes: Mapping[str, Any]) -> XEPGChannel:
    """A copy of ``channel`` with validated changes (from ``channel_changes``)."""
    updated = channel.copy()
    for attribute, value in changes.items():
        setattr(updated, attribute, value)
    if "x_xmltv_file" in changes and changes["x_xmltv_file"] == "-":
        updated.x_mapping = "-"
    return updated


def check_mapping(channel: XEPGChannel, xmltv_files: Mapping[str, Mapping[str, Any]]) -> None:
    """``x-mapping`` must be a channel of the chosen file; an active channel needs both."""
    file, mapping = channel.x_xmltv_file, channel.x_mapping
    if file in ("", "-") and mapping not in ("", "-"):
        raise AdminError("choose an XMLTV file for the XMLTV channel")
    if file not in ("", "-") and mapping not in ("", "-") and mapping not in xmltv_files.get(file, {}):
        raise AdminError(f"{file} has no channel {mapping!r}")
    if channel.x_active and (file in ("", "-") or mapping in ("", "-")):
        raise AdminError("an active channel needs an XMLTV file and channel")


def bulk_channel_changes(changes: Mapping[str, Any]) -> dict[str, Any]:
    """Bulk edit: active / hidden / category / group, and ``x-channels-start`` (renumber)."""
    out: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "x-channels-start":
            out[key] = parse_float(_number_text(value))
            continue
        if key not in BULK_FIELDS:
            raise AdminError(f"{key!r} cannot be changed for several channels at once")
        kind = _CHANNEL_FIELDS[key]
        if (kind == "bool" and not isinstance(value, bool)) or (kind == "str" and not isinstance(value, str)):
            raise AdminError(f"{key}: invalid value {value!r}")
        out[CHANNEL_ATTRIBUTES[key]] = value
    return out


# --- logos -----------------------------------------------------------------------------------

LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
              ".svg": "image/svg+xml", ".webp": "image/webp", ".ico": "image/x-icon"}  # fmt: skip
MAX_LOGO_BYTES = 5 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def logo_filename(name: str) -> str:
    """A safe file name in the upload folder (Go wrote the browser's name anywhere, ``../`` included)."""
    base = _SAFE_NAME.sub("_", Path(name.replace("\\", "/")).name).lstrip(".")
    if Path(base).suffix.lower() not in LOGO_TYPES:
        raise AdminError(f"logos must be one of {', '.join(sorted(LOGO_TYPES))}")
    return base


def save_logo(system: SystemInfo, name: str, data: bytes) -> str:
    """Store an uploaded logo; return its file name (served at ``/data_images/<name>``)."""
    filename = logo_filename(name)
    if not data:
        raise AdminError("empty file")
    if len(data) > MAX_LOGO_BYTES:
        raise AdminError("logos must be at most 5 MB")
    system.folder_images_upload.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(system.folder_images_upload / filename, data)
    return filename


__all__ = [
    "BULK_FIELDS",
    "CHANNEL_ATTRIBUTES",
    "FFMPEG_NOT_FOUND",
    "FILTER_FIELDS",
    "FILTER_TYPES",
    "INVALID_FILTER_RULE",
    "LOGO_TYPES",
    "NEW_FILTER",
    "PROVIDER_KINDS",
    "REBUILD_KEYS",
    "SETTABLE",
    "VLC_NOT_FOUND",
    "AdminError",
    "apply_channel_changes",
    "apply_filter_changes",
    "bulk_channel_changes",
    "channel_changes",
    "check_mapping",
    "copy_settings",
    "logo_filename",
    "parse_update_times",
    "provider_changes",
    "save_logo",
    "update_settings",
]
