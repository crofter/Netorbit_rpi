"""An optional admin password for the web page.

No password: anyone who can reach the Pi can view and edit the map (fine on a trusted home
network, and the same as the first-run state). With a password: every page and API call
needs a login, and the login is a signed cookie that survives restarts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import time

COOKIE = "netorbit_session"
SESSION_DAYS = 30
_ITERATIONS = 200_000


def hash_password(password: str, salt: bytes | None = None, iterations: int = _ITERATIONS) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        test = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(test, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


class Sessions:
    """Signed, expiring session tokens. Changing the password logs everyone out."""

    def __init__(self, secret_path: Path) -> None:
        self._secret = self._load_secret(secret_path)

    @staticmethod
    def _load_secret(path: Path) -> bytes:
        try:
            data = path.read_bytes()
            if len(data) >= 32:
                return data
        except OSError:
            pass
        data = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return data

    def _sig(self, payload: str, password_hash: str) -> str:
        key = self._secret + password_hash.encode()
        return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, password_hash: str, now: float | None = None) -> str:
        expires = int((now or time.time()) + SESSION_DAYS * 86400)
        payload = f"{expires}.{secrets.token_hex(8)}"
        return f"{payload}.{self._sig(payload, password_hash)}"

    def valid(self, token: str | None, password_hash: str, now: float | None = None) -> bool:
        if not token or token.count(".") != 2:
            return False
        expires, nonce, sig = token.split(".")
        payload = f"{expires}.{nonce}"
        if not hmac.compare_digest(sig, self._sig(payload, password_hash)):
            return False
        try:
            return int(expires) > (now or time.time())
        except ValueError:
            return False
