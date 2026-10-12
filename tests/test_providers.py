"""Provider fetching, local copies, counters. Network tests use a local HTTP server."""

from __future__ import annotations

import gzip
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from threadfin.m3u import parse_m3u
from threadfin.providers import (
    ProviderError,
    ProviderManager,
    apply_defaults,
    availability,
    check_xmltv,
    download,
    extract_gzip,
    filename_from_content_disposition,
    filename_from_url,
    go_base,
    hdhr_streams,
    new_provider_id,
)
from threadfin.settings import Settings

M3U = (
    '#EXTM3U\n#EXTINF:-1 tvg-id="a" tvg-name="A" group-title="News" tvg-logo="http://x/l.png",Alpha\n'
    "http://s/1\n#EXTINF:-1 group-title=\"Sports\",Beta\nhttp://s/2\n"
    '#EXTINF:-1 tvg-id="n",\nhttp://s/3\n'
)
LINEUP = json.dumps(
    [
        {"GuideNumber": "2.1", "GuideName": "WXYZ", "URL": "http://tuner/auto/v2.1"},
        {"GuideNumber": "3.1", "GuideName": "NoUrl"},
        "junk",
    ]
)
XMLTV = (
    '<?xml version="1.0"?><tv><channel id="a"><display-name>A</display-name></channel>'
    '<programme channel="a" start="20240101000000" stop="20240101010000"><title>T</title></programme>'
    '<programme channel="a" start="20240101010000" stop="20240101020000"><title>U</title></programme></tv>'
)


class Handler(BaseHTTPRequestHandler):
    seen_agents: list[str] = []

    def log_message(self, *args: object) -> None:  # silence
        pass

    def reply(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # client gave up (timeout tests)
            pass

    def do_GET(self) -> None:  # noqa: N802
        Handler.seen_agents.append(self.headers.get("User-Agent", ""))
        path = self.path
        if path == "/list.m3u":
            self.reply(M3U.encode())
        elif path == "/gz.m3u":
            self.reply(gzip.compress(M3U.encode()))
        elif path == "/cd":
            self.reply(M3U.encode(), headers={"Content-Disposition": 'attachment; filename="Pkg.m3u"; size=3'})
        elif path == "/redir":
            self.reply(b"", 302, {"Location": "/list.m3u"})
        elif path == "/204":
            self.reply(b"", 204)
        elif path == "/slow":
            time.sleep(1.5)
            self.reply(M3U.encode())
        elif path == "/drip":  # many small writes: no single socket read ever stalls
            self.send_response(200)
            self.send_header("Content-Length", "20")
            self.end_headers()
            try:
                for _ in range(20):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == "/big":
            self.reply(b"x" * 5000)
        elif path == "/lineup.json":
            self.reply(LINEUP.encode())
        elif path == "/tv.xml":
            self.reply(XMLTV.encode())
        elif path == "/bad.m3u":
            self.reply(b"<html>not a playlist</html>")
        elif path.startswith("/flaky"):
            if Handler.fail:
                self.reply(b"no", 503)
            else:
                self.reply(M3U.encode())
        else:
            self.reply(b"missing", 404)

    fail = False


class ServerCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        Handler.fail = False
        Handler.seen_agents = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.settings = Settings()
        self.settings.user_agent = "TestAgent/1"
        self.saves = 0
        self.manager = ProviderManager(self.settings, self.data, self._save)

    def _save(self) -> None:
        self.saves += 1


class DownloadTests(ServerCase):
    def test_download_and_user_agent(self) -> None:
        got = download(f"{self.base}/list.m3u", user_agent="UA-1")
        self.assertEqual(got.body, M3U.encode())
        self.assertEqual(got.filename, "list.m3u")
        self.assertEqual(Handler.seen_agents[-1], "UA-1")

    def test_follows_redirects(self) -> None:
        self.assertEqual(download(f"{self.base}/redir").body, M3U.encode())

    def test_filename_from_header(self) -> None:
        self.assertEqual(download(f"{self.base}/cd").filename, "Pkg.m3u")

    def test_error_status_message_matches_go_format(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            download(f"{self.base}/nope")
        self.assertEqual(str(ctx.exception), f"404: {self.base}/nope Not Found")

    def test_non_200_success_status_is_an_error(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            download(f"{self.base}/204")
        self.assertIn("204", str(ctx.exception))

    def test_total_timeout(self) -> None:
        with self.assertRaises(ProviderError):
            download(f"{self.base}/slow", timeout=0.3)

    def test_deadline_applies_to_the_whole_download(self) -> None:
        started = time.monotonic()
        with self.assertRaises(ProviderError) as ctx:
            download(f"{self.base}/drip", timeout=0.5)
        self.assertIn("timed out", str(ctx.exception))
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(download(f"{self.base}/drip", timeout=10).body), 20)

    def test_loopback_ignores_environment_proxy(self) -> None:
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}):
            os.environ.pop("no_proxy", None)
            os.environ.pop("NO_PROXY", None)
            self.assertEqual(download(f"{self.base}/list.m3u").body, M3U.encode())

    def test_size_limit(self) -> None:
        with self.assertRaises(ProviderError):
            download(f"{self.base}/big", max_bytes=1000)

    def test_rejects_non_http_urls(self) -> None:
        for url in ("file:///etc/passwd", "ftp://x/y", "/etc/passwd", "http://"):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                download(url)

    def test_connection_refused_is_a_provider_error(self) -> None:
        with self.assertRaises(ProviderError):
            download("http://127.0.0.1:9/x", timeout=2)


