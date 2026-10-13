"""Scheduled updates, start-up update and restore through the service (Go ``maintenance.go``,
``StartSystem``, ``ThreadfinRestore``)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from threadfin.auth import AuthError
from threadfin.backup import BackupError, create_backup
from threadfin.scheduler import Scheduler, is_due, seconds_to_next_minute
from threadfin.service import BuildReport, Service
from threadfin.settings import load_settings
from threadfin.system import SystemInfo
from threadfin.web import create_app

M3U = '#EXTM3U\n#EXTINF:-1 tvg-id="a" group-title="News",Alpha\nhttp://s/1\n'


class FakeService:
    def __init__(self, times: list[str]) -> None:
        self.settings = SimpleNamespace(update=times)
        self.busy = False
        self.runs: list[datetime] = []
        self.now: datetime | None = None

    async def scheduled_update(self) -> BuildReport:
        assert self.now is not None
        self.runs.append(self.now)
        return BuildReport()


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def test_is_due(self) -> None:
        self.assertTrue(is_due(["0000", "1530"], datetime(2026, 1, 1, 15, 30, 59)))
        self.assertFalse(is_due(["0000", "1530"], datetime(2026, 1, 1, 15, 31)))
        self.assertFalse(is_due([], datetime(2026, 1, 1, 0, 0)))

    def test_seconds_to_next_minute(self) -> None:
        self.assertEqual(seconds_to_next_minute(datetime(2026, 1, 1, 23, 59, 45, 500000)), 14.5)
        self.assertEqual(seconds_to_next_minute(datetime(2026, 1, 1, 3, 0)), 60)

    async def tick(self, scheduler: Scheduler, fake: FakeService, now: datetime) -> bool:
        fake.now = now
        return await scheduler.tick(now)

    async def test_runs_once_per_minute_even_if_listed_twice(self) -> None:
        fake = FakeService(["0300", "0300"])
        scheduler = Scheduler(fake)  # type: ignore[arg-type]
        self.assertTrue(await self.tick(scheduler, fake, datetime(2026, 1, 1, 3, 0, 1)))
        self.assertFalse(await self.tick(scheduler, fake, datetime(2026, 1, 1, 3, 0, 40)))
        self.assertTrue(await self.tick(scheduler, fake, datetime(2026, 1, 2, 3, 0, 1)))  # next day
        self.assertEqual(len(fake.runs), 2)

    async def test_skipped_while_busy(self) -> None:
        fake = FakeService(["0300"])
        scheduler = Scheduler(fake)  # type: ignore[arg-type]
        fake.busy = True
        self.assertFalse(await self.tick(scheduler, fake, datetime(2026, 1, 1, 3, 0, 1)))
        fake.busy = False
        self.assertFalse(await self.tick(scheduler, fake, datetime(2026, 1, 1, 3, 0, 30)))  # that slot is gone
        self.assertEqual(fake.runs, [])

    async def test_failure_does_not_stop_the_loop(self) -> None:
        fake = FakeService(["0300"])

        async def broken() -> BuildReport:
            raise RuntimeError("boom")

        fake.scheduled_update = broken  # type: ignore[method-assign]
        with self.assertLogs("threadfin.scheduler", "ERROR"):
            self.assertTrue(await Scheduler(fake).tick(datetime(2026, 1, 1, 3, 0)))  # type: ignore[arg-type]

    async def test_loop_wakes_every_minute_and_survives_slow_updates(self) -> None:
        """Go slept 60 s after a slow update and could miss the next minute; here every one counts."""
        fake = FakeService(["0300", "0301", "0302"])
        clock = [datetime(2026, 1, 1, 2, 59, 30)]
        sleeps: list[float] = []

        async def update() -> BuildReport:
            fake.runs.append(clock[0])
            clock[0] = clock[0].replace(second=58)  # the update takes most of the minute
            return BuildReport()

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 5:
                raise asyncio.CancelledError
            clock[0] += timedelta(seconds=seconds)

        fake.scheduled_update = update  # type: ignore[method-assign]
        scheduler = Scheduler(fake, clock=lambda: clock[0], sleep=sleep)  # type: ignore[arg-type]
        with self.assertRaises(asyncio.CancelledError):
            await scheduler.run()
        self.assertEqual([t.strftime("%H%M") for t in fake.runs], ["0300", "0301", "0302"])
        self.assertEqual(sleeps, [31, 3, 3, 3, 60])


class Provider(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        type(self).hits += 1
        body = M3U.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ServiceMaintenance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addAsyncCleanup(asyncio.to_thread, self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/p.m3u"

        self.system = SystemInfo(config=Path(tmp.name) / "cfg", temp=Path(tmp.name) / "tmp")
        self.system.create_folders()
        self.system.create_files()
        self.service = Service(self.system, load_settings(self.system))
        self.service.settings.epg_source = "XEPG"

    def backups(self) -> list[str]:
        return sorted(p.name for p in self.system.folder_backup.iterdir())

    async def add_provider(self) -> None:
        await self.service.manager.add("m3u", {"file.source": self.url, "name": "P"})
        Provider.hits = 0

    async def test_start_without_providers_only_builds(self) -> None:
        report = await self.service.start()
        self.assertTrue(report.ok)
        self.assertEqual(self.backups(), [])

    async def test_start_updates_when_files_update_is_on(self) -> None:
        await self.add_provider()
        await self.service.start()
        self.assertEqual(Provider.hits, 1)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual([c.name for c in self.service.channels.values()], ["Alpha"])

    async def test_start_from_local_copies_when_files_update_is_off(self) -> None:
        await self.add_provider()
        self.service.settings.files_update = False
        await self.service.start()
        self.assertEqual((Provider.hits, self.backups()), (0, []))
        self.assertEqual([c.name for c in self.service.channels.values()], ["Alpha"])

    async def test_scheduled_update_backs_up_and_clears_image_cache(self) -> None:
        await self.add_provider()
        cached = self.system.folder_images_cache / "x.png"
        cached.write_bytes(b"x")
        (self.system.folder_images_cache / "sub").mkdir()
        await self.service.scheduled_update()
        self.assertEqual(Provider.hits, 1)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(list(self.system.folder_images_cache.iterdir()), [])

        self.service.settings.cache_images = True
        cached.write_bytes(b"x")
        await self.service.scheduled_update()
        self.assertTrue(cached.exists())

    async def test_failed_backup_does_not_stop_the_update(self) -> None:
        await self.add_provider()
        blocker = Path(self.system.config) / "not-a-folder"
        blocker.write_text("")
        self.service.settings.backup_path = str(blocker) + "/"
        report = await self.service.scheduled_update()
        self.assertTrue(report.warnings[0].startswith("backup: "), report.warnings)
        self.assertEqual(Provider.hits, 1)

    async def test_busy_during_update(self) -> None:
        await self.add_provider()
        seen: list[bool] = []
        original = self.service.manager.refresh

        async def refresh(kind: str, provider_id: str | None = None):  # type: ignore[no-untyped-def]
            seen.append(self.service.busy)
            return await original(kind, provider_id)

        self.service.manager.refresh = refresh  # type: ignore[method-assign]
        await self.service.update()
        self.assertTrue(seen and all(seen))
        self.assertFalse(self.service.busy)

    async def test_restore_reloads_everything(self) -> None:
        await self.add_provider()
        self.service.auth.create_first_user("admin", "pw")
        self.service.settings.tuner = 3
        self.service.save_settings()
        await self.service.update()
        archive = create_backup(self.system)

        # Change everything after the backup.
        self.service.settings.tuner = 1
        self.service.settings.port = "9999"
        self.service.save_settings()
        self.service.auth.save_new_user({"username": "bob", "password": "x"})
        settings_object = self.service.settings

        result = await self.service.restore(archive)
        self.assertEqual((result.old_port, result.new_port), ("9999", "34400"))
        self.assertIs(self.service.settings, settings_object)  # updated in place
        self.assertEqual(self.service.settings.tuner, 3)
        self.assertIs(self.service.manager._settings, settings_object)
        self.service.auth.login("admin", "pw")
        with self.assertRaises(AuthError):
            self.service.auth.login("bob", "x")
        self.assertEqual([c.name for c in self.service.channels.values()], ["Alpha"])
        self.assertEqual(len(self.backups()), 1)  # the update after the restore backs up (update() alone does not)

    async def test_app_runs_the_scheduler(self) -> None:
        def scheduler_running() -> bool:
            return any(t.get_name() == "scheduler" and not t.done() for t in asyncio.all_tasks())

        await self.add_provider()
        app = create_app(self.service)
        async with app.router.lifespan_context(app):
            self.assertEqual(Provider.hits, 1)  # start-up update (files.update)
            self.assertTrue(scheduler_running())
        self.assertFalse(scheduler_running())
        app = create_app(self.service, build_on_start=False, schedule=False)
        async with app.router.lifespan_context(app):
            self.assertFalse(scheduler_running())
        self.assertEqual(Provider.hits, 1)

    async def test_restore_refused_while_busy_or_invalid(self) -> None:
        archive = Path(self.system.folder_temp) / "bad.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("settings.json", json.dumps({"version": "0.0.1", "port": "1"}))
        with self.assertRaisesRegex(BackupError, "at least version 0.1.0"):
            await self.service.restore(archive)
        self.service.updating = True
        with self.assertRaisesRegex(BackupError, "update is running"):
            await self.service.restore(archive)


if __name__ == "__main__":
    unittest.main()
