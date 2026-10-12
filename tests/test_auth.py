"""Authentication parity with the real Go code (tests/golden/auth).

Every case is a sequence of steps against one authentication.json; after each step the result,
the error text and the database file must match Go (random salts / ids / tokens normalised).
"""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from threadfin.auth import (
    DEFAULT_USER_EXISTS,
    EMPTY_CREDENTIALS,
    ID_PATTERN,
    LEVEL_M3U,
    LEVEL_PMS,
    LEVEL_XML,
    LOGIN_FAILED,
    NO_AUTHORIZATION,
    NO_USER_FOR_TOKEN,
    SALT_PATTERN,
    SESSION_EXPIRED,
    Authentication,
    AuthError,
    credential_hash,
)

GOLDEN = Path(__file__).parent / "golden" / "auth"
CASES = sorted(GOLDEN.glob("*.input.json"))

# The first-user form could take over an existing account in Go: tested in test_divergences.
DIVERGENT_CASES = {"first_user_when_users_exist"}
# (case, step index) -> (our error, our result); the database must stay as it was before the step.
DIVERGENT_STEPS: dict[tuple[str, int], tuple[str, Any]] = {
    ("basic_headers", 15): (LOGIN_FAILED, None),  # Go accepted valid Base64 followed by garbage
    ("new_user", 5): (EMPTY_CREDENTIALS, None),  # Go created a user with an empty name and password
}


def normalise(db: Any, fixture_ids: set[str]) -> Any:
    """Replace random salts and new user ids by placeholders and the users map by a sorted list."""
    if not isinstance(db, dict) or "users" not in db:
        return db
    users = []
    for key, user in db["users"].items():
        user = dict(user)
        assert SALT_PATTERN.fullmatch(user["_salt"]), user["_salt"]
        assert key == user["_id"]
        if key not in fixture_ids:
            assert ID_PATTERN.fullmatch(key), key
            user["_id"] = "<new>"
        user["_salt"] = "<salt>"
        users.append(user)
    return {**db, "users": sorted(users, key=lambda u: json.dumps(u, sort_keys=True))}


class Runner:
    def __init__(self, case: dict[str, Any], tmp: Path) -> None:
        self.settings = case["settings"]
        self.path = tmp / "authentication.json"
        if case["db"] is not None:
            self.path.write_text(json.dumps(case["db"]), "utf-8")
        self.fixture_ids = set((case["db"] or {}).get("users", {}))
        self.auth: Authentication | None = None

    def step(self, s: dict[str, Any]) -> tuple[str, Any]:
        try:
            return "", self._step(s)
        except AuthError as exc:
            return str(exc), None

    def _step(self, s: dict[str, Any]) -> Any:
        op = s["op"]
        if op == "init":
            self.auth = Authentication(self.path)
            return None
        if op == "hash":
            return credential_hash(s["secret"])
        auth = self.auth
        assert auth is not None
        if op == "firstUser":
            return bool(auth.create_first_user(s["username"], s["password"]))
        if op == "newUser":
            auth.save_new_user(s["userData"])
            return None
        if op == "saveUserData":
            auth.save_user_data(s["userData"], s.get("deleteUser", False))
            return None
        if op == "basic":
            return auth.basic_auth(s["header"] or "", s["level"])
        if op == "url":
            level = {"m3u": LEVEL_M3U, "xml": LEVEL_XML}.get(s["type"])
            if level is None or not self.settings.get(level):
                return None
            query = parse_qs(s["query"], keep_blank_values=True)
            auth.url_auth(query.get("username", [""])[0], query.get("password", [""])[0], level)
            return None
        if op == "login":
            token = auth.login(s["username"], s["password"])
            if s.get("level"):
                auth.check_level(auth.user_id_for_token(token), s["level"])
            return True
        if op == "tokenRotate":
            token = auth.login(s["username"], s["password"])
            fresh = auth.refresh_token(token)
            errors = []
            for t in (token, fresh):
                try:
                    auth.refresh_token(t)
                    errors.append("<nil>")
                except AuthError as exc:
                    errors.append(str(exc))
            return {"rotated": fresh != token, "oldErr": errors[0], "freshErr": errors[1]}
        raise AssertionError(op)

    def db(self) -> Any:
        return json.loads(self.path.read_text("utf-8")) if self.path.exists() else None


def go_outcome(name: str, i: int, step: dict[str, Any], want: dict[str, Any]) -> tuple[str, Any]:
    if (name, i) in DIVERGENT_STEPS:
        return DIVERGENT_STEPS[(name, i)]
    if want["panic"]:  # malformed Authorization header: Go crashed the request
        return LOGIN_FAILED, None
    result = want["result"]
    if want["err"] and step["op"] in ("basic", "login", "firstUser"):
        result = None  # Go also returned the username / an empty token on failure
    if step["op"] == "url":
        result = None
    return want["err"], result


