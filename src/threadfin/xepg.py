"""The XEPG channel database: which streams become channels, with which number and EPG.

Port of ``createXEPGDatabase``, ``mapping``, ``cleanupXEPG`` and ``removeDuplicateChannels``
(xepg.go). ``tests/golden/xepg`` compares it with the real Go code. The database is a JSON
file (``xepg.json``) mapping ``x-ID.N`` to a channel; the key names match the Go app.

Behaviour kept from Go, on purpose: channels the user deactivated are not updated; new
channels start inactive unless the stream's ``tvg-id`` exists in an XMLTV file (or the stream
is a live event); the user's channel numbers and names are never overwritten; a channel is
identified by ``md5(url + tvg-id + playlist id)``, falling back to the stream's ``tg-name`` UUID.

Differences (see docs/PARITY_NOTES.md): deterministic iteration instead of Go's random map
order; a stream's URL is only refreshed by ``tvg-id`` when that is unambiguous (Go let the
last stream win and pointed all same-``tvg-id`` channels at one URL); the duplicate removal
keeps exactly one channel per group; provider-assigned channel numbers are reserved so
automatic numbers do not collide; an empty ``startingNumber`` no longer renumbers a group
from 0; removed backup channels are cleared instead of staying as stale pointers.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from decimal import Decimal
from typing import Any

from .jsonio import load_json_object, save_json
from .xmltv import DUMMY_FILE, ChannelMapping, ImageURL, identity_image_url

PPV = "PPV"
Stream = Mapping[str, str]
ProgramCounter = Callable[["XEPGChannel"], int]


@dataclass(slots=True)
class BackupStream:
    playlist_id: str = ""
    url: str = ""

    def to_json(self) -> dict[str, str]:
        return {"PlaylistID": self.playlist_id, "URL": self.url}

    @staticmethod
    def from_json(value: Any) -> BackupStream | None:
        if not isinstance(value, dict):
            return None
        playlist_id, url = value.get("PlaylistID"), value.get("URL")
        return BackupStream(playlist_id if isinstance(playlist_id, str) else "", url if isinstance(url, str) else "")


# (attribute, JSON key, kind) in the order Go marshals them.
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("file_m3u_id", "_file.m3u.id", "str"),
    ("file_m3u_name", "_file.m3u.name", "str"),
    ("file_m3u_path", "_file.m3u.path", "str"),
    ("group_title", "group-title", "str"),
    ("name", "name", "str"),
    ("tvg_id", "tvg-id", "str"),
    ("tvg_logo", "tvg-logo", "str"),
    ("tvg_name", "tvg-name", "str"),
    ("tvg_chno", "tvg-chno", "str"),
    ("url", "url", "str"),
    ("uuid_key", "_uuid.key", "str"),
    ("uuid_value", "_uuid.value", "str?"),  # omitempty
    ("values", "_values", "str"),
    ("x_active", "x-active", "bool"),
    ("x_category", "x-category", "str"),
    ("x_channel_id", "x-channelID", "str"),
    ("x_epg", "x-epg", "str"),
    ("x_group_title", "x-group-title", "str"),
    ("x_mapping", "x-mapping", "str"),
    ("x_xmltv_file", "x-xmltv-file", "str"),
    ("x_ppv_extra", "x-ppv-extra", "str"),
    ("x_backup_channel_1", "x-backup-channel-1", "str"),
    ("x_backup_channel_2", "x-backup-channel-2", "str"),
    ("x_backup_channel_3", "x-backup-channel-3", "str"),
    ("x_hide_channel", "x-hide-channel", "bool"),
    ("x_name", "x-name", "str"),
    ("x_update_channel_icon", "x-update-channel-icon", "bool"),
    ("x_update_channel_name", "x-update-channel-name", "bool"),
    ("x_description", "x-description", "str"),
    ("live", "live", "bool"),
    ("is_backup_channel", "is_backup_channel", "bool"),
    ("backup_channel_1", "backup_channel_1", "backup"),
    ("backup_channel_2", "backup_channel_2", "backup"),
    ("backup_channel_3", "backup_channel_3", "backup"),
    ("channel_unique_id", "channelUniqueID", "str"),
)


@dataclass(slots=True)
class XEPGChannel:
    """One channel of ``xepg.json`` (Go ``XEPGChannelStruct``)."""

    file_m3u_id: str = ""
    file_m3u_name: str = ""
    file_m3u_path: str = ""
    group_title: str = ""
    name: str = ""
    tvg_id: str = ""
    tvg_logo: str = ""
    tvg_name: str = ""
    tvg_chno: str = ""
    url: str = ""
    uuid_key: str = ""
    uuid_value: str = ""
    values: str = ""
    x_active: bool = False
    x_category: str = ""
    x_channel_id: str = ""
    x_epg: str = ""
    x_group_title: str = ""
    x_mapping: str = ""
    x_xmltv_file: str = ""
    x_ppv_extra: str = ""
    x_backup_channel_1: str = ""
    x_backup_channel_2: str = ""
    x_backup_channel_3: str = ""
    x_hide_channel: bool = False
    x_name: str = ""
    x_update_channel_icon: bool = False
    x_update_channel_name: bool = False
    x_description: str = ""
    live: bool = False
    is_backup_channel: bool = False
    backup_channel_1: BackupStream | None = None
    backup_channel_2: BackupStream | None = None
    backup_channel_3: BackupStream | None = None
    channel_unique_id: str = ""

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> XEPGChannel:
        """Decode like Go: unknown keys dropped, ``null`` and absent give zero values."""
        channel = XEPGChannel()
        for attr, key, kind in _FIELDS:
            value = data.get(key)
            if kind == "backup":
                setattr(channel, attr, BackupStream.from_json(value))
            elif kind == "bool":
                setattr(channel, attr, value if isinstance(value, bool) else False)
            else:
                setattr(channel, attr, value if isinstance(value, str) else "")
        return channel

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for attr, key, kind in _FIELDS:
            value = getattr(self, attr)
            if kind == "str?":
                if value:
                    out[key] = value
            elif kind == "backup":
                out[key] = value.to_json() if value else None
            else:
                out[key] = value
        return out

    def copy(self) -> XEPGChannel:
        clone = XEPGChannel(**{f.name: getattr(self, f.name) for f in fields(self)})
        return clone


def channel_hash(url: str, tvg_id: str, playlist_id: str) -> str:
    """Stable channel identity: the URL, the ``tvg-id`` when there is one, and the playlist."""
    text = url + tvg_id + playlist_id if tvg_id else url + playlist_id
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def load_xepg(path: str) -> dict[str, XEPGChannel]:
    return {
        key: XEPGChannel.from_json(value) for key, value in load_json_object(path).items() if isinstance(value, dict)
    }


def save_xepg(path: str, channels: Mapping[str, XEPGChannel]) -> None:
    """Write like Go: ids sorted, each channel in struct order."""
    save_json(path, {key: channels[key].to_json() for key in sorted(channels)})


# --- helpers ----------------------------------------------------------------------


def go_g(value: float) -> str:
    """Go ``fmt.Sprintf("%g", value)``: shortest digits, exponent form from 1e6 or below 1e-4."""
    if value == 0:
        return "0"
    if not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("+Inf" if value > 0 else "-Inf")
    _, digits, exponent = Decimal(repr(abs(value))).as_tuple()
    assert isinstance(exponent, int)  # only NaN / infinity have a letter here, handled above
    text = "".join(map(str, digits)).rstrip("0") or "0"
    point = len(digits) + exponent  # value = 0.<digits> * 10**point
    exp10 = point - 1
    prefix = "-" if value < 0 else ""
    if exp10 < -4 or exp10 >= 6:
        mantissa = text[0] + ("." + text[1:] if len(text) > 1 else "")
        return f"{prefix}{mantissa}e{'-' if exp10 < 0 else '+'}{abs(exp10):02d}"
    if point <= 0:
        return f"{prefix}0.{'0' * -point}{text}"
    if len(text) <= point:
        return f"{prefix}{text}{'0' * (point - len(text))}"
    return f"{prefix}{text[:point]}.{text[point:]}"


def parse_float(text: str) -> float | None:
    """``strconv.ParseFloat`` for plain decimal numbers; None if invalid or not finite."""
    try:
        number = float(text.strip()) if text == text.strip() else float("nan")
    except ValueError:
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True, slots=True)
class GroupFilter:
    """The parts of a settings filter that channel creation looks at."""

    group: str
    starting_number: str
    category: str


def group_filters(settings_filter: Mapping[str, Any]) -> list[GroupFilter]:
    """All settings filters in numeric key order (Go: random order); bad types become empty."""

    def order(key: str) -> tuple[int, int | str]:
        try:
            return (0, int(key))
        except ValueError:
            return (1, key)

    def text(raw: Mapping[str, Any], key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    result = []
    for key in sorted(settings_filter, key=order):
        raw = settings_filter[key]
        if isinstance(raw, Mapping):
            result.append(GroupFilter(text(raw, "filter"), text(raw, "startingNumber"), text(raw, "x-category")))
    return result


def _stream_text(stream: Stream, key: str) -> str:
    value = stream.get(key, "")
    return value if isinstance(value, str) else ""


@dataclass(slots=True)
class BuildResult:
    channels: dict[str, XEPGChannel]
    active_hashes: list[str] = field(default_factory=list)
    removed_duplicates: list[str] = field(default_factory=list)
    removed_stale: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    xepg_count: int = 0
    warnings: list[str] = field(default_factory=list)


WARNING_NO_ACTIVE_CHANNELS = "No channels are active. Open the mapping page and activate the channels you want."


# --- duplicates -------------------------------------------------------------------


def _has_mapping(channel: XEPGChannel) -> bool:
    return channel.x_xmltv_file not in ("", "-")


def _loser(current_id: str, current: XEPGChannel, existing_id: str, existing: XEPGChannel) -> str:
    """Which of two channels with the same identity to remove (Go ``handleDuplicate``)."""
    if current.x_active != existing.x_active:
        return existing_id if current.x_active else current_id
    if _has_mapping(current) != _has_mapping(existing):
        return existing_id if _has_mapping(current) else current_id
    return existing_id if current_id > existing_id else current_id


def remove_duplicates(channels: dict[str, XEPGChannel]) -> list[str]:
    """Keep one channel per ``md5(url, tvg-id, playlist)``: active, then mapped, then newest id."""
    winners: dict[str, str] = {}
    removed: list[str] = []
    for channel_id in sorted(channels):
        channel = channels[channel_id]
        key = channel_hash(channel.url, channel.tvg_id, channel.file_m3u_id)
        existing_id = winners.get(key)
        if existing_id is None:
            winners[key] = channel_id
            continue
        loser = _loser(channel_id, channel, existing_id, channels[existing_id])
        removed.append(loser)
        if loser == existing_id:
            winners[key] = channel_id
    for channel_id in removed:
        del channels[channel_id]
    return removed


# --- database update ----------------------------------------------------------------


def _refresh_urls(channels: dict[str, XEPGChannel], streams: Sequence[Stream]) -> None:
    """Follow a changed stream URL when the ``tvg-id`` (or ``tvg-name``) identifies it uniquely."""
    by_key: dict[tuple[str, str], list[Stream]] = {}
    for stream in streams:
        key = _stream_text(stream, "tvg-id") or _stream_text(stream, "tvg-name")
        if key:
            by_key.setdefault((_stream_text(stream, "_file.m3u.id"), key), []).append(stream)
    channels_by_key: dict[tuple[str, str], list[XEPGChannel]] = {}
    for channel in channels.values():
        key = channel.tvg_id or channel.tvg_name
        if key:
            channels_by_key.setdefault((channel.file_m3u_id, key), []).append(channel)
    for pair, candidates in channels_by_key.items():
        matches = by_key.get(pair, [])
        if len(matches) == 1 and len(candidates) == 1:
            candidates[0].url = _stream_text(matches[0], "url")


def update_database(
    channels: dict[str, XEPGChannel],
    streams: Sequence[Stream],
    *,
    mapping_first_channel: float,
    filters: Mapping[str, Any],
    xmltv_mapping: Mapping[str, ChannelMapping],
    program_count: ProgramCounter,
    image_url: ImageURL = identity_image_url,
) -> tuple[list[str], list[str], list[str]]:
    """Match the active streams against the channels, updating and creating channels.

    Modifies ``channels`` in place. Returns ``(active_hashes, removed_duplicates, created_ids)``.
    """
    removed_duplicates = remove_duplicates(channels)
    _refresh_urls(channels, streams)

    # Channels without a number are broken: drop them. Remember the numbers in use.
    used_numbers: set[float] = set()
    for channel_id in list(channels):
        number = channels[channel_id].x_channel_id
        if not number:
            del channels[channel_id]
            continue
        parsed = parse_float(number)
        if parsed is not None:
            used_numbers.add(parsed)

    known: dict[str, XEPGChannel] = {}
    for channel_id in sorted(channels):
        channel = channels[channel_id].copy()
        if not channel.tvg_name:
            channel.tvg_name = channel.name
        known[channel_hash(channel.url, channel.tvg_id, channel.file_m3u_id)] = channel

    # A stream without a hash match takes the first known channel (in the order of ``known``) of its
    # playlist with the same UUID; this index keeps that lookup from scanning every channel.
    uuid_index: dict[tuple[str, str], XEPGChannel] = {}

    def index_uuid(channel: XEPGChannel) -> None:
        if channel.uuid_value:
            uuid_index.setdefault((channel.file_m3u_id, channel.uuid_value), channel)

    for channel in known.values():
        index_uuid(channel)

    group_filter_list = group_filters(filters)
    next_id = 0

    def new_id() -> str:
        nonlocal next_id
        while f"x-ID.{next_id}" in channels:
            next_id += 1
        return f"x-ID.{next_id}"

    next_free: dict[float, float] = {}  # per starting number: every number before this one is taken

    def free_number(start: float) -> str:
        number = next_free.get(start, start)
        while number in used_numbers:
            number += 1
        used_numbers.add(number)
        next_free[start] = number + 1
        return go_g(number)

    active_hashes: list[str] = []
    created: list[str] = []

    for stream in streams:
        url = _stream_text(stream, "url")
        tvg_id = _stream_text(stream, "tvg-id")
        name = _stream_text(stream, "name")
        playlist_id = _stream_text(stream, "_file.m3u.id")
        uuid_value = _stream_text(stream, "_uuid.value")
        live_event = _stream_text(stream, "liveEvent")
        stream_hash = channel_hash(url, tvg_id, playlist_id)
        active_hashes.append(stream_hash)

        match = known.get(stream_hash)
        has_uuid = False
        by_uuid = False
        if match is not None:
            if uuid_value:
                has_uuid = True
        elif uuid_value and (candidate := uuid_index.get((playlist_id, uuid_value))) is not None:
            match, has_uuid, by_uuid = candidate, True, True

        if match is not None:
            found = channels.get(match.x_epg)
            if found is None:
                continue
            channel = found
            if by_uuid:
                # Found by its tvg-name UUID after the stream's URL and tvg-id changed. Go updated only
                # the URL, so the channel's identity hash still differed from the stream's and cleanup
                # deleted it again. Keep the identity in step instead (even for a deactivated channel).
                channel.url = url
                channel.tvg_id = tvg_id
                channel.channel_unique_id = stream_hash
            if not channel.x_active:
                continue  # the user switched this channel off: leave it alone
            if not channel.tvg_name:
                channel.tvg_name = channel.name
            # (the URL needs no refresh here: a hash match means it is identical)
            if live_event == "true":
                channel.live = True
            channel.channel_unique_id = stream_hash

            if has_uuid:
                if (
                    channel.x_update_channel_name
                    or "threadfin-" in channel.tvg_id
                    or (live_event == "true" and program_count(channel) <= 3)
                ):
                    channel.x_name = name
                    channel.tvg_name = _stream_text(stream, "tvg-name")
            elif live_event == "true":
                channel.x_name = name
                channel.tvg_name = _stream_text(stream, "tvg-name")

            if live_event == "true" and channel.live and program_count(channel) <= 3:
                channel.x_xmltv_file = DUMMY_FILE
                channel.x_mapping = PPV

            if channel.x_update_channel_icon:
                channel.tvg_logo = image_url(_stream_text(stream, "tvg-logo"))
            continue

        # a new channel
        group = _stream_text(stream, "group-title")
        first_number = mapping_first_channel
        for group_filter in group_filter_list:
            if group == group_filter.group:
                start = parse_float(group_filter.starting_number)
                if start is not None:
                    first_number = start
        chno = _stream_text(stream, "tvg-chno")
        if chno:
            number_text = chno
            provided = parse_float(chno)
            if provided is not None:
                used_numbers.add(provided)
        else:
            number_text = free_number(first_number)

        channel = XEPGChannel(
            file_m3u_id=playlist_id,
            file_m3u_name=_stream_text(stream, "_file.m3u.name"),
            file_m3u_path=_stream_text(stream, "_file.m3u.path"),
            values=_stream_text(stream, "_values"),
            group_title=group,
            name=name,
            tvg_id=tvg_id,
            tvg_logo=_stream_text(stream, "tvg-logo"),
            tvg_name=_stream_text(stream, "tvg-name"),
            url=url,
            live=live_event.strip().lower() in ("1", "t", "true"),
        )
        if not channel.tvg_name:
            channel.tvg_name = channel.name

        for file, file_channels in xmltv_mapping.items():
            entry = file_channels.get(tvg_id)
            if entry is None:
                continue
            for group_filter in group_filter_list:
                if channel.group_title == group_filter.group:
                    channel.x_category = group_filter.category
            # Go never maps a stream onto the dummy guide this way (its entries have the wrong
            # in-memory type), but the category above is still applied.
            if file != DUMMY_FILE and isinstance(entry.get("id"), str):
                channel.x_xmltv_file = file
                channel.x_mapping = entry["id"]
                channel.x_active = True
                break

        if channel.live and program_count(channel) <= 3:
            channel.x_xmltv_file = DUMMY_FILE
            channel.x_mapping = PPV
            channel.x_active = True

        channel.uuid_key = _stream_text(stream, "_uuid.key")
        channel.uuid_value = _stream_text(stream, "_uuid.value") if channel.uuid_key else ""
        channel.x_name = name
        channel.x_group_title = group
        channel.x_epg = new_id()
        channel.tvg_chno = number_text
        channel.x_channel_id = number_text
        channel.channel_unique_id = stream_hash
        channels[channel.x_epg] = channel
        known[stream_hash] = channel.copy()
        index_uuid(known[stream_hash])
        created.append(channel.x_epg)

    return active_hashes, removed_duplicates, created


# --- mapping --------------------------------------------------------------------------


def map_channels(
    channels: dict[str, XEPGChannel],
    streams: Sequence[Stream],
    *,
    filters: Mapping[str, Any],
    xmltv_mapping: Mapping[str, ChannelMapping],
) -> list[str]:
    """Backup channels, automatic EPG mapping for new channels, and mapping defaults.

    Returns a warning for every backup name of an active channel that matched no stream."""
    warnings: list[str] = []
    group_filter_list = group_filters(filters)
    stream_names = [
        (
            _stream_text(s, "tvg-name") or _stream_text(s, "name"),
            _stream_text(s, "_file.m3u.id"),
            _stream_text(s, "url"),
        )
        for s in streams
    ]

    for channel_id in sorted(channels):
        channel = channels[channel_id].copy()
        if not channel.tvg_name:
            channel.tvg_name = channel.name
        saved = False  # Go only writes some code paths back to the database

        wanted = [
            channel.x_backup_channel_1.strip(" "),
            channel.x_backup_channel_2.strip(" "),
            channel.x_backup_channel_3.strip(" "),
        ]
        backups: list[BackupStream | None] = [None, None, None]
        for index, text in enumerate(wanted):
            if text and text != "-":
                for stream_name, playlist_id, url in stream_names:
                    if stream_name == text:
                        backups[index] = BackupStream(playlist_id, url)  # the last match wins
                if backups[index] is None and channel.x_active:
                    name = channel.x_name or channel.name
                    warnings.append(f'Channel {name}: backup channel {index + 1} "{text}" matches no stream')
        channel.backup_channel_1, channel.backup_channel_2, channel.backup_channel_3 = backups

        if not channel.x_active and len(channel.x_xmltv_file) < 1:
            channel.x_xmltv_file = "-"
            channel.x_mapping = "-"
            saved = True
            for file, file_channels in xmltv_mapping.items():
                entry = file_channels.get(channel.tvg_id)
                if file == DUMMY_FILE or entry is None or not isinstance(entry.get("id"), str):
                    continue
                for group_filter in group_filter_list:
                    if channel.group_title == group_filter.group:
                        channel.x_category = group_filter.category
                channel.x_xmltv_file = file
                channel.x_mapping = entry["id"]
                break

        if channel.x_active and not channel.x_hide_channel:
            saved = True
            category_wanted = True
            if channel.x_xmltv_file != DUMMY_FILE and not channel.live:
                category_wanted = channel.x_mapping in xmltv_mapping.get(channel.x_xmltv_file, {})
            if category_wanted and not channel.x_category:
                for group_filter in group_filter_list:
                    if channel.group_title == group_filter.group:
                        channel.x_category = group_filter.category
            if not channel.x_xmltv_file:
                channel.x_xmltv_file = "-"
            if not channel.x_mapping:
                channel.x_mapping = "-"

        if saved:
            channels[channel_id] = channel
        else:
            # unchanged apart from refreshed backup pointers, which Go also kept in memory only
            pass
    return warnings


# --- cleanup ----------------------------------------------------------------------------


def cleanup(
    channels: dict[str, XEPGChannel], active_hashes: list[str], provider_ids: set[str]
) -> tuple[list[str], int]:
    """Remove channels whose stream or playlist is gone. Returns ``(removed ids, active count)``."""
    live_hashes = set(active_hashes)
    removed: list[str] = []
    count = 0
    for channel_id in sorted(channels):
        channel = channels[channel_id]
        if channel_hash(channel.url, channel.tvg_id, channel.file_m3u_id) not in live_hashes:
            removed.append(channel_id)
            continue
        if channel.file_m3u_id not in provider_ids:
            removed.append(channel_id)
            continue
        if channel.x_active and not channel.x_hide_channel:
            count += 1
    for channel_id in removed:
        del channels[channel_id]
    return removed, count


def build_xepg(
    channels: dict[str, XEPGChannel],
    streams: Sequence[Stream],
    *,
    mapping_first_channel: float,
    filters: Mapping[str, Any],
    xmltv_mapping: Mapping[str, ChannelMapping],
    provider_ids: set[str],
    program_count: ProgramCounter,
    image_url: ImageURL = identity_image_url,
) -> BuildResult:
    """The database half of Go ``buildXEPG``: update, map, clean up."""
    active_hashes, duplicates, created = update_database(
        channels, streams,
        mapping_first_channel=mapping_first_channel, filters=filters,
        xmltv_mapping=xmltv_mapping, program_count=program_count, image_url=image_url,
    )  # fmt: skip
    warnings = map_channels(channels, streams, filters=filters, xmltv_mapping=xmltv_mapping)
    removed, count = cleanup(channels, active_hashes, provider_ids)
    if streams and count == 0:
        warnings.append(WARNING_NO_ACTIVE_CHANNELS)
    return BuildResult(channels, active_hashes, duplicates, removed, created, count, warnings)
