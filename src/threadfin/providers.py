"""Providers: fetching playlists and XMLTV files and keeping local copies.

Port of ``getProviderData`` and ``downloadFileFromServer`` (provider.go), ``extractGZIP``
(compression.go), ``checkXMLCompatibility`` (xepg.go) and the provider helpers in data.go.
HDHomeRun tuners as a source (Go ``files.hdhr``) are not supported; their records are kept in
settings.json but ignored.

Provider records stay plain dicts inside ``settings.files.<kind>`` with the Go key names
(``file.source``, ``http_proxy.ip``, ``counter.error`` ...), so the config folder remains
interchangeable with the Go app.

Differences from Go (all fixes, see docs/PARITY_NOTES.md):

* a ``Content-Disposition`` header without ``=`` no longer crashes the process, and
  parameters after the file name no longer end up in the name;
* every provider's outcome is returned (Go only returned the last provider's error);
* downloads have a hard total deadline and size limits;
* values containing ``"`` survive the playlist copy (see :func:`threadfin.m3u.serialize_m3u`).
"""

from __future__ import annotations

import asyncio
import http
import http.client
import ipaddress
import os
import secrets
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .jsonio import write_bytes_atomic
from .m3u import InvalidM3UError, SkippedEntry, parse_m3u_report, serialize_m3u

KINDS = ("m3u", "xmltv")
EXTENSIONS = {"m3u": ".m3u", "xmltv": ".xml"}
ID_PREFIX = {"m3u": "M", "xmltv": "X"}

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_BYTES = 1 << 30  # 1 GiB, both on the wire and after gunzip

_ID_ALPHABET = "AB1CD2EF3GH4IJ5KL6MN7OP8QR9ST0UVWXYZ"


class ProviderError(Exception):
    """A provider could not be fetched, read or validated."""


# --- small helpers -------------------------------------------------------------


def new_provider_id(kind: str) -> str:
    """``M`` / ``H`` / ``X`` plus 19 random characters, like the Go app."""
    return ID_PREFIX[kind] + "".join(secrets.choice(_ID_ALPHABET) for _ in range(19))


def go_base(path: str) -> str:
    """``filepath.Base`` for slash-separated paths."""
    if path == "":
        return "."
    path = path.rstrip("/")
    if path == "":
        return "/"
    return path.rsplit("/", 1)[-1]


def filename_from_url(url: str) -> str:
    """Go: the last path element, cut at the first ``?``."""
    return go_base(url).split("?", 1)[0]


def filename_from_content_disposition(header: str) -> str | None:
    """The ``filename`` parameter of a ``Content-Disposition`` header, if usable."""
    if "filename" not in header:
        return None
    message = Message()
    message["Content-Disposition"] = header
    try:
        name = message.get_filename()
    except Exception:  # malformed RFC 2231 values
        return None
    if not name:
        return None
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or None


