"""Who may use the web UI and the REST API.

With ``authentication.web`` off everything is open, as in Go. With it on, a request needs a session
token of a user with the ``authentication.web`` level: the ``Token`` cookie (set by the login page,
as in Go) or ``Authorization: Bearer <token>`` (from ``POST /api/v1/login``).

Cross-site request forgery: Go's UI used a WebSocket that accepted any origin, so any web page could
drive it. Here a POST must carry a header a cross-site form cannot set (``HX-Request``, which htmx
sends, ``X-Requested-With``, ``Authorization``) or a JSON body. Browsers only send those cross-site
after a CORS preflight, which this server never approves; PUT / PATCH / DELETE always need one.

DNS rebinding: a web page can point its own domain at this server's LAN address and then counts as
same-origin, which gets it past the CSRF rule. So the UI and the APIs only answer ``Host`` names an
attacker cannot put in public DNS: IP addresses, single-label names (``nas``), private-use suffixes
(``.local``, ``.lan``, ``.home.arpa``, ``.internal``, ``.localhost``), the configured
``httpThreadfinDomain`` / ``httpsThreadfinDomain``, and names listed in ``THREADFIN_ALLOWED_HOSTS``
(comma-separated; ``*`` allows any name).
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any

from starlette.requests import Request

from .auth import LEVEL_WEB, AuthError
from .imagecache import configured_base
from .service import Service

COOKIE = "Token"
ALLOWED_HOSTS_ENV = "THREADFIN_ALLOWED_HOSTS"
PRIVATE_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localhost")


class NotLoggedIn(Exception):
    """No valid session (or the user lacks the web level)."""


def base_url(request: Request, service: Service) -> str:
    """scheme://host[:port] the client used (a configured public domain wins, as in Go)."""
    settings = service.settings
    configured = configured_base(settings)
    if configured:
        return configured
    host = request.headers.get("host") or f"localhost:{settings.port}"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{scheme}://{host}"


def auth_enabled(service: Service) -> bool:
    return service.settings.authentication_web


def request_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header.removeprefix("Bearer ").strip()
    return request.cookies.get(COOKIE, "")


def current_user(request: Request, service: Service) -> str | None:
    """The logged-in user id; ``None`` when authentication is off. Raises ``NotLoggedIn``."""
    if not auth_enabled(service):
        return None
    token = request_token(request)
    if not token:
        raise NotLoggedIn
    try:
        user_id = service.auth.session(token)
        service.auth.check_level(user_id, LEVEL_WEB)
    except AuthError as exc:
        raise NotLoggedIn from exc
    return user_id


def host_name(header: str) -> str:
    """The name in a ``Host`` header, without the port, lower case (``[::1]:80`` gives ``::1``)."""
    header = header.strip().lower()
    if header.startswith("["):
        return header[1:].partition("]")[0]
    if header.count(":") == 1:
        header = header.partition(":")[0]
    return header.rstrip(".")


def configured_hosts(settings: Any, env: str | None = None) -> set[str]:
    """Names allowed beyond the built-in ones: the configured domains and ``THREADFIN_ALLOWED_HOSTS``."""
    raw = os.environ.get(ALLOWED_HOSTS_ENV, "") if env is None else env
    names = {host_name(name) for name in raw.split(",") if name.strip()}
    for domain in (settings.http_threadfin_domain, settings.https_threadfin_domain):
        if domain:
            names.add(host_name(domain))
    return names


def host_allowed(header: str, extra: set[str]) -> bool:
    """Whether the UI / APIs may answer a request for this ``Host`` (see the module docstring)."""
    name = host_name(header)
    if not name or "*" in extra or name in extra or "." not in name or name.endswith(PRIVATE_SUFFIXES):
        return True  # no Host (HTTP/1.0) cannot come from a browser
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def csrf_ok(request: Request) -> bool:
    if request.method != "POST":
        return True  # GET / HEAD are read-only; other methods cannot come from a cross-site form
    headers = request.headers
    if "hx-request" in headers or "x-requested-with" in headers or "authorization" in headers:
        return True
    return headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"


__all__ = [
    "ALLOWED_HOSTS_ENV",
    "COOKIE",
    "NotLoggedIn",
    "auth_enabled",
    "base_url",
    "configured_hosts",
    "csrf_ok",
    "current_user",
    "host_allowed",
    "host_name",
    "request_token",
]
