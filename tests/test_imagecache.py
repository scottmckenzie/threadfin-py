"""Image cache: lookups against the real Go imgcache (tests/golden/images), downloads, and the cache
through the app (guide, playlist, /images/)."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from webfixture import JPEG, PNG, AppTestCase, Provider

from threadfin.imagecache import (
    ImageCache,
    cache_name,
    configured_base,
    default_base,
    image_headers,
    looks_like_image,
)
from threadfin.providers import Download, ProviderError

GOLDEN = Path(__file__).parent / "golden" / "images"
CASES = sorted(GOLDEN.glob("*.input.json"))
GO_BASE = "http://192.168.1.2:34400"  # the cacheURL the Go cases use

# Go cached any extension (and served it with that type); only images are cached here.
NOT_CACHED_HERE = {"http://img.example/page.html"}


def settings(**kw: Any) -> SimpleNamespace:
    base = {"force_https": False, "https_threadfin_domain": "", "https_port": 443, "http_threadfin_domain": "",
            "port": "34400", "bind_ip_address": ""}  # fmt: skip
    return SimpleNamespace(**{**base, **kw})


def case_settings(spec: dict[str, Any]) -> SimpleNamespace:
    return settings(force_https=spec["forceHttps"], https_threadfin_domain=spec["httpsDomain"],
                    https_port=spec["httpsPort"], http_threadfin_domain=spec["httpDomain"], port=spec["port"])  # fmt: skip


def normalise(url: str) -> str:
    """Go wrote default ports (https://host:443/...); they mean the same."""
    return url.replace(":443/", "/", 1) if url.startswith("https://") else url


class Goldens(unittest.TestCase):
    def test_there_are_cases(self) -> None:
        self.assertGreaterEqual(len(CASES), 8)

    def test_cases(self) -> None:
        for path in CASES:
            name = path.name.removesuffix(".input.json")
            spec = json.loads(path.read_text())
            golden = json.loads((GOLDEN / f"{name}.golden.json").read_text())
            if not spec["caching"]:
                with self.subTest(name):  # caching off: Go only trims CR/LF; the guide uses the URLs as they are
                    self.assertEqual([lk["first"] for lk in golden["lookups"]], [s.strip("\r\n") for s in spec["sources"]])
                    self.assertEqual(golden["queue"], [])
                continue
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                for file in spec["files"]:
                    name_ = cache_name(file.removeprefix("md5:"))
                    if name_:
                        (Path(tmp) / name_).write_bytes(PNG)
                cache = ImageCache(tmp)
                base = configured_base(case_settings(spec)) or GO_BASE
                session = cache.session(base)
                for lookup in golden["lookups"]:
                    src = lookup["source"]
                    ours = session.url(src)
                    if src in NOT_CACHED_HERE:
                        if f"md5:{src}" in spec["files"]:  # Go served the cached .html page from this server
                            self.assertIn("/images/", lookup["second"])
                        self.assertEqual(ours, src)
                    else:
                        # Go's second answer is the consistent one (the first ignored the configured domain).
                        self.assertEqual(ours, normalise(lookup["second"]), src)
                # Go queued every URL (and downloaded logo.png and logo.png?x into the same file); one per file here.
                go_queue: dict[str, str] = {}
                for src in golden["queue"]:
                    if cache_name(src) is not None:
                        go_queue.setdefault(cache_name(src) or "", src)
                self.assertEqual(session.wanted, go_queue)


class Names(unittest.TestCase):
    def test_cache_name(self) -> None:
        self.assertEqual(cache_name("http://h/a/logo.png?x=1"), cache_name("http://h/a/logo.png"))
        self.assertTrue(cache_name("http://h/a/Logo.PNG").endswith(".PNG"))
        for src in ("http://h/page.html", "http://h/noext", "ftp://h/a.png", "a.png", "", "http:///a.png",
                    "javascript:alert(1)//.png"):  # fmt: skip
            with self.subTest(src):
                self.assertIsNone(cache_name(src))

    def test_looks_like_image(self) -> None:
        self.assertTrue(looks_like_image(PNG, "a.png"))
        self.assertTrue(looks_like_image(JPEG, "a.jpg"))
        self.assertTrue(looks_like_image(b"GIF89a....", "a.gif"))
        self.assertTrue(looks_like_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "a.webp"))
        self.assertTrue(looks_like_image(b'<?xml version="1.0"?>\n<svg xmlns="..."/>', "a.svg"))
        self.assertFalse(looks_like_image(b"<html><script>", "a.png"))
        self.assertFalse(looks_like_image(b"<html><svg>", "a.svg"))

    def test_paths_stay_in_the_folder(self) -> None:
        cache = ImageCache("/tmp/cache")
        for name in ("../settings.json", "a.png", "d41d8cd98f00b204e9800998ecf8427e.html", "x/d41d8cd98f00b204e9800998ecf8427e.png"):
            with self.subTest(name):
                self.assertIsNone(cache.path(name))
        self.assertIsNotNone(cache.path("d41d8cd98f00b204e9800998ecf8427e.PNG"))

    def test_svg_cannot_script(self) -> None:
        self.assertIn("sandbox", image_headers("x.svg")["Content-Security-Policy"])
        self.assertEqual(image_headers("x.png"), {"X-Content-Type-Options": "nosniff"})

    def test_bases(self) -> None:
        self.assertEqual(configured_base(settings(force_https=True, https_threadfin_domain="tv.example")), "https://tv.example")
        self.assertEqual(configured_base(settings(force_https=True, https_threadfin_domain="tv.example", https_port=8443)),
                         "https://tv.example:8443")  # fmt: skip
        self.assertEqual(configured_base(settings(http_threadfin_domain="tv.example")), "http://tv.example:34400")
        self.assertEqual(configured_base(settings(http_threadfin_domain="tv.example:80")), "http://tv.example:80")
        self.assertIsNone(configured_base(settings(force_https=True)))  # no HTTPS domain: not forced
        self.assertEqual(default_base(settings(bind_ip_address="10.1.2.3")), "http://10.1.2.3:34400")
        self.assertRegex(default_base(settings()), r"^http://[0-9.]+:34400$")


