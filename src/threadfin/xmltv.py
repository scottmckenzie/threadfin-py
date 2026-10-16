"""XMLTV helpers: scanning provider files for their channels and counting programmes.

Port of the channel-mapping half of ``createXEPGMapping`` (xepg.go). Programme generation
and the XMLTV writer live in :mod:`threadfin.epg` (added with the output stage).

Provider XMLTV files can be hundreds of megabytes, so everything here streams the file with
``iterparse`` and releases elements as it goes.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

DUMMY_FILE = "Threadfin Dummy"
DUMMY_MINUTES = ("30", "60", "90", "120", "180", "240", "360")

ChannelMapping = dict[str, dict[str, Any]]  # channel id -> {id, display-name, icon, active}
ImageURL = Callable[[str], str]


def identity_image_url(src: str) -> str:
    """The Go image cache with caching switched off: only trims CR/LF."""
    return src.strip("\r\n")


class XMLTVError(ValueError):
    """An XMLTV file could not be read."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]  # Go's decoder ignores namespaces when matching names


def iter_top_level(path: str | os.PathLike[str]) -> Iterator[ElementTree.Element]:
    """Yield the direct children of the root ``<tv>`` element, one at a time.

    The caller must be done with an element before asking for the next one: it is cleared and taken
    out of the root, so memory stays flat however large the file is.
    """
    try:
        depth = 0
        root = None
        for event, element in ElementTree.iterparse(str(path), events=("start", "end")):
            if event == "start":
                depth += 1
                if depth == 1:
                    if _local(element.tag) != "tv":
                        raise XMLTVError(f"expected element <tv> but have <{_local(element.tag)}>")
                    root = element
            else:
                depth -= 1
                if depth == 1:
                    yield element
                    element.clear()
                    if root is not None:
                        root.remove(element)  # its first child: only elements parsed ahead remain
    except ElementTree.ParseError as exc:
        raise XMLTVError(f"invalid XMLTV file: {exc}") from exc
    except OSError as exc:
        raise XMLTVError("Local copy of the file no longer exists") from exc


def friendly_display_name(names: list[str]) -> str:
    """One name as is, several as ``first (second)`` (Go ``friendlyDisplayName``)."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return f"{names[0]} ({names[1]})"


def _text(element: ElementTree.Element) -> str:
    return element.text or ""


def scan_channels(path: str | os.PathLike[str], image_url: ImageURL = identity_image_url) -> ChannelMapping:
    """The channels of one XMLTV file, as the mapping menu needs them."""
    channels: ChannelMapping = {}
    for element in iter_top_level(path):
        if _local(element.tag) != "channel":
            continue
        names = [_text(child) for child in element if _local(child.tag) == "display-name"]
        icon = ""
        active = False
        for child in element:
            tag = _local(child.tag)
            if tag == "icon" and "src" in child.attrib:
                icon = child.attrib["src"]  # like Go, the last icon that has a src wins
            elif tag == "active":
                active = _text(child).strip().lower() in ("1", "t", "true")
        channel_id = element.attrib.get("id", "")
        channels[channel_id] = {
            "id": channel_id,
            "display-name": friendly_display_name(names),
            "icon": image_url(icon),
            "active": active,
        }
    return channels


def dummy_channels() -> ChannelMapping:
    """The built-in "Threadfin Dummy" guide choices (30 minutes ... PPV)."""
    channels: ChannelMapping = {}
    for minutes in DUMMY_MINUTES:
        channels[f"{minutes}_Minutes"] = {
            "display-name": f"{minutes} Minutes", "id": f"{minutes}_Minutes", "icon": "",
        }  # fmt: skip
    channels["PPV"] = {"display-name": "PPV Event", "id": "PPV", "icon": ""}
    return channels


def build_mapping(
    files: list[tuple[str, Path]], image_url: ImageURL = identity_image_url
) -> tuple[dict[str, ChannelMapping], dict[str, str]]:
    """``{file name: channels}`` for every readable file, plus the dummy guide; and errors by file."""
    mapping: dict[str, ChannelMapping] = {}
    errors: dict[str, str] = {}
    for name, path in files:
        try:
            mapping[name] = scan_channels(path, image_url)
        except XMLTVError as exc:
            errors[name] = str(exc)
    mapping[DUMMY_FILE] = dummy_channels()
    return mapping, errors


def programme_counts(path: str | os.PathLike[str]) -> Counter[str]:
    """How many ``<programme>`` entries each channel id has, in one pass over the file."""
    return Counter(
        element.attrib.get("channel", "") for element in iter_top_level(path) if _local(element.tag) == "programme"
    )


def dummy_minutes(mapping: str) -> int:
    """Length of one dummy-guide slot for a mapping such as ``60_Minutes`` (default 30)."""
    head = mapping.split("_", 1)[0]
    if head.isascii() and head.isdigit() and int(head) > 0:
        return int(head)
    return 30


def dummy_programme_count(mapping: str) -> int:
    """Programmes the dummy guide produces: 3 for PPV, otherwise 4 days of slots."""
    if mapping == "PPV":
        return 3
    return 4 * (1440 // dummy_minutes(mapping))


def make_program_counter(data_folder: str | os.PathLike[str]) -> Callable[[Any], int]:
    """How many programmes ``getProgramData`` would produce for a channel (0 if unreadable).

    Each file is read once, on first use, and its counts are kept: make one counter per build,
    so files changed by a later download are read again.
    """
    folder = Path(data_folder)
    per_file: dict[str, Counter[str]] = {}

    def count(channel: Any) -> int:
        if DUMMY_FILE in channel.x_xmltv_file:
            return dummy_programme_count(channel.x_mapping)
        if not channel.x_xmltv_file:
            return 0
        counts = per_file.get(channel.x_xmltv_file)
        if counts is None:
            try:
                counts = programme_counts(folder / channel.x_xmltv_file)
            except XMLTVError:
                counts = Counter()  # unreadable or invalid: every channel counts 0
            per_file[channel.x_xmltv_file] = counts
        return counts[channel.x_mapping]

    return count