class Golden(unittest.TestCase):
    def test_cases(self) -> None:
        self.assertGreaterEqual(len(CASES), 14)
        for path in CASES:
            name = path.name.removesuffix(".input.json")
            if name in DIVERGENT_CASES:
                continue
            case = json.loads(path.read_text("utf-8"))
            golden = json.loads((GOLDEN / f"{name}.golden.json").read_text("utf-8"))
            with tempfile.TemporaryDirectory() as td:
                run = Runner(case, Path(td))
                want_db = case["db"]
                for i, (step, want) in enumerate(zip(case["steps"], golden, strict=True)):
                    with self.subTest(case=name, step=i, op=step["op"]):
                        self.assertEqual(run.step(step), go_outcome(name, i, step, want))
                        if (name, i) not in DIVERGENT_STEPS:
                            want_db = want["db"]
                        self.assertEqual(normalise(run.db(), run.fixture_ids), normalise(want_db, run.fixture_ids))

    def test_divergence_markers_still_hold(self) -> None:
        """The steps marked divergent really differ in Go (so a regenerated golden is noticed)."""
        basic = json.loads((GOLDEN / "basic_headers.golden.json").read_text("utf-8"))
        self.assertEqual((basic[15]["err"], basic[15]["result"]), ("", "alice"))
        new = json.loads((GOLDEN / "new_user.golden.json").read_text("utf-8"))
        self.assertEqual(new[5]["err"], "")
        first = json.loads((GOLDEN / "first_user_when_users_exist.golden.json").read_text("utf-8"))
        self.assertEqual([(s["err"], s["result"]) for s in first[1:]], [("", True), ("User data could not be saved", False), ("", None)])

    def test_first_user_cannot_take_over_an_account(self) -> None:
        name = "first_user_when_users_exist"
        case = json.loads((GOLDEN / f"{name}.input.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as td:
            run = Runner(case, Path(td))
            run.step(case["steps"][0])
            before = run.db()
            self.assertEqual(run.step(case["steps"][1]), (DEFAULT_USER_EXISTS, None))  # Go: bob got every level
            self.assertEqual(run.step(case["steps"][2]), (DEFAULT_USER_EXISTS, None))
            self.assertEqual(run.db(), before)
            self.assertEqual(run.step(case["steps"][3]), (NO_AUTHORIZATION, None))  # bob still lacks m3u


class Behaviour(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "authentication.json"
        self.now = 1000.0
        self.auth = Authentication(self.path, token_minutes=60, clock=lambda: self.now)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_file_is_private(self) -> None:
        self.auth.create_first_user("admin", "pw")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_tokens_expire(self) -> None:
        self.auth.create_first_user("admin", "pw")
        token = self.auth.login("admin", "pw")
        self.now += 3599
        token = self.auth.refresh_token(token)
        self.now += 3599
        user_id = self.auth.user_id_for_token(token)
        self.assertEqual(self.auth.read_user_data(user_id)["username"], "admin")
        self.now += 2
        with self.assertRaisesRegex(AuthError, NO_USER_FOR_TOKEN):
            self.auth.user_id_for_token(token)
        with self.assertRaisesRegex(AuthError, SESSION_EXPIRED):
            self.auth.refresh_token(token)

    def test_removing_a_user_ends_its_sessions(self) -> None:
        self.auth.create_first_user("admin", "pw")
        token = self.auth.login("admin", "pw")
        self.auth.remove_user(self.auth.user_id_for_token(token))
        with self.assertRaises(AuthError):
            self.auth.user_id_for_token(token)

    def test_basic_and_url_auth_issue_no_tokens(self) -> None:
        self.auth.create_first_user("admin", "pw")
        tokens = dict(self.auth._tokens)
        self.auth.basic_auth("Basic YWRtaW46cHc=", LEVEL_PMS)
        self.auth.url_auth("admin", "pw", LEVEL_M3U)
        self.assertEqual(self.auth._tokens, tokens)

    def test_existing_users_survive_a_restart(self) -> None:
        self.auth.save_new_user({"username": "u", "password": "p", "confirm": "p", LEVEL_XML: True})
        again = Authentication(self.path)
        again.url_auth("u", "p", LEVEL_XML)
        self.assertEqual(set(again.users), set(self.auth.users))

    def test_header_without_colon_is_refused(self) -> None:
        # Go accepted empty passwords, so such a user may exist; Go crashed on this header
        self.path.write_text(json.dumps({"dbVersion": "1.0", "hash": "sha256", "users": {"id-AAAAAAAAAA": {
            "_id": "id-AAAAAAAAAA", "_salt": "S" * 20, "_username": credential_hash("ghost"),
            "_password": credential_hash(""), "data": {LEVEL_PMS: True}}}}), "utf-8")
        auth = Authentication(self.path)
        self.assertEqual(auth.basic_auth("Basic Z2hvc3Q6", LEVEL_PMS), "ghost")  # "ghost:"
        with self.assertRaisesRegex(AuthError, LOGIN_FAILED):
            auth.basic_auth("Basic Z2hvc3Q=", LEVEL_PMS)  # "ghost"

    def test_invalid_database(self) -> None:
        self.path.write_text('{"users": []}', "utf-8")
        with self.assertRaises(ValueError):
            Authentication(self.path)


if __name__ == "__main__":
    unittest.main()
