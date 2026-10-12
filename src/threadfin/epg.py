"""Building the XMLTV guide (``threadfin.xml``) for the active channels.

Port of ``createXMLTVFile``, ``getProgramData`` and its helpers (xepg.go). ``tests/golden/epg``
compares the output with the real Go code, element by element (the XML is parsed on both sides;
see ``tests/test_epg.py`` for what is normalised).

What a channel's guide is made of:

* a real provider XMLTV file: the programmes of the mapped channel, reduced to the fields the Go
  app keeps (titles, descriptions, credits, ratings, icons ... see :class:`Programme`);
* the built-in dummy guide: 4 days of fixed-length slots (``30_Minutes`` ... ``360_Minutes``);
* the live-event guide (``PPV``): "CHANNEL OFFLINE" before and after the event.

Differences from Go: elements are written in XMLTV DTD order, empty ``<credits>`` / ``<video>`` /
``<date>`` and the app-private ``<live>`` / ``<active>`` channel children are left out, generator and
source are attributes of ``<tv>`` (Go wrote them as child elements), control characters Go would have
emitted are replaced with U+FFFD, and a dummy guide with a zero-minute length uses 30 minutes instead
of looping forever. The "event start time" parsing of live-event channel names never worked in Go
(see docs/PARITY_NOTES.md) and is reproduced as it behaves: 06:00 if the name contains a digit,
midnight otherwise.
"""

from __future__ import annotations

import gzip
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

from .xepg import XEPGChannel
from .xmltv import (
    DUMMY_FILE,
    ImageURL,
    XMLTVError,
    dummy_minutes,
    identity_image_url,
    iter_top_level,
)

PPV = "PPV"
OFFLINE_TEXT = "CHANNEL OFFLINE"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True, slots=True)
class EPGOptions:
    enable_non_ascii: bool = False
    replace_missing_images: bool = True
    replace_channel_title: bool = False
    image_url: ImageURL = identity_image_url
    generator: str = "Threadfin"
    source: str = "Threadfin"


# --- programme model -----------------------------------------------------------------


@dataclass(slots=True)
class Poster:
    src: str = ""
    height: str = ""
    width: str = ""
    text: str = ""


@dataclass(slots=True)
class Rating:
    system: str = ""
    value: str = ""
    icons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Programme:
    """The fields of a ``<programme>`` that Threadfin passes on."""

    channel: str = ""
    start: str = ""
    stop: str = ""
    titles: list[tuple[str, str]] = field(default_factory=list)  # (lang, text)
    sub_titles: list[tuple[str, str]] = field(default_factory=list)
    descs: list[tuple[str, str]] = field(default_factory=list)
    categories: list[tuple[str, str]] = field(default_factory=list)
    countries: list[tuple[str, str]] = field(default_factory=list)
    episode_nums: list[tuple[str, str]] = field(default_factory=list)  # (system, text)
    posters: list[Poster] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    actors: list[tuple[str, str]] = field(default_factory=list)  # (name, role)
    writers: list[str] = field(default_factory=list)
    presenters: list[str] = field(default_factory=list)
    producers: list[str] = field(default_factory=list)
    ratings: list[Rating] = field(default_factory=list)
    star_ratings: list[tuple[str, str]] = field(default_factory=list)  # (system, value)
    languages: list[str] = field(default_factory=list)
    aspect: str = ""
    colour: str = ""
    present: str = ""
    quality: str = ""
    date: str = ""
    previously_shown: str | None = None  # the start attribute, when the element exists
    new: str | None = None
    live: str | None = None
    premiere: str | None = None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(element: ElementTree.Element, name: str) -> str:
    """Attribute by local name, namespace-agnostic like Go's decoder (``xml:lang`` counts as ``lang``)."""
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return ""


def _chardata(element: ElementTree.Element) -> str:
    """Character data directly inside the element (not inside child elements)."""
    return (element.text or "") + "".join(child.tail or "" for child in element)


