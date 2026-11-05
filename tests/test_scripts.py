"""Helper scripts shipped with the repository."""

from __future__ import annotations

import importlib.util
import stat
from pathlib import Path
from types import ModuleType

from .conftest import ROOT


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_init_env_generates_only_local_secrets(tmp_path: Path) -> None:
    init_env = _load("init_env")
    env = tmp_path / ".env"
    env.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    assert init_env.main(str(env)) == 0
    values = dict(
        line.split("=", 1) for line in env.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )  # fmt: skip
    for key in ("PANEL_PASSWORD", "METRICS_TOKEN", "GRAFANA_ADMIN_PASSWORD", "SEARXNG_SECRET"):
        assert len(values[key]) >= 16 and "change-me" not in values[key], key
    assert values["PANEL_PASSWORD"] != values["METRICS_TOKEN"]
    # tokens from other services are left for the user to paste
    assert values["TELEGRAM_BOT_TOKEN"] == "000000000:REPLACE_ME"
    assert values["ANTHROPIC_API_KEY"] == ""
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_init_env_keeps_user_values() -> None:
    init_env = _load("init_env")
    text, done = init_env.fill("PANEL_PASSWORD=mine-and-long-enough\nMETRICS_TOKEN=\n")
    assert "PANEL_PASSWORD=mine-and-long-enough" in text
    assert done == ["METRICS_TOKEN"]
