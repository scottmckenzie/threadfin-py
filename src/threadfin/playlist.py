"""The M3U playlist Threadfin serves (``/m3u/threadfin.m3u``) and its stream-ID registry.

Port of ``buildM3U`` (m3u.go) and ``createStreamingURL`` (system.go). Every entry points at
``<base>/stream/<id>``; the id is ``md5("<playlist id>-<url>")`` and the registry (``urls.json``)
maps it back to the provider URL, which the ``/stream/`` endpoint answers with a redirect.

Behaviour kept from Go, quirks included:

* only active, visible channels are listed; the optional group filter compares ``x-group-title``
  while the emitted ``group-title`` prefers ``x-category``;
* a group is capped at ``expected - deactivated`` entries, where ``expected`` is the count in the
  provider group label (``"News (42)"``) and ``deactivated`` counts inactive/hidden channels of the
  group;
* an identical ``group|stream`` pair is written once.

Differences: ties between equal channel numbers are broken by the XEPG id (Go's ``sort.Slice`` is
not stable, so its order was arbitrary), the HTTPS rewrite of provider URLs is not ported (streams
are redirected, never proxied), double quotes in values become single quotes so a line stays
parseable, and registry entries are refreshed on every build, so a rename shows up (Go kept the first name ever seen); within one build the first channel for an id wins, as in Go.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import jsonio
from .xepg import BackupStream, XEPGChannel, parse_float


def stream_id(playlist_id: str, url: str) -> str:
    return hashlib.md5(f"{playlist_id}-{url}".encode()).hexdigest()


@dataclass(slots=True)
class StreamInfo:
    channel_number: str
    name: str
    playlist_id: str
    url: str
    backup_channel_1: BackupStream | None
    backup_channel_2: BackupStream | None
    backup_channel_3: BackupStream | None
    url_id: str

    def to_json(self) -> dict[str, Any]:
        return {
            "channelNumber": self.channel_number,
            "name": self.name,
            "playlistID": self.playlist_id,
            "url": self.url,
            "backup_channel_1": self.backup_channel_1.to_json() if self.backup_channel_1 else None,
            "backup_channel_2": self.backup_channel_2.to_json() if self.backup_channel_2 else None,
            "backup_channel_3": self.backup_channel_3.to_json() if self.backup_channel_3 else None,
            "urlID": self.url_id,
        }

    @staticmethod
    def from_json(value: Mapping[str, Any]) -> StreamInfo:
        def text(key: str) -> str:
            item = value.get(key)
            return item if isinstance(item, str) else ""

        return StreamInfo(
            text("channelNumber"), text("name"), text("playlistID"), text("url"),
            BackupStream.from_json(value.get("backup_channel_1")),
            BackupStream.from_json(value.get("backup_channel_2")),
            BackupStream.from_json(value.get("backup_channel_3")),
            text("urlID"),
        )


class StreamRegistry:
    """``urls.json``: stream id -> provider URL (and backups) for the redirect endpoint."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.entries: dict[str, StreamInfo] = {}
        if self.path and self.path.exists():
            try:
                raw = jsonio.load_json_object(self.path)
            except (OSError, ValueError):
                raw = {}
            if raw:
                self.entries = {k: StreamInfo.from_json(v) for k, v in raw.items() if isinstance(v, dict)}

    def register(self, channel: XEPGChannel, *, keep: bool = False) -> str:
        """Record the channel's stream; ``keep`` leaves an existing entry untouched."""
        return self.add(
            channel.file_m3u_id, channel.x_channel_id, channel.x_name, channel.url,
            (channel.backup_channel_1, channel.backup_channel_2, channel.backup_channel_3), keep=keep,
        )  # fmt: skip

    def add(
        self,
        playlist_id: str,
        number: str,
        name: str,
        url: str,
        backups: tuple[BackupStream | None, BackupStream | None, BackupStream | None] = (None, None, None),
        *,
        keep: bool = False,
    ) -> str:
        uid = stream_id(playlist_id, url)
        if keep and uid in self.entries:
            return uid
        self.entries[uid] = StreamInfo(number, name, playlist_id, url, *backups, uid)
        return uid

    def lookup(self, uid: str) -> StreamInfo | None:
        info = self.entries.get(uid)
        if info is not None:
            info = StreamInfo(**{**{f: getattr(info, f) for f in info.__slots__}, "url": info.url.strip("\r\n")})
        return info

    def replace_with(self, other: StreamRegistry) -> None:
        """Swap in another registry's entries (a build starts from an empty one, as in Go)."""
        self.entries = dict(other.entries)

    def prune(self, keep: Iterable[str]) -> None:
        keep = set(keep)
        self.entries = {k: v for k, v in self.entries.items() if k in keep}

    def save(self) -> None:
        if self.path:
            jsonio.save_json(self.path, {k: v.to_json() for k, v in sorted(self.entries.items())})


