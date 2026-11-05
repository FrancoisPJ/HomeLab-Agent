"""Structured logging with secret masking."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"),  # Telegram bot token
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
]
_SECRET_ENV_HINTS = ("TOKEN", "KEY", "PASSWORD", "SECRET")


class SecretMasker:
    """Replaces known secret values and secret-looking strings with ``***``."""

    def __init__(self, extra_values: list[str] | None = None) -> None:
        self.values: set[str] = set()
        for name, value in os.environ.items():
            if any(h in name.upper() for h in _SECRET_ENV_HINTS) and len(value) >= 6:
                self.values.add(value)
        for v in extra_values or []:
            if v and len(v) >= 4:
                self.values.add(v)

    def add(self, value: str | None) -> None:
        if value and len(value) >= 4:
            self.values.add(value)

    def mask(self, text: str) -> str:
        for v in sorted(self.values, key=len, reverse=True):
            text = text.replace(v, "***")
        for pat in _SECRET_PATTERNS:
            text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
        return text


MASKER = SecretMasker()


class MaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = MASKER.mask(record.getMessage())
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = MASKER.mask(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class MaskingTextFormatter(logging.Formatter):
    def formatException(self, ei: Any) -> str:
        return MASKER.mask(super().formatException(ei))


def setup_logging(level: str = "info", fmt: str = "json") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(MaskingFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            MaskingTextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "aiogram.event", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_fields(**fields: Any) -> dict[str, Any]:
    """Helper: ``log.info("msg", extra=log_fields(a=1))``."""
    return {"fields": fields}
