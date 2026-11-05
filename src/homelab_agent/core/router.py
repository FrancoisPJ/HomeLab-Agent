"""Routing funnel: pick a tier, then a healthy backend, with fallback."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from homelab_agent.config.models import AppConfig, Tier
from homelab_agent.core.budget import BudgetManager
from homelab_agent.llm.base import LLMBackend

log = logging.getLogger(__name__)

# "gpu" is the fast, synchronous tier; in the cpu-only preset it is served by the CPU.
TIER_LABELS = {Tier.GPU: "rapide", Tier.CPU: "lourd", Tier.CLOUD: "cloud"}


@dataclass(slots=True)
class RouteDecision:
    tier: Tier
    reason: str
    prefer: str | None = None  # backend to try first (GPU quality step-up)


@dataclass(slots=True)
class Resolution:
    backend: LLMBackend | None
    tier: Tier | None
    notes: list[str] = field(default_factory=list)


class HealthTracker:
    """Remembers which backends recently failed, and probes them periodically."""

    def __init__(self, cooldown_s: float = 30, clock: Callable[[], float] = time.monotonic):
        self.cooldown_s = cooldown_s
        self.clock = clock
        self._down_until: dict[str, float] = {}
        self.last_probe: dict[str, bool] = {}
        self._task: asyncio.Task[None] | None = None

    def mark_down(self, name: str, duration_s: float | None = None) -> None:
        self._down_until[name] = self.clock() + (
            self.cooldown_s if duration_s is None else duration_s
        )
        self.last_probe[name] = False

    def mark_up(self, name: str) -> None:
        self._down_until.pop(name, None)
        self.last_probe[name] = True

    def is_up(self, name: str) -> bool:
        until = self._down_until.get(name)
        return until is None or self.clock() >= until

    async def probe(self, backends: dict[str, LLMBackend]) -> None:
        async def one(name: str, b: LLMBackend) -> None:
            try:
                ok = await asyncio.wait_for(b.health(), timeout=6)
            except Exception:
                ok = False
            if ok:
                self.mark_up(name)
            else:
                self.mark_down(name)

        await asyncio.gather(*(one(n, b) for n, b in backends.items()))

    def start(self, backends: dict[str, LLMBackend], interval_s: float) -> None:
        async def loop() -> None:
            while True:
                with contextlib.suppress(Exception):
                    await self.probe(backends)
                await asyncio.sleep(interval_s)

        if self._task is None:
            self._task = asyncio.create_task(loop(), name="backend-health")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class Router:
    def __init__(
        self,
        get_config: Callable[[], AppConfig],
        backends: dict[str, LLMBackend],
        health: HealthTracker,
        budget: BudgetManager,
    ) -> None:
        self._cfg = get_config
        self.backends = backends
        self.health = health
        self.budget = budget

    @property
    def cfg(self) -> AppConfig:
        return self._cfg()

    def decide(self, text: str, forced: Tier | None = None) -> RouteDecision:
        if forced is not None:
            return RouteDecision(forced, "explicit command")
        esc = self.cfg.routing.escalation
        low = text.casefold()
        for kw in esc.to_cloud_keywords:
            if kw.casefold() in low:
                return RouteDecision(Tier.CLOUD, f"keyword '{kw}'")
        for kw in esc.to_cpu_keywords:
            if kw.casefold() in low:
                return RouteDecision(Tier.CPU, f"keyword '{kw}'")
        if esc.long_message_chars and len(text) > esc.long_message_chars:
            return RouteDecision(Tier.CPU, "long message")
        quality = self.cfg.routing.gpu_quality
        if quality.backend:
            if quality.min_chars and len(text) >= quality.min_chars:
                return RouteDecision(Tier.GPU, "medium request (length)", quality.backend)
            for kw in quality.keywords:
                if kw.casefold() in low:
                    return RouteDecision(Tier.GPU, f"medium request ('{kw}')", quality.backend)
        return RouteDecision(Tier.GPU, "default")

    async def pick(
        self, tier: Tier, exclude: set[str] | None = None, prefer: str | None = None
    ) -> tuple[LLMBackend | None, str]:
        """Return the first usable backend of ``tier`` (or None and a reason).

        ``prefer`` (the GPU quality backend) is tried first but is never used as a failover.
        """
        exclude = exclude or set()
        reasons: list[str] = []
        if prefer and prefer not in exclude:
            preferred = self.backends.get(prefer)
            if preferred is not None and preferred.tier == tier and self.health.is_up(prefer):
                return preferred, ""
        names = self.cfg.backends_for(tier)
        if not names:
            return None, f"aucun backend configuré pour le niveau {TIER_LABELS[tier]}"
        for name in names:
            backend = self.backends.get(name)
            if backend is None or name in exclude:
                continue
            if not self.health.is_up(name):
                reasons.append(f"{name} indisponible")
                continue
            if tier == Tier.CLOUD:
                check = await self.budget.check(backend.provider)
                if not check.allowed:
                    reasons.append(check.reason)
                    continue
            return backend, ""
        return None, "; ".join(dict.fromkeys(reasons)) or f"{TIER_LABELS[tier]} indisponible"

    def neighbours(self, tier: Tier) -> list[Tier]:
        order = list(self.cfg.routing.fallback_order)
        if tier not in order:
            return order
        idx = order.index(tier)
        # nearest tiers first: after, before, after+1, ...
        out: list[Tier] = []
        for d in range(1, len(order)):
            for j in (idx + d, idx - d):
                if 0 <= j < len(order) and order[j] not in out:
                    out.append(order[j])
        return out

    async def resolve(
        self, tier: Tier, exclude: set[str] | None = None, prefer: str | None = None
    ) -> Resolution:
        backend, why = await self.pick(tier, exclude, prefer)
        if backend is not None:
            return Resolution(backend, tier)
        notes = [f"{TIER_LABELS[tier]} : {why}"]
        for alt in self.neighbours(tier):
            backend, alt_why = await self.pick(alt, exclude)
            if backend is not None:
                notes.append(f"→ bascule sur {TIER_LABELS[alt]}")
                return Resolution(backend, alt, notes)
            notes.append(f"{TIER_LABELS[alt]} : {alt_why}")
        return Resolution(None, None, notes)
