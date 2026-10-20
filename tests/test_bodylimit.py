"""Tests for request body size limits."""

from __future__ import annotations

from collections.abc import Iterator

from webfixture import AppTestCase

from threadfin.auth import LEVEL_WEB

MB = 1024 * 1024
JSON = {"content-type": "application/json"}


class BodyLimit(AppTestCase):
    def test_declared_size_over_limit(self) -> None:
        before = len(self.client.get("/api/v1/users").json())
        response = self.client.post(
            "/api/v1/users",
            content=b"x" * (8 * MB + 1),
            headers=JSON,
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"error": "request body too large (limit 8 MB)"})
        after = len(self.client.get("/api/v1/users").json())
        self.assertEqual(before, after)

    def test_chunked_over_limit(self) -> None:
        def chunks() -> Iterator[bytes]:
            for _ in range(9):
                yield b"x" * MB

        before = len(self.client.get("/api/v1/users").json())
        response = self.client.post(
            "/api/v1/users",
            content=chunks(),
            headers=JSON,
        )
        self.assertEqual(response.status_code, 413)
        after = len(self.client.get("/api/v1/users").json())
        self.assertEqual(before, after)

    def test_ui_gets_plain_text(self) -> None:
        response = self.client.post(
            "/web/login",
            content=b"x" * (8 * MB + 1),
        )
        self.assertEqual(response.status_code, 413)
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.assertEqual(response.text, "request body too large (limit 8 MB)")

    def test_under_limit_unaffected(self) -> None:
        response = self.client.post(
            "/api/v1/users",
            json={"username": "bob", "password": "pw", LEVEL_WEB: True},
        )
        self.assertEqual(response.status_code, 201)

    def test_restore_limit(self) -> None:
        response = self.client.post(
            "/api/v1/restore",
            content=b"x" * (9 * MB),
            headers={"X-Requested-With": "x"},
        )
        self.assertEqual(response.status_code, 400)  # not a zip
        response = self.client.post(
            "/api/v1/restore",
            content=b"x" * (64 * MB + 1),
            headers={"X-Requested-With": "x"},
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"error": "request body too large (limit 64 MB)"})

    def test_get_unaffected(self) -> None:
        response = self.client.get("/m3u/threadfin.m3u")
        self.assertEqual(response.status_code, 200)
