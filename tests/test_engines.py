from __future__ import annotations

from ruamel.yaml import YAML

from homelab_agent.config.loader import load_config
from homelab_agent.engines import render_llama_swap

from .conftest import ROOT


def test_render_gpu_and_cpu_engines() -> None:
    cfg = load_config(ROOT / "config.example.yaml")
    gpu = YAML(typ="safe").load(render_llama_swap(cfg, "gpu"))
    cpu = YAML(typ="safe").load(render_llama_swap(cfg, "cpu"))
    assert set(gpu["models"]) == {"fast", "quality"}
    assert "groups" not in gpu
    assert set(cpu["models"]) == {"heavy"}
    fast = gpu["models"]["fast"]
    assert "--n-gpu-layers 99" in fast["cmd"]
    assert "${PORT}" in fast["cmd"]
    assert "--jinja" in fast["cmd"]
    assert "--cache-type-k q8_0" in fast["cmd"]
    assert fast["ttl"] == 600
    heavy = cpu["models"]["heavy"]["cmd"]
    assert "--n-gpu-layers 0" in heavy and "--threads 8" in heavy
    assert "/models/Qwen3-14B-Q4_K_M.gguf" in heavy


def test_every_backend_model_exists_in_models_items() -> None:
    cfg = load_config(ROOT / "config.example.yaml")
    names = {m.name for m in cfg.models.items}
    for backend in cfg.backends.values():
        if backend.kind == "openai_compat":
            assert backend.model in names


def test_cpu_only_preset_keeps_both_models_loaded() -> None:
    cfg = load_config(ROOT / "config.cpu-only.example.yaml")
    assert cfg.backends["gpu_fast"].url.startswith("http://llama-cpu")  # type: ignore[union-attr]
    assert cfg.routing.gpu_quality.backend is None
    assert render_llama_swap(cfg, "gpu").count("cmd:") == 0
    cpu = YAML(typ="safe").load(render_llama_swap(cfg, "cpu"))
    assert set(cpu["models"]) == {"fast", "heavy"}
    group = cpu["groups"]["cpu-all"]
    assert group["swap"] is False and set(group["members"]) == {"fast", "heavy"}
