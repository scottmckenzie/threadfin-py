"""Shared set-up for the REST API and web UI tests: a local provider server and an app around a
Service with one playlist and one guide."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from starlette.testclient import TestClient

from threadfin.service import Service
from threadfin.settings import load_settings
from threadfin.system import SystemInfo
from threadfin.web import create_app

M3U = (
    "#EXTM3U\n"
    '#EXTINF:-1 tvg-id="a" tvg-name="Alpha" group-title="News" tvg-logo="{images}/a.png",Alpha\nhttp://s/1\n'
    '#EXTINF:-1 tvg-id="b" tvg-name="Beta" group-title="Sports" tvg-logo="{images}/bad.png",Beta\nhttp://s/2\n'
    '#EXTINF:-1 tvg-id="c" tvg-name="Gamma" group-title="News",Gamma\nhttp://s/3\n'
)  # {images}: the provider server's /img/ (a.png and poster.jpg are images, bad.png is an HTML page)
XMLTV = (
    '<?xml version="1.0"?><tv>'
    '<channel id="a"><display-name>Alpha TV</display-name></channel>'
    '<channel id="b"><display-name>Beta TV</display-name></channel>'
    '<channel id="c"><display-name>Gamma TV</display-name></channel>'
    '<programme channel="a" start="20260101000000 +0000" stop="20260101010000 +0000"><title>A</title>'
    '<icon src="{images}/poster.jpg"/></programme>'
    "</tv>"
)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


class Provider(BaseHTTPRequestHandler):
    hits: ClassVar[dict[str, int]] = {}

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        type(self).hits[self.path] = type(self).hits.get(self.path, 0) + 1
        if self.path.endswith("/missing.m3u"):
            self.send_error(404)
            return
        images = f"http://{self.headers['Host']}/img"
        if self.path.startswith("/img/"):
            body = {"/img/a.png": PNG, "/img/poster.jpg": JPEG}.get(self.path, b"<html>not found</html>")
        else:
            body = (M3U if self.path.endswith(".m3u") else XMLTV).replace("{images}", images).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AppTestCase(unittest.TestCase):
    """``self.client`` talks to a fresh app; ``self.url`` serves the M3U / XMLTV fixtures."""

    PROVIDERS = True  # add one playlist and one guide (XEPG mode)

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        Provider.hits = {}

        self.system = SystemInfo(config=Path(tmp.name) / "cfg", temp=Path(tmp.name) / "tmp")
        self.system.create_folders()
        self.system.create_files()
        self.settings = load_settings(self.system)
        self.settings.epg_source = "XEPG"
        self.service = Service(self.system, self.settings)
        if self.PROVIDERS:
            asyncio.run(self._add_providers())
        self.client = TestClient(create_app(self.service, build_on_start=False, schedule=False))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    async def _add_providers(self) -> None:
        # The guide first: channels are mapped by tvg-id when they are created.
        await self.service.add_provider("xmltv", {"file.source": f"{self.url}/g.xml", "name": "Guide"})
        await self.service.add_provider("m3u", {"file.source": f"{self.url}/p.m3u", "name": "Playlist"})

    def channel_ids(self) -> dict[str, str]:
        """Channel name -> id."""
        return {c.name: cid for cid, c in self.service.channels.items()}

    def enable_auth(self) -> None:
        self.service.settings.authentication_web = True
