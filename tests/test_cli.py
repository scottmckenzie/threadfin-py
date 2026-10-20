"""The threadfin command: flags, -info, -restore, and a real start / stop."""

from __future__ import annotations

import io
import json
import logging
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.request
from pathlib import Path

from threadfin.backup import create_backup
from threadfin.cli import listen_address, listening_on_loopback, main, parser, port_number, setup_logging
from threadfin.redact import RedactAccessLog
from threadfin.settings import Settings, SettingsError, load_settings, save_settings
from threadfin.system import SystemInfo


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


class Flags(unittest.TestCase):
    def test_go_and_gnu_spellings(self) -> None:
        for argv in (["-config", "/c", "-port", "1", "-bind", "10.0.0.1", "-debug", "2"],
                     ["--config", "/c", "--port", "1", "--bind", "10.0.0.1", "--debug", "2"]):  # fmt: skip
            with self.subTest(argv):
                args = parser().parse_args(argv)
                self.assertEqual((args.config, args.port, args.bind, args.debug), ("/c", "1", "10.0.0.1", 2))

    def test_debug_range(self) -> None:
        with self.assertRaises(SystemExit), io.StringIO() as err, unittest.mock.patch("sys.stderr", err):
            parser().parse_args(["-debug", "4"])

    def test_listen_address(self) -> None:
        # No address and web authentication off: loopback, so the admin UI and API are not on the network.
        self.assertEqual(listen_address("", Settings()), "127.0.0.1")
        self.assertTrue(listening_on_loopback("127.0.0.1"))
        self.assertTrue(listening_on_loopback("::1"))
        self.assertTrue(listening_on_loopback("localhost"))
        self.assertFalse(listening_on_loopback("0.0.0.0"))
        self.assertFalse(listening_on_loopback("::"))
        # An explicit address is kept, including the old all-interfaces value, and -bind wins.
        self.assertEqual(listen_address("", Settings(bind_ip_address="10.0.0.2")), "10.0.0.2")
        self.assertEqual(listen_address("", Settings(bind_ip_address="0.0.0.0")), "0.0.0.0")
        self.assertEqual(listen_address("10.0.0.3", Settings(bind_ip_address="10.0.0.2")), "10.0.0.3")
        self.assertEqual(listen_address("0.0.0.0", Settings()), "0.0.0.0")
        # Web authentication already on, and no address chosen: keep all interfaces so login still works.
        self.assertEqual(listen_address("", Settings(authentication_web=True)), "0.0.0.0")
        self.assertEqual(listen_address("", Settings(bind_ip_address="10.0.0.2", authentication_web=True)), "10.0.0.2")

    def test_port(self) -> None:
        self.assertEqual(port_number(Settings()), 34400)
        for port in ("", "x", "0", "70000"):
            with self.subTest(port), self.assertRaises(SettingsError):
                port_number(Settings(port=port))


