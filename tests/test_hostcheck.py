"""The ``Host`` check against DNS rebinding (access.host_allowed and web.HostCheck)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from webfixture import AppTestCase

from threadfin.access import ALLOWED_HOSTS_ENV, configured_hosts, host_allowed, host_name


class HostRules(unittest.TestCase):
    def test_host_name(self) -> None:
        for header, name in [
            ("NAS:34400", "nas"),
            ("192.168.1.2:34400", "192.168.1.2"),
            ("[::1]:34400", "::1"),
            ("[fe80::1]", "fe80::1"),
            ("::1", "::1"),  # bare IPv6 (no port possible)
            ("example.com.", "example.com"),
            (" tv.example.com ", "tv.example.com"),
            ("", ""),
        ]:
            with self.subTest(header):
                self.assertEqual(host_name(header), name)

    def test_names_an_attacker_cannot_put_in_public_dns_are_allowed(self) -> None:
        for host in ["192.168.1.2:34400", "10.0.0.5", "[::1]:34400", "[fd00::2]:80", "localhost:34400",
                     "nas", "NAS:34400", "nas.local", "tv.lan", "box.home.arpa", "box.internal", "app.localhost",
                     "", "testserver"]:  # fmt: skip
            with self.subTest(host):
                self.assertTrue(host_allowed(host, set()))

    def test_public_names_are_refused(self) -> None:
        for host in ["evil.example.com", "evil.example.com:34400", "rebind.attacker.io", "192.168.1.2.nip.io",
                     "local.example.com", "nas.local.evil.com", "lan.evil.com"]:  # fmt: skip
            with self.subTest(host):
                self.assertFalse(host_allowed(host, set()))

    def test_configured_and_listed_names(self) -> None:
        settings = SimpleNamespace(http_threadfin_domain="tv.example.com:8080", https_threadfin_domain="TV.Example.org")
        extra = configured_hosts(settings, " a.example.net , B.example.net:443 ,, ")
        self.assertEqual(extra, {"tv.example.com", "tv.example.org", "a.example.net", "b.example.net"})
        for host in ["tv.example.com", "tv.example.org:443", "a.example.net", "b.example.net:34400"]:
            self.assertTrue(host_allowed(host, extra))
        self.assertFalse(host_allowed("c.example.net", extra))
        self.assertTrue(host_allowed("anything.example.com", {"*"}))

    def test_environment_variable(self) -> None:
        settings = SimpleNamespace(http_threadfin_domain="", https_threadfin_domain="")
        with mock.patch.dict("os.environ", {ALLOWED_HOSTS_ENV: "tv.example.com"}):
            self.assertEqual(configured_hosts(settings), {"tv.example.com"})
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(configured_hosts(settings), set())


EVIL = {"Host": "rebind.attacker.example:34400"}


class HostCheckApp(AppTestCase):
    PROVIDERS = False

    def test_ui_and_apis_refuse_public_names(self) -> None:
        for method, path in [("GET", "/web/"), ("GET", "/web"), ("GET", "/web/static/htmx.min.js"),
                             ("GET", "/api/v1/status"), ("POST", "/api/v1/build"), ("POST", "/api/"),
                             ("GET", "/api"), ("GET", "/ppv/enable")]:  # fmt: skip
            with self.subTest(path=path, method=method):
                response = self.client.request(method, path, headers={**EVIL, "X-Requested-With": "x"})
                self.assertEqual(response.status_code, 403)
                self.assertIn("unknown host 'rebind.attacker.example'", response.text)
                self.assertIn(ALLOWED_HOSTS_ENV, response.text)

    def test_nothing_runs_for_a_refused_request(self) -> None:
        self.service.settings.api = True
        with mock.patch.object(self.service, "rebuild") as rebuild:
            self.client.post("/api/v1/build", headers={**EVIL, "X-Requested-With": "x"})
            self.client.post("/api/", json={"cmd": "update.xepg"}, headers=EVIL)
        rebuild.assert_not_called()

    def test_player_and_hdhomerun_endpoints_answer_any_name(self) -> None:
        for path in ["/discover.json", "/lineup.json", "/lineup_status.json", "/device.xml", "/",
                     "/m3u/threadfin.m3u", "/webhooks-not-guarded"]:  # fmt: skip
            with self.subTest(path):
                self.assertNotEqual(self.client.get(path, headers=EVIL).status_code, 403)

    def test_allowed_names_reach_the_ui(self) -> None:
        for host in ["192.168.1.2:34400", "nas:34400", "nas.local", "localhost"]:
            with self.subTest(host):
                self.assertEqual(self.client.get("/api/v1/status", headers={"Host": host}).status_code, 200)

    def test_configured_domain_is_allowed_without_a_restart(self) -> None:
        self.assertEqual(self.client.get("/api/v1/status", headers={"Host": "tv.example.com"}).status_code, 403)
        self.service.settings.http_threadfin_domain = "tv.example.com"
        self.assertEqual(self.client.get("/api/v1/status", headers={"Host": "tv.example.com"}).status_code, 200)

    def test_each_refused_name_is_logged_once(self) -> None:
        with self.assertLogs("threadfin", "WARNING") as logs:
            for _ in range(3):
                self.client.get("/web/", headers=EVIL)
            self.client.get("/web/", headers={"Host": "other.example.com"})
        self.assertEqual(len(logs.records), 2)


if __name__ == "__main__":
    unittest.main()
