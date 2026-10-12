"""The application core: ties providers, filters, channel database, guide and playlist together.

``Service.build()`` is Go's ``buildXEPG``: provider copies -> stream database -> channel database
(``xepg.json``) -> ``threadfin.xml`` (+ ``.gz``) -> stream registry (``urls.json``). The playlist
is generated per request from the channel database, so the links in it always carry the host the
client used to reach this server (Go baked one address into ``threadfin.m3u`` at build time).
``Service.update()`` is the manual "download all providers, then build"; ``scheduled_update()``
(Go ``maintenance``) makes an automatic backup first, and ``start()`` (Go ``StartSystem``) runs it
on start-up when ``files.update`` is on. ``restore()`` unpacks a backup and reloads everything.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime

from .auth import Authentication
from .backup import AutoBackupResult, BackupError, RestoreResult, auto_backup, restore_backup
from .epg import EPGOptions, XMLTVStats, write_xmltv
from .filters import FilterError
from .hdhr import Device, PMSIds, lineup_pms, lineup_xepg
from .playlist import PlaylistOptions, StreamInfo, StreamRegistry, build_playlist
from .providers import ProviderManager, RefreshResult
from .settings import Settings, load_settings, save_settings
from .streams_db import StreamDatabase, build_stream_database
from .system import SystemInfo
from .xepg import XEPGChannel, build_xepg, load_xepg, save_xepg
from .xmltv import build_mapping, make_program_counter

XMLTV_FILE = "threadfin.xml"


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
        self.report = BuildReport()
        self._lock = threading.Lock()
        self.building = False
        self.updating = False
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
        fresh = load_settings(self.system)
        for name in Settings.model_fields:
            setattr(self.settings, name, getattr(fresh, name))
        self.settings.__pydantic_extra__ = dict(fresh.__pydantic_extra__ or {})
        self._load_state()

    @property
    def busy(self) -> bool:
        """A download or build is running (Go ``ScanInProgress``)."""
        return self.building or self.updating

    # --- settings -------------------------------------------------------------
    def save_settings(self) -> None:
        save_settings(self.settings, self.system)

    @property
    def xmltv_path(self):
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
        report.xmltv_errors.update(errors)

        provider_ids = set(self.manager.records("m3u")) | set(self.manager.records("hdhr"))
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
        report.created, report.removed = result.created, len(result.removed_duplicates) + len(result.removed_stale)
        save_xepg(str(system.file_xepg), self.channels)

        options = EPGOptions(
            enable_non_ascii=settings.enable_non_ascii,
            replace_missing_images=settings.xepg_replace_missing_images,
            replace_channel_title=settings.xepg_replace_channel_title,
            generator=system.name,
            source=f"{system.name} - {system.version}",
        )
        stats: XMLTVStats = write_xmltv(
            self.xmltv_path, self.channels, system.folder_data, options, now,
            gzip_path=str(self.xmltv_path) + ".gz",
        )  # fmt: skip
        report.xmltv_errors.update(stats.errors)
        report.programmes, report.channels = stats.programmes, stats.channels

        self._register_streams(db)

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
            for kind in ("m3u", "hdhr", "xmltv") if self.xepg_mode else ("m3u", "hdhr"):
                results += await self.manager.refresh(kind)
            report = await asyncio.to_thread(self.build)
        finally:
            self.updating = False
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
        if self.settings.cache_images or not folder.is_dir():
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
        if self.settings.files_update and (self.manager.records("m3u") or self.manager.records("hdhr")):
            return await self.scheduled_update()
        return await asyncio.to_thread(self.build)

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

    # --- serving ----------------------------------------------------------------
    def playlist(self, base_url: str, groups: list[str] | None = None) -> str:
        options = PlaylistOptions(
            base_url=base_url, group_labels=tuple(g.label for g in self.stream_db.groups)
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

    def stream_target(self, stream_id: str) -> str | None:
        """Where ``/stream/<id>`` redirects to (None if unknown)."""
        info: StreamInfo | None = self.registry.lookup(stream_id)
        if info is None or not info.url:
            return None
        url = info.url
        udpxy = self.settings.udpxy
        if udpxy and url.startswith("udp://@"):
            url = f"http://{udpxy}/udp/{url.removeprefix('udp://@')}/"
        return url


__all__ = ["BuildReport", "Service", "XMLTV_FILE"]