class Commands(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "cfg"
        self.runs: list[tuple[SystemInfo, Settings, str, int]] = []

    def run_main(self, *argv: str, answer: str = "") -> tuple[int, str]:
        out = io.StringIO()
        code = main(["-config", str(self.config), *argv], ask=lambda _: answer, out=out,
                    run=lambda *a: self.runs.append(a))  # fmt: skip
        return code, out.getvalue()

    def test_open_bind_without_auth_is_logged(self) -> None:
        with self.assertLogs("threadfin", level="WARNING") as captured:
            code, _ = self.run_main("-bind", "0.0.0.0")
        self.assertEqual(code, 0)
        self.assertIn("authentication.web is off", "\n".join(captured.output))
        self.assertIn("0.0.0.0", "\n".join(captured.output))

    def test_loopback_and_web_auth_do_not_warn(self) -> None:
        with self.assertNoLogs("threadfin", level="WARNING"):
            code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(listen_address("", self.runs[-1][1]), "127.0.0.1")

        self.run_main("-info")  # create the config, then turn web authentication on
        settings = load_settings(SystemInfo(config=self.config))
        settings.authentication_web = True
        save_settings(settings, SystemInfo(config=self.config))
        with self.assertNoLogs("threadfin", level="WARNING"):
            code, _ = self.run_main("-bind", "0.0.0.0")
        self.assertEqual(code, 0)
        self.assertTrue(self.runs[-1][1].authentication_web)

    def test_start(self) -> None:
        code, _ = self.run_main("-port", "34999", "-bind", "127.0.0.1", "-debug", "1")
        self.assertEqual(code, 0)
        [(system, settings, bind, debug)] = self.runs
        self.assertEqual((system.config, settings.port, bind, debug), (self.config, "34999", "127.0.0.1", 1))
        self.assertTrue(system.folder_backup.is_dir())
        self.assertTrue(system.file_authentication.is_file())
        self.assertEqual(json.loads(system.file_settings.read_text())["port"], "34999")  # saved, as in Go

    def test_invalid_port_does_not_start(self) -> None:
        with unittest.mock.patch("sys.stderr", io.StringIO()) as err:
            code, _ = self.run_main("-port", "nope")
        self.assertEqual((code, self.runs), (1, []))
        self.assertIn("invalid port", err.getvalue())

    def test_info(self) -> None:
        code, out = self.run_main("-info")
        self.assertEqual((code, self.runs), (0, []))
        self.assertIn(f"Folder (config):    {self.config}", out)
        self.assertIn("Port:               34400", out)

    def backup_with_tuner(self, tuner: int) -> Path:
        system = SystemInfo(config=self.config)
        system.create_folders()
        system.create_files()
        settings = load_settings(system)
        settings.tuner = tuner
        save_settings(settings, system)
        archive = create_backup(system)
        settings.tuner = 1
        save_settings(settings, system)
        return archive

    def test_restore(self) -> None:
        archive = self.backup_with_tuner(7)
        code, out = self.run_main("-restore", str(archive), answer="no")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((self.config / "settings.json").read_text())["tuner"], 1)
        code, out = self.run_main("-restore", str(archive), answer="maybe")
        self.assertEqual((code, out.strip().splitlines()[-1]), (1, "Invalid input"))
        code, out = self.run_main("-restore", str(archive), answer="YES")
        self.assertEqual(code, 0)
        self.assertIn("successfully restored", out)
        self.assertEqual(json.loads((self.config / "settings.json").read_text())["tuner"], 7)
        self.assertEqual(self.runs, [])  # restore never starts the server

    def test_restore_errors(self) -> None:
        code, out = self.run_main("-restore", str(self.config / "missing.zip"), answer="yes")
        self.assertEqual(code, 1)
        self.assertIn("Restore failed: not a backup archive", out)

    def test_restore_without_a_terminal(self) -> None:
        out = io.StringIO()

        def eof(_: str) -> str:
            raise EOFError

        code = main(["-config", str(self.config), "-restore", "x.zip"], ask=eof, out=out,
                    run=lambda *a: self.runs.append(a))  # fmt: skip
        self.assertEqual(self.runs, [])
        self.assertEqual((code, out.getvalue().strip().splitlines()[-1]), (1, "Invalid input"))

    def test_access_log_is_redacted(self) -> None:
        logger = logging.getLogger("uvicorn.access")
        self.addCleanup(lambda: [logger.filters.remove(f) for f in logger.filters if isinstance(f, RedactAccessLog)])
        setup_logging(2)
        setup_logging(2)
        self.assertEqual(len([f for f in logger.filters if isinstance(f, RedactAccessLog)]), 1)
        with self.assertLogs("uvicorn.access", level="INFO") as cm:
            logger.info(
                '%s - "%s %s HTTP/%s" %d',
                "1.2.3.4:5",
                "GET",
                "/m3u/threadfin.m3u?username=u&password=SECRET",
                "1.1",
                200,
            )
        self.assertNotIn("SECRET", "\n".join(cm.output))
        self.assertIn("/m3u/threadfin.m3u?…", "\n".join(cm.output))


class RealServer(unittest.TestCase):
    def test_start_serve_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            port = free_port()
            cmd = [sys.executable, "-m", "threadfin", "-config", f"{tmp}/cfg", "-port", str(port), "-bind", "127.0.0.1"]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/discover.json", timeout=1) as r:
                            info = json.load(r)
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail("the server did not start")
                self.assertEqual(info["BaseURL"], f"http://127.0.0.1:{port}")
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/web/", timeout=5) as r:
                    self.assertEqual(r.status, 200)
            finally:
                proc.send_signal(signal.SIGTERM)
                output, _ = proc.communicate(timeout=10)
            # uvicorn shuts down cleanly, then re-raises the signal (the usual exit status for SIGTERM).
            self.assertIn(proc.returncode, (0, -signal.SIGTERM))
            self.assertIn(f"Web interface: http://127.0.0.1:{port}/web/", output)
            self.assertNotIn("Traceback", output)

    def test_web_interface_is_announced_once_it_answers(self) -> None:
        """The "Web interface" line comes after uvicorn's startup, when the port already answers."""
        with tempfile.TemporaryDirectory() as tmp:
            port = free_port()
            cmd = [sys.executable, "-m", "threadfin", "-config", f"{tmp}/cfg", "-port", str(port), "-bind", "127.0.0.1",
                   "-debug", "1"]  # fmt: skip
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            lines: queue.Queue[str] = queue.Queue()
            reader = threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout or ()], daemon=True)
            reader.start()
            seen: list[str] = []
            try:
                deadline = time.monotonic() + 30
                while not any("Web interface:" in line for line in seen):
                    seen.append(lines.get(timeout=max(0.1, deadline - time.monotonic())))
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    pass  # listening at the moment the line was logged
            finally:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)
                reader.join(timeout=5)
                if proc.stdout:
                    proc.stdout.close()
            startup = next(i for i, line in enumerate(seen) if "Application startup complete" in line)
            announced = next(i for i, line in enumerate(seen) if "Web interface:" in line)
            self.assertLess(startup, announced)
            self.assertIn(f"Web interface: http://127.0.0.1:{port}/web/", seen[announced])


if __name__ == "__main__":
    unittest.main()
