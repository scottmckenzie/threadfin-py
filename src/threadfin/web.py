"""HTTP endpoints that clients (players, Plex/Jellyfin, DVR software) talk to.

``/stream/<id>``        302 to the provider URL (no buffering / proxying)
``/m3u/threadfin.m3u``  playlist, optional ``?group-title=A,B``
``/xmltv/threadfin.xml`` the guide (``.gz`` served pre-compressed)

``/discover.json``, ``/lineup_status.json``, ``/lineup.json``, ``/device.xml``, ``/capability``, ``/``
                        HDHomeRun emulation (see hdhr.py)

Authentication (see auth.py), each switched on by its setting:
``authentication.m3u`` / ``authentication.xml``: ``/m3u/`` / ``/xmltv/`` need ``?username=..&password=..``;
``authentication.pms``: ``/lineup.json`` needs HTTP Basic credentials. Refused requests get a 403
without a ``WWW-Authenticate`` challenge, as in Go.

On start-up the service updates from the providers (``files.update``) or builds from the local
copies, then the scheduler runs the ``update`` times (see scheduler.py).

``/images/<name>``       the image cache (``cache.images``, see imagecache.py)
``/web/``                the web UI (ui.py); ``/api/v1/`` the REST API, ``/api/`` Go's API, ``/ppv/``,
                        ``/data_images/`` (api.py)

The UI and the APIs (``/web``, ``/api``, ``/ppv``) refuse ``Host`` names that could be DNS rebinding
(see access.py); the player and HDHomeRun endpoints answer any name.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from . import api, hdhr, ui
from .access import ALLOWED_HOSTS_ENV, base_url, configured_hosts, host_allowed, host_name
from .auth import LEVEL_M3U, LEVEL_PMS, LEVEL_XML, AuthError
from .imagecache import IMAGE_TYPES, image_headers
from .scheduler import Scheduler
from .service import Service


def forbidden() -> Response:
    return PlainTextResponse("Forbidden [403]\n", status_code=403)


def url_auth_ok(request: Request, service: Service, level: str) -> bool:
    """``?username=..&password=..`` check for /m3u/ and /xmltv/ (Go takes the first value of each)."""
    enabled = service.settings.authentication_m3u if level == LEVEL_M3U else service.settings.authentication_xml
    if not enabled:
        return True
    username = next(iter(request.query_params.getlist("username")), "")
    password = next(iter(request.query_params.getlist("password")), "")
    try:
        service.auth.url_auth(username, password, level)
    except AuthError:
        return False
    return True


def basic_auth_ok(request: Request, service: Service, level: str) -> bool:
    try:
        service.auth.basic_auth(request.headers.get("authorization", ""), level)
    except AuthError:
        return False
    return True


GUARDED_PREFIXES = ("/web", "/api", "/ppv")
log = logging.getLogger("threadfin")


class HostCheck:
    """ASGI middleware: refuse UI / API requests whose ``Host`` could be DNS rebinding."""

    def __init__(self, app: Any, service: Service) -> None:
        self.app, self.service = app, service
        self.reported: set[str] = set()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if any(path == prefix or path.startswith(prefix + "/") for prefix in GUARDED_PREFIXES):
            host = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"host"), "")
            if not host_allowed(host, configured_hosts(self.service.settings)):
                await self.refuse(host)(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def refuse(self, host: str) -> Response:
        if host not in self.reported and len(self.reported) < 100:  # log each name once
            self.reported.add(host)
            log.warning(
                "refused a request for host %r (possible DNS rebinding); allow it in %s", host, ALLOWED_HOSTS_ENV
            )
        name = host_name(host)
        return PlainTextResponse(
            f"Forbidden: unknown host {name!r}. To use this name, add it to {ALLOWED_HOSTS_ENV} and restart,"
            " or set it as httpThreadfinDomain in the settings.\n",
            status_code=403,
        )


def create_app(service: Service, *, build_on_start: bool = True, schedule: bool = True) -> Starlette:
    async def stream(request: Request) -> Response:
        target = service.stream_target(request.path_params["stream_id"])
        if target is None:
            return PlainTextResponse("404 page not found\n", status_code=404)
        return RedirectResponse(target, status_code=302)

    async def m3u(request: Request) -> Response:
        if not url_auth_ok(request, service, LEVEL_M3U):
            return forbidden()
        raw = request.query_params.get("group-title", "")
        groups = [g for g in raw.split(",")] if raw else []
        text = await asyncio.to_thread(service.playlist, base_url(request, service), groups)
        return Response(
            text,
            media_type="audio/x-mpegurl",
            headers={"Content-Disposition": "attachment; filename=threadfin.m3u"},
        )

    async def xmltv(request: Request) -> Response:
        if not url_auth_ok(request, service, LEVEL_XML):
            return forbidden()
        path = service.xmltv_path
        if not path.exists():
            return PlainTextResponse("404 page not found\n", status_code=404)
        return FileResponse(path, media_type="application/xml; charset=utf-8")

    async def xmltv_gz(request: Request) -> Response:
        if not url_auth_ok(request, service, LEVEL_XML):
            return forbidden()
        path = service.xmltv_path.with_name(service.xmltv_path.name + ".gz")
        if not path.exists():
            return PlainTextResponse("404 page not found\n", status_code=404)
        return FileResponse(path, media_type="application/gzip")

    def device(request: Request) -> hdhr.Device:
        return service.device(base_url(request, service))

    async def discover(request: Request) -> Response:
        return Response(hdhr.to_json(hdhr.discover(device(request))), media_type="application/json")

    async def lineup_status(request: Request) -> Response:
        return Response(hdhr.to_json(hdhr.lineup_status(service.building)), media_type="application/json")

    async def lineup(request: Request) -> Response:
        if service.settings.authentication_pms and not basic_auth_ok(request, service, LEVEL_PMS):
            return forbidden()
        entries = await asyncio.to_thread(service.lineup, base_url(request, service))
        return Response(hdhr.to_json(entries), media_type="application/json")

    async def images(request: Request) -> Response:
        """Go ``/images/``: the image cache (open, like the guide that links to it)."""
        name = request.path_params["name"]
        path = service.images.path(name)
        if path is None or not path.is_file():
            return PlainTextResponse("Not Found [404]\n", status_code=404)
        return FileResponse(path, media_type=IMAGE_TYPES[path.suffix.lower()], headers=image_headers(name))

    async def capability(request: Request) -> Response:
        return Response(hdhr.capability_xml(device(request)), media_type="application/xml")

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        logger = logging.getLogger("threadfin")
        logger.addHandler(service.log)
        if logger.getEffectiveLevel() > logging.INFO:
            logger.setLevel(logging.INFO)
        if build_on_start:
            await service.start()  # also restores group caps / registry after a restart
        task = asyncio.create_task(Scheduler(service).run(), name="scheduler") if schedule else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            logger.removeHandler(service.log)
            service.close()

    app = Starlette(
        routes=[
            Route("/stream/{stream_id}", stream, methods=["GET", "HEAD"]),
            Route("/m3u/threadfin.m3u", m3u, methods=["GET", "HEAD"]),
            Route("/xmltv/threadfin.xml", xmltv, methods=["GET", "HEAD"]),
            Route("/xmltv/threadfin.xml.gz", xmltv_gz, methods=["GET", "HEAD"]),
            Route("/images/{name}", images, methods=["GET", "HEAD"]),
            Route("/discover.json", discover, methods=["GET", "HEAD"]),
            Route("/lineup_status.json", lineup_status, methods=["GET", "HEAD"]),
            Route("/lineup.json", lineup, methods=["GET", "HEAD"]),
            Route("/device.xml", capability, methods=["GET", "HEAD"]),
            Route("/capability", capability, methods=["GET", "HEAD"]),
            Route("/", capability, methods=["GET", "HEAD"]),
            *api.routes(service),
            *ui.routes(service),
        ],
        lifespan=lifespan,
        middleware=[Middleware(HostCheck, service=service)],
    )
    app.state.service = service
    return app
