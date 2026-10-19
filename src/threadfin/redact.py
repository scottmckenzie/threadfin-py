"""
Provider URLs often carry credentials in the query string or userinfo.
Error messages show these URLs without credentials for safety.
See spec finding 3 (docs/superpowers/specs/2026-10-19-credentials-and-size-limits-design.md).
"""

from __future__ import annotations

import logging
import re
from typing import override
from urllib.parse import urlsplit, urlunsplit


def display_url(url: str) -> str:
    """
    Redact credentials from a URL by replacing userinfo passwords with '***'
    and truncating query strings with '…'.

    Args:
        url: The URL to redact.

    Returns:
        The redacted URL.
    """
    try:
        parsed = urlsplit(url)
        scheme, netloc, path, _, _ = parsed
        # Handle userinfo password redaction
        if "@" in netloc:
            userinfo, _, host = netloc.rpartition("@")
            if ":" in userinfo:
                user, _, _ = userinfo.partition(":")
                netloc = f"{user}:***@{host}"
        # Handle query redaction
        if parsed.query:
            return urlunsplit((scheme, netloc, path, "…", ""))
        return urlunsplit((scheme, netloc, path, "", ""))
    except ValueError:
        # Fallback for malformed URLs
        if "?" in url:
            return url[: url.index("?")] + "?…"
        return url


def clean_text(text: str) -> str:
    """
    Replace all URLs in the given text with their redacted versions.

    Args:
        text: The input text containing URLs.

    Returns:
        The text with all URLs redacted.
    """
    pattern = r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+"

    def _clean(match: re.Match[str]) -> str:
        run = match.group(0)
        body = run.rstrip(":,;)")
        tail = run[len(body) :]
        return display_url(body) + tail

    return re.sub(pattern, _clean, text)


class RedactAccessLog(logging.Filter):
    @override
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            args = (*args[:2], display_url(args[2]), *args[3:])
            record.args = args
        return True
