"""Stream filters: decide which provider streams are active.

Port of ``createFilterRules`` (data.go), ``filterThisStream`` and
``checkConditions`` (m3u.go). ``tests/golden/filters`` proves parity against the
real Go code. Intentional differences:

* filters are evaluated in numeric key order (Go ranges over a map, so the order
  was random, and because the first matching filter decides, so was the result);
* a ``custom-filter`` is never a live-event filter (only ``group-title`` filters carry
  that flag); Go left it to inherit the flag of whichever filter was processed before it.

Quirks kept for parity: rule text containing a ``.`` inside ``{...}`` is not
recognised as include/exclude conditions, and ``!!{x}`` / ``{{x}}`` mangle the
condition text exactly like Go.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# Go regexes, verbatim. ``[^.]`` also matches newlines in both engines.
_EXCLUDE_RE = re.compile(r"!+[{]+[^.]+[}]")
_INCLUDE_RE = re.compile(r"[{]+[^.]+[}]")

CUSTOM_FILTER = "custom-filter"
GROUP_TITLE = "group-title"


class FilterError(ValueError):
    """A filter definition in the settings could not be decoded."""


@dataclass(frozen=True, slots=True)
class FilterRule:
    """One compiled rule (Go ``Filter``)."""

    rule: str
    type: str
    case_sensitive: bool = False
    live_event: bool = False


_FIELDS: dict[str, type] = {
    "active": bool,
    "liveEvent": bool,
    "caseSensitive": bool,
    "description": str,
    "exclude": str,
    "filter": str,
    "include": str,
    "name": str,
    "rule": str,
    "type": str,
    "startingNumber": str,
    "x-category": str,
}


def _decode_filter(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Decode a filter dict like Go's ``json.Unmarshal`` into ``FilterStruct``.

    Unknown keys are ignored, key matching is case-insensitive, ``null`` is the zero
    value and a value of the wrong JSON type is an error.
    """
    folded: dict[str, Any] = {}
    for key, value in raw.items():
        folded.setdefault(key.lower(), value)
    for key, value in raw.items():  # an exact-case key beats a differently-cased one
        if key in _FIELDS:
            folded[key.lower()] = value
    out: dict[str, Any] = {}
    for name, kind in _FIELDS.items():
        value = folded.get(name.lower())
        if value is None:
            out[name] = kind()
        elif isinstance(value, kind) and (kind is not bool or isinstance(value, bool)):
            out[name] = value
        else:
            raise FilterError(f"cannot decode filter field {name!r}={value!r} as {kind.__name__}")
    return out


def _sort_key(key: str) -> tuple[int, int | str]:
    try:
        return (0, int(key))
    except ValueError:
        return (1, key)


def build_filter_rules(filters: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> list[FilterRule]:
    """Compile ``Settings.filter`` into rules, in numeric key order."""
    if isinstance(filters, Mapping):
        items = [filters[k] for k in sorted(filters, key=_sort_key)]
    else:
        items = list(filters)

    rules: list[FilterRule] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise FilterError("filter definition is not an object")
        f = _decode_filter(item)
        if f["type"] == CUSTOM_FILTER:
            rules.append(FilterRule(f["filter"], CUSTOM_FILTER, f["caseSensitive"]))
        elif f["type"] == GROUP_TITLE:
            include = f" {{{f['include']}}}" if f["include"] else ""
            exclude = f" !{{{f['exclude']}}}" if f["exclude"] else ""
            rules.append(
                FilterRule(
                    f"{f['filter']}{include}{exclude}", GROUP_TITLE, f["caseSensitive"], f["liveEvent"]
                )
            )
    return rules


def go_lower(text: str) -> str:
    """``strings.ToLower``: per-character simple lowercasing.

    Python's ``str.lower`` differs: it turns U+0130 into two characters and picks the
    word-final sigma depending on context; Go does neither.
    """
    if text.isascii():
        return text.lower()
    dotted_capital_i = "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}"
    return "".join("i" if ch == dotted_capital_i else ch.lower() for ch in text)


def check_conditions(stream_values: str, conditions: str, kind: str) -> bool:
    """Comma separated keywords: ``exclude`` fails on any hit, ``include`` needs one.

    An empty keyword (trailing comma, empty list) is contained in everything, as in Go.
    """
    conditions = conditions.replace(", ", ",").replace(" ,", ",")
    for key in conditions.split(","):
        if key in stream_values:
            return kind != "exclude"
    return kind == "exclude"


def filter_stream(stream: Mapping[str, str], rules: Iterable[FilterRule]) -> tuple[bool, bool]:
    """Return ``(active, live_event)`` for one stream.

    The first rule that matches decides. ``live_event`` is the flag of that rule, or of
    the last rule looked at when nothing matched (Go behaviour).
    """
    live_event = False
    for rule in rules:
        if rule.rule == "":
            continue
        live_event = rule.live_event

        text = rule.rule
        exclude = include = ""
        values = stream.get("_values", "").replace("\r", "")
        group = stream.get("group-title", "")
        name = stream.get("name", "")

        found = _EXCLUDE_RE.search(text)
        if found:
            token = found.group(0)
            exclude = token[2:-1]
            text = text.replace(" " + token, "").replace(token, "")
        found = _INCLUDE_RE.search(text)
        if found:
            token = found.group(0)
            include = token[1:-1]
            text = text.replace(" " + token, "").replace(token, "")

        if not rule.case_sensitive:
            values, text = go_lower(values), go_lower(text)
            exclude, include = go_lower(exclude), go_lower(include)
            group, name = go_lower(group), go_lower(name)

        if rule.type == GROUP_TITLE:
            search, match = name, group == text
        elif rule.type == CUSTOM_FILTER:
            search, match = values, text in values
        else:
            continue

        if match:
            if exclude and not check_conditions(search, exclude, "exclude"):
                return False, live_event
            if include and not check_conditions(search, include, "include"):
                return False, live_event
            return True, live_event
    return False, live_event
