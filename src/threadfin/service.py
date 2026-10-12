"""The application core: ties providers, filters, channel database, guide and playlist together.

``Service.build()`` is Go's ``buildXEPG``: provider copies -> stream database -> channel database
(``xepg.json``) -> ``threadfin.xml`` (+ ``.gz``) -> stream registry (``urls.json``). The playlist
is generated per request from the channel database, so the links in it always carry the host the
client used to reach this server (Go baked one address into ``threadfin.m3u`` at build time).
``Service.update()`` is the scheduled/manual "download all providers, then build".
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime

from .epg import EPGOptions, XMLTVStats, write_xmltv
from .filters import FilterError
from .playlist import PlaylistOptions, StreamInfo, StreamRegistry, build_playlist
from .providers import ProviderManager, RefreshResult
from .settings import Settings, save_settings
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
        self.channels: dict[str, XEPGChannel] = {}
        self.registry = StreamRegistry(system.file_urls)
        self.stream_db = StreamDatabase()
        self.report = BuildReport()
        self._lock = threading.Lock()
        if system.file_xepg.exists():
            self.channels = load_xepg(str(system.file_xepg))

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
            try:
                self._build(report, (now or datetime.now().astimezone()))
            except FilterError as exc:
                report.ok, report.error = False, f"filter: {exc}"
            self.report = report
            return report

    def _build(self, report: BuildReport, now: datetime) -> None:
        settings, system = self.settings, self.system
        db = build_stream_database(self.manager, settings)
        self.stream_db = db
        report.warnings += db.warnings
        report.provider_errors = dict(db.errors)
        report.streams = len(db.all)

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

        # the registry starts empty on every build: ids of vanished channels stop resolving
        fresh = StreamRegistry()
        build_playlist(self.channels, PlaylistOptions(group_labels=tuple(g.label for g in db.groups)), fresh)
        self.registry.replace_with(fresh)
        self.registry.save()

    async def update(self) -> BuildReport:
        """Download every provider, then rebuild."""
        results: list[RefreshResult] = []
        for kind in ("m3u", "hdhr", "xmltv"):
            results += await self.manager.refresh(kind)
        report = await asyncio.to_thread(self.build)
        for result in results:
            if not result.ok:
                report.provider_errors[result.provider_id] = result.error or "download failed"
        self.report = report
        return report

    # --- serving ----------------------------------------------------------------
    def playlist(self, base_url: str, groups: list[str] | None = None) -> str:
        options = PlaylistOptions(
            base_url=base_url, group_labels=tuple(g.label for g in self.stream_db.groups)
        )
        return build_playlist(self.channels, options, StreamRegistry(), groups or ())

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
