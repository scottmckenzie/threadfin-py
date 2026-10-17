"""The web UI under ``/web/`` (replaces Go's WebSocket UI): server-rendered pages, htmx for the dialogs.

Pages: status, playlists (M3U), XMLTV, filters, mapping, users, settings (with backup and
restore), log. Dialog forms load into one ``<dialog>``; a successful save answers with an empty body
and ``HX-Trigger`` events that close the dialog, reload the list and show a message. A refused save
re-renders the form with the error.

Login (with ``authentication.web`` on) works as in Go: the ``Token`` cookie, and a create-first-user
page while there are no users.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from starlette.background import BackgroundTask
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .access import (
    COOKIE,
    NotLoggedIn,
    auth_enabled,
    base_url,
    client_address,
    csrf_ok,
    current_user,
    request_token,
)
from .admin import CHANNEL_ATTRIBUTES, PROVIDER_KINDS, AdminError, save_logo
from .auth import LEVEL_WEB, AuthError, TooManyAttempts
from .backup import BackupError, create_backup
from .probe import InvalidProbeURLError
from .providers import ProviderError
from .service import Service
from .ui_text import CHANNEL_LABELS, SETTING_FIELDS, SETTINGS_PAGE, USER_LEVELS, SettingField
from .xepg import XEPGChannel, parse_float
from .xmltv import DUMMY_FILE

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")


def dom_id(value: str) -> str:
    """``value`` as part of an HTML id that is also a plain CSS ``#id`` selector (htmx looks up
    out-of-band targets that way): characters other than letters, digits and ``-`` become ``_<hex>``,
    so ``x-ID.12`` gives ``x-ID_2e12`` and different values never share an id."""
    return "".join(c if c.isascii() and (c.isalnum() or c == "-") else f"_{ord(c):x}" for c in value)


templates.env.filters["dom_id"] = dom_id
Handler = Callable[[Request], Awaitable[Response]]
MAX_ROWS = 500
ERRORS = (AdminError, AuthError, BackupError, ProviderError, InvalidProbeURLError)

PROVIDER_TITLES = {"m3u": "M3U playlist", "xmltv": "XMLTV guide"}


def events(**triggers: Any) -> dict[str, str]:
    return {"HX-Trigger": json.dumps(triggers)}


def done(message: str, *, refresh: bool = True, close: bool = True) -> Response:
    """A successful dialog save: close it, reload the list, say so."""
    triggers: dict[str, Any] = {"toast": message}
    if close:
        triggers["close-dialog"] = True
    if refresh:
        triggers["refresh-list"] = True
    return Response("", headers=events(**triggers))


def categories(service: Service) -> list[tuple[str, str]]:
    """``(label, value)`` from the ``epgCategories`` setting (``Kids:kids|News:news``)."""
    out = []
    for item in service.settings.epg_categories.split("|"):
        label, _, value = item.partition(":")
        if label and value:
            out.append((label, value))
    return out


def xmltv_files(service: Service) -> list[tuple[str, str]]:
    """``(file name, label)`` for the XMLTV file select (provider names, dummy guide last)."""
    records = service.manager.records("xmltv")
    files = []
    for name in service.xmltv_mapping:
        if name == DUMMY_FILE:
            continue
        record = records.get(name.removesuffix(".xml"), {})
        files.append((name, str(record.get("name") or name)))
    return [*sorted(files, key=lambda f: f[1].lower()), (DUMMY_FILE, DUMMY_FILE)]


MAX_MATCHES = 50


def xmltv_label(service: Service, file: str, channel_id: str) -> str:
    """``Display name (id)`` of a guide channel, ``""`` if the guide has no such channel."""
    info = service.xmltv_mapping.get(file, {}).get(channel_id)
    if info is None:
        return ""
    name = str(info.get("display-name") or channel_id)
    return name if name == channel_id else f"{name} ({channel_id})"


def picker_label(service: Service, file: str, channel_id: str) -> str:
    """What the picker shows: the guide channel, or the stored id if the guide lacks it now (the
    mapping is kept, not silently dropped)."""
    if channel_id in ("", "-"):
        return ""
    return xmltv_label(service, file, channel_id) or f"{channel_id} (not in this guide)"


def search_xmltv(service: Service, file: str, query: str, limit: int = MAX_MATCHES) -> list[tuple[str, str]]:
    """Guide channels whose name or id contains ``query``: exact matches first, then prefixes."""
    text = query.strip().lower()
    if not text:
        return []
    found = []
    for channel_id, info in service.xmltv_mapping.get(file, {}).items():
        name = str(info.get("display-name") or channel_id)
        keys = (name.lower(), channel_id.lower())
        if any(text in key for key in keys):
            rank = 0 if text in keys else 1 if any(key.startswith(text) for key in keys) else 2
            found.append((rank, name.lower(), channel_id, xmltv_label(service, file, channel_id)))
    found.sort()
    return [(channel_id, label) for _, _, channel_id, label in found[:limit]]


def channel_number(channel: XEPGChannel) -> float:
    number = parse_float(channel.x_channel_id)
    return number if number is not None else float("inf")


def form_bool(form: FormData, key: str) -> bool:
    return form.get(key) in ("on", "true", "1")


def form_text(form: FormData, key: str) -> str:
    value = form.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def settings_changes(form: FormData, fields: list[SettingField]) -> dict[str, Any]:
    """Form values -> typed settings changes (unchecked checkboxes are absent from a form)."""
    changes: dict[str, Any] = {}
    for field in fields:
        if field.kind == "bool":
            changes[field.key] = form_bool(form, field.key)
            continue
        text = form_text(form, field.key)
        if field.kind == "int":
            if not text.lstrip("-").isdigit():
                raise AdminError(f"{field.title}: {text!r} is not a number")
            changes[field.key] = int(text)
        elif field.kind == "times":
            changes[field.key] = [t for t in text.replace(";", ",").split(",") if t.strip()]
        else:
            changes[field.key] = text
    return changes


def submitted_settings(form: FormData, fields: list[SettingField]) -> dict[str, Any]:
    """The form as submitted, in the shape the settings form displays, to show it again unchanged
    when a save is refused (text as typed, even where it is not a valid number)."""
    values: dict[str, Any] = {}
    for field in fields:
        if field.kind == "bool":
            values[field.key] = form_bool(form, field.key)
        elif field.kind == "times":
            text = form_text(form, field.key)
            values[field.key] = [t.strip() for t in text.replace(";", ",").split(",") if t.strip()]
        else:
            values[field.key] = form_text(form, field.key)
    return values


def routes(service: Service) -> list[BaseRoute]:
    def render(request: Request, name: str, status: int = 200, **context: Any) -> HTMLResponse:
        context.update(
            service=service,
            settings=service.settings,
            xepg=service.xepg_mode,
            auth_on=auth_enabled(service),
            user=getattr(request.state, "user", None),
            username=service.auth.username(request.state.user) if getattr(request.state, "user", None) else "",
            version=service.system.version,
        )
        return templates.TemplateResponse(request, name, context, status_code=status)

    def login_redirect(request: Request) -> Response:
        target = "/web/first-user" if not service.auth.users else "/web/login"
        if "hx-request" in request.headers:
            return Response(status_code=401, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    def page(handler: Handler) -> Handler:
        """Session check (login page if needed) and CSRF check for changes."""

        async def wrapper(request: Request) -> Response:
            if not csrf_ok(request):
                return Response("Forbidden: missing HX-Request header\n", status_code=403)
            try:
                request.state.user = current_user(request, service)
            except NotLoggedIn:
                return login_redirect(request)
            return await handler(request)

        return wrapper

    # --- login ---------------------------------------------------------------------------------
    def set_session(request: Request, response: Response, token: str) -> Response:
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https", path="/")  # fmt: skip
        return response

    async def login(request: Request) -> Response:
        if not auth_enabled(service):
            return RedirectResponse("/web/", status_code=303)
        if not service.auth.users:
            return RedirectResponse("/web/first-user", status_code=303)
        if request.method == "GET":
            return render(request, "login.html")
        async with request.form() as form:
            username, password = form_text(form, "username"), str(form.get("password", ""))
        try:
            token = service.auth.login(username, password, client_address(request))
            service.auth.check_level(service.auth.session(token), LEVEL_WEB)
        except TooManyAttempts as exc:
            response = render(request, "login.html", 429, error=str(exc), username_value=username)
            response.headers["Retry-After"] = str(exc.retry_after)
            return response
        except AuthError:
            return render(request, "login.html", 401, error="User authentication failed", username_value=username)
        return set_session(request, RedirectResponse("/web/", status_code=303), token)

    async def first_user(request: Request) -> Response:
        if not auth_enabled(service) or service.auth.users:
            return RedirectResponse("/web/", status_code=303)
        if request.method == "GET":
            return render(request, "first_user.html")
        async with request.form() as form:
            username = form_text(form, "username")
            password, confirm = str(form.get("password", "")), str(form.get("confirm", ""))
        if password != confirm:
            return render(request, "first_user.html", 400, error="The passwords do not match", username_value=username)
        try:
            token = service.auth.create_first_user(username, password)
        except AuthError as exc:
            return render(request, "first_user.html", 400, error=str(exc), username_value=username)
        return set_session(request, RedirectResponse("/web/", status_code=303), token)

    async def logout(request: Request) -> Response:
        service.auth.logout(request_token(request))
        response = Response(status_code=204, headers={"HX-Redirect": "/web/login"})
        response.delete_cookie(COOKIE, path="/")
        return response

    # --- status, log, update ---------------------------------------------------------------------
    async def status_page(request: Request) -> Response:
        return render(request, "status.html", page="status", info=service.status(base_url(request, service)),
                      report=service.report)  # fmt: skip

    async def status_panel(request: Request) -> Response:
        return render(request, "partials/status_panel.html", info=service.status(base_url(request, service)),
                      report=service.report)  # fmt: skip

    async def update_all(request: Request) -> Response:
        if service.busy:
            return Response("", headers=events(toast="An update is already running"))
        report = await service.update()
        message = "Update finished" if report.ok and not report.provider_errors else "Update finished with errors"
        return Response("", headers=events(toast=message, **{"refresh-list": True}))

    async def rebuild(request: Request) -> Response:
        report = await service.rebuild()
        message = f"Rebuilt: {report.channels} channels" if report.ok else f"Build failed: {report.error}"
        return Response("", headers=events(toast=message, **{"refresh-list": True}))

    async def log_page(request: Request) -> Response:
        return render(request, "log.html", page="log", entries=service.log.entries())

    async def log_entries(request: Request) -> Response:
        return render(request, "partials/log_entries.html", entries=service.log.entries())

    async def log_clear(request: Request) -> Response:
        service.log.clear()
        return Response("", headers=events(toast="Log cleared", **{"refresh-list": True}))

    # --- providers -------------------------------------------------------------------------------
    def kind_of(request: Request) -> str:
        kind = str(request.path_params["kind"])
        if kind not in PROVIDER_KINDS:
            raise KeyError(kind)
        return kind

    async def providers_page(request: Request) -> Response:
        kinds = ["xmltv"] if request.url.path.rstrip("/").endswith("xmltv") else ["m3u"]
        return render(request, "providers.html", page=kinds[0] if kinds == ["xmltv"] else "playlists",
                      kinds=kinds, titles=PROVIDER_TITLES)  # fmt: skip

    async def provider_list(request: Request) -> Response:
        kind = kind_of(request)
        return render(request, "partials/provider_list.html", kind=kind, titles=PROVIDER_TITLES,
                      records=service.manager.records(kind), errors=service.report.provider_errors)  # fmt: skip

    def provider_form(request: Request, kind: str, provider_id: str, data: Mapping[str, Any],
                      error: str = "") -> Response:  # fmt: skip
        return render(request, "partials/provider_form.html", kind=kind, provider_id=provider_id, data=data,
                      title=PROVIDER_TITLES[kind], error=error)  # fmt: skip

    async def provider_new(request: Request) -> Response:
        kind = kind_of(request)
        return provider_form(request, kind, "", {"tuner": 1, "buffer": "-"})

    async def provider_edit(request: Request) -> Response:
        kind, provider_id = kind_of(request), request.path_params["id"]
        return provider_form(request, kind, provider_id, service.manager.records(kind)[provider_id])

    def provider_data(kind: str, form: FormData) -> dict[str, Any]:
        keys = ["name", "description", "file.source", "http_proxy.ip", "http_proxy.port"]
        if kind == "m3u":
            keys += ["tuner", "http_headers.origin", "http_headers.referer"]
        return {key: form_text(form, key) for key in keys}

    async def provider_save(request: Request) -> Response:
        kind = kind_of(request)
        provider_id = request.path_params.get("id", "")
        async with request.form() as form:
            data = provider_data(kind, form)
        try:
            if provider_id:
                if not await service.edit_provider(kind, provider_id, data):
                    raise KeyError(provider_id)
                return done("Saved")
            result = await service.add_provider(kind, data)
        except ERRORS as exc:
            return provider_form(request, kind, provider_id, data, str(exc))
        return done(f"Added: {result.streams} streams" if kind != "xmltv" else "Added")

    async def provider_refresh(request: Request) -> Response:
        kind, provider_id = kind_of(request), request.path_params["id"]
        [result] = await service.refresh_provider(kind, provider_id)
        message = f"Updated: {result.streams} streams" if result.ok else f"Update failed: {result.error}"
        if result.ok and result.skipped:
            message += f" ({len(result.skipped)} entries skipped)"
        return done(message, close=False)

    async def provider_delete(request: Request) -> Response:
        await service.delete_provider(kind_of(request), request.path_params["id"])
        return done("Deleted")

    # --- filters ---------------------------------------------------------------------------------
    async def filters_page(request: Request) -> Response:
        return render(request, "filters.html", page="filters")

    async def filter_list(request: Request) -> Response:
        rows = sorted(service.settings.filter.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)
        return render(request, "partials/filter_list.html", filters=rows)

    def filter_form(request: Request, filter_id: str, data: Mapping[str, Any], error: str = "") -> Response:
        return render(request, "partials/filter_form.html", filter_id=filter_id, data=data, error=error,
                      groups=service.stream_db.groups, categories=categories(service))  # fmt: skip

    async def filter_new(request: Request) -> Response:
        kind = request.query_params.get("type", "group-title")
        return filter_form(request, "", {"type": kind, "active": True, "startingNumber": "1000"})

    async def filter_edit(request: Request) -> Response:
        filter_id = request.path_params["id"]
        data = service.settings.filter[filter_id]
        if request.query_params.get("type"):
            data = {**data, "type": request.query_params["type"]}
        return filter_form(request, filter_id, data)

    async def filter_save(request: Request) -> Response:
        filter_id = request.path_params.get("id", "")
        async with request.form() as form:
            kind = form_text(form, "type")
            data: dict[str, Any] = {
                "type": kind, "name": form_text(form, "name"), "description": form_text(form, "description"),
                "filter": form_text(form, "filter"), "active": form_bool(form, "active"),
                "caseSensitive": form_bool(form, "caseSensitive"),
                "startingNumber": form_text(form, "startingNumber"), "x-category": form_text(form, "x-category"),
            }  # fmt: skip
            if kind == "group-title":
                data |= {"include": form_text(form, "include"), "exclude": form_text(form, "exclude"),
                         "liveEvent": form_bool(form, "liveEvent")}  # fmt: skip
        try:
            await service.change_filters({filter_id or "-1": data})
        except ERRORS as exc:
            return filter_form(request, filter_id, data, str(exc))
        return done("Saved")

    async def filter_delete(request: Request) -> Response:
        await service.change_filters({request.path_params["id"]: {"delete": True}})
        return done("Deleted")

    # --- mapping ---------------------------------------------------------------------------------
    async def mapping_page(request: Request) -> Response:
        groups = sorted({c.x_group_title for c in service.channels.values() if c.x_group_title}, key=str.lower)
        return render(request, "mapping.html", page="mapping", groups=groups, categories=categories(service))

    def mapping_rows_for(query: Mapping[str, str]) -> tuple[list[tuple[str, XEPGChannel]], int]:
        text = query.get("q", "").strip().lower()
        group, state = query.get("group", ""), query.get("state", "active")
        rows = []
        by_state: dict[str, Callable[[XEPGChannel], bool]] = {
            "active": lambda c: c.x_active and not c.x_hide_channel,
            "inactive": lambda c: not c.x_active and not c.x_hide_channel,
            "hidden": lambda c: c.x_hide_channel,
            "all": lambda c: True,  # also any other state
        }
        wanted = by_state.get(state, by_state["all"])
        for channel_id, channel in service.channels.items():
            if not wanted(channel):
                continue
            if group and channel.x_group_title != group:
                continue
            haystack = f"{channel.x_name} {channel.name} {channel.tvg_id} {channel.x_channel_id}".lower()
            if text and text not in haystack:
                continue
            rows.append((channel_id, channel))
        rows.sort(key=lambda r: (channel_number(r[1]), r[1].x_name.lower()))
        return rows[:MAX_ROWS], len(rows)

    def mapping_context(rows: list[tuple[str, XEPGChannel]]) -> dict[str, Any]:
        names = {
            file: {cid: str(info.get("display-name") or cid) for cid, info in channels.items()}
            for file, channels in service.xmltv_mapping.items()
        }
        providers = {name: label for name, label in xmltv_files(service)}
        return {"names": names, "providers": providers,
                "probes": service.probe_results([cid for cid, _ in rows])}  # fmt: skip

    async def mapping_rows(request: Request) -> Response:
        rows, total = mapping_rows_for(request.query_params)
        return render(request, "partials/mapping_rows.html", rows=rows, total=total, limit=MAX_ROWS,
                      **mapping_context(rows))  # fmt: skip

    def channel_form(request: Request, channel_id: str, channel: XEPGChannel, error: str = "") -> Response:
        file = channel.x_xmltv_file
        probe = service.probe_results([channel_id]).get(channel_id)
        others = sorted({c.x_name for cid, c in service.channels.items() if cid != channel_id and c.x_name})
        return render(request, "partials/channel_form.html", channel_id=channel_id, c=channel, error=error,
                      labels=CHANNEL_LABELS, files=xmltv_files(service), categories=categories(service),
                      probe=probe, other_names=others, file=file, selected=channel.x_mapping,
                      label=picker_label(service, file, channel.x_mapping))  # fmt: skip

    async def channel_edit(request: Request) -> Response:
        channel_id = request.path_params["id"]
        return channel_form(request, channel_id, service.channels[channel_id])

    async def channel_save(request: Request) -> Response:
        channel_id = request.path_params["id"]
        channel = service.channels[channel_id]
        async with request.form() as form:
            data: dict[str, Any] = {key: form_text(form, key) for key in (
                "x-channelID", "x-name", "x-description", "tvg-logo", "x-category", "x-group-title",
                "x-xmltv-file", "x-mapping", "x-ppv-extra",
                "x-backup-channel-1", "x-backup-channel-2", "x-backup-channel-3",
            )}  # fmt: skip
            for key in ("x-active", "x-hide-channel", "x-update-channel-name", "x-update-channel-icon"):
                data[key] = form_bool(form, key)
        try:
            await service.edit_channel(channel_id, data)
        except ERRORS as exc:
            preview = channel.copy()  # what was entered, so nothing is lost
            for key, value in data.items():
                setattr(preview, CHANNEL_ATTRIBUTES[key], value)
            return channel_form(request, channel_id, preview, str(exc))
        return done("Saved")

    async def xmltv_picker(request: Request) -> Response:
        """The XMLTV channel picker after another guide file was chosen: the guide channel with the
        stream's tvg-id is preselected if the file has one (as in Go)."""
        file = request.query_params.get("x-xmltv-file", "")
        channel = service.channels[request.query_params.get("channel", "")]
        selected = channel.tvg_id if xmltv_label(service, file, channel.tvg_id) else "-"
        return render(request, "partials/xmltv_picker.html", file=file, selected=selected,
                      label=picker_label(service, file, selected))  # fmt: skip

    async def xmltv_search(request: Request) -> Response:
        """Matches for what was typed in the picker; nothing for an empty search."""
        query = request.query_params
        matches = search_xmltv(service, query.get("x-xmltv-file", ""), query.get("q", ""))
        return render(request, "partials/xmltv_matches.html", matches=matches, query=query.get("q", "").strip(),
                      limit=MAX_MATCHES)  # fmt: skip

    async def channel_probe(request: Request) -> Response:
        channel_id = request.path_params["id"]
        try:
            record = await service.probe_channel(channel_id, force=True)
        except ERRORS as exc:
            return render(request, "partials/probe_result.html", error=str(exc), probe=None)
        # the result for the dialog, and the channel's Probe cell in the list behind it (out-of-band)
        return render(request, "partials/probe_done.html", probe=record, error="", id=channel_id, p=record)

    async def mapping_bulk(request: Request) -> Response:
        async with request.form() as form:
            ids = [i for i in form.getlist("ids") if isinstance(i, str)]
            action, value = form_text(form, "action"), form_text(form, "value")
        if not ids:
            return Response("", headers=events(toast="Select channels first"))
        actions: dict[str, dict[str, Any]] = {
            "activate": {"x-active": True}, "deactivate": {"x-active": False},
            "hide": {"x-hide-channel": True}, "unhide": {"x-hide-channel": False},
            "category": {"x-category": value}, "group": {"x-group-title": value},
            "renumber": {"x-channels-start": value},
        }  # fmt: skip
        changes = actions.get(action, {})
        if not changes:
            return Response("", headers=events(toast="Choose an action"))
        try:
            count = await service.edit_channels(ids, changes)
        except ERRORS as exc:
            return Response("", headers=events(toast=str(exc)))
        return done(f"{count} channels changed", close=False)

    async def logo_upload(request: Request) -> Response:
        async with request.form() as form:
            upload = form.get("logo")
            current = form_text(form, "tvg-logo")
            if not isinstance(upload, UploadFile):
                return render(request, "partials/logo_field.html", value=current, logo_error="Choose a file")
            name, data = upload.filename or "", await upload.read()
        try:
            filename = await asyncio.to_thread(save_logo, service.system, name, data)
        except ERRORS as exc:
            return render(request, "partials/logo_field.html", value=current, logo_error=str(exc))
        url = f"{base_url(request, service)}/data_images/{filename}"
        return render(request, "partials/logo_field.html", value=url)

    # --- users -----------------------------------------------------------------------------------
    async def users_page(request: Request) -> Response:
        return render(request, "users.html", page="users")

    async def user_list(request: Request) -> Response:
        users = [(uid, service.auth.read_user_data(uid)) for uid in service.auth.users]
        users.sort(key=lambda u: str(u[1].get("username", "")).lower())
        return render(request, "partials/user_list.html", users=users, levels=USER_LEVELS)

    def user_form(request: Request, user_id: str, data: Mapping[str, Any], error: str = "") -> Response:
        return render(request, "partials/user_form.html", user_id=user_id, data=data, error=error,
                      levels=USER_LEVELS)  # fmt: skip

    async def user_new(request: Request) -> Response:
        return user_form(request, "", {LEVEL_WEB: True})

    async def user_edit(request: Request) -> Response:
        user_id = request.path_params["id"]
        return user_form(request, user_id, service.auth.read_user_data(user_id))

    async def user_save(request: Request) -> Response:
        user_id = request.path_params.get("id", "")
        async with request.form() as form:
            username = form_text(form, "username")
            password, confirm = str(form.get("password", "")), str(form.get("confirm", ""))
            levels = {key: form_bool(form, key) for key, _ in USER_LEVELS}
        data: dict[str, Any] = {"username": username, **levels}
        try:
            if password != confirm:
                raise AdminError("The passwords do not match")
            if not username:
                raise AdminError("Username must not be empty")
            if user_id:
                if user_id not in service.auth.users:
                    raise KeyError(user_id)
                merged = {**service.auth.read_user_data(user_id), **data, "password": password}  # "" = unchanged
                if user_id == request.state.user and not levels[LEVEL_WEB]:
                    raise AdminError("You cannot remove your own web access")
                service.auth.save_user_data({user_id: merged})
            else:
                service.auth.save_new_user({**data, "password": password})
        except ERRORS as exc:
            return user_form(request, user_id, data, str(exc))
        return done("Saved")

    async def user_delete(request: Request) -> Response:
        user_id = request.path_params["id"]
        if user_id == request.state.user:
            return Response("", headers=events(toast="You cannot delete your own account"))
        if len(service.auth.users) == 1 and auth_enabled(service):
            return Response("", headers=events(toast="The last user cannot be deleted while login is required"))
        service.auth.remove_user(user_id)
        return done("Deleted")

    # --- settings, backup ------------------------------------------------------------------------
    def settings_form(
        request: Request,
        message: str = "",
        error: str = "",
        status: int = 200,
        submitted: Mapping[str, Any] | None = None,
    ) -> Response:
        """The settings form: the saved values, or after a refused save what was submitted (so the
        user's other edits are not lost; nothing was saved)."""
        values = {**service.settings.to_json_dict(), **(submitted or {})}
        return render(request, "settings.html" if "hx-request" not in request.headers else
                      "partials/settings_form.html", status, page="settings", sections=SETTINGS_PAGE,
                      values=values, message=message, error=error)  # fmt: skip

    async def settings_page(request: Request) -> Response:
        return settings_form(request)

    async def settings_save(request: Request) -> Response:
        fields = list(SETTING_FIELDS.values())
        async with request.form() as form:
            submitted = submitted_settings(form, fields)
            try:
                changes = settings_changes(form, fields)
            except AdminError as exc:
                return settings_form(request, error=str(exc), submitted=submitted)
        try:
            changed = await service.change_settings(changes)
        except ERRORS as exc:
            return settings_form(request, error=str(exc), submitted=submitted)
        response = settings_form(request, message="Saved" if changed else "No changes")
        if "authentication.web" in changed:
            response.headers["HX-Refresh"] = "true"  # menu and login change
        return response

    async def backup_download(request: Request) -> Response:
        path = await asyncio.to_thread(create_backup, service.system)
        return FileResponse(path, media_type="application/zip", filename=path.name,
                            background=BackgroundTask(path.unlink, missing_ok=True))  # fmt: skip

    async def restore(request: Request) -> Response:
        async with request.form() as form:
            upload = form.get("archive")
            data = await upload.read() if isinstance(upload, UploadFile) else b""
        if not data:
            return render(request, "partials/restore_result.html", error="Choose a backup file")
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            result = await service.restore(tmp)
        except ERRORS as exc:
            return render(request, "partials/restore_result.html", error=str(exc))
        finally:
            await asyncio.to_thread(Path(tmp).unlink, missing_ok=True)
        return render(request, "partials/restore_result.html", error="",
                      web_url=result.web_url(base_url(request, service)))  # fmt: skip

    def not_found(handler: Handler) -> Handler:
        async def wrapper(request: Request) -> Response:
            try:
                return await handler(request)
            except KeyError:
                return Response("Not found\n", status_code=404)

        return wrapper

    pages: list[tuple[str, Handler, list[str]]] = [
        ("/", status_page, ["GET"]),
        ("/status/panel", status_panel, ["GET"]),
        ("/update", update_all, ["POST"]),
        ("/rebuild", rebuild, ["POST"]),
        ("/log", log_page, ["GET"]),
        ("/log/entries", log_entries, ["GET"]),
        ("/log/clear", log_clear, ["POST"]),
        ("/playlists", providers_page, ["GET"]),
        ("/xmltv", providers_page, ["GET"]),
        ("/providers/{kind}/list", provider_list, ["GET"]),
        ("/providers/{kind}/new", provider_new, ["GET"]),
        ("/providers/{kind}", provider_save, ["POST"]),
        ("/providers/{kind}/{id}/edit", provider_edit, ["GET"]),
        ("/providers/{kind}/{id}", provider_save, ["POST"]),
        ("/providers/{kind}/{id}/refresh", provider_refresh, ["POST"]),
        ("/providers/{kind}/{id}/delete", provider_delete, ["POST"]),
        ("/filters", filters_page, ["GET"]),
        ("/filters/list", filter_list, ["GET"]),
        ("/filters/new", filter_new, ["GET"]),
        ("/filters", filter_save, ["POST"]),
        ("/filters/{id}/edit", filter_edit, ["GET"]),
        ("/filters/{id}", filter_save, ["POST"]),
        ("/filters/{id}/delete", filter_delete, ["POST"]),
        ("/mapping", mapping_page, ["GET"]),
        ("/mapping/rows", mapping_rows, ["GET"]),
        ("/mapping/xmltv-picker", xmltv_picker, ["GET"]),
        ("/mapping/xmltv-search", xmltv_search, ["GET"]),
        ("/mapping/bulk", mapping_bulk, ["POST"]),
        ("/mapping/{id}/edit", channel_edit, ["GET"]),
        ("/mapping/{id}", channel_save, ["POST"]),
        ("/mapping/{id}/probe", channel_probe, ["POST"]),
        ("/logo", logo_upload, ["POST"]),
        ("/users", users_page, ["GET"]),
        ("/users/list", user_list, ["GET"]),
        ("/users/new", user_new, ["GET"]),
        ("/users", user_save, ["POST"]),
        ("/users/{id}/edit", user_edit, ["GET"]),
        ("/users/{id}", user_save, ["POST"]),
        ("/users/{id}/delete", user_delete, ["POST"]),
        ("/settings", settings_page, ["GET"]),
        ("/settings", settings_save, ["POST"]),
        ("/backup", backup_download, ["GET"]),
        ("/restore", restore, ["POST"]),
    ]
    by_path: dict[str, dict[str, Handler]] = {}
    for path, handler, methods in pages:
        for method in methods:
            by_path.setdefault(path, {})[method] = handler

    def dispatch(handlers: dict[str, Handler]) -> Handler:
        async def endpoint(request: Request) -> Response:
            method = "GET" if request.method == "HEAD" else request.method
            handler = handlers.get(method)
            if handler is None:
                return Response("Method not allowed\n", status_code=405)
            return await handler(request)

        return endpoint

    def web_root(request: Request) -> Response:
        return RedirectResponse("/web/", status_code=307)

    out: list[BaseRoute] = [
        Mount("/web/static", app=StaticFiles(directory=HERE / "static"), name="static"),
        Route("/web/login", login, methods=["GET", "POST"]),
        Route("/web/first-user", first_user, methods=["GET", "POST"]),
        Route("/web/logout", page(logout), methods=["POST"]),
        Route("/web", web_root, methods=["GET", "HEAD"]),
    ]
    for path, handlers in by_path.items():
        methods = sorted(set(handlers) | ({"HEAD"} if "GET" in handlers else set()))
        out.append(Route("/web" + path, page(not_found(dispatch(handlers))), methods=methods))
    return out


__all__ = ["routes", "settings_changes", "templates"]