@dataclass(frozen=True, slots=True)
class PlaylistOptions:
    base_url: str = "http://localhost:34400"  # scheme://host[:port] clients reach this server on
    xmltv_url: str | None = None  # default: <base_url>/xmltv/threadfin.xml
    image_url: Callable[[str], str] = lambda url: url
    group_labels: tuple[str, ...] = field(default_factory=tuple)  # provider group labels "Name (N)"


def expected_group_counts(labels: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in labels:
        start = label.rfind(" (")
        end = label.rfind(")")
        if start > 0 and end > start + 2:
            digits = label[start + 2 : end]
            if digits.isascii() and digits.lstrip("+-").isdigit():
                counts[label[:start]] = int(digits)
    return counts


def _group(channel: XEPGChannel) -> str:
    return channel.x_category or channel.x_group_title or channel.group_title


def _value(text: str) -> str:
    return text.replace('"', "'").replace("\r", " ").replace("\n", " ")


def channel_order(channel: XEPGChannel) -> tuple[int, float, str, str]:
    number = parse_float(channel.tvg_chno)
    if number is not None:
        return (0, number, "", channel.x_epg)
    return (1, 0.0, channel.tvg_chno, channel.x_epg)


def build_playlist(
    channels: Mapping[str, XEPGChannel],
    options: PlaylistOptions,
    registry: StreamRegistry,
    groups: Iterable[str] = (),
) -> str:
    wanted = list(groups)
    expected = expected_group_counts(options.group_labels)
    deactivated: dict[str, int] = {}
    entries: list[XEPGChannel] = []
    for source in channels.values():
        if not source.x_active or source.x_hide_channel:
            deactivated[_group(source)] = deactivated.get(_group(source), 0) + 1
            continue
        if wanted and source.x_group_title not in wanted:
            continue
        item = source.copy()
        if not item.tvg_name:
            item.tvg_name = item.name
        entries.append(item)
    entries.sort(key=channel_order)

    xmltv = options.xmltv_url or f"{options.base_url}/xmltv/threadfin.xml"
    out = [f'#EXTM3U url-tvg="{xmltv}" x-tvg-url="{xmltv}"\n']
    seen: set[str] = set()
    touched: set[str] = set()
    emitted: dict[str, int] = {}
    for channel in entries:
        group = _group(channel)
        if group in expected and emitted.get(group, 0) >= max(expected[group] - deactivated.get(group, 0), 0):
            continue
        logo = options.image_url(channel.tvg_logo) if channel.tvg_logo else ""
        uid = stream_id(channel.file_m3u_id, channel.url)
        registry.register(channel, keep=uid in touched)
        touched.add(uid)
        stream = f"{options.base_url}/stream/{uid}"
        key = f"{group}|{stream}"
        if key in seen:
            continue
        seen.add(key)
        number, name = _value(channel.x_channel_id), _value(channel.x_name)
        out.append(
            f'#EXTINF:0 channelID="{_value(channel.x_epg)}" tvg-chno="{number}" tvg-name="{name}" '
            f'tvg-id="{number}" tvg-logo="{_value(logo)}" group-title="{_value(group)}",{name}\n{stream}\n'
        )
        emitted[group] = emitted.get(group, 0) + 1
    return "".join(out)