class RefreshTests(ServerCase):
    async def test_add_m3u_writes_normalised_copy(self) -> None:
        result = await self.manager.add("m3u", {"file.source": f"{self.base}/list.m3u"})
        self.assertTrue(result.ok)
        self.assertEqual(result.streams, 2)
        self.assertEqual([e.reason for e in result.skipped], ["no name"])
        (pid, record), = self.settings.files.m3u.items()
        self.assertEqual(pid[0], "M")
        self.assertEqual(len(pid), 20)
        self.assertEqual(record["name"], "list.m3u")
        self.assertEqual(record["tuner"], 1)
        self.assertEqual(record["provider.availability"], 100)
        self.assertEqual(record["counter.download"], 1)
        self.assertEqual(record["counter.error"], 0)
        self.assertEqual(record["file.threadfin"], pid + ".m3u")
        self.assertEqual(record["type"], "m3u")
        copy = (self.data / f"{pid}.m3u").read_text()
        self.assertTrue(copy.startswith("#EXTM3U\n#EXTINF:-1 tvg-id=\"a\" tvg-name=\"A\" tvg-chno=\"\""))
        self.assertEqual([s["name"] for s in parse_m3u(copy)], ["Alpha", "Beta"])
        self.assertGreater(self.saves, 0)

    async def test_explicit_values_beat_defaults(self) -> None:
        await self.manager.add(
            "m3u", {"file.source": f"{self.base}/list.m3u", "name": "Mine", "tuner": 4}
        )
        record = next(iter(self.settings.files.m3u.values()))
        self.assertEqual((record["name"], record["tuner"]), ("Mine", 4))

    async def test_gzip_and_header_name(self) -> None:
        await self.manager.add("m3u", {"file.source": f"{self.base}/gz.m3u"})
        record = next(iter(self.settings.files.m3u.values()))
        self.assertEqual(record["name"], "gz.m3u")
        await self.manager.add("m3u", {"file.source": f"{self.base}/cd"})
        self.assertIn("Pkg.m3u", {r["name"] for r in self.settings.files.m3u.values()})

    async def test_failed_add_leaves_nothing_behind(self) -> None:
        for source in (f"{self.base}/nope", f"{self.base}/bad.m3u"):
            with self.subTest(source=source), self.assertRaises(ProviderError):
                await self.manager.add("m3u", {"file.source": source})
        self.assertEqual(self.settings.files.m3u, {})
        self.assertEqual(list(self.data.iterdir()), [])

    async def test_local_file_source(self) -> None:
        local = self.data / "mine.m3u"
        local.write_text(M3U)
        await self.manager.add("m3u", {"file.source": str(local)})
        self.assertEqual(next(iter(self.settings.files.m3u.values()))["name"], "mine.m3u")

    async def test_local_files_can_be_forbidden(self) -> None:
        local = self.data / "mine.m3u"
        local.write_text(M3U)
        manager = ProviderManager(self.settings, self.data, self._save, allow_local_files=False)
        with self.assertRaises(ProviderError):
            await manager.add("m3u", {"file.source": str(local)})

    async def test_missing_local_file(self) -> None:
        with self.assertRaises(ProviderError):
            await self.manager.add("m3u", {"file.source": str(self.data / "absent.m3u")})

    async def test_failure_counters_and_availability(self) -> None:
        await self.manager.add("m3u", {"file.source": f"{self.base}/flaky"})
        (pid, record), = self.settings.files.m3u.items()
        copy = (self.data / f"{pid}.m3u").read_bytes()

        Handler.fail = True
        result, = await self.manager.refresh("m3u", pid)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, f"503: {self.base}/flaky Service Unavailable")
        self.assertEqual((record["counter.error"], record["counter.download"]), (1, 2))
        self.assertEqual(record["provider.availability"], 50)
        self.assertEqual((self.data / f"{pid}.m3u").read_bytes(), copy)  # old copy kept

        Handler.fail = False
        await self.manager.refresh("m3u", pid)
        self.assertEqual((record["counter.error"], record["counter.download"]), (1, 3))
        self.assertEqual(record["provider.availability"], 66)  # 66.67 truncated, as Go

    async def test_refresh_all_reports_every_provider(self) -> None:
        await self.manager.add("m3u", {"file.source": f"{self.base}/list.m3u"})
        await self.manager.add("m3u", {"file.source": f"{self.base}/flaky"})
        Handler.fail = True
        results = await self.manager.refresh("m3u")
        self.assertEqual(sorted(r.ok for r in results), [False, True])

    async def test_refresh_unknown_provider(self) -> None:
        result, = await self.manager.refresh("m3u", "nope")
        self.assertFalse(result.ok)

    async def test_hdhr(self) -> None:
        host = self.base.removeprefix("http://")
        result = await self.manager.add("hdhr", {"file.source": host, "name": "Tuner"})
        self.assertEqual(result.streams, 1)
        self.assertEqual(len(result.skipped), 2)
        (pid, record), = self.settings.files.hdhr.items()
        self.assertEqual(record["tuner"], 1)
        self.assertTrue((self.data / f"{pid}.json").exists())

    async def test_xmltv_compatibility_counts(self) -> None:
        await self.manager.add("xmltv", {"file.source": f"{self.base}/tv.xml"})
        (pid, record), = self.settings.files.xmltv.items()
        self.assertEqual(record["compatibility"], {"xmltv.channels": 1, "xmltv.programs": 2})
        self.assertNotIn("tuner", record)
        self.assertTrue((self.data / f"{pid}.xml").exists())

    async def test_remove(self) -> None:
        await self.manager.add("m3u", {"file.source": f"{self.base}/list.m3u"})
        pid = next(iter(self.settings.files.m3u))
        saves = self.saves
        self.assertTrue(await self.manager.remove("m3u", pid))
        self.assertGreater(self.saves, saves)
        self.assertFalse(await self.manager.remove("m3u", pid))
        self.assertEqual(list(self.data.iterdir()), [])

    async def test_proxy_setting_is_used(self) -> None:
        seen = {}

        def fake(url: str, **kwargs: object):
            seen.update(kwargs, url=url)
            return download(f"{self.base}/list.m3u")

        manager = ProviderManager(self.settings, self.data, self._save, fetch=fake)
        await manager.add(
            "m3u",
            {"file.source": "http://prov/list.m3u", "http_proxy.ip": "10.0.0.1", "http_proxy.port": "3128"},
        )
        self.assertEqual(seen["proxy"], "http://10.0.0.1:3128")
        self.assertEqual(seen["user_agent"], "TestAgent/1")
        self.assertEqual(seen["timeout"], 500.0)  # buffer.timeout default

    async def test_parameter_and_compatibility_helpers(self) -> None:
        self.settings.files.m3u["M1"] = {"name": "N", "tuner": 3.0, "flag": True}
        self.assertEqual(self.manager.parameter("m3u", "M1", "name"), "N")
        self.assertEqual(self.manager.parameter("m3u", "M1", "tuner"), "3")
        self.assertEqual(self.manager.parameter("m3u", "M1", "flag"), "")
        self.assertEqual(self.manager.parameter("m3u", "M1", "missing"), "")
        self.assertEqual(self.manager.parameter("m3u", "ZZ", "name"), "")
        self.manager.set_compatibility("m3u", "M1", {"streams": 4})
        self.assertEqual(self.settings.files.m3u["M1"]["compatibility"], {"streams": 4})


