"""scrypt password hashes: format, verification, upgrade of Go hashes, and the verified-credential cache."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from threadfin.auth import (
    LEVEL_M3U,
    LEVEL_PMS,
    LOGIN_FAILED,
    PASSWORD_COST,
    Authentication,
    AuthError,
    credential_hash,
    hash_password,
    verify_password,
)

CHEAP = (4, 1, 1)
GO_USER = {
    "_id": "id-AAAAAAAAAA",
    "_salt": "S" * 20,
    "_username": credential_hash("alice"),
    "_password": credential_hash("secret"),  # as the Go app stores it
    "data": {"username": "alice", LEVEL_PMS: True, LEVEL_M3U: True, "authentication.web": True},
}


class Hashes(unittest.TestCase):
    def test_format_and_round_trip(self) -> None:
        stored = hash_password("pässword", CHEAP)
        prefix, name, params, salt, digest = stored.split("$")
        self.assertEqual((prefix, name, params), ("", "scrypt", "ln=4,r=1,p=1"))
        self.assertEqual(len(base64.b64decode(salt + "==")), 16)
        self.assertEqual(len(base64.b64decode(digest + "=")), 32)
        self.assertEqual(verify_password(stored, "pässword", CHEAP), (True, False))
        self.assertEqual(verify_password(stored, "pässword".encode(), CHEAP), (True, False))
        self.assertEqual(verify_password(stored, "password", CHEAP), (False, False))

    def test_salted(self) -> None:
        self.assertNotEqual(hash_password("x", CHEAP), hash_password("x", CHEAP))

    def test_default_cost(self) -> None:
        self.assertEqual(PASSWORD_COST, (14, 8, 5))  # OWASP's scrypt N=2^14, r=8, p=5
        self.assertTrue(hash_password("x").startswith("$scrypt$ln=14,r=8,p=5$"))

    def test_go_hashes_match_and_need_a_new_hash(self) -> None:
        self.assertEqual(verify_password(credential_hash("secret"), "secret"), (True, True))
        self.assertEqual(verify_password(credential_hash("secret"), "other"), (False, False))

    def test_weaker_parameters_need_a_new_hash(self) -> None:
        stored = hash_password("x", CHEAP)
        self.assertEqual(verify_password(stored, "x", (5, 1, 1)), (True, True))
        self.assertEqual(verify_password(stored, "y", (5, 1, 1)), (False, False))

    def test_malformed_hashes_never_match(self) -> None:
        good = hash_password("x", CHEAP)
        _, _, params, salt, digest = good.split("$")
        for bad in ["", "$scrypt$", f"$scrypt${params}${salt}", f"$scrypt$ln=4,r=1${salt}${digest}",
                    f"$scrypt$ln=4,r=1,p=1,x=2${salt}${digest}", f"$scrypt$ln=99,r=1,p=1${salt}${digest}",
                    f"$scrypt$ln=0,r=1,p=1${salt}${digest}", f"$scrypt$ln=a,r=1,p=1${salt}${digest}",
                    f"$scrypt${params}$!!!${digest}", f"$scrypt${params}${salt}${digest}$extra", "plain"]:  # fmt: skip
            with self.subTest(bad):
                self.assertEqual(verify_password(bad, "x", CHEAP), (False, False))


class Upgrade(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "authentication.json"
        self.path.write_text(json.dumps({"dbVersion": "1.0", "hash": "sha256", "users": {GO_USER["_id"]: GO_USER}}))
        self.auth = Authentication(self.path, password_cost=CHEAP)

    def stored(self) -> str:
        return json.loads(self.path.read_text())["users"][GO_USER["_id"]]["_password"]

    def test_go_hash_replaced_at_the_next_successful_login(self) -> None:
        with self.assertRaisesRegex(AuthError, LOGIN_FAILED):
            self.auth.login("alice", "wrong")
        self.assertEqual(self.stored(), credential_hash("secret"))  # a failed login changes nothing
        self.auth.login("alice", "secret")
        self.assertTrue(self.stored().startswith("$scrypt$ln=4,r=1,p=1$"))
        self.assertEqual(json.loads(self.path.read_text())["users"][GO_USER["_id"]]["_username"], GO_USER["_username"])
        again = Authentication(self.path, password_cost=CHEAP)  # still works after a restart
        again.login("alice", "secret")
        with self.assertRaises(AuthError):
            again.login("alice", "wrong")

    def test_basic_and_url_credentials_upgrade_too(self) -> None:
        self.auth.basic_auth("Basic " + base64.b64encode(b"alice:secret").decode(), LEVEL_PMS)
        self.assertTrue(self.stored().startswith("$scrypt$"))

    def test_weaker_scrypt_hash_replaced(self) -> None:
        self.auth.login("alice", "secret")
        weak = self.stored()
        stronger = Authentication(self.path, password_cost=(5, 1, 1))
        stronger.login("alice", "secret")
        self.assertNotEqual(self.stored(), weak)
        self.assertTrue(self.stored().startswith("$scrypt$ln=5,r=1,p=1$"))

    def test_new_users_and_password_changes_get_scrypt(self) -> None:
        user_id = self.auth.save_new_user({"username": "bob", "password": "pw1", LEVEL_M3U: True})
        self.assertTrue(self.auth.users[user_id]["_password"].startswith("$scrypt$"))
        self.auth.change_credentials(user_id, "bob", "pw2")
        changed = self.auth.users[user_id]["_password"]
        self.assertTrue(changed.startswith("$scrypt$ln=4,r=1,p=1$"))
        self.assertEqual(verify_password(changed, "pw2", CHEAP), (True, False))
        self.auth.url_auth("bob", "pw2", LEVEL_M3U)
        with self.assertRaises(AuthError):
            self.auth.url_auth("bob", "pw1", LEVEL_M3U)


class Timing(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.auth = Authentication(Path(tmp.name) / "authentication.json", password_cost=CHEAP)
        self.auth.create_first_user("admin", "secret")

    def scrypt_calls(self, fn) -> int:
        with (
            mock.patch("threadfin.auth.hashlib.scrypt", side_effect=hashlib.scrypt) as spy,
            contextlib.suppress(AuthError),
        ):
            fn()
        return spy.call_count

    def test_unknown_name_costs_one_scrypt_like_a_known_one(self) -> None:
        self.scrypt_calls(lambda: self.auth.login("nobody", "x"))  # makes the dummy hash
        self.assertEqual(self.scrypt_calls(lambda: self.auth.login("nobody", "x")), 1)
        self.assertEqual(self.scrypt_calls(lambda: self.auth.login("admin", "wrong")), 1)

    def test_repeated_correct_credentials_skip_scrypt(self) -> None:
        header = "Basic " + base64.b64encode(b"admin:secret").decode()
        self.assertEqual(self.scrypt_calls(lambda: self.auth.basic_auth(header, LEVEL_PMS)), 1)
        self.assertEqual(self.scrypt_calls(lambda: self.auth.basic_auth(header, LEVEL_PMS)), 0)
        self.assertEqual(self.scrypt_calls(lambda: self.auth.url_auth("admin", "wrong", LEVEL_M3U)), 1)

    def test_cache_does_not_outlive_a_password_change(self) -> None:
        self.auth.login("admin", "secret")
        user_id = next(iter(self.auth.users))
        self.auth.change_credentials(user_id, "", "new")
        with self.assertRaisesRegex(AuthError, LOGIN_FAILED):
            self.auth.login("admin", "secret")
        self.auth.login("admin", "new")

    def test_cache_is_bounded(self) -> None:
        for i in range(300):
            self.auth.save_new_user({"username": f"u{i}", "password": "pw"})
            self.auth.login(f"u{i}", "pw")
        self.assertLessEqual(len(self.auth._verified), 256)


if __name__ == "__main__":
    unittest.main()
