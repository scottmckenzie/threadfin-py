"""Image cache (port of ``internal/imgcache``): channel logos and programme posters served by this
server under ``/images/<name>`` instead of the provider's address (``cache.images`` setting).

How it works, as in Go: while the guide is written, every image URL is looked up. If the image is
in ``cache/images/`` the guide gets ``<base>/images/<name>``; otherwise it keeps the original URL
and the image is queued. After the build the queue is downloaded, images the guide no longer uses
are deleted, and the guide is written again. The file name is ``md5(URL without the query)`` plus the
path's extension, so an existing Go cache folder is reused.

Differences (see docs/PARITY_NOTES.md): only http(s) URLs with an image extension are cached and a
download must be an image (Go cached any extension and served it with that type, so a provider URL
ending in ``.html`` became a page on this server); the base URL is the same for every lookup (Go
answered the first lookup with the address of the last request); cached URLs are only written to
the guide and playlist, never into ``xepg.json``; failed downloads are retried after a while instead
of on every build.
"""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import re
import socket
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .jsonio import write_bytes_atomic
from .providers import Download, ProviderError, download

log = logging.getLogger(__name__)

IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".bmp": "image/bmp",
}  # fmt: skip
MAX_IMAGE_BYTES = 5 * 1024 * 1024
DOWNLOAD_TIMEOUT = 20.0
RETRY_FAILED_AFTER = 6 * 3600.0
WORKERS = 4
CACHE_NAME = re.compile(r"[0-9a-f]{32}\.[A-Za-z0-9]{1,5}")

# Headers for images served from this origin (cache and uploads): no content sniffing, and an SVG
# cannot run scripts or load anything.
SAFE_IMAGE_HEADERS = {"X-Content-Type-Options": "nosniff"}
SVG_HEADERS = {
    **SAFE_IMAGE_HEADERS,
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
}


def image_headers(name: str) -> dict[str, str]:
    return SVG_HEADERS if name.lower().endswith(".svg") else SAFE_IMAGE_HEADERS


def cache_name(src: str) -> str | None:
    """The cache file name for an image URL, or ``None`` if it is not cached."""
    src = src.strip("\r\n")
    parts = urlsplit(src)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    ext = posixpath.splitext(parts.path)[1]
    if ext.lower() not in IMAGE_TYPES:
        return None
    return hashlib.md5(src.split("?", 1)[0].encode("utf-8")).hexdigest() + ext


def looks_like_image(data: bytes, name: str) -> bool:
    """Magic numbers of the formats in ``IMAGE_TYPES`` (an HTML error page is not an image)."""
    head = data[:512]
    if name.lower().endswith(".svg"):
        text = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        return text.startswith(b"<svg") or (text.startswith(b"<?xml") and b"<svg" in data[:4096].lower())
    signatures = (
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff",
        b"GIF87a",
        b"GIF89a",
        b"\x00\x00\x01\x00",
        b"BM",
    )  # png jpeg gif ico bmp
    return head.startswith(signatures) or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")


