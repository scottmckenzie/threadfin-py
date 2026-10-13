"""HDHomeRun emulation: what Plex, Jellyfin, Emby and other DVR software ask a tuner for.

Port of ``getDiscover``, ``getLineupStatus``, ``getLineup``, ``getCapability`` and
``getGuideNumberPMS`` (hdhr.go). ``tests/golden/hdhr`` compares all of them with the real Go code.

Two lineup sources, chosen by the ``epgSource`` setting:

* ``XEPG``: the active, visible channels of the channel database, numbered by ``x-channelID``;
* ``PMS``: every active stream of the stream database; the guide number is the provider's own
  number (``_uuid.value``) or a stable ``id-N`` kept per channel name in ``pms.json``.

Differences from Go: the XEPG lineup is sorted by channel number (Go listed channels in random map order), an empty lineup is ``[]`` (Go sent ``null``), a missing ``pms.json`` starts
empty instead of giving every channel an empty guide number, and ``pms.json`` is written atomically.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from . import jsonio
from .playlist import StreamRegistry, channel_order, stream_id
from .xepg import XEPGChannel

SPEC_NS = "urn:schemas-upnp-org:device-1-0"


@dataclass(frozen=True, slots=True)
class Device:
    name: str  # friendly name shown in the DVR software
    app_name: str
    version: str
    uuid: str
    tuner: int
    base_url: str  # scheme://host[:port] the client reached us on

    @property
    def device_id(self) -> str:
        return self.uuid if self.tuner == 1 else f"{self.uuid}:{self.tuner}"


def discover(device: Device) -> dict[str, Any]:
    return {
        "BaseURL": device.base_url,
        "DeviceAuth": device.app_name,
        "DeviceID": device.device_id,
        "FirmwareName": f"bin_{device.version}",
        "FirmwareVersion": device.version,
        "FriendlyName": device.name,
        "LineupURL": f"{device.base_url}/lineup.json",
        "Manufacturer": "Golang",
        "ModelNumber": device.version,
        "TunerCount": device.tuner,
    }


def lineup_status(scan_in_progress: bool = False) -> dict[str, Any]:
    return {
        "ScanInProgress": 1 if scan_in_progress else 0,
        "ScanPossible": 0,
        "Source": "Cable",
        "SourceList": ["Cable"],
    }


def capability_xml(device: Device) -> str:
    """``/device.xml`` in exactly the layout Go produced (first line indented by one space)."""
    d = escape
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f' <root xmlns="{SPEC_NS}">\n'
        f"   <URLBase>{d(device.base_url)}</URLBase>\n"
        "   <specVersion>\n     <major>1</major>\n     <minor>0</minor>\n   </specVersion>\n"
        "   <device>\n"
        "     <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>\n"
        f"     <friendlyName>{d(device.name)}</friendlyName>\n"
        "     <manufacturer>Silicondust</manufacturer>\n"
        "     <modelName>HDTC-2US</modelName>\n"
        "     <modelNumber>HDTC-2US</modelNumber>\n"
        "     <serialNumber></serialNumber>\n"
        f"     <UDN>uuid:{d(device.device_id)}</UDN>\n"
        "   </device>\n"
        " </root>"
    )


class PMSIds:
    """``pms.json``: channel name -> stable guide number ``id-N``."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.ids: dict[str, str] = {}
        if self.path and self.path.exists():
            try:
                raw = jsonio.load_json_object(self.path)
            except (OSError, ValueError):
                raw = {}
            self.ids = {k: v for k, v in raw.items() if isinstance(v, str)}

    def number_for(self, name: str) -> str:
        known = self.ids.get(name)
        if known is not None:
            return known
        used = set(self.ids.values())
        i = 0
        while f"id-{i}" in used:
            i += 1
        self.ids[name] = f"id-{i}"
        self._save()
        return self.ids[name]

    def _save(self) -> None:
        if self.path:
            jsonio.save_json(self.path, self.ids)


def lineup_xepg(channels: Mapping[str, XEPGChannel], registry: StreamRegistry, base_url: str) -> list[dict[str, str]]:
    entries = []
    for channel in sorted(channels.values(), key=channel_order):
        if channel.x_active and not channel.x_hide_channel:
            uid = registry.register(channel)
            entries.append(
                {"GuideName": channel.x_name, "GuideNumber": channel.x_channel_id, "URL": f"{base_url}/stream/{uid}"}
            )
    return entries


def lineup_pms(
    streams: Iterable[Mapping[str, Any]], pms: PMSIds, registry: StreamRegistry, base_url: str
) -> list[dict[str, str]]:
    entries = []
    touched: set[str] = set()
    for stream in streams:
        name = str(stream.get("name", ""))
        number = str(stream.get("_uuid.value", "")) or pms.number_for(name)
        playlist_id, url = str(stream.get("_file.m3u.id", "")), str(stream.get("url", ""))
        uid = registry.add(playlist_id, number, name, url, keep=stream_id(playlist_id, url) in touched)
        touched.add(uid)
        entries.append({"GuideName": name, "GuideNumber": number, "URL": f"{base_url}/stream/{uid}"})
    return entries


def to_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)
