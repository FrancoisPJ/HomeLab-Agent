from __future__ import annotations

from homelab_agent.config.models import Tier
from homelab_agent.core.budget import BudgetManager
from homelab_agent.core.router import HealthTracker, Router
from homelab_agent.llm.base import ChatResult, LLMBackend, Message, ToolSpec
from homelab_agent.store.db import Database

from .conftest import base_config


class Dummy(LLMBackend):
    def __init__(self, name: str, tier: Tier, healthy: bool = True) -> None:
        super().__init__(name, tier, name)
        self.healthy = healthy

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResult:
        return ChatResult("ok")

    async def health(self) -> bool:
        return self.healthy


def make(db: Database, **cfg_over: object) -> Router:
    cfg = base_config(**cfg_over)
    backends = {
        "gpu_fast": Dummy("gpu_fast", Tier.GPU),
        "cpu_heavy": Dummy("cpu_heavy", Tier.CPU),
        "cloud": Dummy("cloud", Tier.CLOUD),
    }
    return Router(
        lambda: cfg, backends, HealthTracker(30, clock=lambda: 0.0), BudgetManager(lambda: cfg, db)
    )


async def test_decide(db: Database) -> None:
    r = make(db)
    assert r.decide("allume").tier == Tier.GPU
    assert r.decide("Analyse ce texte").tier == Tier.CPU
    assert r.decide("c'est URGENT").tier == Tier.CLOUD
    assert r.decide("x" * 700).tier == Tier.CPU
    assert r.decide("x", Tier.CLOUD).reason == "explicit command"


async def test_neighbours(db: Database) -> None:
    r = make(db)
    assert r.neighbours(Tier.GPU) == [Tier.CPU, Tier.CLOUD]
    assert r.neighbours(Tier.CPU) == [Tier.CLOUD, Tier.GPU]
    assert r.neighbours(Tier.CLOUD) == [Tier.CPU, Tier.GPU]


async def test_resolve_with_fallback(db: Database) -> None:
    r = make(db)
    res = await r.resolve(Tier.GPU)
    assert res.backend is not None and res.backend.name == "gpu_fast" and not res.notes
    r.health.mark_down("gpu_fast")
    res = await r.resolve(Tier.GPU)
    assert res.tier == Tier.CPU and "bascule" in " ".join(res.notes)


async def test_cloud_blocked_by_budget(db: Database) -> None:
    r = make(db, cloud={"enabled": False})
    backend, why = await r.pick(Tier.CLOUD)
    assert backend is None and "désactivé" in why
    res = await r.resolve(Tier.CLOUD)
    assert res.tier == Tier.CPU


async def test_health_probe_and_cooldown(db: Database) -> None:
    now = [0.0]
    h = HealthTracker(10, clock=lambda: now[0])
    backends = {"a": Dummy("a", Tier.GPU, healthy=False), "b": Dummy("b", Tier.GPU)}
    await h.probe(backends)  # type: ignore[arg-type]
    assert not h.is_up("a") and h.is_up("b")
    now[0] = 11
    assert h.is_up("a")  # cooldown expired → will be retried


async def test_empty_tier(db: Database) -> None:
    r = make(db, routing={"tiers": {"gpu": ["gpu_fast"]}})
    backend, why = await r.pick(Tier.CPU)
    assert backend is None and "aucun backend" in why


async def test_gpu_quality_step_up_is_not_a_failover(db: Database) -> None:
    cfg = base_config(
        backends={
            "gpu_fast": {
                "kind": "openai_compat",
                "tier": "gpu",
                "url": "http://gpu/v1",
                "model": "fast",
            },
            "gpu_quality": {
                "kind": "openai_compat",
                "tier": "gpu",
                "url": "http://gpu/v1",
                "model": "q",
            },
            "cpu_heavy": {
                "kind": "openai_compat",
                "tier": "cpu",
                "url": "http://cpu/v1",
                "model": "heavy",
            },
            "cloud": {
                "kind": "openai_compat",
                "tier": "cloud",
                "url": "http://cloud/v1",
                "model": "big",
            },
        },
        routing={
            "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]},
            "gpu_quality": {"backend": "gpu_quality", "min_chars": 50, "keywords": ["pourquoi"]},
        },
    )
    backends = {n: Dummy(n, b.tier) for n, b in cfg.backends.items()}
    r = Router(
        lambda: cfg, backends, HealthTracker(30, clock=lambda: 0.0), BudgetManager(lambda: cfg, db)
    )  # type: ignore[arg-type]
    assert r.decide("allume").prefer is None
    assert r.decide("pourquoi le ciel est bleu").prefer == "gpu_quality"
    assert r.decide("x" * 60).prefer == "gpu_quality"
    b, _ = await r.pick(Tier.GPU, prefer="gpu_quality")
    assert b is not None and b.name == "gpu_quality"
    r.health.mark_down("gpu_quality", 300)
    b, _ = await r.pick(Tier.GPU, prefer="gpu_quality")
    assert b is not None and b.name == "gpu_fast"
    r.health.mark_down("gpu_fast")
    res = await r.resolve(Tier.GPU)  # fast down: never fails over to the quality model
    assert res.tier == Tier.CPU


def test_quality_backend_must_be_gpu() -> None:
    import pytest

    from homelab_agent.config.loader import ConfigError, parse_config

    with pytest.raises(ConfigError, match="must have tier 'gpu'"):
        parse_config(
            "backends:\n  c: {kind: openai_compat, tier: cpu, url: 'http://x/v1', model: m}\n"
            "routing:\n  tiers: {cpu: [c]}\n  gpu_quality: {backend: c}\n"
        )
