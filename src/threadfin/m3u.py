"""Extended-M3U playlist parser.

Port of ``src/internal/m3u-parser/xteve_m3uParser.go`` (the "original"
implementation that the Go app uses for playlists up to 10 MB).

Behaviour is kept identical to the Go code, including its quirks, so the
golden-file tests in ``tests/golden/m3u`` can prove parity. The quirks are
catalogued in ``docs/PARITY_NOTES.md``. Intentional differences:

* apostrophes are plain text (Go rewrote every ``'`` to ``"``, corrupting names, groups
  and URLs), see ``tests/test_m3u_apostrophes.py``;
* only the part of the ``#EXTINF`` line before the name is parsed for ``key="value"``
  attributes (Go also parsed the name, so ``The "Best" Channel`` lost text),
  see ``tests/test_m3u_quoting.py``;
* entries without a name are skipped and reported instead of being emitted as blank
  channels, see ``tests/test_m3u_skipped.py``.

The Go app's second "optimized" parser (used above 10 MB) returns no channels
for any input, so it is not ported; this parser is used for every size.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

Stream = dict[str, str]

# An attribute is ``key="value"`` or ``key='value'``. A single-quoted value must start right
# after "=", so apostrophes inside names, URLs or double-quoted values are never mistaken for
# attribute quotes.
_PARAM_RE = re.compile(r"""[-a-zA-Z&=]*(?:".*?"|(?<==)'[^']*')""")
_EXTINF = "#EXTINF"
_EXTINF_RE = re.compile(_EXTINF)

_INVALID_MSG = "Invalid M3U file, an extended M3U file is required."

NO_NAME = "no name"
NO_URL = "no url"


class InvalidM3UError(ValueError):
    """The input is not an extended M3U playlist."""


@dataclass(frozen=True, slots=True)
class SkippedEntry:
    """An ``#EXTINF`` entry that did not become a stream, and why."""

    line: int  # 1-based line of the #EXTINF in the file
    reason: str  # NO_NAME or NO_URL
    url: str | None  # the URL line, when the entry had one
    extinf: str  # the original #EXTINF line (trimmed, at most 200 chars)


@dataclass(frozen=True, slots=True)
class ParseResult:
    streams: list[Stream]
    skipped: list[SkippedEntry]

    @property
    def entries(self) -> int:
        """Number of ``#EXTINF`` entries seen (streams + skipped)."""
        return len(self.streams) + len(self.skipped)


@dataclass(frozen=True, slots=True)
class _Skip:
    reason: str
    url: str | None = None


def parse_m3u(content: bytes | str) -> list[Stream]:
    """Parse an extended M3U playlist into a list of stream dicts.

    Entries that cannot become a stream (no name, no URL) are left out; use
    :func:`parse_m3u_report` to learn which and why.
    """
    return parse_m3u_report(content).streams


def parse_m3u_report(content: bytes | str) -> ParseResult:
    """Like :func:`parse_m3u`, but also reports every entry that was skipped.

    Raises :class:`InvalidM3UError` for HLS media playlists and for input
    without an ``#EXTM3U`` header.
    """
    original = content if isinstance(content, str) else content.decode("utf-8", errors="replace")

    if "#EXT-X-TARGETDURATION" in original or "#EXT-X-MEDIA-SEQUENCE" in original:
        raise InvalidM3UError(_INVALID_MSG)
    if "#EXTM3U" not in original:
        raise InvalidM3UError(_INVALID_MSG)

    # Go also deletes ":-1" everywhere (it turns "#EXTINF:-1" into "#EXTINF"); kept for parity.
    # Newlines are untouched, so line numbers stay valid.
    text = original.replace(":-1", "")

    streams: list[Stream] = []
    skipped: list[SkippedEntry] = []
    original_lines: list[str] | None = None

    starts = [m.start() for m in _EXTINF_RE.finditer(text)]
    line, previous = 1, 0
    for i, start in enumerate(starts):
        line += text.count("\n", previous, start)
        previous = start
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        outcome = _parse_channel(text[start + len(_EXTINF) : end])
        if isinstance(outcome, _Skip):
            if original_lines is None:
                original_lines = original.split("\n")
            skipped.append(
                SkippedEntry(line, outcome.reason, outcome.url, original_lines[line - 1].strip()[:200])
            )
        else:
            streams.append(outcome)
    return ParseResult(streams, skipped)


