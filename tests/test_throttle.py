"""Throttling of failed credential checks (auth.LoginThrottle and its use by the web endpoints)."""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from webfixture import AppTestCase

from threadfin.auth import (
    FAILURE_WINDOW,
    LEVEL_M3U,
    LEVEL_PMS,
    LEVEL_WEB,
    LEVEL_XML,
    LOGIN_FAILED,
    MAX_FAILURES,
    NO_AUTHORIZATION,
    TOO_MANY_ATTEMPTS,
    Authentication,
    AuthError,
    LoginThrottle,
    TooManyAttempts,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Throttle(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.throttle = LoginThrottle(self.clock, max_failures=3, window=60)

    def record_failures(self, client: str = "a", times: int = 1) -> None:
        for _ in range(times):
            self.throttle.failed(client)

    def test_refused_after_max_failures_until_the_oldest_ages_out(self) -> None:
        self.record_failures(times=2)
        self.throttle.check("a")
        self.clock.now += 10
        self.record_failures()
        with self.assertRaises(TooManyAttempts) as caught:
            self.throttle.check("a")
        self.assertEqual(caught.exception.retry_after, 50)  # the first failure is 60 s old at 1060
        self.assertEqual(str(caught.exception), TOO_MANY_ATTEMPTS)
        self.assertIsInstance(caught.exception, AuthError)
        self.clock.now += 49.5
        with self.assertRaises(TooManyAttempts) as caught:
            self.throttle.check("a")
        self.assertEqual(caught.exception.retry_after, 1)  # never 0
        self.clock.now += 0.5
        self.throttle.check("a")  # the first two failures have aged out

    def test_clients_are_separate_and_success_clears(self) -> None:
        self.record_failures("a", 3)
        self.throttle.check("b")
        with self.assertRaises(TooManyAttempts):
            self.throttle.check("a")
        self.throttle.succeeded("a")
        self.throttle.check("a")

    def test_memory_is_bounded(self) -> None:
        from threadfin import auth

        old = auth.MAX_THROTTLED_CLIENTS
        auth.MAX_THROTTLED_CLIENTS = 5
        self.addCleanup(setattr, auth, "MAX_THROTTLED_CLIENTS", old)
        self.record_failures("old", 3)
        for i in range(8):
            self.clock.now += 1
            self.record_failures(f"c{i}")
        self.assertLessEqual(len(self.throttle._failures), 5)
        self.assertNotIn("old", self.throttle._failures)  # the least recent offender went first
        self.assertIn("c7", self.throttle._failures)


class AuthenticationThrottle(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = Clock()
        self.auth = Authentication(Path(tmp.name) / "authentication.json", clock=self.clock)
        self.auth.create_first_user("admin", "secret")

    def fail_logins(self, times: int, client: str = "10.0.0.9") -> None:
        for _ in range(times):
            with self.assertRaisesRegex(AuthError, LOGIN_FAILED):
                self.auth.login("admin", "wrong", client)

    def test_correct_password_refused_while_throttled(self) -> None:
        self.fail_logins(MAX_FAILURES)
        with self.assertRaises(TooManyAttempts):
            self.auth.login("admin", "secret", "10.0.0.9")
        self.auth.login("admin", "secret", "10.0.0.10")  # another client is not affected
        self.clock.now += FAILURE_WINDOW
        self.auth.login("admin", "secret", "10.0.0.9")

    def test_success_clears_the_count(self) -> None:
        self.fail_logins(MAX_FAILURES - 1)
        self.auth.login("admin", "secret", "10.0.0.9")
        self.fail_logins(MAX_FAILURES - 1)
        self.auth.login("admin", "secret", "10.0.0.9")

    def test_every_credential_check_counts(self) -> None:
        header = "Basic " + base64.b64encode(b"admin:wrong").decode()
        for _ in range(MAX_FAILURES // 2):
            with self.assertRaises(AuthError):
                self.auth.basic_auth(header, LEVEL_PMS, "10.0.0.9")
            with self.assertRaises(AuthError):
                self.auth.url_auth("admin", "wrong", LEVEL_XML, "10.0.0.9")
        with self.assertRaises(TooManyAttempts):
            self.auth.url_auth("admin", "secret", LEVEL_M3U, "10.0.0.9")

    def test_missing_level_is_not_a_failed_login(self) -> None:
        self.auth.save_new_user({"username": "viewer", "password": "pw", LEVEL_WEB: False})
        for _ in range(MAX_FAILURES + 2):
            with self.assertRaisesRegex(AuthError, NO_AUTHORIZATION):
                self.auth.url_auth("viewer", "pw", LEVEL_M3U, "10.0.0.9")
        self.auth.login("admin", "secret", "10.0.0.9")

    def test_internal_checks_without_a_client_are_not_throttled(self) -> None:
        self.fail_logins(MAX_FAILURES)
        self.auth.login("admin", "secret")


class ThrottledEndpoints(AppTestCase):
    PROVIDERS = False

    def setUp(self) -> None:
        super().setUp()
        self.enable_auth()
        self.service.auth.create_first_user("admin", "secret")
        for level in ("authentication_m3u", "authentication_xml", "authentication_pms"):
            setattr(self.service.settings, level, True)

    def exhaust(self) -> None:
        for _ in range(MAX_FAILURES):
            self.service.auth.throttle.failed("testclient")  # Starlette's TestClient address

    def assert_429(self, response) -> None:
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers["Retry-After"]), 0)

    def test_failures_over_http_lead_to_429(self) -> None:
        for _ in range(MAX_FAILURES):
            self.assertEqual(
                self.client.post("/api/v1/login", json={"username": "admin", "password": "x"}).status_code, 401
            )
        self.assert_429(self.client.post("/api/v1/login", json={"username": "admin", "password": "secret"}))

    def test_ui_login(self) -> None:
        self.exhaust()
        response = self.client.post("/web/login", data={"username": "admin", "password": "secret"},
                                    headers={"HX-Request": "true"}, follow_redirects=False)  # fmt: skip
        self.assert_429(response)
        self.assertIn(TOO_MANY_ATTEMPTS, response.text)
        self.assertNotIn("Token", response.cookies)

    def test_player_endpoints(self) -> None:
        self.exhaust()
        good = "Basic " + base64.b64encode(b"admin:secret").decode()
        self.assert_429(self.client.get("/lineup.json", headers={"Authorization": good}))
        self.assert_429(self.client.get("/m3u/threadfin.m3u?username=admin&password=secret"))
        self.assert_429(self.client.get("/xmltv/threadfin.xml?username=admin&password=secret"))

    def test_go_api_login(self) -> None:
        self.service.settings.api = True
        self.service.settings.authentication_api = True
        self.exhaust()
        response = self.client.post("/api/", json={"cmd": "login", "username": "admin", "password": "secret"})
        self.assertEqual(response.json(), {"err": TOO_MANY_ATTEMPTS, "status": False})

    def test_wrong_password_still_403_before_the_limit(self) -> None:
        bad = "Basic " + base64.b64encode(b"admin:nope").decode()
        self.assertEqual(self.client.get("/lineup.json", headers={"Authorization": bad}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
