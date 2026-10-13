"""Writes tests/golden/auth/*.input.json. Run run_auth_golden.sh afterwards."""

import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)


def go_hash(secret):
    # Go's SHA256(secret, salt) ignores the salt; the golden "hash" steps check this against Go.
    return base64.b64encode(hmac.new(secret.encode(), b"_remote_db", hashlib.sha256).digest()).decode()


def user(uid, username, password, data):
    return {"_id": uid, "_salt": "S" * 20, "_username": go_hash(username), "_password": go_hash(password), "data": data}


def db(*users):
    return {"dbVersion": "1.0", "hash": "sha256", "users": {u["_id"]: u for u in users}}


def b64(text):
    return "Basic " + base64.b64encode(text.encode()).decode()


ALL = {
    "authentication.web": True,
    "authentication.pms": True,
    "authentication.m3u": True,
    "authentication.xml": True,
    "authentication.api": False,
    "username": "alice",
    "defaultUser": True,
}
ALICE = user("id-ALICE00001", "alice", "secret", ALL)
BOB = user("id-BOB0000001", "bob", "pa:ss", {"authentication.web": True, "username": "bob"})
TWO = db(ALICE, BOB)
AUTH_ON = {"authentication.m3u": True, "authentication.xml": True, "authentication.pms": True}


def case(name, steps, *, database=None, settings=None):
    body = {"settings": AUTH_ON if settings is None else settings, "db": database, "steps": [{"op": "init"}, *steps]}
    (out / f"{name}.input.json").write_text(json.dumps(body, indent=1, ensure_ascii=False) + "\n")


def basic(header, level="authentication.pms"):
    return {"op": "basic", "header": header, "level": level}


def url(query, kind="m3u"):
    return {"op": "url", "query": query, "type": kind}


case(
    "hashes",
    [
        {"op": "hash", "secret": s}
        for s in ["", "a", "secret", "pässwörd", "日本語", "with:colon", "x" * 200, " spaced "]
    ],
)
case(
    "fresh_missing_file",
    [
        {"op": "firstUser", "username": "admin", "password": "pw"},
        {"op": "login", "username": "admin", "password": "pw", "level": "authentication.web"},
        {"op": "login", "username": "admin", "password": "pw", "level": "authentication.api"},
        {"op": "login", "username": "admin", "password": "nope"},
        basic(b64("admin:pw")),
        basic(b64("admin:wrong")),
        url("username=admin&password=pw"),
        url("username=admin&password=pw", "xml"),
        url(""),
    ],
)
case(
    "empty_object_file",
    [
        {"op": "firstUser", "username": "admin", "password": "pw"},
        basic(b64("admin:pw")),
    ],
    database={},
)
case(
    "first_user_when_users_exist",
    [
        {"op": "firstUser", "username": "bob", "password": "pa:ss"},
        {"op": "firstUser", "username": "someone", "password": "else"},
        url("username=bob&password=pa:ss"),
    ],
    database=TWO,
)
case(
    "basic_headers",
    [
        basic(None),
        basic(""),
        basic("Bearer abc"),
        basic("basic " + b64("alice:secret")[6:]),
        basic(b64("alice:secret")),
        basic(b64("alice:secret"), "authentication.web"),
        basic(b64("alice:secret"), "authentication.api"),
        basic(b64("alice:secret"), "authentication.unknown"),
        basic(b64("bob:pa:ss")),
        basic(b64("bob:pa:ss"), "authentication.web"),
        basic(b64("alice")),
        basic("Basic"),
        basic("Basic  " + b64("alice:secret")[6:]),
        basic("Basic !!!!"),
        basic(b64("alice:secret") + "!!"),
        basic(b64("alice:secret").rstrip("=")),
        basic(b64(":")),
        basic(b64("alice:")),
        basic(b64("ALICE:secret")),
        basic(b64("carol:pw")),
        basic(b64("carol:pw").rstrip("=")),
        basic(b64("carol:pw")[:-3]),
        basic(b64("carol:pw1")),
        basic(b64("carol:pw1").rstrip("=")),
    ],
    database=db(ALICE, BOB, user("id-CAROL000001", "carol", "pw", ALL), user("id-CAROL000002", "carol", "pw1", ALL)),
)
case(
    "url_queries",
    [
        url("username=alice&password=secret"),
        url("username=alice&password=secret", "xml"),
        url("username=alice&password=wrong"),
        url("password=secret"),
        url(""),
        url("username=bob&password=pa%3Ass"),
        url("username=bob&password=pa%3Ass", "xml"),
        url("username=alice&password=secret&username=bob"),
        url("username=bob&username=alice&password=secret"),
        url("username=alice&password=secret", "other"),
    ],
    database=TWO,
)
case(
    "url_auth_disabled",
    [
        url(""),
        url("", "xml"),
        url("username=x&password=y"),
        basic(None),
        basic(b64("alice:secret")),
    ],
    database=TWO,
    settings={},
)
case(
    "user_data_empty",
    [
        {"op": "saveUserData", "userData": {"id-ALICE00001": {}}},
        basic(b64("alice:secret")),
        url("username=alice&password=secret"),
    ],
    database=TWO,
)
case(
    "user_without_data_key_level",
    [
        basic(b64("bob:pa:ss"), "authentication.m3u"),
        url("username=bob&password=pa:ss"),
    ],
    database=TWO,
)
case(
    "new_user",
    [
        {
            "op": "newUser",
            "userData": {
                "username": "carol",
                "password": "pw",
                "confirm": "pw",
                "authentication.m3u": True,
                "authentication.xml": False,
            },
        },
        url("username=carol&password=pw"),
        url("username=carol&password=pw", "xml"),
        {"op": "newUser", "userData": {"username": "alice", "password": "other", "confirm": "other"}},
        {"op": "newUser", "userData": {"username": "", "password": "", "confirm": ""}},
    ],
    database=TWO,
)
case(
    "save_user_data",
    [
        {
            "op": "saveUserData",
            "userData": {
                "id-BOB0000001": {
                    "username": "bobby",
                    "password": "new",
                    "confirm": "new",
                    "authentication.pms": True,
                    "authentication.web": False,
                }
            },
        },
        basic(b64("bobby:new")),
        basic(b64("bob:pa:ss")),
        {"op": "saveUserData", "userData": {"id-BOB0000001": {"username": "bob2", "password": ""}}},
        basic(b64("bob2:new")),
        {"op": "saveUserData", "userData": {"id-BOB0000001": {"password": "ignored"}}},
        basic(b64("bob2:new")),
        basic(b64("bob2:ignored")),
        {"op": "saveUserData", "userData": {"id-NOPE": {"username": "x"}}},
        {"op": "saveUserData", "userData": {"id-NOPE": {"authentication.pms": True}}},
    ],
    database=TWO,
)
case(
    "delete_user_flag",
    [
        {"op": "saveUserData", "userData": {"id-BOB0000001": {"authentication.pms": True}}, "deleteUser": True},
        basic(b64("bob:pa:ss")),
    ],
    database=TWO,
)
case(
    "delete_user_key",
    [
        {"op": "saveUserData", "userData": {"id-BOB0000001": {"delete": True}}},
        basic(b64("bob:pa:ss")),
        basic(b64("alice:secret")),
    ],
    database=TWO,
)
case(
    "tokens",
    [
        {"op": "tokenRotate", "username": "alice", "password": "secret"},
        {"op": "tokenRotate", "username": "alice", "password": "bad"},
    ],
    database=TWO,
)