def local_address() -> str:
    """This machine's address on the LAN (no packet is sent), or 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.254.254.254", 1))
            address: str = s.getsockname()[0]
            return address
        except OSError:
            return "127.0.0.1"


def configured_base(settings: Any) -> str | None:
    """``https://<httpsThreadfinDomain>[:httpsPort]`` with forceHttps, ``http://<httpThreadfinDomain>:port``,
    else None."""
    if settings.force_https and settings.https_threadfin_domain:
        port = settings.https_port
        suffix = f":{port}" if port and port != 443 else ""
        return f"https://{settings.https_threadfin_domain}{suffix}"
    domain = settings.http_threadfin_domain
    if domain:
        return f"http://{domain}" if ":" in domain or not settings.port else f"http://{domain}:{settings.port}"
    return None


def default_base(settings: Any) -> str:
    """Where clients reach this server when no request tells us: the configured domain, else the
    bind address or LAN address with the port (Go's start-up default)."""
    configured = configured_base(settings)
    if configured:
        return configured
    host = settings.bind_ip_address if settings.bind_ip_address not in ("", "0.0.0.0") else local_address()
    return f"http://{host}:{settings.port}" if settings.port else f"http://{host}"


@dataclass(slots=True)
class ImageSession:
    """The image lookups of one guide write: what it used and what is missing."""

    cache: ImageCache
    base: str
    referenced: set[str] = field(default_factory=set)
    wanted: dict[str, str] = field(default_factory=dict)  # cache name -> source URL

    def url(self, src: str) -> str:
        name = cache_name(src)
        if name is None:
            return src
        self.referenced.add(name)
        if self.cache.has(name):
            return f"{self.base}/images/{name}"
        self.wanted.setdefault(name, src.strip("\r\n"))
        return src.strip("\r\n")


class ImageCache:
    def __init__(
        self,
        folder: str | os.PathLike[str],
        *,
        user_agent: Callable[[], str] = lambda: "",
        fetch: Callable[..., Download] = download,
        clock: Callable[[], float] = time.monotonic,
        workers: int = WORKERS,
    ) -> None:
        self.folder = Path(folder)
        self._user_agent = user_agent
        self._fetch = fetch
        self._clock = clock
        self._workers = workers
        self._failed: dict[str, float] = {}  # cache name -> when it last failed

    def path(self, name: str) -> Path | None:
        """The file for a cache name, if the name is valid (never anything outside the folder)."""
        if not CACHE_NAME.fullmatch(name) or posixpath.splitext(name)[1].lower() not in IMAGE_TYPES:
            return None
        return self.folder / name

    def has(self, name: str) -> bool:
        path = self.path(name)
        return path is not None and path.is_file()

    def session(self, base: str) -> ImageSession:
        return ImageSession(self, base.rstrip("/"))

    def lookup(self, src: str, base: str) -> str:
        """The cached URL if the image is cached, else ``src`` (nothing is queued)."""
        name = cache_name(src)
        if name is None or not self.has(name):
            return src
        return f"{base.rstrip('/')}/images/{name}"

    def _download(self, name: str, src: str) -> bool:
        try:
            fetched = self._fetch(src, user_agent=self._user_agent(), timeout=DOWNLOAD_TIMEOUT,
                                  max_bytes=MAX_IMAGE_BYTES)  # fmt: skip
        except (ProviderError, OSError) as exc:
            log.debug("image %s: %s", src, exc)
            return False
        if not looks_like_image(fetched.body, name):
            log.debug("image %s: not an image", src)
            return False
        self.folder.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(self.folder / name, fetched.body)
        return True

    def fill(self, session: ImageSession) -> tuple[int, int]:
        """Download what the session is missing; returns (downloaded, failed)."""
        now = self._clock()
        todo = [(n, s) for n, s in session.wanted.items()
                if not self.has(n) and now - self._failed.get(n, -RETRY_FAILED_AFTER) >= RETRY_FAILED_AFTER]  # fmt: skip
        if not todo:
            return 0, 0
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            results = list(pool.map(self._download, [n for n, _ in todo], [s for _, s in todo]))
        for (name, _), ok in zip(todo, results, strict=True):
            if ok:
                self._failed.pop(name, None)
            else:
                self._failed[name] = now
        downloaded = sum(results)
        return downloaded, len(results) - downloaded

    def prune(self, keep: set[str]) -> int:
        """Delete cached images not in ``keep`` (Go ``Remove``); returns the number deleted."""
        if not self.folder.is_dir():
            return 0
        removed = 0
        for path in self.folder.iterdir():
            if path.name not in keep and (path.is_file() or path.is_symlink()):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def clear(self) -> None:
        self.prune(set())


__all__ = [
    "IMAGE_TYPES",
    "ImageCache",
    "ImageSession",
    "cache_name",
    "configured_base",
    "default_base",
    "image_headers",
    "local_address",
    "looks_like_image",
]
