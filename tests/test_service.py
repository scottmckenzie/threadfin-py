"""End to end: providers -> database -> guide + playlist -> HTTP endpoints (real server, real sockets)."""

from __future__ import annotations

import asyncio
import base64
import gzip
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn

from threadfin.auth import LEVEL_M3U, LEVEL_PMS
from threadfin.service import Service
from threadfin.settings import load_settings
from threadfin.system import SystemInfo
from threadfin.web import create_app

M3U = (
    "#EXTM3U\n"
    '#EXTINF:-1 tvg-id="a" tvg-name="Alpha" group-title="News" tvg-logo="http://x/l.png",Alpha\nhttp://s/1\n'
    '#EXTINF:-1 tvg-id="b" tvg-name="Beta" group-title="Sports",Beta\nhttp://s/2\n'
    '#EXTINF:-1 tvg-id="zzz" tvg-name="Gamma" group-title="News",Gamma "Q"\nhttp://s/3\n'
)
XMLTV = (
    '<?xml version="1.0"?><tv><channel id="a"><display-name>A</display-name></channel>'
    '<channel id="b"><display-name>B</display-name></channel>'
    '<programme channel="a" start="20260101000000 +0000" stop="20260101010000 +0000"><title>Show A</title></programme>'
    '<programme channel="b" start="20260101000000 +0000" stop="20260101010000 +0000"><title>Show B</title></programme>'
    "</tv>"
)


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        body = (M3U if self.path.endswith(".m3u") else XMLTV).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Base(unittest.IsolatedAsyncioTestCase):
    SOURCE = "XEPG"

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        self.src = f"http://127.0.0.1:{self.provider.server_address[1]}"

        system = SystemInfo(config=Path(self.tmp.name) / "cfg")
        system.create_folders()
        system.create_files()
        self.settings = load_settings(system)
        self.settings.epg_source = self.SOURCE
        self.service = Service(system, self.settings)
        await self.service.manager.add("m3u", {"file.source": f"{self.src}/p.m3u", "name": "P"})
        await self.service.manager.add("xmltv", {"file.source": f"{self.src}/g.xml"})
        self.report = await self.service.update()

        self.port = free_port()
        config = uvicorn.Config(create_app(self.service), host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)

    async def asyncTearDown(self) -> None:
        self.server.should_exit = True
        await asyncio.to_thread(self.thread.join, 5)
        await asyncio.to_thread(self.provider.shutdown)
        self.provider.server_close()
        self.tmp.cleanup()

    def get(self, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()


class EndToEnd(Base):
    async def test_hdhr_endpoints(self) -> None:
        base = f"http://127.0.0.1:{self.port}"
        status, headers, body = await asyncio.to_thread(self.get, "/discover.json")
        info = json.loads(body)
        self.assertEqual((status, info["BaseURL"], info["LineupURL"]), (200, base, base + "/lineup.json"))
        self.assertEqual(info["DeviceID"], self.settings.uuid)
        _, _, body = await asyncio.to_thread(self.get, "/lineup_status.json")
        self.assertEqual(json.loads(body)["ScanInProgress"], 0)
        _, _, body = await asyncio.to_thread(self.get, "/lineup.json")
        lineup = json.loads(body)
        self.assertEqual([(e["GuideNumber"], e["GuideName"]) for e in lineup], [("1000", "Alpha"), ("1001", "Beta")])
        status, headers, _ = await asyncio.to_thread(self.get, lineup[0]["URL"].removeprefix(base))
        self.assertEqual((status, headers["location"]), (302, "http://s/1"))
        for path in ("/device.xml", "/capability", "/"):
            _, headers, body = await asyncio.to_thread(self.get, path)
            self.assertIn("application/xml", headers["content-type"])
            self.assertIn(f"<UDN>uuid:{self.settings.uuid}</UDN>", body.decode())

    async def test_scan_in_progress_while_building(self) -> None:
        self.service.building = True
        _, _, body = await asyncio.to_thread(self.get, "/lineup_status.json")
        self.assertEqual(json.loads(body)["ScanInProgress"], 1)
        self.service.building = False

    async def test_lineup_follows_tuner_setting(self) -> None:
        self.settings.tuner = 3
        _, _, body = await asyncio.to_thread(self.get, "/discover.json")
        info = json.loads(body)
        self.assertEqual((info["TunerCount"], info["DeviceID"]), (3, self.settings.uuid + ":3"))

    async def test_build_report(self) -> None:
        self.assertTrue(self.report.ok, self.report)
        # only channels whose tvg-id is in the guide start active
        self.assertEqual(self.report.channels, 2)
        self.assertEqual(self.report.streams, 3)
        self.assertEqual(self.report.programmes, 2)

    async def test_playlist_and_redirect(self) -> None:
        status, headers, body = await asyncio.to_thread(self.get, "/m3u/threadfin.m3u")
        self.assertEqual(status, 200)
        self.assertIn("audio/x-mpegurl", headers["content-type"])
        lines = body.decode().splitlines()
        self.assertTrue(lines[0].startswith("#EXTM3U url-tvg=\"http://127.0.0.1:%d/xmltv/threadfin.xml\"" % self.port))
        links = [line for line in lines if line.startswith("http")]
        self.assertEqual(len(links), 2)
        self.assertTrue(all(link.startswith(f"http://127.0.0.1:{self.port}/stream/") for link in links))
        targets = []
        for link in links:
            status, headers, _ = await asyncio.to_thread(self.get, link.removeprefix(f"http://127.0.0.1:{self.port}"))
            self.assertEqual(status, 302)
            targets.append(headers["location"])
        self.assertEqual(sorted(targets), ["http://s/1", "http://s/2"])

    async def test_group_filter(self) -> None:
        _, _, body = await asyncio.to_thread(self.get, "/m3u/threadfin.m3u?group-title=Sports")
        self.assertEqual(body.decode().count("#EXTINF"), 1)
        self.assertIn("Beta", body.decode())

    async def test_several_groups(self) -> None:
        _, _, body = await asyncio.to_thread(self.get, "/m3u/threadfin.m3u?group-title=Sports,News")
        self.assertEqual(body.decode().count("#EXTINF"), 2)

    async def test_unknown_stream_is_404(self) -> None:
        status, _, _ = await asyncio.to_thread(self.get, "/stream/" + "0" * 32)
        self.assertEqual(status, 404)

    async def test_guide_served_plain_and_gzipped(self) -> None:
        status, headers, body = await asyncio.to_thread(self.get, "/xmltv/threadfin.xml")
        self.assertEqual(status, 200)
        root = ET.fromstring(body)
        self.assertEqual([e.get("id") for e in root if e.tag == "channel"], ["1000", "1001"])
        self.assertEqual(len([e for e in root if e.tag == "programme"]), 2)
        _, _, packed = await asyncio.to_thread(self.get, "/xmltv/threadfin.xml.gz")
        self.assertEqual(gzip.decompress(packed), body)

    async def test_state_survives_restart(self) -> None:
        again = Service(self.service.system, self.settings)
        self.assertEqual(set(again.channels), set(self.service.channels))
        some = next(iter(self.service.registry.entries))
        self.assertIsNotNone(again.stream_target(some))

    async def test_rebuild_drops_vanished_channels(self) -> None:
        before = set(self.service.registry.entries)
        self.service.manager.local_path("m3u", next(iter(self.settings.files.m3u))).write_text(
            '#EXTM3U\n#EXTINF:-1 tvg-id="a" tvg-name="Alpha" group-title="News",Alpha\nhttp://s/9\n'
        )
        await asyncio.to_thread(self.service.build)
        (current,) = self.service.registry.entries
        self.assertNotIn(current, before)
        self.assertEqual(self.service.stream_target(current), "http://s/9")
        for old in before:
            self.assertIsNone(self.service.stream_target(old))

    async def test_udpxy_rewrite(self) -> None:
        self.settings.udpxy = "10.0.0.1:4022"
        uid = next(iter(self.service.registry.entries))
        self.service.registry.entries[uid].url = "udp://@239.0.0.1:1234"
        self.assertEqual(self.service.stream_target(uid), "http://10.0.0.1:4022/udp/239.0.0.1:1234/")
        self.service.registry.entries[uid].url = "udp://239.0.0.1:1234"  # no '@': not multicast syntax
        self.assertEqual(self.service.stream_target(uid), "udp://239.0.0.1:1234")


class PMSMode(Base):
    SOURCE = "PMS"

    async def test_no_guide_or_playlist_work(self) -> None:
        self.assertTrue(self.report.ok, self.report)
        self.assertFalse(self.service.xmltv_path.exists())
        self.assertEqual(self.service.channels, {})
        self.assertEqual(self.report.streams, 3)

    async def test_lineup_uses_provider_streams(self) -> None:
        _, _, body = await asyncio.to_thread(self.get, "/lineup.json")
        lineup = json.loads(body)
        self.assertEqual([e["GuideName"] for e in lineup], ["Alpha", "Beta", "Gamma \"Q\""])
        self.assertEqual([e["GuideNumber"] for e in lineup], ["Alpha", "Beta", "Gamma"])  # tvg-name is the provider's own number
        _, _, again = await asyncio.to_thread(self.get, "/lineup.json")
        self.assertEqual(json.loads(again), lineup)
        base = f"http://127.0.0.1:{self.port}"
        status, headers, _ = await asyncio.to_thread(self.get, lineup[2]["URL"].removeprefix(base))
        self.assertEqual((status, headers["location"]), (302, "http://s/3"))

    async def test_update_skips_guide_downloads(self) -> None:
        kinds: list[str] = []
        real = self.service.manager.refresh

        async def spy(kind: str, provider_id: str | None = None):
            kinds.append(kind)
            return await real(kind, provider_id)

        self.service.manager.refresh = spy  # type: ignore[method-assign]
        await self.service.update()
        self.assertEqual(kinds, ["m3u", "hdhr"])

    async def test_links_survive_a_rebuild(self) -> None:
        _, _, body = await asyncio.to_thread(self.get, "/lineup.json")
        link = json.loads(body)[0]["URL"].removeprefix(f"http://127.0.0.1:{self.port}")
        await asyncio.to_thread(self.service.build)
        status, _, _ = await asyncio.to_thread(self.get, link)
        self.assertEqual(status, 302)


if __name__ == "__main__":
    unittest.main()


class AuthEndpoints(Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.service.auth.create_first_user("admin", "pw")  # every level but the API
        self.service.auth.save_new_user({"username": "viewer", "password": "v:1", LEVEL_M3U: True})

    async def status(self, path: str, headers: dict[str, str] | None = None) -> int:
        status, _, body = await asyncio.to_thread(self.get, path, headers)
        if status == 403:
            self.assertEqual(body, b"Forbidden [403]\n")
        return status

    async def test_open_while_disabled(self) -> None:
        for path in ("/m3u/threadfin.m3u", "/xmltv/threadfin.xml", "/xmltv/threadfin.xml.gz", "/lineup.json"):
            self.assertEqual(await self.status(path), 200, path)

    async def test_m3u_and_xmltv_url_credentials(self) -> None:
        self.settings.authentication_m3u = True
        self.settings.authentication_xml = True
        cases = [
            ("/m3u/threadfin.m3u", 403),
            ("/m3u/threadfin.m3u?username=admin&password=bad", 403),
            ("/m3u/threadfin.m3u?username=admin&password=pw", 200),
            ("/m3u/threadfin.m3u?username=admin&password=pw&group-title=News", 200),
            ("/m3u/threadfin.m3u?username=viewer&password=v%3A1", 200),
            ("/m3u/threadfin.m3u?username=admin&username=viewer&password=pw", 200),  # first value wins
            ("/m3u/threadfin.m3u?username=viewer&username=admin&password=pw", 403),
            ("/xmltv/threadfin.xml?username=admin&password=pw", 200),
            ("/xmltv/threadfin.xml.gz?username=admin&password=pw", 200),
            ("/xmltv/threadfin.xml?username=viewer&password=v%3A1", 403),  # no xml level
            ("/xmltv/threadfin.xml.gz", 403),
            ("/lineup.json", 200),  # authentication.pms is off
        ]
        for path, want in cases:
            self.assertEqual(await self.status(path), want, path)

    async def test_m3u_and_xmltv_settings_are_separate(self) -> None:
        self.settings.authentication_xml = True
        self.assertEqual(await self.status("/m3u/threadfin.m3u"), 200)
        self.assertEqual(await self.status("/xmltv/threadfin.xml"), 403)
        self.settings.authentication_xml, self.settings.authentication_m3u = False, True
        self.assertEqual(await self.status("/m3u/threadfin.m3u"), 403)
        self.assertEqual(await self.status("/xmltv/threadfin.xml"), 200)

    async def test_lineup_basic_auth(self) -> None:
        self.settings.authentication_pms = True
        cases = [
            ({}, 403),
            ({"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}, 200),
            ({"Authorization": "Basic " + base64.b64encode(b"admin:bad").decode()}, 403),
            ({"Authorization": "Basic " + base64.b64encode(b"viewer:v:1").decode()}, 403),  # no pms level
            ({"Authorization": "Basic " + base64.b64encode(b"admin").decode()}, 403),  # Go: crashed
            ({"Authorization": "Basic !!!"}, 403),
            ({"Authorization": "Bearer x"}, 403),
        ]
        for headers, want in cases:
            self.assertEqual(await self.status("/lineup.json", headers), want, headers)
        # the other HDHomeRun endpoints stay open (Plex discovers the tuner before it has credentials)
        for path in ("/discover.json", "/lineup_status.json", "/device.xml"):
            self.assertEqual(await self.status(path), 200, path)
        # the viewer was denied pms, which Go records as false
        viewer = next(u for u in self.service.auth.users.values() if u["data"].get("username") == "viewer")
        self.assertIs(viewer["data"][LEVEL_PMS], False)