def _strip_delimiters(attribute: str) -> str:
    """Drop the two quote characters that delimit an attribute value, keep everything else."""
    opening = min(i for i, ch in enumerate(attribute) if ch in "\"'")
    return attribute[:opening] + attribute[opening + 1 : -1]


def _name_comma(extinf: str, attributes: list[re.Match[str]]) -> int:
    """Index of the comma that starts the channel name, or -1.

    It is the first comma that is not inside a quoted attribute value, i.e. the first
    comma in the gaps between attribute matches. Commas inside ``group-title="A, B"`` are
    part of a match and therefore skipped.
    """
    position = 0
    for match in attributes:
        comma = extinf.find(",", position, match.start())
        if comma != -1:
            return comma
        position = match.end()
    return extinf.find(",", position)


def _parse_channel(chunk: str) -> Stream | _Skip:
    lines = [
        line
        for line in chunk.replace("\r\n", "\n").split("\n")
        if line and not line.startswith("#")
    ]
    if len(lines) < 2:
        return _Skip(NO_URL)

    stream: Stream = {}
    # The first remaining line is the rest of the #EXTINF line, the second is the URL.
    url = lines[1].strip("\r\n")
    stream["url"] = url
    extinf = lines[0]

    # Attributes are only looked for before the name; the name is everything after the
    # first comma outside a quoted value, taken verbatim (it may contain quotes).
    found = list(_PARAM_RE.finditer(extinf))
    comma = _name_comma(extinf, found)
    name = ""
    if comma != -1:
        found = [m for m in found if m.end() <= comma]
        name = extinf[comma + 1 :].rstrip("\r\n").rstrip(" ")

    values: list[str] = []
    for match in found:
        key, sep, val = _strip_delimiters(match.group(0)).partition("=")
        if not sep:
            continue
        # Case-sensitive "tvg" test, as in Go: "TVG-ID" is kept as-is.
        stream[key.lower() if "tvg" in key else key] = val
        if "://" not in val and val:
            values.append(val + " ")

    if not name:
        name = stream.get("tvg-name", "").rstrip(" ")
    if not name:
        return _Skip(NO_NAME, url)

    if stream.get("tvg-id", "") in ("", "(no tvg-id)"):
        digest = hashlib.md5(url.encode("utf-8")).hexdigest()  # noqa: S324
        stream["tvg-id"] = f"threadfin-{digest}"

    stream["name"] = name
    stream["_values"] = "".join(values) + name

    for key, val in stream.items():
        if "tvg-name" in key.lower():
            stream["_uuid.key"] = key
            stream["_uuid.value"] = val
            break
    return stream


def _attribute(key: str, value: str) -> str:
    """``key="value"``, switching quote style when the value itself contains ``"``."""
    if '"' not in value:
        return f'{key}="{value}"'
    if "'" not in value:
        return f"{key}='{value}'"
    return f'{key}="{value.replace(chr(34), chr(39))}"'  # both kinds: lossy, but still parseable


def serialize_m3u(streams: list[Stream]) -> str:
    """Write the local playlist copy the Go app keeps for each provider.

    Only ``tvg-id``, ``tvg-name``, ``tvg-chno``, ``tvg-logo``, ``group-title`` and the name
    survive (as in Go), so custom filters see the same text as before. Unlike Go, values
    containing ``"`` are written with single quotes instead of producing a broken line.
    """
    out = ["#EXTM3U\n"]
    for s in streams:
        attributes = " ".join(
            _attribute(key, s.get(key, ""))
            for key in ("tvg-id", "tvg-name", "tvg-chno", "tvg-logo", "group-title")
        )
        out.append(f"#EXTINF:-1 {attributes},{s.get('name', '')}\n{s.get('url', '')}\n")
    return "".join(out)
