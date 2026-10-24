"""The standalone store: the shared SQL store plus UI users and sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from foxray.core.sql import SqlStore, init_schema, open_backend
from foxray.core.util import utcnow

ITERATIONS = 390_000


def hash_password(password: str, *, salt: str | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt, _ = encoded.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    return hmac.compare_digest(hash_password(password, salt=salt, iterations=int(iterations)), encoded)


class StandaloneStore(SqlStore):
    def create_user(self, email: str, password: str, *, is_admin: bool = True) -> int:
        if len(password) < 8:
            raise ValueError("use a password of at least 8 characters")
        row = self._write_one(
            "INSERT INTO foxray_user (email, password_hash, is_admin, created_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (email) DO UPDATE SET password_hash = excluded.password_hash RETURNING id",
            (email.strip().lower(), hash_password(password), is_admin, utcnow()))
        return int(row[0])

    def user_by_email(self, email: str):
        return self._one("SELECT id, email, password_hash, is_admin FROM foxray_user WHERE email = %s",
                         (email.strip().lower(),))

    def user_by_id(self, user_id: int):
        return self._one("SELECT id, email, password_hash, is_admin FROM foxray_user WHERE id = %s", (user_id,))

    def user_count(self) -> int:
        return int(self._one("SELECT count(*) FROM foxray_user")[0])

    def secret(self, configured: str = "") -> str:
        if configured:
            return configured
        value = self.get_state_value("secret")
        if not value:
            value = secrets.token_hex(32)
            self._write("INSERT INTO foxray_state (name, last_run_at, lease_until, value) VALUES ('secret', NULL, "
                        "NULL, %s) ON CONFLICT (name) DO NOTHING", (value,))
            value = self.get_state_value("secret")
        return value


def open_store(database_url: str) -> StandaloneStore:
    backend = open_backend(database_url)
    init_schema(backend)
    return StandaloneStore(backend)


class Sessions:
    """Signed cookies: ``user_id.expiry.signature``. The signature covers part
    of the password hash, so changing a password ends old sessions."""

    def __init__(self, store: StandaloneStore, secret: str, max_age: int = 14 * 86400):
        self.store = store
        self.key = secret.encode()
        self.max_age = max_age

    def _sign(self, text: str) -> str:
        return hmac.new(self.key, text.encode(), hashlib.sha256).hexdigest()

    def make(self, user_row) -> str:
        expiry = int(time.time()) + self.max_age
        base = f"{user_row[0]}.{expiry}"
        return f"{base}.{self._sign(base + '.' + user_row[2][-16:])}"

    def user(self, cookie: str | None):
        if not cookie:
            return None
        try:
            uid, expiry, sig = cookie.split(".")
            if int(expiry) < time.time():
                return None
            row = self.store.user_by_id(int(uid))
        except (ValueError, TypeError):
            return None
        if row is None:
            return None
        if not hmac.compare_digest(sig, self._sign(f"{uid}.{expiry}.{row[2][-16:]}")):
            return None
        return row

    def csrf(self, cookie: str) -> str:
        return self._sign("csrf:" + (cookie or ""))[:32]
