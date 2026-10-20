"""JSON APIs.

``/api/v1/...``  REST API for everything the web UI does (see ``routes()``). Session as in
                 access.py; errors are ``{"error": "..."}`` with a 4xx status.
``/api/``        Go's API, kept for existing scripts: ``POST {"cmd": "status" | "login" |
                 "update.m3u" | "update.xmltv" | "update.xepg"}``, only with the
                 ``api`` setting on (423 otherwise); with ``authentication.api`` on, ``login`` returns
                 a token that every later request sends and that is replaced in every answer.
``/ppv/enable``, ``/ppv/disable``   switch the channels mapped to the PPV dummy guide on / off.
``/data_images/<name>``             uploaded logos.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any

from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import BaseRoute, Route

from .access import NotLoggedIn, base_url, client_address, csrf_ok, current_user, request_token
from .admin import LOGO_TYPES, PROVIDER_KINDS, AdminError, logo_filename, receive_upload, save_logo
from .auth import LEVEL_API, LEVEL_WEB, AuthError, TooManyAttempts
from .backup import BackupError, create_backup
from .imagecache import image_headers
from .probe import InvalidProbeURLError
from .probe_store import ProbeRecord
from .providers import ProviderError
from .service import API_VERSION, Service
from .xepg import XEPGChannel

Handler = Callable[[Request], Awaitable[Response]]


def error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def json_body(request: Request) -> Any:
    try:
        return json.loads(await request.body() or b"{}")
    except ValueError as exc:
        raise AdminError(f"invalid JSON: {exc}") from exc


async def json_object(request: Request) -> dict[str, Any]:
    body = await json_body(request)
    if not isinstance(body, dict):
        raise AdminError("expected a JSON object")
    return body


def channel_json(channel_id: str, channel: XEPGChannel) -> dict[str, Any]:
    return {"id": channel_id, **channel.to_json()}


def probe_json(record: ProbeRecord) -> dict[str, Any]:
    details = record.details
    return {
        "url": record.url,
        "probedAt": record.probed_at.isoformat(),
        "ok": record.ok,
        "error": record.error,
        "durationMs": record.duration_ms,
        "cached": record.cached,
        "resolution": record.summary.resolution,
        "frameRate": record.summary.frame_rate,
        "audioChannels": record.summary.audio_channels,
        "videoCodec": details.video_codec,
        "audioCodec": details.audio_codec,
        "width": details.width,
        "height": details.height,
        "videoBitRate": details.video_bit_rate,
        "audioBitRate": details.audio_bit_rate,
    }


def user_json(user_id: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"id": user_id, **{k: v for k, v in data.items() if k not in ("password", "confirm")}}


async def upload_bytes(request: Request, field: str = "file") -> tuple[str, bytes]:
    """A multipart upload (``field``) or the raw body; returns (file name, content)."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        async with request.form() as form:
            upload = form.get(field)
            if not isinstance(upload, UploadFile):
                raise AdminError(f"missing file field {field!r}")
            return upload.filename or "", await upload.read()
    return request.query_params.get("filename", ""), await request.body()


