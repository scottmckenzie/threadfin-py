"""Who may use the web UI and the REST API.

With ``authentication.web`` off everything is open, as in Go. With it on, a request needs a session
token of a user with the ``authentication.web`` level: the ``Token`` cookie (set by the login page,
as in Go) or ``Authorization: Bearer <token>`` (from ``POST /api/v1/login``).

Cross-site request forgery: Go's UI used a WebSocket that accepted any origin, so any web page could
drive it. Here a POST must carry a header a cross-site form cannot set (``HX-Request``, which htmx
sends, ``X-Requested-With``, ``Authorization``) or a JSON body. Browsers only send those cross-site
after a CORS preflight, which this server never approves; PUT / PATCH / DELETE always need one.
"""

from __future__ import annotations

from starlette.requests import Request

from .auth import LEVEL_WEB, AuthError
from .imagecache import configured_base
from .service import Service

COOKIE = "Token"


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


def csrf_ok(request: Request) -> bool:
    if request.method != "POST":
        return True  # GET / HEAD are read-only; other methods cannot come from a cross-site form
    headers = request.headers
    if "hx-request" in headers or "x-requested-with" in headers or "authorization" in headers:
        return True
    return headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"


__all__ = ["COOKIE", "NotLoggedIn", "auth_enabled", "base_url", "csrf_ok", "current_user", "request_token"]