class PureTests(unittest.TestCase):
    def test_availability_matches_go_formula(self) -> None:
        self.assertEqual(availability(0, 5), 100)
        self.assertEqual(availability(1, 3), 66)
        self.assertEqual(availability(1, 2), 50)
        self.assertEqual(availability(2, 2), 0)
        self.assertEqual(availability(1, 0), 100)  # Go would divide by zero

    def test_go_base_and_url_names(self) -> None:
        self.assertEqual(go_base("/a/b/"), "b")
        self.assertEqual(go_base(""), ".")
        self.assertEqual(go_base("///"), "/")
        self.assertEqual(filename_from_url("http://h/get.php?user=a&type=m3u"), "get.php")
        self.assertEqual(filename_from_url("http://host/"), "host")

    def test_content_disposition(self) -> None:
        f = filename_from_content_disposition
        self.assertEqual(f('attachment; filename="a.m3u"; size=3'), "a.m3u")
        self.assertEqual(f("attachment; filename=b.m3u"), "b.m3u")
        self.assertEqual(f("attachment; filename*=UTF-8''c%20d.m3u"), "c d.m3u")
        self.assertEqual(f('attachment; filename="../../etc/x.m3u"'), "x.m3u")
        self.assertIsNone(f("attachment"))
        self.assertIsNone(f("filename"))  # Go indexed past the end here
        self.assertIsNone(f('attachment; filename=""'))

    def test_extract_gzip(self) -> None:
        self.assertEqual(extract_gzip(gzip.compress(b"hello")), b"hello")
        self.assertEqual(extract_gzip(b"plain"), b"plain")
        self.assertEqual(extract_gzip(gzip.compress(b"a") + gzip.compress(b"b")), b"ab")
        truncated = gzip.compress(b"hello" * 100)[:-6]
        self.assertEqual(extract_gzip(truncated), truncated)
        corrupt = b"\x1f\x8bgarbage"
        self.assertEqual(extract_gzip(corrupt), corrupt)
        with self.assertRaises(ProviderError):
            extract_gzip(gzip.compress(b"0" * 100_000), max_bytes=1000)

    def test_hdhr_streams(self) -> None:
        streams, skipped = hdhr_streams(LINEUP, "Tuner", "H123")
        self.assertEqual(
            streams,
            [
                {
                    "group-title": "Tuner",
                    "name": "WXYZ",
                    "tvg-id": "WXYZ",
                    "url": "http://tuner/auto/v2.1",
                    "ID-H123": "2.1",
                    "_uuid.key": "ID-H123",
                    "_values": "Tuner WXYZ",
                }
            ],
        )
        self.assertEqual([e.line for e in skipped], [2, 3])
        self.assertEqual(hdhr_streams("null", "T", "H")[0], [])
        for bad in ("{}", "not json", '"x"'):
            with self.subTest(bad=bad), self.assertRaises(ProviderError):
                hdhr_streams(bad, "T", "H")

    def test_check_xmltv(self) -> None:
        self.assertEqual(check_xmltv(XMLTV.encode()), {"xmltv.channels": 1, "xmltv.programs": 2})
        for bad in (b"<rss></rss>", b"<tv><channel></tv>", b"", b"plain"):
            with self.subTest(bad=bad), self.assertRaises(ProviderError):
                check_xmltv(bad)

    def test_ids(self) -> None:
        self.assertEqual(len({new_provider_id("xmltv") for _ in range(50)}), 50)
        self.assertTrue(new_provider_id("hdhr").startswith("H"))

    def test_apply_defaults_keeps_existing(self) -> None:
        data = {"name": "Mine", "counter.error": 2}
        apply_defaults(data, kind="xmltv", provider_id="X1", source="s", server_name="srv", app="threadfin")
        self.assertEqual((data["name"], data["counter.error"]), ("Mine", 2))
        self.assertEqual(data["file.threadfin"], "X1.xml")
        self.assertEqual(data["id.provider"], "X1")
        self.assertNotIn("tuner", data)


if __name__ == "__main__":
    unittest.main()