class Downloads(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "images"
        self.calls: list[str] = []
        self.now = 1000.0
        self.bodies: dict[str, bytes] = {"http://h/a.png": PNG, "http://h/html.png": b"<html>"}

        def fetch(url: str, **kw: Any) -> Download:
            self.calls.append(url)
            self.assertEqual(kw["user_agent"], "UA")
            if url not in self.bodies:
                raise ProviderError("404")
            return Download("x", self.bodies[url])

        self.cache = ImageCache(self.folder, fetch=fetch, user_agent=lambda: "UA", clock=lambda: self.now)

    def test_fill_and_prune(self) -> None:
        session = self.cache.session("http://me:1")
        urls = [session.url(u) for u in ("http://h/a.png", "http://h/html.png", "http://h/gone.png", "http://h/a.png?v=2")]
        self.assertEqual(urls[0], "http://h/a.png")  # not cached yet
        self.assertEqual(self.cache.fill(session), (1, 2))
        name = cache_name("http://h/a.png")
        self.assertEqual((self.folder / name).read_bytes(), PNG)
        self.assertEqual(self.cache.lookup("http://h/a.png?v=3", "http://me:1/"), f"http://me:1/images/{name}")

        (self.folder / "stale.png").write_bytes(PNG)
        self.assertEqual(self.cache.prune({name}), 1)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), [name])

    def test_failures_wait_before_retrying(self) -> None:
        session = self.cache.session("http://me")
        session.url("http://h/gone.png")
        self.cache.fill(session)
        self.cache.fill(session)
        self.assertEqual(self.calls.count("http://h/gone.png"), 1)
        self.now += 6 * 3600
        self.cache.fill(session)
        self.assertEqual(self.calls.count("http://h/gone.png"), 2)

    def test_cached_images_are_not_downloaded_again(self) -> None:
        session = self.cache.session("http://me")
        session.url("http://h/a.png")
        self.cache.fill(session)
        session2 = self.cache.session("http://me")
        self.assertIn("/images/", session2.url("http://h/a.png"))
        self.assertEqual(session2.wanted, {})
        self.assertEqual(self.cache.fill(session2), (0, 0))
        self.assertEqual(self.calls, ["http://h/a.png"])