def routes(service: Service) -> list[BaseRoute]:
    def guarded(handler: Handler) -> Handler:
        """Session + CSRF check, and errors as JSON."""

        async def wrapper(request: Request) -> Response:
            if not csrf_ok(request):
                return error("missing X-Requested-With header or JSON body", 403)
            try:
                request.state.user = current_user(request, service)
            except NotLoggedIn:
                return error("not logged in", 401)
            try:
                return await handler(request)
            except (AdminError, AuthError, BackupError, ProviderError, InvalidProbeURLError) as exc:
                return error(str(exc))
            except KeyError as exc:
                return error(f"not found: {exc.args[0] if exc.args else ''}", 404)

        return wrapper

    # --- session ---------------------------------------------------------------------------
    async def login(request: Request) -> Response:
        try:
            body = await json_object(request)
        except AdminError as exc:
            return error(str(exc))
        username, password = body.get("username"), body.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            return error("username and password are required")
        try:
            token = service.auth.login(username, password, client_address(request))
            service.auth.check_level(service.auth.session(token), LEVEL_WEB)
        except TooManyAttempts as exc:
            response = error(str(exc), 429)
            response.headers["Retry-After"] = str(exc.retry_after)
            return response
        except AuthError as exc:
            return error(str(exc), 401)
        return JSONResponse({"token": token})

    async def logout(request: Request) -> Response:
        service.auth.logout(request_token(request))
        return Response(status_code=204)

    # --- status, log, update ---------------------------------------------------------------
    async def status(request: Request) -> Response:
        report = service.report
        return JSONResponse({
            **service.status(base_url(request, service)),
            "report": {
                "ok": report.ok, "error": report.error, "warnings": report.warnings,
                "providerErrors": report.provider_errors, "xmltvErrors": report.xmltv_errors,
                "channels": report.channels, "programmes": report.programmes, "streams": report.streams,
            },
        })  # fmt: skip

    async def get_log(request: Request) -> Response:
        return JSONResponse({
            "errors": service.log.errors, "warnings": service.log.warnings,
            "entries": [{"time": e.time.isoformat(timespec="seconds"), "level": e.level, "message": e.message}
                        for e in service.log.entries()],
        })  # fmt: skip

    async def clear_log(request: Request) -> Response:
        service.log.clear()
        return Response(status_code=204)

    async def update(request: Request) -> Response:
        if service.busy:
            return error("an update is already running", 409)
        report = await service.update()
        return JSONResponse({"ok": report.ok, "error": report.error, "providerErrors": report.provider_errors})

    async def build(request: Request) -> Response:
        report = await service.rebuild()
        return JSONResponse({"ok": report.ok, "error": report.error, "channels": report.channels})

    # --- settings ----------------------------------------------------------------------------
    async def get_settings(request: Request) -> Response:
        return JSONResponse(service.settings.to_json_dict())

    async def patch_settings(request: Request) -> Response:
        changed = await service.change_settings(await json_object(request))
        return JSONResponse({"changed": sorted(changed), "settings": service.settings.to_json_dict()})

    # --- providers ---------------------------------------------------------------------------
    def kind_of(request: Request) -> str:
        kind = str(request.path_params["kind"])
        if kind not in PROVIDER_KINDS:
            raise KeyError(kind)
        return kind

    async def list_providers(request: Request) -> Response:
        return JSONResponse(service.manager.records(kind_of(request)))

    async def add_provider(request: Request) -> Response:
        result = await service.add_provider(kind_of(request), await json_object(request))
        record = service.manager.records(result.kind)[result.provider_id]
        return JSONResponse({"id": result.provider_id, "streams": result.streams, "provider": record}, 201)

    async def get_provider(request: Request) -> Response:
        return JSONResponse(service.manager.records(kind_of(request))[request.path_params["id"]])

    async def patch_provider(request: Request) -> Response:
        kind, provider_id = kind_of(request), request.path_params["id"]
        if not await service.edit_provider(kind, provider_id, await json_object(request)):
            raise KeyError(provider_id)
        return JSONResponse(service.manager.records(kind)[provider_id])

    async def delete_provider(request: Request) -> Response:
        if not await service.delete_provider(kind_of(request), request.path_params["id"]):
            raise KeyError(request.path_params["id"])
        return Response(status_code=204)

    async def refresh_provider(request: Request) -> Response:
        kind, provider_id = kind_of(request), request.path_params["id"]
        if provider_id not in service.manager.records(kind):
            raise KeyError(provider_id)
        [result] = await service.refresh_provider(kind, provider_id)
        body = {"ok": result.ok, "error": result.error, "streams": result.streams,
                "skipped": [{"line": s.line, "reason": s.reason, "url": s.url} for s in result.skipped]}  # fmt: skip
        return JSONResponse(body, 200 if result.ok else 502)

    # --- filters -----------------------------------------------------------------------------
    async def list_filters(request: Request) -> Response:
        return JSONResponse(service.settings.filter)

    async def add_filter(request: Request) -> Response:
        [new_id] = await service.change_filters({"-1": await json_object(request)})
        return JSONResponse({"id": new_id, "filter": service.settings.filter[new_id]}, 201)

    async def patch_filter(request: Request) -> Response:
        filter_id = request.path_params["id"]
        if filter_id not in service.settings.filter:
            raise KeyError(filter_id)
        body = await json_object(request)
        body.pop("delete", None)
        await service.change_filters({filter_id: body})
        return JSONResponse(service.settings.filter[filter_id])

    async def delete_filter(request: Request) -> Response:
        filter_id = request.path_params["id"]
        if filter_id not in service.settings.filter:
            raise KeyError(filter_id)
        await service.change_filters({filter_id: {"delete": True}})
        return Response(status_code=204)

    async def groups(request: Request) -> Response:
        return JSONResponse([{"name": g.name, "count": g.count} for g in service.stream_db.groups])

    # --- channels ----------------------------------------------------------------------------
    async def list_channels(request: Request) -> Response:
        query = request.query_params
        channels = [channel_json(cid, ch) for cid, ch in service.channels.items()]
        if "active" in query:
            want = query["active"].lower() in ("1", "true", "yes")
            channels = [c for c in channels if c["x-active"] is want]
        if query.get("group"):
            channels = [c for c in channels if c["x-group-title"] == query["group"]]
        return JSONResponse(channels)

    async def get_channel(request: Request) -> Response:
        channel_id = request.path_params["id"]
        channel = service.channels[channel_id]
        return JSONResponse(channel_json(channel_id, channel))

    async def patch_channel(request: Request) -> Response:
        channel_id = request.path_params["id"]
        if channel_id not in service.channels:
            raise KeyError(channel_id)
        channel = await service.edit_channel(channel_id, await json_object(request))
        return JSONResponse(channel_json(channel_id, channel))

    async def bulk_channels(request: Request) -> Response:
        body = await json_object(request)
        ids, changes = body.get("ids"), body.get("changes")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or not isinstance(changes, dict):
            raise AdminError('expected {"ids": [...], "changes": {...}}')
        return JSONResponse({"changed": await service.edit_channels(ids, changes)})

    async def probe_channel(request: Request) -> Response:
        force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
        record = await service.probe_channel(request.path_params["id"], force=force)
        return JSONResponse(probe_json(record))

    async def probe_history(request: Request) -> Response:
        channel = service.channels[request.path_params["id"]]
        if not service.system.file_probe_db.exists():
            return JSONResponse([])
        return JSONResponse([probe_json(r) for r in service.probes.store.history(channel.url)])

    async def xmltv_channels(request: Request) -> Response:
        return JSONResponse({
            name: [{"id": cid, "name": info.get("display-name", "")} for cid, info in channels.items()]
            for name, channels in service.xmltv_mapping.items()
        })  # fmt: skip

    async def upload_logo(request: Request) -> Response:
        name, data = await upload_bytes(request)
        filename = await _run(save_logo, service.system, name, data)
        return JSONResponse({"url": f"{base_url(request, service)}/data_images/{filename}"}, 201)

    # --- users -------------------------------------------------------------------------------
    async def list_users(request: Request) -> Response:
        return JSONResponse([user_json(uid, service.auth.read_user_data(uid)) for uid in service.auth.users])

    async def add_user(request: Request) -> Response:
        body = await json_object(request)
        user_id = service.auth.save_new_user(body)
        return JSONResponse(user_json(user_id, service.auth.read_user_data(user_id)), 201)

    async def patch_user(request: Request) -> Response:
        user_id = request.path_params["id"]
        if user_id not in service.auth.users:
            raise KeyError(user_id)
        body = await json_object(request)
        body.pop("delete", None)
        for key, value in body.items():
            if key.startswith("authentication.") and not isinstance(value, bool):
                raise AdminError(f"{key}: invalid value {value!r}")
        if user_id == request.state.user and body.get(LEVEL_WEB) is False:
            raise AdminError("you cannot remove your own web access")
        data = {**service.auth.read_user_data(user_id), **body}
        service.auth.save_user_data({user_id: data})
        return JSONResponse(user_json(user_id, service.auth.read_user_data(user_id)))

    async def delete_user(request: Request) -> Response:
        user_id = request.path_params["id"]
        if user_id not in service.auth.users:
            raise KeyError(user_id)
        if user_id == request.state.user:
            raise AdminError("you cannot delete your own account")
        if len(service.auth.users) == 1 and service.settings.authentication_web:
            raise AdminError("the last user cannot be deleted while login is required")
        service.auth.remove_user(user_id)
        return Response(status_code=204)

    # --- backup ------------------------------------------------------------------------------
    async def download_backup(request: Request) -> Response:
        path = await _run(create_backup, service.system)
        return FileResponse(path, media_type="application/zip", filename=path.name,
                            background=BackgroundTask(path.unlink, missing_ok=True))  # fmt: skip

    async def restore(request: Request) -> Response:
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        try:
            os.close(fd)
            size = await receive_upload(request, Path(tmp), "file")
            if not size:
                raise AdminError("no archive uploaded")
            result = await service.restore(tmp)
        finally:
            await asyncio.to_thread(Path(tmp).unlink, missing_ok=True)
        return JSONResponse({"portChanged": result.port_changed,
                             "webURL": result.web_url(base_url(request, service))})  # fmt: skip

    v1: list[tuple[str, Handler, list[str]]] = [
        ("/status", status, ["GET"]),
        ("/log", get_log, ["GET"]),
        ("/log", clear_log, ["DELETE"]),
        ("/update", update, ["POST"]),
        ("/build", build, ["POST"]),
        ("/settings", get_settings, ["GET"]),
        ("/settings", patch_settings, ["PATCH"]),
        ("/providers/{kind}", list_providers, ["GET"]),
        ("/providers/{kind}", add_provider, ["POST"]),
        ("/providers/{kind}/{id}", get_provider, ["GET"]),
        ("/providers/{kind}/{id}", patch_provider, ["PATCH"]),
        ("/providers/{kind}/{id}", delete_provider, ["DELETE"]),
        ("/providers/{kind}/{id}/refresh", refresh_provider, ["POST"]),
        ("/filters", list_filters, ["GET"]),
        ("/filters", add_filter, ["POST"]),
        ("/filters/{id}", patch_filter, ["PATCH"]),
        ("/filters/{id}", delete_filter, ["DELETE"]),
        ("/groups", groups, ["GET"]),
        ("/channels", list_channels, ["GET"]),
        ("/channels/bulk", bulk_channels, ["POST"]),
        ("/channels/{id}", get_channel, ["GET"]),
        ("/channels/{id}", patch_channel, ["PATCH"]),
        ("/channels/{id}/probe", probe_channel, ["POST"]),
        ("/channels/{id}/probes", probe_history, ["GET"]),
        ("/xmltv", xmltv_channels, ["GET"]),
        ("/logos", upload_logo, ["POST"]),
        ("/users", list_users, ["GET"]),
        ("/users", add_user, ["POST"]),
        ("/users/{id}", patch_user, ["PATCH"]),
        ("/users/{id}", delete_user, ["DELETE"]),
        ("/backup", download_backup, ["GET"]),
        ("/restore", restore, ["POST"]),
    ]
    # Starlette matches the first route by path, so methods sharing a path go into one handler.
    by_path: dict[str, dict[str, Handler]] = {}
    for path, handler, methods in v1:
        for method in methods:
            by_path.setdefault(path, {})[method] = handler

    def dispatch(handlers: dict[str, Handler]) -> Handler:
        async def endpoint(request: Request) -> Response:
            handler = handlers.get(request.method) or (handlers.get("GET") if request.method == "HEAD" else None)
            if handler is None:
                return error("method not allowed", 405)
            return await handler(request)

        return endpoint

    out: list[BaseRoute] = [
        Route("/api/v1/login", login, methods=["POST"]),
        Route("/api/v1/logout", logout, methods=["POST"]),
    ]
    for path, handlers in by_path.items():
        methods = sorted(set(handlers) | ({"HEAD"} if "GET" in handlers else set()))
        out.append(Route("/api/v1" + path, guarded(dispatch(handlers)), methods=methods))
    out += legacy_routes(service)
    return out


