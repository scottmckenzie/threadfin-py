"""Test module for threadfin.redact functions."""

from __future__ import annotations

import unittest

from threadfin.redact import clean_text, display_url


class DisplayURL(unittest.TestCase):
    def test_query_is_hidden(self) -> None:
        self.assertEqual(
            display_url("http://provider.tv:8080/get.php?username=U&password=P&type=m3u_plus"),
            "http://provider.tv:8080/get.php?…",
        )

    def test_unchanged_without_query(self) -> None:
        self.assertEqual(display_url("http://host/nope"), "http://host/nope")
        self.assertEqual(display_url("http://h/a%20b"), "http://h/a%20b")

    def test_fragment_dropped(self) -> None:
        self.assertEqual(display_url("http://h/x#frag"), "http://h/x")
        self.assertEqual(display_url("http://h/x?a=1#f"), "http://h/x?…")

    def test_userinfo_password(self) -> None:
        self.assertEqual(display_url("http://user:pass@host/x"), "http://user:***@host/x")
        self.assertEqual(display_url("http://user@host/x"), "http://user@host/x")

    def test_relative_path(self) -> None:
        self.assertEqual(
            display_url("/m3u/threadfin.m3u?username=u&password=p"),
            "/m3u/threadfin.m3u?…",
        )

    def test_password_containing_at_sign(self) -> None:
        self.assertEqual(display_url("http://u:p@ss@h/x"), "http://u:***@h/x")

    def test_unparseable(self) -> None:
        self.assertEqual(display_url("http://[::1/x?password=p"), "http://[::1/x?…")
        self.assertEqual(display_url(""), "")
        self.assertEqual(display_url("no url"), "no url")


class CleanText(unittest.TestCase):
    def test_every_url_in_text(self) -> None:
        self.assertEqual(
            clean_text("http://a/x?p=1: redirected to 'https://b/y?token=T'"),
            "http://a/x?…: redirected to 'https://b/y?…'",
        )

    def test_text_without_urls_unchanged(self) -> None:
        self.assertEqual(clean_text("Connection refused"), "Connection refused")

    def test_port_and_userinfo_urls(self) -> None:
        self.assertEqual(clean_text("http://h:8080/x?password=S: refused"), "http://h:8080/x?…: refused")
        self.assertEqual(clean_text("see http://u:pw@h:81/x."), "see http://u:***@h:81/x.")

    def test_other_schemes_are_cleaned(self) -> None:
        result = clean_text("fetch ftp://u:pw@h/x?k=v failed")
        self.assertNotIn("pw", result)
        self.assertNotIn("k=v", result)
        self.assertEqual(result, "fetch ftp://u:***@h/x?… failed")
