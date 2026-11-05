"""Load, validate, save and hot-reload ``config.yaml``.

The YAML file is the single source of truth: the web panel writes back into it
(keeping comments, thanks to ruamel round-trip mode) and then reloads it.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from homelab_agent.config.models import AppConfig

# ``$${...}`` is an escaped literal ``${...}`` (not expanded)
_ENV_RE = re.compile(r"(\$?)\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """Configuration could not be loaded; the message is meant for humans."""


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 120
    return y


def contains_env_reference(text: str) -> bool:
    """True when ``text`` could be expanded from the environment once saved in the config."""
    return "${" in text


def expand_env(value: Any, env: dict[str, str] | None = None) -> Any:
    """Recursively replace ``${VAR}`` / ``${VAR:-default}`` in strings (``$${`` = literal)."""
    env = dict(os.environ) if env is None else env
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            if m.group(1):  # escaped: $${VAR} -> literal ${VAR}
                return m.group(0)[1:]
            name, default = m.group(2), m.group(3)
            if name in env and env[name] != "":
                return env[name]
            if default is not None:
                return default
            raise ConfigError(f"environment variable '{name}' is referenced but not set")

        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: expand_env(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, env) for v in value]
    return value


def _plain(data: Any) -> Any:
    """Convert ruamel containers into plain dict/list."""
    if isinstance(data, dict):
        return {str(k): _plain(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_plain(v) for v in data]
    return data


def format_validation_error(err: ValidationError) -> str:
    lines = ["invalid configuration:"]
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "<root>"
        lines.append(f"  - {loc}: {e['msg']}")
    return "\n".join(lines)


def parse_config(text: str, env: dict[str, str] | None = None) -> AppConfig:
    try:
        raw = _yaml().load(text) or {}
    except YAMLError as exc:
        raise ConfigError(f"YAML syntax error: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("the configuration root must be a mapping")
    data = expand_env(_plain(raw), env)
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc)) from exc


def load_config(path: str | Path, env: dict[str, str] | None = None) -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p} (copy config.example.yaml to start)")
    return parse_config(p.read_text(encoding="utf-8"), env)


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + rename; fall back to in-place write for bind-mounted files
    (Docker single-file mounts cannot be replaced by rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    except OSError:
        path.write_text(text, encoding="utf-8")
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        path.write_text(text, encoding="utf-8")


Listener = Callable[[AppConfig], None]


class ConfigStore:
    """Holds the live configuration and writes changes back to the YAML file."""

    def __init__(self, path: str | Path, config: AppConfig | None = None) -> None:
        self.path = Path(path)
        self._config = config if config is not None else load_config(self.path)
        self._listeners: list[Listener] = []

    @property
    def config(self) -> AppConfig:
        return self._config

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def _publish(self) -> None:
        for listener in self._listeners:
            listener(self._config)

    def reload(self) -> AppConfig:
        self._config = load_config(self.path)
        self._publish()
        return self._config

    def raw_text(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    def replace_text(self, text: str) -> AppConfig:
        """Validate then persist a full YAML document (panel editor)."""
        cfg = parse_config(text)
        atomic_write(self.path, text)
        self._config = cfg
        self._publish()
        return cfg

    def update_section(self, keys: list[str], value: Any) -> AppConfig:
        """Set ``keys`` path to ``value`` in the YAML, keeping comments elsewhere."""
        y = _yaml()
        doc = y.load(self.raw_text()) or {}
        node = doc
        for k in keys[:-1]:
            if k not in node or node[k] is None:
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value
        buf = io.StringIO()
        y.dump(doc, buf)
        return self.replace_text(buf.getvalue())
