"""Panel authentication: password from the environment, HMAC-signed session cookie."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

COOKIE = "hla_session"
SESSION_TTL_S = 7 * 24 * 3600


def _key(password: str) -> bytes:
    return hashlib.sha256(b"homelab-agent-panel:" + password.encode()).digest()


def make_session(password: str, now: float | None = None) -> str:
    exp = int((now or time.time()) + SESSION_TTL_S)
    nonce = secrets.token_hex(8)
    payload = f"{exp}.{nonce}"
    sig = hmac.new(_key(password), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def check_session(password: str, token: str | None, now: float | None = None) -> bool:
    if not token:
        return False
    try:
        exp_s, nonce, sig = token.split(".")
        exp = int(exp_s)
    except ValueError:
        return False
    expected = hmac.new(_key(password), f"{exp_s}.{nonce}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected) and exp > (now or time.time())


def check_password(password: str, candidate: str | None) -> bool:
    return bool(candidate) and hmac.compare_digest(password.encode(), (candidate or "").encode())