def parse_programme(element: ElementTree.Element) -> Programme:
    """Read a provider ``<programme>`` the way Go's struct decoding does."""
    p = Programme(channel=_attr(element, "channel"), start=_attr(element, "start"), stop=_attr(element, "stop"))
    for child in element:
        tag = _local(child.tag)
        text = _chardata(child)
        if tag == "title":
            p.titles.append((_attr(child, "lang"), text))
        elif tag == "sub-title":
            p.sub_titles.append((_attr(child, "lang"), text))
        elif tag == "desc":
            p.descs.append((_attr(child, "lang"), text))
        elif tag == "category":
            p.categories.append((_attr(child, "lang"), text))
        elif tag == "country":
            p.countries.append((_attr(child, "lang"), text))
        elif tag == "episode-num":
            p.episode_nums.append((_attr(child, "system"), text))
        elif tag == "icon":
            p.posters.append(Poster(_attr(child, "src"), _attr(child, "height"), _attr(child, "width"), text))
        elif tag == "credits":
            for person in child:
                kind, name = _local(person.tag), _chardata(person)
                if kind == "director":
                    p.directors.append(name)
                elif kind == "actor":
                    p.actors.append((name, _attr(person, "role")))
                elif kind == "writer":
                    p.writers.append(name)
                elif kind == "presenter":
                    p.presenters.append(name)
                elif kind == "producer":
                    p.producers.append(name)
        elif tag == "rating":
            rating = Rating(system=_attr(child, "system"))
            for part in child:
                if _local(part.tag) == "value":
                    rating.value = _chardata(part)
                elif _local(part.tag) == "icon":
                    rating.icons.append(_attr(part, "src"))
            p.ratings.append(rating)
        elif tag == "star-rating":
            value = ""
            for part in child:
                if _local(part.tag) == "value":
                    value = _chardata(part)
            p.star_ratings.append((_attr(child, "system"), value))
        elif tag == "language":
            p.languages.append(text)
        elif tag == "video":
            for part in child:
                name = _local(part.tag)
                if name == "aspect":
                    p.aspect = _chardata(part)
                elif name == "colour":
                    p.colour = _chardata(part)
                elif name == "present":
                    p.present = _chardata(part)
                elif name == "quality":
                    p.quality = _chardata(part)
        elif tag == "date":
            p.date = text  # a later element replaces an earlier one
        elif tag == "previously-shown":
            p.previously_shown = _attr(child, "start")
        elif tag == "new":
            p.new = text
        elif tag == "live":
            p.live = text
        elif tag == "premiere":
            p.premiere = text
    return p


# --- conversion for one channel (Go getProgramData) -----------------------------------


def strip_non_ascii(text: str) -> str:
    """Drop everything above U+007F, then trim whitespace (Go ``strings.Map`` + ``TrimSpace``)."""
    return "".join(ch for ch in text if ord(ch) <= 127).strip()


