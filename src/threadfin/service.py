"""The application core: ties providers, filters, channel database, guide and playlist together.

``Service.build()`` is Go's ``buildXEPG``: provider copies -> stream database -> channel database
(``xepg.json``) -> ``threadfin.xml`` (+ ``.gz``) -> stream registry (``urls.json``). The playlist
is generated per request from the channel database, so the links in it always carry the host the
client used to reach this server (Go baked one address into ``threadfin.m3u`` at build time).
``Service.update()`` is the manual "download all providers, then build"; ``scheduled_update()``
(Go ``maintenance``) makes an automatic backup first, and ``start()`` (Go ``StartSystem``) runs it
on start-up when ``files.update`` is on. ``restore()`` unpacks a backup and reloads everything.

The "management" methods (settings, providers, filters, channels, PPV, probes) are what the web UI
and REST API call: they validate with ``admin.py``, change state under the build lock, save, and
rebuild when the guide or stream registry depends on the change.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from . import admin
from .auth import Authentication
from .backup import AutoBackupResult, BackupError, RestoreResult, auto_backup, restore_backup
from .epg import EPGOptions, XMLTVStats, write_xmltv
from .failover import Failover
from .filters import FilterError
from .hdhr import Device, PMSIds, lineup_pms, lineup_xepg
from .imagecache import ImageCache, ImageSession, default_base
from .logbuffer import LogBuffer
from .playlist import PlaylistOptions, StreamInfo, StreamRegistry, build_playlist
from .playlist import stream_id as stream_id_of
from .probe import resolve_ffprobe
from .probe_service import ProbeService
from .probe_store import ProbeRecord, ProbeStore
from .providers import ProviderManager, RefreshResult
from .settings import Settings, load_settings, save_settings
from .streams_db import StreamDatabase, build_stream_database
from .system import SystemInfo
from .xepg import PPV, XEPGChannel, build_xepg, go_g, load_xepg, parse_float, save_xepg
from .xmltv import ChannelMapping, build_mapping, identity_image_url, make_program_counter

XMLTV_FILE = "threadfin.xml"
API_VERSION = "1.2.40"  # Go's, so API clients see a version they know

log = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(slots=True)
class BuildReport:
    ok: bool = True
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    provider_errors: dict[str, str] = field(default_factory=dict)
    xmltv_errors: dict[str, str] = field(default_factory=dict)
    channels: int = 0  # active, visible channels
    created: int = 0
    removed: int = 0
    programmes: int = 0
    streams: int = 0


class Service:
    def __init__(self, system: SystemInfo, settings: Settings, manager: ProviderManager | None = None) -> None:
        self.system = system
        self.settings = settings
        self.manager = manager or ProviderManager(
            settings, system.folder_data, self.save_settings, app_name=system.app_name
        )
        self.stream_db = StreamDatabase()
        self.failover = Failover()
        self.xmltv_mapping: dict[str, ChannelMapping] = {}
        self.report = BuildReport()
        self.log = LogBuffer(settings.log_entries_ram)
        self._lock = threading.Lock()
        self.building = False
        self.updating = False
        self._probes: ProbeService | None = None
        self.images = ImageCache(system.folder_images_cache, user_agent=lambda: self.settings.user_agent)
        self._image_session: ImageSession | None = None  # lookups of the last guide written
        self.image_task: asyncio.Task[None] | None = None
        self.caching_images = False
        self._load_state()

    def _load_state(self) -> None:
        """Users, stream registry and channel database from the config folder."""
        self.auth = Authentication(self.system.file_authentication)
        self.registry = StreamRegistry(self.system.file_urls)
        self.channels: dict[str, XEPGChannel] = {}
        if self.system.file_xepg.exists():
            self.channels = load_xepg(str(self.system.file_xepg))

    def reload(self) -> None:
        """Re-read every file from the config folder (after a restore).

        The settings object is updated in place: the provider manager and the web app hold it.
        """
        admin.copy_settings(load_settings(self.system), self.settings)
        self._load_state()

    @property
    def busy(self) -> bool:
        """A download or build is running (Go ``ScanInProgress``)."""
        return self.building or self.updating

    # --- settings -------------------------------------------------------------
    def save_settings(self) -> None:
        save_settings(self.settings, self.system)

    @property
    def xmltv_path(self) -> Path:
        return self.system.folder_data / XMLTV_FILE

    # --- building ---------------------------------------------------------------
    def build(self, now: datetime | None = None) -> BuildReport:
        """Rebuild channel database, guide and stream registry from the provider copies on disk."""
        with self._lock:  # one build at a time; the web side only reads finished state
            report = BuildReport()
            self.building = True
            try:
                self._build(report, (now or datetime.now().astimezone()))
            except FilterError as exc:
                report.ok, report.error = False, f"filter: {exc}"
            finally:
                self.building = False
            self.report = report
            return report

    @property
    def xepg_mode(self) -> bool:
        """``epgSource`` XEPG builds guide + playlist; PMS only serves the lineup."""
        return self.settings.epg_source == "XEPG"

    def _build(self, report: BuildReport, now: datetime) -> None:
        settings, system = self.settings, self.system
        self._image_session = None
        db = build_stream_database(self.manager, settings)
        self.stream_db = db
        report.warnings += db.warnings
        report.provider_errors = dict(db.errors)
        report.streams = len(db.all)

        if not self.xepg_mode:
            self._register_streams(db)
            return

        files = [(path.name, path) for path in self.manager.local_files("xmltv") if path.exists()]
        mapping, errors = build_mapping(files)
        self.xmltv_mapping = mapping
        report.xmltv_errors.update(errors)

        provider_ids = set(self.manager.records("m3u"))
        result = build_xepg(
            self.channels,
            db.active,
            mapping_first_channel=settings.mapping_first_channel,
            filters=settings.filter,
            xmltv_mapping=mapping,
            provider_ids=provider_ids,
            program_count=make_program_counter(system.folder_data),
        )
        self.channels = result.channels
        report.warnings += result.warnings
        report.created, report.removed = len(result.created), len(result.removed_duplicates) + len(result.removed_stale)
        save_xepg(str(system.file_xepg), self.channels)

        stats = self._write_guide(now)
        report.xmltv_errors.update(stats.errors)
        report.programmes, report.channels = stats.programmes, stats.channels

        self._register_streams(db)

    def _write_guide(self, now: datetime) -> XMLTVStats:
        """threadfin.xml (+ .gz); with ``cache.images`` the images go through the image cache."""
        settings, system = self.settings, self.system
        session = self.images.session(default_base(settings)) if settings.cache_images else None
        self._image_session = session
        options = EPGOptions(
            enable_non_ascii=settings.enable_non_ascii,
            replace_missing_images=settings.xepg_replace_missing_images,
            replace_channel_title=settings.xepg_replace_channel_title,
            image_url=session.url if session else identity_image_url,
            generator=system.name,
            source=f"{system.name} - {system.version}",
        )
        return write_xmltv(
            self.xmltv_path, self.channels, system.folder_data, options, now,
            gzip_path=str(self.xmltv_path) + ".gz",
        )  # fmt: skip

    # --- image cache ---------------------------------------------------------------
    def _schedule_image_caching(self) -> None:
        """After a build: download missing images in the background (Go ``Image.Caching``)."""
        session = self._image_session
        if session is None or self.caching_images:
            return
        self.caching_images = True
        self.image_task = asyncio.create_task(self._cache_images(session), name="image-cache")

    async def _cache_images(self, session: ImageSession) -> None:
        try:
            downloaded, failed = await asyncio.to_thread(self.images.fill, session)

            def finish() -> int:
                if not self.settings.cache_images:  # switched off meanwhile: nothing may stay behind
                    removed = len(list(self.images.folder.glob("*"))) if self.images.folder.is_dir() else 0
                    self.images.clear()
                    return removed
                if self._image_session is not session:
                    return 0  # a newer build wrote the guide; its own run follows
                if any(self.images.has(name) for name in session.wanted):
                    self._write_guide(datetime.now().astimezone())  # now with the cached URLs
                current = self._image_session
                return self.images.prune(current.referenced if current else set())

            removed = await self._locked(finish)
            if downloaded or failed or removed:
                log.info("image cache: %d downloaded, %d failed, %d removed", downloaded, failed, removed)
        except Exception:
            log.exception("image cache failed")
        finally:
            self.caching_images = False
        if self._image_session is not None and self._image_session is not session:
            self._schedule_image_caching()  # a build happened while this one ran

    def _register_streams(self, db: StreamDatabase) -> None:
        """The registry starts empty on every build: ids of vanished channels stop resolving."""
        fresh = StreamRegistry()
        if self.xepg_mode:
            build_playlist(self.channels, PlaylistOptions(group_labels=tuple(g.label for g in db.groups)), fresh)
            lineup_xepg(self.channels, fresh, "")
        else:
            lineup_pms(db.active, PMSIds(self.system.file_pms), fresh, "")
        self.registry.replace_with(fresh)
        self.registry.save()

    async def update(self) -> BuildReport:
        """Download every provider, then rebuild."""
        self.updating = True
        try:
            results: list[RefreshResult] = []
            for kind in ("m3u", "xmltv") if self.xepg_mode else ("m3u",):
                results += await self.manager.refresh(kind)
            report = await asyncio.to_thread(self.build)
        finally:
            self.updating = False
        self._schedule_image_caching()
        for result in results:
            if not result.ok:
                report.provider_errors[result.provider_id] = result.error or "download failed"
        self.report = report
        return report

    # --- maintenance --------------------------------------------------------------
    def auto_backup(self) -> AutoBackupResult:
        return auto_backup(self.system, self.settings)

    def clear_image_cache(self) -> None:
        """Go empties the image cache before every scheduled build unless ``cache.images`` is on."""
        folder = self.system.folder_images_cache
        if self.settings.cache_images or self.caching_images or not folder.is_dir():
            return
        for child in folder.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    async def scheduled_update(self) -> BuildReport:
        """Automatic backup, then download and build (Go ``maintenance`` / ``StartSystem(true)``).

        A failed backup is reported as a warning and does not stop the update, as in Go.
        """
        backup_warning = ""
        try:
            await asyncio.to_thread(self.auto_backup)
        except OSError as exc:
            backup_warning = f"backup: {exc}"
        await asyncio.to_thread(self.clear_image_cache)
        report = await self.update()
        if backup_warning:
            report.warnings.insert(0, backup_warning)
        return report

    async def start(self) -> BuildReport:
        """Start-up: update from the providers if ``files.update`` is on and there are any, else
        build from the local copies."""
        if self.settings.files_update and self.manager.records("m3u"):
            return await self.scheduled_update()
        return await self.rebuild()

    async def restore(self, archive: str | os.PathLike[str]) -> RestoreResult:
        """Restore a backup, reload everything and update (Go ``ThreadfinRestore``).

        A changed port only takes effect after a restart (``RestoreResult.web_url``).
        """
        if self.busy:
            raise BackupError("an update is running, try again when it has finished")

        def unpack() -> RestoreResult:
            with self._lock:  # no build while the files change under it
                result = restore_backup(self.system, archive, self.settings.port)
                self.reload()
                return result

        result = await asyncio.to_thread(unpack)
        await self.scheduled_update()
        return result

    # --- management (web UI / REST API) ---------------------------------------------
    async def _locked(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` in a thread while no build is running."""

        def run() -> T:
            with self._lock:
                return fn()

        return await asyncio.to_thread(run)

    async def rebuild(self) -> BuildReport:
        report = await asyncio.to_thread(self.build)
        if not report.ok:
            log.error("build failed: %s", report.error)
        self._schedule_image_caching()
        return report

    async def change_settings(self, changes: Mapping[str, Any]) -> set[str]:
        """Settings page (Go ``updateServerSettings``); returns the keys that changed."""

        def apply() -> set[str]:
            new, changed = admin.update_settings(self.settings, changes)
            admin.copy_settings(new, self.settings)
            self.save_settings()
            return changed

        changed = await self._locked(apply)
        self.log.resize(self.settings.log_entries_ram)
        if changed:
            log.info("settings saved: %s", ", ".join(sorted(changed)))
        if "cache.images" in changed and not self.settings.cache_images:
            await asyncio.to_thread(self.clear_image_cache)
        if changed & admin.REBUILD_KEYS:
            await self.rebuild()
        return changed

    async def change_filters(self, changes: Mapping[str, Mapping[str, Any]]) -> list[str]:
        """Filter page (Go ``saveFilter``); returns the ids of new filters. Rebuilds."""

        def apply() -> list[str]:
            filters, new_ids = admin.apply_filter_changes(self.settings.filter, changes)
            self.settings.filter = filters
            self.save_settings()
            return new_ids

        new_ids = await self._locked(apply)
        await self.rebuild()
        return new_ids

    async def add_provider(self, kind: str, data: Mapping[str, Any]) -> RefreshResult:
        """New playlist / tuner / guide: downloaded first, kept only if that works. Rebuilds."""
        record = admin.provider_changes(kind, data, new=True)
        result = await self.manager.add(kind, record)
        log.info("%s provider %s added (%d streams)", kind, result.provider_id, result.streams)
        await self.rebuild()
        return result

    async def edit_provider(self, kind: str, provider_id: str, data: Mapping[str, Any]) -> bool:
        """Change a provider's fields (no download, as in Go)."""
        changes = admin.provider_changes(kind, data, new=False)
        return await self.manager.update(kind, provider_id, changes)

    async def delete_provider(self, kind: str, provider_id: str) -> bool:
        if kind not in admin.PROVIDER_KINDS:
            raise admin.AdminError(f"unknown provider type {kind!r}")
        removed = await self.manager.remove(kind, provider_id)
        if removed:
            log.info("%s provider %s deleted", kind, provider_id)
            await self.rebuild()
        return removed

    async def refresh_provider(self, kind: str, provider_id: str | None = None) -> list[RefreshResult]:
        """Download one provider (or all of one kind) again, then rebuild (Go ``updateFile``)."""
        if kind not in admin.PROVIDER_KINDS:
            raise admin.AdminError(f"unknown provider type {kind!r}")
        self.updating = True
        try:
            results = await self.manager.refresh(kind, provider_id)
        finally:
            self.updating = False
        for result in results:
            if result.ok:
                log.info("%s provider %s updated (%d streams)", kind, result.provider_id, result.streams)
            else:
                log.warning("%s provider %s: %s", kind, result.provider_id, result.error)
        await self.rebuild()
        return results

    def channel(self, channel_id: str) -> XEPGChannel | None:
        return self.channels.get(channel_id)

    async def edit_channel(self, channel_id: str, data: Mapping[str, Any]) -> XEPGChannel:
        """Mapping dialog (Go ``saveXEpgMapping``, which replaced the whole database with the
        browser's copy). Raises ``KeyError`` for an unknown channel. Rebuilds."""
        changes = admin.channel_changes(data, self.xmltv_mapping)

        def apply() -> XEPGChannel:
            updated = admin.apply_channel_changes(self.channels[channel_id], changes)
            admin.check_mapping(updated, self.xmltv_mapping)
            self.channels = {**self.channels, channel_id: updated}
            save_xepg(str(self.system.file_xepg), self.channels)
            return updated

        updated = await self._locked(apply)
        await self.rebuild()
        return self.channels.get(channel_id, updated)

    async def edit_channels(self, channel_ids: Iterable[str], data: Mapping[str, Any]) -> int:
        """Bulk edit; ``x-channels-start`` numbers the channels from there, in their current order."""
        changes = admin.bulk_channel_changes(data)
        start = changes.pop("x-channels-start", None)
        ids = list(dict.fromkeys(channel_ids))

        def apply() -> int:
            missing = [i for i in ids if i not in self.channels]
            if missing:
                raise admin.AdminError(f"unknown channels: {', '.join(missing)}")
            updated = {i: admin.apply_channel_changes(self.channels[i], changes) for i in ids}
            if start is not None:
                order = sorted(ids, key=lambda i: (parse_float(updated[i].x_channel_id) or 0, i))
                for offset, i in enumerate(order):
                    updated[i].x_channel_id = go_g(start + offset)
            for channel in updated.values():
                admin.check_mapping(channel, self.xmltv_mapping)
            self.channels = {**self.channels, **updated}
            save_xepg(str(self.system.file_xepg), self.channels)
            return len(updated)

        count = await self._locked(apply)
        await self.rebuild()
        return count

    async def set_ppv(self, active: bool) -> int:
        """``/ppv/enable`` activates every channel mapped to the PPV dummy guide, ``/ppv/disable``
        deactivates them. Returns the number changed. Rebuilds."""

        def apply() -> int:
            changed = {}
            for channel_id, channel in self.channels.items():
                if channel.x_mapping == PPV and channel.x_active != active:
                    updated = channel.copy()
                    updated.x_active = active
                    changed[channel_id] = updated
            if changed:
                self.channels = {**self.channels, **changed}
                save_xepg(str(self.system.file_xepg), self.channels)
            return len(changed)

        count = await self._locked(apply)
        await self.rebuild()
        return count

    # --- channel probe ---------------------------------------------------------------
    @property
    def probes(self) -> ProbeService:
        """ffprobe next to the configured ffmpeg (as Go), else the one on the PATH."""
        if self._probes is None:
            ffprobe = resolve_ffprobe(self.settings.ffmpeg_path) or "ffprobe"
            store = ProbeStore(self.system.file_probe_db)
            self._probes = ProbeService(store, ffprobe_path=ffprobe, user_agent=self.settings.user_agent)
        return self._probes

    def close(self) -> None:
        """Release the probe database (the app calls this on shutdown)."""
        if self._probes is not None:
            self._probes.store.close()
            self._probes = None
        if self.image_task is not None and not self.image_task.done():
            self.image_task.cancel()

    async def probe_channel(self, channel_id: str, *, force: bool = False) -> ProbeRecord:
        """Probe the channel's stream (network URLs only, never local files)."""
        channel = self.channels.get(channel_id)
        if channel is None:
            raise KeyError(channel_id)
        return await self.probes.probe(channel.url, channel_key=channel_id, force=force)

    def probe_results(self, channel_ids: Iterable[str]) -> dict[str, ProbeRecord]:
        """Latest stored probe of each channel's stream, by channel id."""
        urls = {cid: self.channels[cid].url for cid in channel_ids if cid in self.channels}
        if not urls or not self.system.file_probe_db.exists():
            return {}
        latest = self.probes.store.latest_many(list(set(urls.values())))
        return {cid: latest[url] for cid, url in urls.items() if url in latest}

    # --- status --------------------------------------------------------------------
    def status(self, base_url: str) -> dict[str, Any]:
        """What the status page and the API show (Go ``clientInfo`` / API ``status``)."""
        settings = self.settings
        host = base_url.split("://", 1)[-1]
        m3u = f"{base_url}/m3u/threadfin.m3u"
        xml = f"{base_url}/xmltv/threadfin.xml"
        if settings.authentication_web and settings.authentication_m3u:
            m3u += "?username=xxx&password=yyy"
        if settings.authentication_web and settings.authentication_xml:
            xml += "?username=xxx&password=yyy"
        tuners = sum(
            int(r.get("tuner", 0)) for r in settings.files.m3u.values()
            if isinstance(r, dict) and isinstance(r.get("tuner"), (int, float))
        )  # fmt: skip
        return {
            "version": self.system.version,
            "version.api": API_VERSION,
            "epg.source": settings.epg_source,
            "uuid": settings.uuid,
            "tuner": settings.tuner,
            "playlist.tuners": tuners,
            "streams.all": len(self.stream_db.all),
            "streams.active": len(self.stream_db.active),
            "streams.xepg": self.report.channels,
            "url.dvr": host,
            "url.m3u": m3u if self.xepg_mode else "",
            "url.xepg": xml if self.xepg_mode else "",
            "providers": {kind: len(self.manager.records(kind)) for kind in admin.PROVIDER_KINDS},
            "busy": self.busy,
            "log.errors": self.log.errors,
            "log.warnings": self.log.warnings,
        }

    # --- serving ----------------------------------------------------------------
    def playlist(self, base_url: str, groups: list[str] | None = None) -> str:
        options = PlaylistOptions(
            base_url=base_url,
            group_labels=tuple(g.label for g in self.stream_db.groups),
            image_url=(lambda url: self.images.lookup(url, base_url))
            if self.settings.cache_images
            else identity_image_url,
        )
        return build_playlist(self.channels, options, StreamRegistry(), groups or ())

    def device(self, base_url: str) -> Device:
        return Device(
            self.system.name, self.system.app_name, self.system.version,
            self.settings.uuid, self.settings.tuner, base_url,
        )  # fmt: skip

    def lineup(self, base_url: str) -> list[dict[str, str]]:
        if self.xepg_mode:
            entries = lineup_xepg(self.channels, self.registry, base_url)
        else:
            entries = lineup_pms(self.stream_db.active, PMSIds(self.system.file_pms), self.registry, base_url)
        self.registry.save()
        return entries

    def stream_target(self, stream_id: str, *, client: str) -> str | None:
        """Where ``/stream/<id>`` redirects to (None if unknown).

        A client that keeps asking for the same stream is sent to the channel's next backup.
        """
        info: StreamInfo | None = self.registry.lookup(stream_id)
        if info is None or not info.url:
            return None
        slots = [
            (slot, backup.url)
            for slot, backup in enumerate((info.backup_channel_1, info.backup_channel_2, info.backup_channel_3), 1)
            if backup and backup.url
        ]
        requests, seconds = self.settings.failover_requests, self.settings.failover_seconds
        decision = self.failover.request(client, stream_id, backups=len(slots), requests=requests, seconds=seconds)
        where = f"stream {stream_id[:8]}\u2026 ({info.name}) for {client}: {requests} requests in {seconds}s"
        url = info.url
        if decision.step:
            slot, url = slots[decision.step - 1]
            if decision.switched:
                name = self._backup_name(stream_id, slot)
                log.info("%s, switching to backup %d%s", where, slot, f" ({name})" if name else "")
        if decision.stuck:
            log.info("%s, no backup left to switch to", where)
        udpxy = self.settings.udpxy
        if udpxy and url.startswith("udp://@"):
            url = f"http://{udpxy}/udp/{url.removeprefix('udp://@')}/"
        return url

    def _backup_name(self, stream_id: str, slot: int) -> str:
        """The ``x-backup-channel-<slot>`` text of the main channel behind a stream id ("" if not found)."""
        for channel in list(self.channels.values()):  # build_xepg replaces the dict in a worker thread
            if stream_id_of(channel.file_m3u_id, channel.url) == stream_id:
                return (channel.x_backup_channel_1, channel.x_backup_channel_2, channel.x_backup_channel_3)[slot - 1]
        return ""


__all__ = ["XMLTV_FILE", "BuildReport", "Service"]
