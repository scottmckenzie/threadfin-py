"""HTTP endpoints that clients (players, Plex/Jellyfin, DVR software) talk to.

``/stream/<id>``        302 to the provider URL (no buffering / proxying)
``/m3u/threadfin.m3u``  playlist, optional ``?group-title=A,B``
``/xmltv/threadfin.xml`` the guide (``.gz`` served pre-compressed)

Authentication, HDHomeRun emulation and the management API/UI are added on top of this app.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .service import Service


def base_url(request: Request, service: Service) -> str:
    """scheme://host[:port] the client used (a configured public domain wins, as in Go)."""
    settings = service.settings
    if settings.force_https and settings.https_threadfin_domain:
        return f"https://{settings.https_threadfin_domain}"
    if settings.http_threadfin_domain:
        port = settings.port
        return f"http://{settings.http_threadfin_domain}:{port}" if port else f"http://{settings.http_threadfin_domain}"
    host = request.headers.get("host") or f"localhost:{settings.port}"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{scheme}://{host}"


def create_app(service: Service, *, build_on_start: bool = True) -> Starlette:
    async def stream(request: Request) -> Response:
        target = service.stream_target(request.path_params["stream_id"])
        if target is None:
            return PlainTextResponse("404 page not found\n", status_code=404)
        return RedirectResponse(target, status_code=302)

    async def m3u(request: Request) -> Response:
        raw = request.query_params.get("group-title", "")
        groups = [g for g in raw.split(",")] if raw else []
        text = await asyncio.to_thread(service.playlist, base_url(request, service), groups)
        return Response(
            text,
            media_type="audio/x-mpegurl",
            headers={"Content-Disposition": "attachment; filename=threadfin.m3u"},
        )

    async def xmltv(request: Request) -> Response:
        path = service.xmltv_path
        if not path.exists():
            return PlainTextResponse("404 page not found\n", status_code=404)
        return FileResponse(path, media_type="application/xml; charset=utf-8")

    async def xmltv_gz(request: Request) -> Response:
        path = service.xmltv_path.with_name(service.xmltv_path.name + ".gz")
        if not path.exists():
            return PlainTextResponse("404 page not found\n", status_code=404)
        return FileResponse(path, media_type="application/gzip")

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if build_on_start:
            await asyncio.to_thread(service.build)  # restores group caps / registry after a restart
        yield

    app = Starlette(
        routes=[
            Route("/stream/{stream_id}", stream, methods=["GET", "HEAD"]),
            Route("/m3u/threadfin.m3u", m3u, methods=["GET", "HEAD"]),
            Route("/xmltv/threadfin.xml", xmltv, methods=["GET", "HEAD"]),
            Route("/xmltv/threadfin.xml.gz", xmltv_gz, methods=["GET", "HEAD"]),
        ],
        lifespan=lifespan,
    )
    app.state.service = service
    return app
