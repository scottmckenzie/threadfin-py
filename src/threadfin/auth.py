"""Users, passwords and access levels (port of ``authentication.go`` and
``internal/authentication``).

``authentication.json`` keeps the Go format so existing config folders keep working::

    {"dbVersion": "1.0", "hash": "sha256",
     "users": {"id-XXXXXXXXXX": {"_id": ..., "_salt": ..., "_username": <hash>, "_password": <hash>,
                                 "data": {"username": "...", "authentication.web": true, ...}}}}

Usernames and passwords are stored as ``base64(HMAC-SHA256(key=secret, msg="_remote_db"))``.
Go never used the per-user ``_salt`` (it is generated and stored, then ignored), so neither do we:
salting now would lock every existing user out.

A user may use an area when its ``data`` has ``authentication.<area>: true``; a missing or
non-boolean entry is written back as ``false`` and denied, as in Go. Tokens (web UI / API sessions)
live in memory only, are valid for ``token_minutes`` and are replaced by a new one on every check.

Differences from Go (see docs/PARITY_NOTES.md): malformed ``Authorization`` headers are refused
instead of crashing the request, Base64 is decoded strictly, the first-user form cannot take over an
existing account, empty usernames/passwords are refused, and Basic / URL auth do not mint tokens.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .jsonio import JSONFileError, load_json_object, save_json

LEVEL_WEB = "authentication.web"
LEVEL_PMS = "authentication.pms"
LEVEL_M3U = "authentication.m3u"
LEVEL_XML = "authentication.xml"
LEVEL_API = "authentication.api"

# Added to every user that lacks them on start (Go leaves out m3u here; it is added as false on first use).
DEFAULT_LEVELS: dict[str, Any] = {LEVEL_WEB: False, LEVEL_PMS: False, LEVEL_XML: False, LEVEL_API: False}

# Go error texts, kept so API clients see the same messages.
DEFAULT_USER_EXISTS = "Default user already exists"
NO_USER_FOR_TOKEN = "No user id found for this token"
LOGIN_FAILED = "User authentication failed"
SESSION_EXPIRED = "Session has expired"
USER_EXISTS = "User already exists"
DATA_NOT_SAVED = "User data could not be saved"
DATA_NOT_READ = "User data could not be read"
USER_NOT_FOUND = "User ID was not found"
NO_AUTHORIZATION = "No authorization"
EMPTY_CREDENTIALS = "Username and password must not be empty"

_TOKEN_CHARS = "-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789aBcDeFgHiJkLmNoPqRsTuVwXyZ_"
_ID_CHARS = "ABCDEFGHJKLMNOPQRSTUVWXYZ0123456789"
SALT_PATTERN = re.compile(r"[-A-Za-z0-9_]{20}")
ID_PATTERN = re.compile(r"id-[A-HJ-Z0-9]{10}")


class AuthError(Exception):
    """Authentication or authorization failed; the message is Go's error text."""


def credential_hash(secret: str | bytes) -> str:
    """Go's ``SHA256(secret, salt)``: the salt argument was never used."""
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return base64.b64encode(hmac.new(key, b"_remote_db", hashlib.sha256).digest()).decode("ascii")


def _random(chars: str, n: int) -> str:
    return "".join(secrets.choice(chars) for _ in range(n))


def _empty_database() -> dict[str, Any]:
    return {"dbVersion": "1.0", "hash": "sha256", "users": {}}