def _air_date(start: str) -> str | None:
    token = start.split(" ")[0]
    if len(token) != 14 or not (token.isascii() and token.isdigit()):
        return None
    try:
        parsed = datetime.strptime(token, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def convert_programme(source: Programme, channel: XEPGChannel, options: EPGOptions) -> Programme:
    """Apply the channel's settings to a provider programme (categories, posters, episode, quality)."""
    out = Programme(channel=channel.x_channel_id, start=source.start, stop=source.stop)

    if source.titles:
        first_lang, first_text = source.titles[0]
        if not options.enable_non_ascii:
            first_text = strip_non_ascii(first_text)
        out.titles = [(first_lang, first_text), *source.titles[1:]]

    out.categories = list(source.categories)
    if channel.x_category:
        out.categories.append(("en", channel.x_category.lower()))

    out.sub_titles = list(source.sub_titles)
    out.descs = list(source.descs)
    out.directors, out.actors = list(source.directors), list(source.actors)
    out.writers, out.presenters = list(source.writers), list(source.presenters)
    out.producers = list(source.producers)
    out.ratings, out.star_ratings = list(source.ratings), list(source.star_ratings)
    out.countries = list(source.countries)

    for poster in source.posters:
        out.posters.append(replace(poster, src=options.image_url(poster.src)))
    if options.replace_missing_images and not source.posters:
        out.posters.append(Poster(src=options.image_url(channel.tvg_logo)))

    out.languages = list(source.languages)

    out.episode_nums = list(source.episode_nums)
    if channel.x_category and channel.x_category != "Movie" and not source.episode_nums:
        air_date = _air_date(source.start)
        if air_date is not None:
            out.episode_nums.append(("original-air-date", air_date))

    out.aspect, out.colour, out.present, out.quality = source.aspect, source.colour, source.present, source.quality
    if not source.quality:
        upper = channel.x_name.upper()
        if " HD" in upper or " FHD" in upper:
            out.quality = "HDTV"
        if " UHD" in upper or " 4K" in upper:
            out.quality = "UHDTV"

    out.date = source.date
    out.previously_shown, out.new = source.previously_shown, source.new
    out.live, out.premiere = source.live, source.premiere
    return out


# --- built-in guides ---------------------------------------------------------------------


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%d%H%M%S %z")


def live_programmes(channel: XEPGChannel, channel_id: str, options: EPGOptions, now: datetime) -> list[Programme]:
    """The "PPV" guide: offline until the event, the event, offline the next day."""
    name = channel.x_name or channel.tvg_name
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_second = now.replace(hour=23, minute=59, second=59, microsecond=0)

    start = midnight
    # Go looks for an event time in the name, but its parser always fails and falls back to 06:00.
    if any("0" <= ch <= "9" for ch in name):
        start = now.replace(hour=6, minute=0, second=0, microsecond=0)

    offline = OFFLINE_TEXT if options.enable_non_ascii else strip_non_ascii(OFFLINE_TEXT)
    before = Programme(
        channel=channel_id, start=_stamp(midnight), stop=_stamp(start),
        titles=[("en", offline)], descs=[("en", offline)],
    )  # fmt: skip
    main = Programme(channel=channel_id, start=_stamp(start), stop=_stamp(last_second))
    if options.replace_channel_title and channel.x_mapping == PPV:
        title = f"{name} {channel.x_ppv_extra}"
        if not options.enable_non_ascii:
            title = strip_non_ascii(title)
        main.titles = [("en", title)]
        main.descs = [("en", title)]

    next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    after = Programme(
        channel=channel_id, start=_stamp(next_day),
        stop=_stamp(next_day.replace(hour=23, minute=59, second=59)),
        titles=[("en", offline)], descs=[("en", offline)],
    )  # fmt: skip
    return [before, main, after]


def dummy_programmes(channel: XEPGChannel, options: EPGOptions, now: datetime) -> list[Programme]:
    """Four days of equal slots, labelled with today's UTC offset (no DST adjustment, as in Go)."""
    if channel.x_mapping == PPV:
        return live_programmes(channel, PPV, options, now)

    minutes = dummy_minutes(channel.x_mapping)
    offset = " " + now.strftime("%z")
    today = datetime(now.year, now.month, now.day)  # naive: wall-clock arithmetic only
    out: list[Programme] = []

    def ascii_if_needed(text: str) -> str:
        return text if options.enable_non_ascii else strip_non_ascii(text)

    for day in range(4):
        slot_start = today + timedelta(days=day)
        for _ in range(minutes, 1441, minutes):
            slot_stop = slot_start + timedelta(minutes=minutes)
            hhmm_start, hhmm_stop = slot_start.strftime("%H:%M"), slot_stop.strftime("%H:%M")
            weekday = WEEKDAYS[slot_start.weekday()]
            programme = Programme(
                channel=channel.x_mapping,
                start=slot_start.strftime("%Y%m%d%H%M%S") + offset,
                stop=slot_stop.strftime("%Y%m%d%H%M%S") + offset,
            )
            programme.titles = [
                ("en", ascii_if_needed(f"{channel.x_name} ({weekday[:2]}. {hhmm_start} - {hhmm_stop})"))
            ]
            description = channel.x_description or (
                f"Threadfin: ({minutes} Minutes) {weekday} {hhmm_start} - {hhmm_stop}"
            )
            programme.descs = [("en", ascii_if_needed(description))]
            if options.replace_missing_images:
                programme.posters = [Poster(src=options.image_url(channel.tvg_logo))]
            if channel.x_category != "Movie":
                programme.episode_nums = [("original-air-date", slot_start.strftime("%Y-%m-%d %H:%M:%S"))]
            programme.new = ""
            out.append(programme)
            slot_start = slot_stop
    return out


# --- writing XML -----------------------------------------------------------------------------


def _clean(text: str) -> str:
    """Replace characters XML 1.0 cannot carry (Go substitutes U+FFFD too)."""
    if text.isprintable() and text.isascii():
        return text
    return "".join(
        ch if ch in "\t\n\r" or 0x20 <= ord(ch) <= 0xD7FF or (0xE000 <= ord(ch) <= 0xFFFD)
        or ord(ch) >= 0x10000 else "�"
        for ch in text
    )


def _text(value: str) -> str:
    return escape(_clean(value))


def _attrs(**pairs: str) -> str:
    return "".join(f" {name.replace('_', '-')}={quoteattr(_clean(value))}" for name, value in pairs.items() if value)


def render_programme(p: Programme) -> str:
    """One ``<programme>`` element, children in XMLTV DTD order."""
    parts = [f"  <programme{_attrs(start=p.start, stop=p.stop)} channel={quoteattr(_clean(p.channel))}>\n"]

    def add(tag: str, value: str, **attrs: str) -> None:
        parts.append(f"    <{tag}{_attrs(**attrs)}>{_text(value)}</{tag}>\n")

    for lang, text in p.titles:
        add("title", text, lang=lang)
    for lang, text in p.sub_titles:
        add("sub-title", text, lang=lang)
    for lang, text in p.descs:
        add("desc", text, lang=lang)
    if p.directors or p.actors or p.writers or p.presenters or p.producers:
        parts.append("    <credits>\n")
        for name in p.directors:
            parts.append(f"      <director>{_text(name)}</director>\n")
        for name, role in p.actors:
            parts.append(f"      <actor{_attrs(role=role)}>{_text(name)}</actor>\n")
        for name in p.writers:
            parts.append(f"      <writer>{_text(name)}</writer>\n")
        for name in p.presenters:
            parts.append(f"      <presenter>{_text(name)}</presenter>\n")
        for name in p.producers:
            parts.append(f"      <producer>{_text(name)}</producer>\n")
        parts.append("    </credits>\n")
    if p.date:
        add("date", p.date)
    for lang, text in p.categories:
        add("category", text, lang=lang)
    for text in p.languages:
        add("language", text)
    for poster in p.posters:
        parts.append(
            f"    <icon{_attrs(src=poster.src, width=poster.width, height=poster.height)}>{_text(poster.text)}</icon>\n"
        )
    for lang, text in p.countries:
        add("country", text, lang=lang)
    for system, text in p.episode_nums:
        add("episode-num", text, system=system)
    if p.aspect or p.colour or p.present or p.quality:
        parts.append("    <video>\n")
        for tag, value in (("present", p.present), ("colour", p.colour), ("aspect", p.aspect), ("quality", p.quality)):
            if value:
                parts.append(f"      <{tag}>{_text(value)}</{tag}>\n")
        parts.append("    </video>\n")
    if p.previously_shown is not None:
        parts.append(f"    <previously-shown{_attrs(start=p.previously_shown)}/>\n")
    if p.premiere is not None:
        parts.append(f"    <premiere>{_text(p.premiere)}</premiere>\n")
    if p.new is not None:
        parts.append(f"    <new>{_text(p.new)}</new>\n")
    if p.live is not None:
        parts.append(f"    <live>{_text(p.live)}</live>\n")
    for rating in p.ratings:
        parts.append(f"    <rating{_attrs(system=rating.system)}>\n")
        if rating.value:
            parts.append(f"      <value>{_text(rating.value)}</value>\n")
        for icon in rating.icons:
            parts.append(f"      <icon{_attrs(src=icon)}/>\n")
        parts.append("    </rating>\n")
    for system, value in p.star_ratings:
        parts.append(f"    <star-rating{_attrs(system=system)}>\n      <value>{_text(value)}</value>\n    </star-rating>\n")
    parts.append("  </programme>\n")
    return "".join(parts)


def sort_key(channel: XEPGChannel) -> tuple[int, float, str, str]:
    """Numeric channel numbers first (by value), then the rest alphabetically."""
    try:
        return (0, float(channel.tvg_chno), "", channel.x_epg)
    except ValueError:
        return (1, 0.0, channel.tvg_chno, channel.x_epg)


@dataclass(slots=True)
class XMLTVStats:
    channels: int = 0
    programmes: int = 0
    errors: dict[str, str] = field(default_factory=dict)  # xmltv file -> message


def write_xmltv(
    path: str | os.PathLike[str],
    channels: Mapping[str, XEPGChannel],
    data_folder: str | os.PathLike[str],
    options: EPGOptions,
    now: datetime,
    *,
    gzip_path: str | os.PathLike[str] | None = None,
) -> XMLTVStats:
    """Write the guide for all active, visible channels. ``now`` must carry the local time zone."""
    stats = XMLTVStats()
    entries: list[XEPGChannel] = []
    for channel in channels.values():
        item = channel.copy()
        if not item.tvg_name:
            item.tvg_name = item.name
        if not item.x_name:
            item.x_name = item.tvg_name
        if item.x_active and not item.x_hide_channel:
            entries.append(item)
    entries.sort(key=sort_key)

    folder = Path(data_folder)
    programmes: dict[str, list[str]] = {entry.x_epg: [] for entry in entries}

    # real guides: one pass per provider file, serving every channel that maps into it
    wanted: dict[str, dict[str, list[XEPGChannel]]] = {}
    for entry in entries:
        if DUMMY_FILE in entry.x_xmltv_file:
            for programme in dummy_programmes(entry, options, now):
                programmes[entry.x_epg].append(render_programme(convert_programme(programme, entry, options)))
        elif entry.x_xmltv_file:
            wanted.setdefault(entry.x_xmltv_file, {}).setdefault(entry.x_mapping, []).append(entry)
    for file, by_channel in wanted.items():
        try:
            for element in iter_top_level(folder / file):
                if element.tag.rsplit("}", 1)[-1] != "programme":
                    continue
                targets = by_channel.get(element.attrib.get("channel", ""))
                if targets:
                    source = parse_programme(element)
                    for target in targets:
                        programmes[target.x_epg].append(
                            render_programme(convert_programme(source, target, options))
                        )
        except XMLTVError as exc:
            stats.errors[file] = str(exc)

    target = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            out.write(f"<tv{_attrs(generator_info_name=options.generator, source_info_name=options.source)}>\n")
            for entry in entries:
                if (options.replace_channel_title and entry.x_mapping == PPV) or entry.x_name:
                    icon = options.image_url(entry.tvg_logo)
                    out.write(f"  <channel id={quoteattr(_clean(entry.x_channel_id))}>\n")
                    out.write(f"    <display-name>{_text(entry.x_name)}</display-name>\n")
                    out.write(f"    <icon{_attrs(src=icon)}/>\n" if icon else "    <icon src=\"\"/>\n")
                    out.write("  </channel>\n")
                    stats.channels += 1
            for entry in entries:
                chunk = programmes[entry.x_epg]
                out.writelines(chunk)
                stats.programmes += len(chunk)
            out.write("</tv>\n")
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise

    if gzip_path is not None:
        with open(target, "rb") as source_file, gzip.open(gzip_path, "wb") as gz_file:
            while block := source_file.read(1 << 20):
                gz_file.write(block)
    return stats
