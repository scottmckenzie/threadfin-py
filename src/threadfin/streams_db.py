"""The stream database: every stream of every provider, split into active and inactive.

Port of ``buildDatabaseDVR`` (data.go). ``tests/golden/streams`` compares it with the real Go
code. Differences from Go:

* a provider whose local copy cannot be read is reported in :attr:`StreamDatabase.errors` and
  the rest is still built (Go carried the *last* provider's error out of the function, so the
  outcome depended on random map order);
* providers are visited in settings order, not random order;
* group labels and values are one sorted list of ``(name, count)`` (Go sorted the label list and
  the value list separately, which can pair a label with the wrong value).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .filters import build_filter_rules, filter_stream
from .m3u import SkippedEntry, Stream, parse_m3u_report
from .providers import ProviderError, ProviderManager, hdhr_streams

PLEX_CHANNEL_LIMIT = 480
UNFILTERED_CHANNEL_LIMIT = 480

WARNING_PLEX_LIMIT = (
    "Plex can not handle more than {limit} streams. "
    "If you do not use Plex, you can ignore this warning."
)
WARNING_UNFILTERED = (
    "Threadfin has loaded more than {limit} streams. Use the filter to reduce the number of streams."
)


@dataclass(frozen=True, slots=True)
class GroupCount:
    name: str
    count: int

    @property
    def label(self) -> str:
        return f"{self.name} ({self.count})"


@dataclass(slots=True)
class StreamDatabase:
    all: list[Stream] = field(default_factory=list)
    active: list[Stream] = field(default_factory=list)
    inactive: list[Stream] = field(default_factory=list)
    preview_active: list[str] = field(default_factory=list)
    preview_inactive: list[str] = field(default_factory=list)
    groups: list[GroupCount] = field(default_factory=list)
    compatibility: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, list[SkippedEntry]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _percent(count: int, total: int) -> int:
    return count * 100 // total if count else 0


def _load(
    manager: ProviderManager, kind: str, provider_id: str, name: str
) -> tuple[list[Stream], list[SkippedEntry]]:
    path = manager.local_path(kind, provider_id)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ProviderError(f"{name}: Local copy of the file no longer exists ({exc.strerror})") from exc
    if kind == "hdhr":
        return hdhr_streams(content, name, provider_id)
    parsed = parse_m3u_report(content)
    return parsed.streams, parsed.skipped


def build_stream_database(
    manager: ProviderManager,
    settings: Any,
    *,
    unfiltered_limit: int = UNFILTERED_CHANNEL_LIMIT,
    plex_limit: int = PLEX_CHANNEL_LIMIT,
) -> StreamDatabase:
    """Read all local provider copies and apply the filters. Raises ``FilterError`` for a bad filter."""
    rules = build_filter_rules(settings.filter)
    db = StreamDatabase()
    group_counts: dict[str, int] = {}

    for kind in ("m3u", "hdhr"):
        for provider_id in list(manager.records(kind)):
            name = manager.parameter(kind, provider_id, "name")
            path = str(manager.local_path(kind, provider_id))
            try:
                channels, skipped = _load(manager, kind, provider_id, name)
            except (ProviderError, ValueError) as exc:
                db.errors[provider_id] = str(exc)
                channels, skipped = [], []
            if skipped:
                db.skipped[provider_id] = skipped

            with_group = with_id = with_uuid = 0
            for stream in channels:
                stream["_file.m3u.path"] = path
                stream["_file.m3u.name"] = name
                stream["_file.m3u.id"] = provider_id

                if stream.get("_uuid.key"):
                    with_uuid += 1
                group = stream.get("group-title", "")
                if group:
                    group_counts[group] = group_counts.get(group, 0) + 1
                    with_group += 1
                if stream.get("tvg-id"):
                    with_id += 1

                db.all.append(stream)
                if settings.ignore_filters:
                    active = True
                else:
                    active, live_event = filter_stream(stream, rules)
                    stream["liveEvent"] = "true" if live_event else "false"

                preview = f"{stream.get('name', '')} [{group}]"
                if active:
                    db.preview_active.append(preview)
                    db.active.append(stream)
                else:
                    db.preview_inactive.append(preview)
                    db.inactive.append(stream)

            total = len(channels)
            compatibility = {
                "tvg.id": _percent(with_id, total),
                "group.title": _percent(with_group, total),
                "stream.id": _percent(with_uuid, total),
                "streams": total,
            }
            db.compatibility[provider_id] = compatibility
            manager.set_compatibility(kind, provider_id, compatibility)

    db.groups = [GroupCount(name, group_counts[name]) for name in sorted(group_counts)]

    if not db.active and len(db.all) <= unfiltered_limit and not settings.filter:
        # No filter at all and a small playlist: everything is active.
        db.active = list(db.all)
        db.inactive = []
        db.preview_active = db.preview_inactive
        db.preview_inactive = []

    if len(db.active) > plex_limit:
        db.warnings.append(WARNING_PLEX_LIMIT.format(limit=plex_limit))
    if not settings.filter and len(db.all) > unfiltered_limit:
        db.warnings.append(WARNING_UNFILTERED.format(limit=unfiltered_limit))

    db.preview_active.sort()
    db.preview_inactive.sort()
    return db
