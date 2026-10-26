"""Dashboard login: one password (PBKDF2-SHA256 hash in .env as DASHBOARD_PASSWORD_HASH), a signed HttpOnly
session cookie for 30 days, and a cap on failed attempts. Set or change it with `seodesk password`.

With no password set, the dashboard is open, and serve() only allows that on 127.0.0.1 (your own machine)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import dotenv_values

from ..settings import home

COOKIE = "seo_session"
SESSION_DAYS = 30
MAX_FAILS, FAIL_WINDOW = 8, 15 * 60
_ITERATIONS = 600_000  # OWASP's recommendation for PBKDF2-SHA256 (scrypt isn't in every Python build)


def env_file() -> Path:
    return home() / ".env"


def read_env(path: Optional[Path] = None) -> Dict[str, str]:
    path = path or env_file()
    return {k: v or "" for k, v in dotenv_values(path).items()} if path.exists() else {}


def write_env(key: str, value: str, path: Optional[Path] = None) -> None:
    path = path or env_file()
    lines = path.read_text().splitlines() if path.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")
    os.chmod(path, 0o600)


def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256:{iterations}:{base64.b64encode(salt).decode()}:{base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, iterations, salt, digest = stored.split(":")
        if kind != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def set_password(password: str, path: Optional[Path] = None) -> None:
    if len(password) < 10:
        raise ValueError("use at least 10 characters")
    write_env("DASHBOARD_PASSWORD_HASH", hash_password(password), path)
    write_env("DASHBOARD_SECRET", secrets.token_hex(32), path)  # new secret = everyone signed out


class Auth:
    def __init__(self, env_path: Optional[Path] = None):
        self.env_path = env_path or env_file()
        self.fails: Dict[str, List[float]] = {}

    def has_password(self) -> bool:
        return bool(read_env(self.env_path).get("DASHBOARD_PASSWORD_HASH"))

    def _sign(self, value: str, secret: str) -> str:
        return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def session_valid(self, cookie_value: Optional[str]) -> bool:
        secret = read_env(self.env_path).get("DASHBOARD_SECRET", "")
        if not secret or not cookie_value or "." not in cookie_value:
            return False
        expires, mac = cookie_value.split(".", 1)
        if not expires.isdigit() or int(expires) < time.time():
            return False
        return hmac.compare_digest(mac, self._sign(expires, secret))

    def login(self, client: str, password: str) -> Tuple[bool, str, int]:
        """-> (ok, session cookie value or error message, HTTP status)."""
        now = time.time()
        recent = [t for t in self.fails.get(client, []) if now - t < FAIL_WINDOW]
        if len(recent) >= MAX_FAILS:
            return False, "Too many tries. Wait 15 minutes.", 429
        values = read_env(self.env_path)
        if not values.get("DASHBOARD_PASSWORD_HASH"):
            return False, "No password is set yet. Run: seodesk password", 403
        if not verify_password(password, values["DASHBOARD_PASSWORD_HASH"]):
            self.fails[client] = recent + [now]
            return False, "Wrong password.", 401
        self.fails.pop(client, None)
        secret = values.get("DASHBOARD_SECRET") or ""
        if not secret:
            secret = secrets.token_hex(32)
            write_env("DASHBOARD_SECRET", secret, self.env_path)
        expires = str(int(now) + SESSION_DAYS * 86400)
        return True, f"{expires}.{self._sign(expires, secret)}", 200
