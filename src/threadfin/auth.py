"""Users, passwords and access levels (port of ``authentication.go`` and
``internal/authentication``).

``authentication.json`` keeps the Go format so existing config folders keep working::

    {"dbVersion": "1.0", "hash": "sha256",
     "users": {"id-XXXXXXXXXX": {"_id": ..., "_salt": ..., "_username": <hash>, "_password": <hash>,
                                 "data": {"username": "...", "authentication.web": true, ...}}}}

Usernames are stored as Go stores them, ``base64(HMAC-SHA256(key=username, msg="_remote_db"))``,
because users are looked up by that hash. Go stored passwords the same way: fast and unsalted (the
per-user ``_salt`` is generated and stored, then ignored). Here passwords are stored as salted scrypt
hashes (:func:`hash_password`, ``$scrypt$ln=14,r=8,p=5$<salt>$<hash>``). Go hashes are still accepted
and replaced at the user's next successful login, as are scrypt hashes with weaker parameters; after
that the Go app can no longer log that user in. Correct credentials are remembered in memory (keyed by
an HMAC with a per-process secret) so that Basic / URL credentials sent with every player request do
not cost a scrypt computation each time.

A user may use an area when its ``data`` has ``authentication.<area>: true``; a missing or
non-boolean entry is written back as ``false`` and denied, as in Go. Tokens (web UI / API sessions)
live in memory only, are valid for ``token_minutes`` and are replaced by a new one on every check.

Differences from Go (see docs/PARITY_NOTES.md): malformed ``Authorization`` headers are refused
instead of crashing the request, Base64 is decoded strictly, the first-user form cannot take over an
existing account, empty usernames/passwords are refused, Basic / URL auth do not mint tokens,
failed credential checks are throttled per client (:class:`LoginThrottle`), and passwords are stored
as scrypt hashes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from collections import deque
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
TOO_MANY_ATTEMPTS = "Too many failed logins, try again later"

MAX_FAILURES = 10  # failed credential checks per client ...
FAILURE_WINDOW = 15 * 60  # ... within this many seconds, then the client is refused
MAX_THROTTLED_CLIENTS = 10_000

_TOKEN_CHARS = "-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789aBcDeFgHiJkLmNoPqRsTuVwXyZ_"
_ID_CHARS = "ABCDEFGHJKLMNOPQRSTUVWXYZ0123456789"
SALT_PATTERN = re.compile(r"[-A-Za-z0-9_]{20}")
ID_PATTERN = re.compile(r"id-[A-HJ-Z0-9]{10}")


class AuthError(Exception):
    """Authentication or authorization failed; the message is Go's error text."""


class TooManyAttempts(AuthError):
    """The client failed too often recently; ``retry_after`` is the wait in seconds."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(TOO_MANY_ATTEMPTS)
        self.retry_after = retry_after


class LoginThrottle:
    """Failed credential checks per client (IP address).

    After ``max_failures`` failures within ``window`` seconds the client is refused, before its
    credentials are even checked, until the oldest of those failures is older than the window. A
    success clears the client's record. Not thread-safe: :class:`Authentication` calls it under its lock.
    """

    def __init__(
        self, clock: Callable[[], float], max_failures: int = MAX_FAILURES, window: float = FAILURE_WINDOW
    ) -> None:
        self._clock, self.max_failures, self.window = clock, max_failures, window
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, client: str, now: float) -> deque[float]:
        times = self._failures.get(client, deque())
        while times and times[0] <= now - self.window:
            times.popleft()
        return times

    def check(self, client: str) -> None:
        now = self._clock()
        times = self._recent(client, now)
        if len(times) >= self.max_failures:
            raise TooManyAttempts(max(1, math.ceil(times[0] + self.window - now)))

    def failed(self, client: str) -> None:
        now = self._clock()
        times = self._recent(client, now)
        times.append(now)
        self._failures[client] = times
        if len(self._failures) > MAX_THROTTLED_CLIENTS:
            self._failures = {c: t for c, t in self._failures.items() if self._recent(c, now)}
            while len(self._failures) > MAX_THROTTLED_CLIENTS:  # keep the most recent offenders
                del self._failures[min(self._failures, key=lambda c: self._failures[c][-1])]

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)


def credential_hash(secret: str | bytes) -> str:
    """Go's ``SHA256(secret, salt)``: the salt argument was never used. Usernames (and Go passwords)."""
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return base64.b64encode(hmac.new(key, b"_remote_db", hashlib.sha256).digest()).decode("ascii")


PasswordCost = tuple[int, int, int]  # scrypt (log2 N, r, p)
PASSWORD_COST: PasswordCost = (14, 8, 5)  # OWASP: N=2^14, r=8, p=5 (16 MB, about 0.1 s)
_SCRYPT_PREFIX = "$scrypt$"
_SCRYPT_LIMITS = (20, 16, 16)  # refuse parameters beyond these from a hand-edited file
_SCRYPT_MAXMEM = 256 * 1024 * 1024


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


def _scrypt(secret: bytes, salt: bytes, cost: PasswordCost) -> bytes:
    log_n, r, p = cost
    return hashlib.scrypt(secret, salt=salt, n=1 << log_n, r=r, p=p, maxmem=_SCRYPT_MAXMEM, dklen=32)