class Authentication:
    def __init__(
        self,
        path: str | Path,
        *,
        token_minutes: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = Path(path)
        self.token_seconds = token_minutes * 60
        self._clock = clock
        self._lock = threading.RLock()
        self._tokens: dict[str, tuple[str, float]] = {}  # token -> (user id, expires)
        self._db = self._load()
        # activatedSystemAuthentication()
        self.set_default_user_data(DEFAULT_LEVELS)

    # -- database ---------------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            db = _empty_database()
            self._save(db)
            return db
        db = load_json_object(self.path)
        if not db:  # Threadfin creates the system files as "{}"
            db = _empty_database()
            self._save(db)
            return db
        if not isinstance(db.get("users"), dict):
            raise JSONFileError(f"{self.path}: 'users' must be an object")
        return db

    def _save(self, db: dict[str, Any] | None = None) -> None:
        save_json(self.path, self._db if db is None else db, mode=0o600)

    @property
    def users(self) -> dict[str, dict[str, Any]]:
        users: dict[str, dict[str, Any]] = self._db["users"]
        return users

    def _new_user(self, username: str, password: str) -> dict[str, Any]:
        if not username or not password:
            raise AuthError(EMPTY_CREDENTIALS)
        while (user_id := "id-" + _random(_ID_CHARS, 10)) in self.users:
            pass
        user = {
            "_id": user_id,
            "_salt": _random(_TOKEN_CHARS, 20),
            "_username": credential_hash(username),
            "_password": credential_hash(password),
            "data": {},
        }
        self.users[user_id] = user
        return user

    def create_default_user(self, username: str, password: str) -> str:
        with self._lock:
            if self.users:
                raise AuthError(DEFAULT_USER_EXISTS)
            user_id: str = self._new_user(username, password)["_id"]
            self._save()
            return user_id

    def create_user(self, username: str, password: str) -> str:
        with self._lock:
            name_hash = credential_hash(username)
            if any(u.get("_username") == name_hash for u in self.users.values()):
                raise AuthError(USER_EXISTS)
            user_id: str = self._new_user(username, password)["_id"]
            self._save()
            return user_id

    def authenticate(self, username: str | bytes, password: str | bytes) -> str:
        """Return the user id for these credentials or raise ``AuthError``."""
        name_hash, pass_hash = credential_hash(username), credential_hash(password)
        with self._lock:
            for user_id, user in self.users.items():
                if user.get("_username") == name_hash and user.get("_password") == pass_hash:
                    return user_id
        raise AuthError(LOGIN_FAILED)

    def read_user_data(self, user_id: str) -> dict[str, Any]:
        with self._lock:
            user = self.users.get(user_id)
            if not isinstance(user, dict):
                raise AuthError(DATA_NOT_READ)
            data = user.get("data")
            if not isinstance(data, dict):
                data = user["data"] = {}
            return data

    def write_user_data(self, user_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            user = self.users.get(user_id)
            if not isinstance(user, dict):
                raise AuthError(DATA_NOT_SAVED)
            user["data"] = data
            self._save()

    def remove_user(self, user_id: str) -> None:
        with self._lock:
            if user_id not in self.users:
                raise AuthError(USER_NOT_FOUND)
            del self.users[user_id]
            self._tokens = {t: v for t, v in self._tokens.items() if v[0] != user_id}
            self._save()

    def change_credentials(self, user_id: str, username: str, password: str) -> None:
        """Empty values are left unchanged."""
        with self._lock:
            user = self.users.get(user_id)
            if not isinstance(user, dict):
                raise AuthError(USER_NOT_FOUND)
            if username:
                user["_username"] = credential_hash(username)
            if password:
                user["_password"] = credential_hash(password)
            self._save()

    def set_default_user_data(self, defaults: dict[str, Any]) -> None:
        with self._lock:
            changed = False
            for user in self.users.values():
                data = user.setdefault("data", {})
                for key, value in defaults.items():
                    if key not in data:
                        data[key] = value
                        changed = True
            if changed:
                self._save()

    # -- tokens (web UI and API sessions) ---------------------------------------------------

    def _issue_token(self, user_id: str) -> str:
        now = self._clock()
        self._tokens = {t: v for t, v in self._tokens.items() if v[1] > now}
        while (token := _random(_TOKEN_CHARS, 40)) in self._tokens:
            pass
        self._tokens[token] = (user_id, now + self.token_seconds)
        return token

    def login(self, username: str, password: str) -> str:
        user_id = self.authenticate(username, password)
        with self._lock:
            return self._issue_token(user_id)

    def refresh_token(self, token: str) -> str:
        """Replace a valid token by a new one (the old one stops working)."""
        with self._lock:
            entry = self._tokens.pop(token, None)
            if entry is None or entry[1] < self._clock():
                raise AuthError(SESSION_EXPIRED)
            return self._issue_token(entry[0])

    def session(self, token: str) -> str:
        """Web-UI session check: the user id, with the token's lifetime extended (not rotated).

        Go replaced the cookie token on every request, which breaks requests the page sends in
        parallel; here a session stays valid while it is used.
        """
        with self._lock:
            entry = self._tokens.get(token)
            now = self._clock()
            if entry is None or entry[1] < now:
                self._tokens.pop(token, None)
                raise AuthError(SESSION_EXPIRED)
            self._tokens[token] = (entry[0], now + self.token_seconds)
            return entry[0]

    def logout(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def username(self, user_id: str) -> str:
        """The name shown in the UI (Go keeps it in the user's data)."""
        name = self.read_user_data(user_id).get("username")
        return name if isinstance(name, str) else ""

    def user_id_for_token(self, token: str) -> str:
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None or entry[1] < self._clock():
                raise AuthError(NO_USER_FOR_TOKEN)
            return entry[0]

    # -- Threadfin rules (authentication.go, data.go) ---------------------------------------

    def check_level(self, user_id: str, level: str) -> None:
        """checkAuthorizationLevel: raise ``AuthError`` unless ``data[level]`` is ``true``.

        A missing / non-boolean level is written back as ``false`` unless the data is empty (as in Go).
        """
        with self._lock:
            data = self.read_user_data(user_id)
            value = data.get(level)
            if value is True:
                return
            if data and not isinstance(value, bool):
                data[level] = False
                self.write_user_data(user_id, data)
            raise AuthError(NO_AUTHORIZATION)

    def basic_auth(self, header: str, level: str) -> str:
        """Check an ``Authorization: Basic`` header; return the username."""
        scheme, space, payload = header.partition(" ")
        if scheme != "Basic" or not space:
            raise AuthError(LOGIN_FAILED)
        try:
            decoded = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            raise AuthError(LOGIN_FAILED) from None
        username, colon, password = decoded.partition(b":")
        if not colon:
            raise AuthError(LOGIN_FAILED)
        self.check_level(self.authenticate(username, password), level)
        return username.decode("utf-8", "replace")

    def url_auth(self, username: str, password: str, level: str) -> None:
        """``?username=..&password=..`` on /m3u/ and /xmltv/."""
        self.check_level(self.authenticate(username, password), level)

    def create_first_user(self, username: str, password: str) -> str:
        """createFirstUserForAuthentication: the first user gets every level but the API; returns a token."""
        with self._lock:
            user_id = self.create_default_user(username, password)
            self.write_user_data(
                user_id,
                {
                    "username": username,
                    LEVEL_WEB: True,
                    LEVEL_PMS: True,
                    LEVEL_M3U: True,
                    LEVEL_XML: True,
                    LEVEL_API: False,
                    "defaultUser": True,
                },
            )
            return self._issue_token(user_id)

    def save_new_user(self, data: dict[str, Any]) -> str:
        """saveNewUser (web UI): ``data`` holds username, password, confirm and the levels."""
        data = dict(data)
        username, password = data.get("username"), data.get("password")
        data.pop("password", None)
        data.pop("confirm", None)
        if not isinstance(username, str) or not isinstance(password, str):
            raise AuthError(EMPTY_CREDENTIALS)
        user_id = self.create_user(username, password)
        self.write_user_data(user_id, data)
        return user_id

    def save_user_data(self, user_data: dict[str, dict[str, Any]], delete_user: bool = False) -> None:
        """saveUserData (web UI): ``{user id: data}``.

        A new username (with an optional new password) changes the credentials; a password without
        a username is ignored, as in Go. ``delete_user`` removes the first user given; a ``delete`` key
        removes that user. Otherwise the data replaces the stored data (minus password/confirm).
        """
        with self._lock:
            for user_id, new in user_data.items():
                username, password = new.get("username"), new.get("password")
                if isinstance(username, str) and username:
                    self.change_credentials(user_id, username, password if isinstance(password, str) else "")
                if delete_user:
                    self.remove_user(user_id)
                    return
                new = {k: v for k, v in new.items() if k not in ("password", "confirm")}
                if "delete" in new:
                    if user_id in self.users:
                        self.remove_user(user_id)
                else:
                    self.write_user_data(user_id, new)