class ThroughTheApp(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.settings.http_threadfin_domain = "tv.example"

    def enable(self) -> None:
        self.assertEqual(self.client.patch("/api/v1/settings", json={"cache.images": True}).status_code, 200)
        self.wait()

    def wait(self) -> None:
        """Until no cache run is going (the app's event loop runs in the test client's thread)."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            task = self.service.image_task
            if not self.service.caching_images and (task is None or task.done()):
                return
            time.sleep(0.02)
        self.fail("the image cache did not finish")

    def test_off_by_default(self) -> None:
        guide = self.client.get("/xmltv/threadfin.xml").text
        self.assertIn(f"{self.url}/img/a.png", guide)
        self.assertNotIn("/images/", guide)
        self.assertEqual(list(self.system.folder_images_cache.iterdir()), [])

    def test_guide_playlist_and_serving(self) -> None:
        self.enable()
        logo, poster = cache_name(f"{self.url}/img/a.png"), cache_name(f"{self.url}/img/poster.jpg")
        self.assertEqual(sorted(p.name for p in self.system.folder_images_cache.iterdir()), sorted([logo, poster]))
        guide = self.client.get("/xmltv/threadfin.xml").text
        self.assertIn(f'src="http://tv.example:34400/images/{logo}"', guide)
        self.assertIn(f'src="http://tv.example:34400/images/{poster}"', guide)
        self.assertIn(f"{self.url}/img/bad.png", guide)  # an HTML page is not cached
        self.assertNotIn(cache_name(f"{self.url}/img/bad.png"), guide)

        playlist = self.client.get("/m3u/threadfin.m3u").text
        self.assertIn(f'tvg-logo="http://tv.example:34400/images/{logo}"', playlist)
        self.service.settings.http_threadfin_domain = ""
        playlist = self.client.get("/m3u/threadfin.m3u").text
        self.assertIn(f'tvg-logo="http://testserver/images/{logo}"', playlist)  # the address the client used

        served = self.client.get(f"/images/{logo}")
        self.assertEqual((served.status_code, served.content, served.headers["content-type"]), (200, PNG, "image/png"))
        self.assertEqual(served.headers["x-content-type-options"], "nosniff")
        for path in ("/images/..%2Fsettings.json", "/images/nope.png", f"/images/{logo[:-4]}.html"):
            with self.subTest(path):
                self.assertEqual(self.client.get(path).status_code, 404)

        # The channel database keeps the provider's URL (Go stored the cache URL in it).
        saved = json.loads(self.system.file_xepg.read_text())
        self.assertEqual({c["tvg-logo"] for c in saved.values()} - {""}, {f"{self.url}/img/a.png", f"{self.url}/img/bad.png"})

    def test_rebuild_does_not_download_again_and_prunes(self) -> None:
        self.enable()
        self.assertEqual(Provider.hits.get("/img/a.png"), 1)
        self.assertEqual(Provider.hits.get("/img/bad.png"), 1)
        (self.system.folder_images_cache / "0123456789abcdef0123456789abcdef.png").write_bytes(PNG)
        self.client.post("/api/v1/build", headers={"X-Requested-With": "x"})
        self.wait()
        self.assertEqual(Provider.hits.get("/img/a.png"), 1)
        self.assertEqual(Provider.hits.get("/img/bad.png"), 1)  # failed: retried later, not on every build
        self.assertEqual(len(list(self.system.folder_images_cache.iterdir())), 2)  # the unused file is gone

    def test_turning_it_off_clears_the_cache(self) -> None:
        self.enable()
        self.client.patch("/api/v1/settings", json={"cache.images": False})
        self.assertEqual(list(self.system.folder_images_cache.iterdir()), [])
        self.assertNotIn("/images/", self.client.get("/xmltv/threadfin.xml").text)

    def blocking_fetch(self) -> threading.Event:
        """Downloads wait until the returned event is set."""
        release = threading.Event()
        fetch = self.service.images._fetch

        def slow(url: str, **kw: Any) -> Download:
            release.wait(10)
            return fetch(url, **kw)

        self.service.images._fetch = slow
        return release

    def test_switched_off_while_downloading(self) -> None:
        release = self.blocking_fetch()
        self.client.patch("/api/v1/settings", json={"cache.images": True})
        self.client.patch("/api/v1/settings", json={"cache.images": False})
        release.set()
        self.wait()
        self.assertEqual(list(self.system.folder_images_cache.iterdir()), [])

    def test_build_while_downloading_gets_its_own_run(self) -> None:
        release = self.blocking_fetch()
        self.client.patch("/api/v1/settings", json={"cache.images": True})
        first = self.service.image_task
        # A new playlist logo appears while the first run is still downloading.
        channel_id = self.channel_ids()["Gamma"]
        self.client.patch(f"/api/v1/channels/{channel_id}", json={"tvg-logo": f"{self.url}/img/a.png?v=2",
                                                                 "x-name": "Gamma 2"})  # fmt: skip
        self.service.channels[channel_id].tvg_logo = f"{self.url}/img/poster.jpg"
        self.client.post("/api/v1/build", headers={"X-Requested-With": "x"})
        release.set()
        self.wait()
        self.assertIsNot(self.service.image_task, first)  # a follow-up run for the newer build
        guide = self.client.get("/xmltv/threadfin.xml").text
        self.assertIn(f"/images/{cache_name(self.url + '/img/a.png')}", guide)

    def test_shown_on_the_settings_page(self) -> None:
        self.assertIn('name="cache.images"', self.client.get("/web/settings").text)


if __name__ == "__main__":
    unittest.main()
