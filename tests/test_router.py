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