def _as_bytes(secret: str | bytes) -> bytes:
    return secret.encode("utf-8") if isinstance(secret, str) else secret


def hash_password(password: str | bytes, cost: PasswordCost = PASSWORD_COST) -> str:
    """``$scrypt$ln=<log2 N>,r=<r>,p=<p>$<salt>$<hash>`` (unpadded Base64) with a random salt."""
    salt = secrets.token_bytes(16)
    log_n, r, p = cost
    return f"{_SCRYPT_PREFIX}ln={log_n},r={r},p={p}${_b64(salt)}${_b64(_scrypt(_as_bytes(password), salt, cost))}"


def _parse_scrypt(stored: str) -> tuple[PasswordCost, bytes, bytes] | None:
    try:
        _, _, params, salt, digest = stored.split("$")
        values = dict(item.split("=", 1) for item in params.split(","))
        cost = (int(values["ln"]), int(values["r"]), int(values["p"]))
        if set(values) != {"ln", "r", "p"} or not all(1 <= v <= m for v, m in zip(cost, _SCRYPT_LIMITS, strict=True)):
            return None
        return cost, _unb64(salt), _unb64(digest)
    except (ValueError, KeyError, binascii.Error):
        return None


def verify_password(stored: str, password: str | bytes, cost: PasswordCost = PASSWORD_COST) -> tuple[bool, bool]:
    """``(matches, needs a new hash)`` for a stored scrypt or Go hash; anything else never matches."""
    secret = _as_bytes(password)
    if stored.startswith(_SCRYPT_PREFIX):
        parsed = _parse_scrypt(stored)
        if parsed is None:
            return False, False
        stored_cost, salt, digest = parsed
        matches = hmac.compare_digest(_scrypt(secret, salt, stored_cost), digest)
        return matches, matches and stored_cost != cost
    matches = hmac.compare_digest(stored.encode("ascii", "replace"), credential_hash(secret).encode("ascii"))
    return matches, matches


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
        password_cost: PasswordCost = PASSWORD_COST,
    ) -> None:
        self.path = Path(path)
        self.password_cost = password_cost
        self._verified: dict[bytes, str] = {}  # HMAC(process key, user id + password) -> stored hash
        self._verified_key = secrets.token_bytes(32)
        self._dummy_hash: str | None = None  # checked when no user has the name: same work, same timing
        self.token_seconds = token_minutes * 60
        self._clock = clock
        self._lock = threading.RLock()
        self._tokens: dict[str, tuple[str, float]] = {}  # token -> (user id, expires)
        self.throttle = LoginThrottle(clock)
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
            "_password": hash_password(password, self.password_cost),
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

    def authenticate(self, username: str | bytes, password: str | bytes, client: str | None = None) -> str:
        """Return the user id for these credentials or raise ``AuthError``.

        With ``client`` (the caller's address) failures are counted, and a client that failed too
        often recently gets :class:`TooManyAttempts` without its credentials being checked. A Go hash
        (or a weaker scrypt hash) is replaced by a new scrypt hash on success.
        """
        if client is not None:
            with self._lock:
                self.throttle.check(client)
        name_hash, secret = credential_hash(username), _as_bytes(password)
        with self._lock:
            candidates = [(uid, u["_password"]) for uid, u in self.users.items()
                          if isinstance(u, dict) and u.get("_username") == name_hash
                          and isinstance(u.get("_password"), str)]  # fmt: skip
            if not candidates and self._dummy_hash is None:
                self._dummy_hash = hash_password(secrets.token_bytes(16), self.password_cost)
            dummy = self._dummy_hash
        if not candidates and dummy is not None:
            verify_password(dummy, secret, self.password_cost)  # an unknown name takes as long as a known one
        for user_id, stored in candidates:
            key = hmac.new(self._verified_key, user_id.encode() + b"\0" + secret, hashlib.sha256).digest()
            if self._verified.get(key) == stored:
                matches, upgrade = True, False
            else:
                matches, upgrade = verify_password(stored, secret, self.password_cost)
            if not matches:
                continue
            new_hash = hash_password(secret, self.password_cost) if upgrade else stored
            with self._lock:
                user = self.users.get(user_id)
                if upgrade and isinstance(user, dict) and user.get("_password") == stored:
                    user["_password"] = new_hash
                    self._save()
                if len(self._verified) >= 256:
                    self._verified.clear()
                self._verified[key] = new_hash
                if client is not None:
                    self.throttle.succeeded(client)
            return user_id
        if client is not None:
            with self._lock:
                self.throttle.failed(client)
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
                user["_password"] = hash_password(password, self.password_cost)
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

    def login(self, username: str, password: str, client: str | None = None) -> str:
        user_id = self.authenticate(username, password, client)
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

    def basic_auth(self, header: str, level: str, client: str | None = None) -> str:
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
        self.check_level(self.authenticate(username, password, client), level)
        return username.decode("utf-8", "replace")

    def url_auth(self, username: str, password: str, level: str, client: str | None = None) -> None:
        """``?username=..&password=..`` on /m3u/ and /xmltv/."""
        self.check_level(self.authenticate(username, password, client), level)

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
