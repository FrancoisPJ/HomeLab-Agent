"""Panel authentication: password from the environment, HMAC-signed session cookie."""

from __future__ import annotations

import functools
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable

from homelab_agent.config.models import AppConfig

COOKIE = "hla_session"
SESSION_TTL_S = 7 * 24 * 3600
MIN_PASSWORD = 8
MIN_METRICS_TOKEN = 16


class PanelSecretMissing(SystemExit):
    pass


def require_panel_secret(cfg: AppConfig) -> str:
    secret = AppConfig.secret(cfg.web.password_env)
    if not secret or len(secret) < MIN_PASSWORD:
        raise PanelSecretMissing(
            f"Refusing to start the web panel: set {cfg.web.password_env} "
            f"(at least {MIN_PASSWORD} characters) in .env, or disable it with web.enabled: false"
        )
    return secret


KEY_ITERATIONS = 200_000


@functools.lru_cache(maxsize=4)
def _key(password: str) -> bytes:
    """Cookie signing key, derived from the panel password with a slow KDF (computed once)."""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), b"homelab-agent-panel-session", KEY_ITERATIONS
    )


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


def bearer_of(authorization: str) -> str:
    return authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""


def is_authorized(
    cfg: AppConfig, *, path: str, method: str, authorization: str, cookie: str | None
) -> bool:
    """Session cookie or panel password as bearer; the metrics token only opens GET /metrics."""
    password = require_panel_secret(cfg)
    bearer = bearer_of(authorization)
    if bearer and check_password(password, bearer):
        return True
    if bearer and path == "/metrics" and method == "GET":
        token = AppConfig.secret(cfg.web.metrics_token_env)
        if token and len(token) >= MIN_METRICS_TOKEN and check_password(token, bearer):
            return True
    return check_session(password, cookie)


class LoginLimiter:
    """At most ``max_failures`` wrong passwords per client within ``window_s``."""

    def __init__(
        self, max_failures: int = 5, window_s: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:  # fmt: skip
        self.max_failures = max_failures
        self.window_s = window_s
        self.clock = clock
        self._failures: dict[str, list[float]] = {}

    def _recent(self, client: str) -> list[float]:
        now = self.clock()
        # forget every client whose failures are old, not only this one
        self._failures = {
            c: kept
            for c, times in self._failures.items()
            if (kept := [t for t in times if now - t < self.window_s])
        }
        return self._failures.get(client, [])

    def blocked(self, client: str) -> bool:
        return len(self._recent(client)) >= self.max_failures

    def failed(self, client: str) -> None:
        self._failures[client] = [*self._recent(client), self.clock()]

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)