async def _run(fn: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.to_thread(fn, *args)


# --- Go's /api/, /ppv/, /data_images/ ----------------------------------------------------------

INVALID_COMMAND = "Invalid API command"  # Go error 5000
LOGIN_INCORRECT = "Login incorrect"


def legacy_routes(service: Service) -> list[BaseRoute]:
    def go_error(code: int) -> PlainTextResponse:
        return PlainTextResponse(f"{HTTPStatus(code).phrase} [{code}]\n", status_code=code)

    async def api(request: Request) -> Response:
        """Go ``API()``. Go wrote a second (success) object after an error object; one object here."""
        settings = service.settings
        if not settings.api:
            return go_error(423)
        if request.method == "GET":
            return go_error(404)
        try:
            body = json.loads(await request.body())
        except ValueError:
            return go_error(400)
        if not isinstance(body, dict):
            return go_error(400)

        def text(key: str) -> str:
            value = body.get(key)
            return value if isinstance(value, str) else ""

        def failed(message: str) -> JSONResponse:
            return JSONResponse({"err": message, "status": False})

        response: dict[str, Any] = {"status": True}
        cmd = text("cmd")
        if settings.authentication_api:
            try:
                if text("token"):
                    token = service.auth.refresh_token(text("token"))
                elif cmd == "login":
                    token = service.auth.login(text("username"), text("password"), client_address(request))
                else:
                    return failed(LOGIN_INCORRECT)
                service.auth.check_level(service.auth.user_id_for_token(token), LEVEL_API)
            except AuthError as exc:
                return failed(str(exc))
            response["token"] = token

        if cmd == "login":
            pass
        elif cmd == "status":
            info = service.status(base_url(request, service))
            response.update({
                "epg.source": info["epg.source"],
                "streams.active": info["streams.active"],
                "streams.all": info["streams.all"],
                "streams.xepg": info["streams.xepg"],
                "url.dvr": info["url.dvr"],
                "url.m3u": f"{base_url(request, service)}/m3u/threadfin.m3u",
                "url.xepg": f"{base_url(request, service)}/xmltv/threadfin.xml",
                "version.api": API_VERSION,
                "version.threadfin": service.system.version,
            })  # fmt: skip
            response = {k: v for k, v in response.items() if v not in ("", 0)}  # Go: omitempty
        elif cmd in ("update.m3u", "update.xmltv"):
            await service.refresh_provider(cmd.removeprefix("update."))
        elif cmd == "update.xepg":
            await service.rebuild()
        else:
            return failed(INVALID_COMMAND)
        return JSONResponse(dict(sorted(response.items())))

    def ppv(active: bool) -> Handler:
        async def endpoint(request: Request) -> Response:
            await service.set_ppv(active)
            return Response(status_code=200, media_type="application/json")

        return endpoint

    async def data_images(request: Request) -> Response:
        try:
            name = logo_filename(request.path_params["name"])
        except AdminError:
            return go_error(404)
        path = service.system.folder_images_upload / name
        if name != request.path_params["name"] or not path.is_file():
            return go_error(404)
        return FileResponse(path, media_type=LOGO_TYPES[path.suffix.lower()], headers=image_headers(name))

    return [
        Route("/api/", api, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        Route("/ppv/enable", ppv(True), methods=["GET", "POST"]),
        Route("/ppv/disable", ppv(False), methods=["GET", "POST"]),
        Route("/data_images/{name}", data_images, methods=["GET", "HEAD"]),
    ]


__all__ = ["INVALID_COMMAND", "LOGIN_INCORRECT", "probe_json", "routes"]
