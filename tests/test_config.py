from __future__ import annotations

import json
from pathlib import Path

import pytest

from homelab_agent.cli import main, schema_json
from homelab_agent.config.loader import (
    ConfigError,
    ConfigStore,
    expand_env,
    load_config,
    parse_config,
)
from homelab_agent.config.models import Policy, Tier

from .conftest import ROOT


def test_example_config_is_valid() -> None:
    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg.cloud.enabled is False
    assert cfg.cloud.limits.monthly_eur == 0
    assert cfg.home_assistant.default_policy == Policy.ALLOW
    assert cfg.home_assistant.confirm == []
    assert cfg.web.bind == "127.0.0.1"
    assert cfg.backends_for(Tier.GPU) == ["gpu_fast", "gpu_fallback"]


def test_dev_config_is_valid() -> None:
    load_config(ROOT / "deploy" / "config.dev.yaml")


def test_committed_schema_is_up_to_date() -> None:
    committed = (ROOT / "config.schema.json").read_text(encoding="utf-8")
    assert committed == schema_json(), "run `make schema` and commit config.schema.json"
    assert json.loads(committed)["title"] == "homelab-agent configuration"


def test_invalid_config_gives_readable_error() -> None:
    with pytest.raises(ConfigError) as err:
        parse_config("web:\n  port: 99999\nagent:\n  log_level: loud\n")
    msg = str(err.value)
    assert "web.port" in msg
    assert "agent.log_level" in msg


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ConfigError, match=r"telegram\.alowed_user_ids"):
        parse_config("telegram:\n  alowed_user_ids: [1]\n")


def test_yaml_syntax_error() -> None:
    with pytest.raises(ConfigError, match="YAML syntax error"):
        parse_config("agent: [unclosed\n")


def test_tier_references_unknown_backend() -> None:
    with pytest.raises(ConfigError, match="unknown backend 'nope'"):
        parse_config("routing:\n  tiers:\n    gpu: [nope]\n")


def test_tier_mismatch() -> None:
    text = """
backends:
  a: {kind: openai_compat, tier: cpu, url: "http://x/v1", model: m}
routing:
  tiers: {gpu: [a]}
"""
    with pytest.raises(ConfigError, match="has tier 'cpu'"):
        parse_config(text)


def test_env_expansion() -> None:
    env = {"HA_URL": "http://ha.example:8123"}
    cfg = parse_config(
        "home_assistant:\n  url: ${HA_URL}\nweb:\n  bind: ${BIND:-0.0.0.0}\n"
        "routing:\n  tiers: {}\n",
        env,
    )
    assert cfg.home_assistant.url == "http://ha.example:8123"
    assert cfg.web.bind == "0.0.0.0"
    with pytest.raises(ConfigError, match="MISSING"):
        expand_env("${MISSING}", {})


def test_secret_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from homelab_agent.config.models import AppConfig

    monkeypatch.setenv("SOME_TOKEN", "abc")
    assert AppConfig.secret("SOME_TOKEN") == "abc"
    assert AppConfig.secret("NOT_SET_ANYWHERE") is None
    assert AppConfig.secret(None) is None


def test_store_update_keeps_comments(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        "# top comment\nrouting:\n  tiers: {}\nhome_assistant:\n  default_policy: allow  # keep\n",
        encoding="utf-8",
    )
    store = ConfigStore(p)
    seen = []
    store.subscribe(lambda c: seen.append(c.home_assistant.default_policy))
    store.update_section(["home_assistant", "rules"], [{"domain": "lock", "mode": "deny"}])
    text = p.read_text(encoding="utf-8")
    assert "# top comment" in text and "# keep" in text
    assert store.config.home_assistant.rules[0].mode == Policy.DENY
    assert seen == [Policy.ALLOW]


def test_store_rejects_invalid_text(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("routing:\n  tiers: {}\n", encoding="utf-8")
    store = ConfigStore(p)
    with pytest.raises(ConfigError):
        store.replace_text("web:\n  port: -1\n")
    assert p.read_text(encoding="utf-8") == "routing:\n  tiers: {}\n"


def test_cli_check_config(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["-c", str(ROOT / "config.example.yaml"), "check-config"]) == 0
    assert "configuration OK" in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    bad.write_text("web: {port: 0}\n", encoding="utf-8")
    assert main(["-c", str(bad), "check-config"]) == 2
    assert main(["-c", str(tmp_path / "missing.yaml"), "check-config"]) == 2


def test_cli_models_and_engines(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["-c", str(ROOT / "config.example.yaml"), "models"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("Qwen/Qwen3-4B-GGUF\tQwen3-4B-Q4_K_M.gguf\t")
    assert (
        main(["-c", str(ROOT / "config.example.yaml"), "render-engines", "-o", str(tmp_path)]) == 0
    )
    assert (tmp_path / "llama-swap-gpu.yaml").exists()