def extract_gzip(body: bytes, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    """Gunzip ``body``; anything that is not (valid) gzip is returned unchanged, as in Go.

    Concatenated gzip members are joined (Go reads them too). A truncated or corrupt stream
    yields the raw bytes, which then fail validation with a clear error. Output beyond
    ``max_bytes`` raises, so a small download cannot expand without bound.
    """
    if body[:2] != b"\x1f\x8b":
        return body
    out = bytearray()
    data = body
    try:
        while data:
            member = zlib.decompressobj(wbits=31)
            while not member.eof:
                room = max_bytes + 1 - len(out)
                chunk = member.decompress(data if data else member.unconsumed_tail, room)
                out += chunk
                if len(out) > max_bytes:
                    raise ProviderError(f"decompressed file exceeds {max_bytes} bytes")
                data = b""
                if not chunk and not member.eof and not member.unconsumed_tail:
                    return body  # input ended inside a member
            data = member.unused_data
    except zlib.error:
        return body
    return bytes(out)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class Download:
    filename: str
    body: bytes


def download(
    url: str,
    *,
    proxy: str = "",
    user_agent: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Download:
    """Blocking HTTP GET with a total deadline (run it in a thread)."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ProviderError(f"parse {url!r}: invalid URI for request")

    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    elif _is_loopback(parts.hostname):
        handler = urllib.request.ProxyHandler({})  # Go also never proxies loopback
    else:
        handler = urllib.request.ProxyHandler()  # environment, like Go's default transport
    opener = urllib.request.build_opener(handler)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent} if user_agent else {})

    deadline = time.monotonic() + timeout
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise ProviderError(_status_error(response.status, url))
            disposition = response.headers.get("Content-Disposition", "")
            filename = filename_from_content_disposition(disposition) or filename_from_url(url)
            chunks: list[bytes] = []
            size = 0
            while True:
                if time.monotonic() > deadline:
                    raise ProviderError(f"download of {url} timed out after {timeout:g}s")
                chunk = response.read1(1 << 16)  # returns what has arrived; read() would block for all 64 KiB
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ProviderError(f"download of {url} exceeds {max_bytes} bytes")
                chunks.append(chunk)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ProviderError(_status_error(exc.code, url)) from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise ProviderError(f"{url}: {exc}") from exc
    return Download(filename, b"".join(chunks))


def _status_error(status: int, url: str) -> str:
    try:
        phrase = http.HTTPStatus(status).phrase
    except ValueError:
        phrase = ""
    return f"{status}: {url} {phrase}"


# --- content handling -----------------------------------------------------------


def check_xmltv(body: bytes) -> dict[str, int]:
    """Validate an XMLTV file and count its channels and programmes (Go ``checkXMLCompatibility``)."""
    import io

    channels = programmes = 0
    depth = 0
    try:
        for event, element in ElementTree.iterparse(io.BytesIO(body), events=("start", "end")):
            if event == "start":
                depth += 1
                if depth == 1 and element.tag != "tv":
                    raise ProviderError(f"expected element <tv> but have <{element.tag}>")
            else:
                depth -= 1
                if depth == 1:
                    if element.tag == "channel":
                        channels += 1
                    elif element.tag == "programme":
                        programmes += 1
                    element.clear()
    except ElementTree.ParseError as exc:
        raise ProviderError(f"invalid XMLTV file: {exc}") from exc
    return {"xmltv.channels": channels, "xmltv.programs": programmes}


# --- provider records -------------------------------------------------------------


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def availability(errors: float, downloads: float) -> int:
    """Percentage of successful downloads, truncated like Go's ``int(...)`` of a float."""
    if errors == 0 or downloads <= 0:
        return 100
    return int(errors * 100 / downloads * -1 + 100)


def apply_defaults(
    data: dict[str, Any], *, kind: str, provider_id: str, source: str, server_name: str, app: str
) -> None:
    """Fill in the keys the Go app adds to a provider the first time it is saved."""
    defaults: dict[str, Any] = {
        "name": server_name,
        "description": "",
        "type": kind,
        f"file.{app}": provider_id + EXTENSIONS[kind],
        "file.source": source,
        "http_proxy.ip": "",
        "http_proxy.port": "",
        "last.update": _now(),
        "compatibility": {},
        "counter.download": 0,
        "counter.error": 0,
        "provider.availability": 100,
    }
    if kind == "m3u":
        defaults["tuner"] = 1
    for key, value in defaults.items():
        data.setdefault(key, value)
    data.setdefault("id.provider", provider_id)


@dataclass(slots=True)
class RefreshResult:
    provider_id: str
    kind: str
    ok: bool
    error: str | None = None
    streams: int = 0
    skipped: list[SkippedEntry] = field(default_factory=list)


class ProviderManager:
    """Fetches providers and maintains their records in ``settings.files``.

    ``settings`` is the live :class:`~threadfin.settings.Settings`; ``save`` persists it
    (called after every provider, like Go). Network and disk work runs in threads.
    """

    def __init__(
        self,
        settings: Any,
        data_folder: str | os.PathLike[str],
        save: Callable[[], None],
        *,
        app_name: str = "threadfin",
        max_bytes: int = DEFAULT_MAX_BYTES,
        fetch: Callable[..., Download] = download,
    ) -> None:
        self._settings = settings
        self._data = Path(data_folder)
        self._save = save
        self._app = app_name
        self._max_bytes = max_bytes
        self._fetch = fetch
        self._lock = asyncio.Lock()

    # --- access ---
    def records(self, kind: str) -> dict[str, Any]:
        return getattr(self._settings.files, kind)

    def local_path(self, kind: str, provider_id: str) -> Path:
        return self._data / (provider_id + EXTENSIONS[kind])

    def local_files(self, kind: str) -> list[Path]:
        return [self.local_path(kind, pid) for pid in self.records(kind)]

    def parameter(self, kind: str, provider_id: str, key: str) -> str:
        """Go ``getProviderParameter``: strings as-is, numbers as integers, else empty."""
        value = self.records(kind).get(provider_id, {}).get(key)
        if isinstance(value, bool):
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)):
            return str(int(value))
        return ""

    def set_compatibility(self, kind: str, provider_id: str, compatibility: dict[str, int]) -> None:
        record = self.records(kind).get(provider_id)
        if isinstance(record, dict):
            record["compatibility"] = compatibility
            self._save()

    # --- operations ---
    async def refresh(self, kind: str, provider_id: str | None = None) -> list[RefreshResult]:
        """Re-download one provider (or all of one kind) and update counters.

        A failed refresh keeps the previous local copy and counts against the provider's
        availability.
        """
        async with self._lock:
            ids = [provider_id] if provider_id else list(self.records(kind))
            results = []
            for pid in ids:
                if pid not in self.records(kind):
                    results.append(RefreshResult(pid, kind, False, "unknown provider"))
                    continue
                results.append(await self._refresh_one(kind, pid, new=False))
            return results

    async def add(self, kind: str, data: dict[str, Any]) -> RefreshResult:
        """Add a provider. It is fetched first; on failure nothing is kept and it raises."""
        if "file.source" not in data or not isinstance(data["file.source"], str):
            raise ProviderError("file.source is required")
        async with self._lock:
            provider_id = new_provider_id(kind)
            record = {k: v for k, v in data.items() if k not in ("new", "delete")}
            record.setdefault("type", kind)
            self.records(kind)[provider_id] = record
            result = await self._refresh_one(kind, provider_id, new=True)
            if not result.ok:
                self.records(kind).pop(provider_id, None)
                raise ProviderError(result.error or "download failed")
            return result

    async def update(self, kind: str, provider_id: str, changes: dict[str, Any]) -> bool:
        """Change fields of a provider record (Go ``saveFiles`` for an existing id); no download."""
        async with self._lock:
            record = self.records(kind).get(provider_id)
            if not isinstance(record, dict):
                return False
            record.update(changes)
            await asyncio.to_thread(self._save)
            return True

    async def remove(self, kind: str, provider_id: str) -> bool:
        """Delete a provider record and its local copy."""
        async with self._lock:
            if self.records(kind).pop(provider_id, None) is None:
                return False
            await asyncio.to_thread(self.local_path(kind, provider_id).unlink, True)
            self._save()
            return True

    # --- internals ---
    async def _refresh_one(self, kind: str, provider_id: str, *, new: bool) -> RefreshResult:
        record = self.records(kind)[provider_id]
        source = record.get("file.source")
        if not isinstance(source, str):
            return self._failed(kind, provider_id, record, "file.source is missing", new)

        proxy = ""
        ip, port = record.get("http_proxy.ip"), record.get("http_proxy.port")
        if isinstance(ip, str) and isinstance(port, str) and ip and port:
            proxy = f"http://{ip}:{port}"

        try:
            filename, body = await asyncio.to_thread(self._fetch_source, kind, source, proxy)
            result = await asyncio.to_thread(self._store, kind, provider_id, record, source, filename, body)
        except (ProviderError, InvalidM3UError, OSError) as exc:
            return self._failed(kind, provider_id, record, str(exc), new)
        if not new:
            self._update_availability(record)
        await asyncio.to_thread(self._save)
        return result

    def _fetch_source(self, kind: str, source: str, proxy: str) -> tuple[str, bytes]:
        timeout = self._settings.buffer_timeout
        timeout = float(timeout) if timeout and timeout > 0 else DEFAULT_TIMEOUT
        agent = self._settings.user_agent
        scheme = urlsplit(source).scheme.lower()
        if scheme in ("http", "https"):
            fetched = self._fetch(source, proxy=proxy, user_agent=agent, timeout=timeout, max_bytes=self._max_bytes)
            return fetched.filename, fetched.body
        raise ProviderError(f"unsupported source {source!r}: use an http:// or https:// URL")

    def _store(
        self,
        kind: str,
        provider_id: str,
        record: dict[str, Any],
        source: str,
        server_name: str,
        body: bytes,
    ) -> RefreshResult:
        apply_defaults(
            record, kind=kind, provider_id=provider_id, source=source,
            server_name=server_name, app=self._app,
        )  # fmt: skip
        body = extract_gzip(body, max_bytes=self._max_bytes)

        skipped: list[SkippedEntry] = []
        count = 0
        if kind == "m3u":
            parsed = parse_m3u_report(body)
            skipped = parsed.skipped
            count = len(parsed.streams)
            body = serialize_m3u(parsed.streams).encode("utf-8")
        else:
            record["compatibility"] = check_xmltv(body)

        write_bytes_atomic(self._data / str(record[f"file.{self._app}"]), body)
        record["last.update"] = _now()
        record["counter.download"] = _number(record.get("counter.download")) + 1
        return RefreshResult(provider_id, kind, True, None, count, skipped)

    def _failed(self, kind: str, provider_id: str, record: dict[str, Any], error: str, new: bool) -> RefreshResult:
        if not new:
            record["counter.error"] = _number(record.get("counter.error")) + 1
            record["counter.download"] = _number(record.get("counter.download")) + 1
            self._update_availability(record)
            self._save()
        return RefreshResult(provider_id, kind, False, error)

    @staticmethod
    def _update_availability(record: dict[str, Any]) -> None:
        record["provider.availability"] = availability(
            _number(record.get("counter.error")), _number(record.get("counter.download"))
        )
