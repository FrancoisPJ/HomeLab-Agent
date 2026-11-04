"""Agent loop: message → (fast path | routing) → LLM ↔ tools → answer."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homelab_agent.config.models import AppConfig, Tier
from homelab_agent.core import fastpath
from homelab_agent.core.budget import BudgetManager
from homelab_agent.core.prompts import build_system_prompt
from homelab_agent.core.router import TIER_LABELS, Router
from homelab_agent.gateway.base import IncomingMessage, Replier, StreamHandle
from homelab_agent.llm.base import (
    BackendError,
    BackendUnavailable,
    ChatResult,
    LLMBackend,
    Message,
    StreamDelta,
    StreamDone,
    Usage,
)
from homelab_agent.observability.logging import log_fields
from homelab_agent.observability.metrics import METRICS
from homelab_agent.store.db import Database
from homelab_agent.tools.registry import ESCALATE_TOOL_NAME, ToolContext, ToolRegistry

if TYPE_CHECKING:
    from homelab_agent.core.jobs import JobQueue
    from homelab_agent.tools.homeassistant.tools import HomeAssistantService

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentOutcome:
    text: str = ""
    usage: Usage = field(default_factory=Usage)
    escalation: dict[str, str] | None = None
    tool_failures: int = 0
    finished: bool = False


class _NullStream:
    async def update(self, text: str) -> None:
        return None

    async def finish(self, text: str) -> None:
        return None


class Orchestrator:
    def __init__(
        self,
        get_config: Callable[[], AppConfig],
        db: Database,
        router: Router,
        registry: ToolRegistry,
        budget: BudgetManager,
        ha: HomeAssistantService | None = None,
    ) -> None:
        self._cfg = get_config
        self.db = db
        self.router = router
        self.registry = registry
        self.budget = budget
        self.ha = ha
        self.jobs: JobQueue | None = None

    @property
    def cfg(self) -> AppConfig:
        return self._cfg()

    # ----------------------------------------------------------------- entry
    async def handle(self, msg: IncomingMessage, replier: Replier) -> None:
        METRICS.inc("messages_total")
        if msg.forced_tier is None and await self._try_fast_path(msg, replier):
            return
        decision = self.router.decide(msg.text, msg.forced_tier)
        log.info(
            "route tier=%s reason=%s",
            decision.tier.value,
            decision.reason,
            extra=log_fields(tier=decision.tier.value, reason=decision.reason),
        )
        await self.db.add_message(msg.chat_id, "user", msg.text)
        await self._dispatch(msg, replier, decision.tier, set(), [])

    async def _try_fast_path(self, msg: IncomingMessage, replier: Replier) -> bool:
        if self.ha is None or not self.cfg.home_assistant.enabled:
            return False
        m = fastpath.match(msg.text, self.cfg.routing.fast_path, self.ha.client.cache)
        if m is None:
            return False
        start = time.monotonic()
        ctx = ToolContext(
            requester=msg.user_id, chat_id=msg.chat_id, tier="fast_path", model="-",
            confirm=replier.confirm,
        )  # fmt: skip
        res = await self.ha.act(m.domain, m.service, m.entity_ids, {}, ctx, tool_name="fast_path")
        if res.ok:
            await replier.send(f"✅ {m.domain}.{m.service} → {', '.join(m.names)}")
        elif res.outcome == "declined":
            await replier.send("👍 Action annulée.")
        else:
            await replier.send(f"⛔ {res.error}")
        METRICS.inc("replies_total", tier="fast_path")
        await self.db.record_interaction(
            chat_id=msg.chat_id, tier="fast_path", backend="-", model="-",
            latency_ms=int((time.monotonic() - start) * 1000),
        )  # fmt: skip
        return True

    async def _dispatch(
        self,
        msg: IncomingMessage,
        replier: Replier,
        tier: Tier,
        exclude: set[str],
        notes: list[str],
        depth: int = 0,
    ) -> None:
        res = await self.router.resolve(tier, exclude)
        notes = notes + res.notes
        if res.backend is None or res.tier is None:
            await replier.send("❌ Aucun moteur disponible.\n" + "\n".join(notes))
            return
        backend, tier = res.backend, res.tier
        if tier == Tier.CPU and self.jobs is not None:
            job_id = await self.jobs.enqueue(msg.chat_id, msg.user_id, msg.text)
            note = ("\nℹ️ " + " ".join(notes)) if notes else ""
            await replier.send(f"⏳ Je m'en occupe (tâche #{job_id}), je te réponds ici.{note}")
            return
        if notes:
            await replier.send("ℹ️ " + " ".join(notes))
        start = time.monotonic()
        try:
            outcome = await self.run_agent(backend, tier, msg, replier.stream(), replier.confirm)
        except BackendUnavailable as exc:
            log.warning("backend unavailable: %s", exc)
            self.router.health.mark_down(backend.name)
            if depth >= 3:
                await replier.send(f"❌ {exc}")
                return
            await self._dispatch(
                msg, replier, tier, exclude | {backend.name},
                [f"{backend.name} ne répond pas."], depth + 1,
            )  # fmt: skip
            return
        except BackendError as exc:
            log.error("backend error: %s", exc)
            await replier.send(f"❌ Erreur du moteur {backend.name}.")
            return
        await self._account(msg.chat_id, backend, tier, outcome.usage, start, replier)

        if outcome.escalation and depth < 3:
            target = Tier(outcome.escalation["target"])
            reason = outcome.escalation.get("reason") or "demande du modèle"
            await self._dispatch(
                msg, replier, target, exclude,
                [f"Escalade vers {TIER_LABELS[target]} ({reason})."], depth + 1,
            )  # fmt: skip
            return
        if not outcome.finished and depth < 3:
            nxt = next((t for t in self.router.neighbours(tier) if t != Tier.GPU), None)
            if nxt is not None:
                await self._dispatch(
                    msg, replier, nxt, exclude,
                    ["Échecs d'outils répétés, je passe la main."], depth + 1,
                )  # fmt: skip
                return
        if outcome.text:
            await self.db.add_message(msg.chat_id, "assistant", outcome.text)

    async def _account(
        self,
        chat_id: str,
        backend: LLMBackend,
        tier: Tier,
        usage: Usage,
        start: float,
        replier: Replier | None,
    ) -> float:
        cost = 0.0
        if tier == Tier.CLOUD:
            cost, alerts = await self.budget.record(
                backend.provider, usage.input_tokens, usage.output_tokens
            )
            for alert in alerts:
                if replier is not None:
                    await replier.send(alert)
        latency = int((time.monotonic() - start) * 1000)
        METRICS.inc("replies_total", tier=tier.value)
        METRICS.observe("latency_ms", latency, tier=tier.value)
        METRICS.inc("tokens_total", usage.input_tokens + usage.output_tokens, backend=backend.name)
        await self.db.record_interaction(
            chat_id=chat_id, tier=tier.value, backend=backend.name, model=backend.model,
            latency_ms=latency, tokens_in=usage.input_tokens, tokens_out=usage.output_tokens,
            cost_eur=cost,
        )  # fmt: skip
        return cost

    # ------------------------------------------------------------ agent loop
    async def build_messages(self, chat_id: str, tier: Tier, text: str) -> list[Message]:
        summary = ""
        if self.ha is not None and self.cfg.home_assistant.enabled:
            summary = self.ha.context_summary()
        messages = [Message("system", build_system_prompt(self.cfg, tier, summary))]
        history = await self.db.history(chat_id, self.cfg.agent.history_messages + 1)
        # the current user message is already stored as the last item
        if history and history[-1].role == "user" and history[-1].content == text:
            history = history[:-1]
        for h in history[-self.cfg.agent.history_messages :] if history else []:
            if h.role in ("user", "assistant"):
                messages.append(Message(h.role, h.content))  # type: ignore[arg-type]
        messages.append(Message("user", text))
        return messages

    async def run_agent(
        self,
        backend: LLMBackend,
        tier: Tier,
        msg: IncomingMessage,
        stream: StreamHandle | None = None,
        confirm: Callable[[str], object] | None = None,
    ) -> AgentOutcome:
        stream = stream or _NullStream()
        cfg = self.cfg
        messages = await self.build_messages(msg.chat_id, tier, msg.text)
        exclude = set()
        if tier != Tier.GPU or not cfg.routing.escalation.allow_model_escalation:
            exclude.add("internal")
        if self.ha is None or not cfg.home_assistant.enabled:
            exclude.add("ha")
        tools = self.registry.specs(exclude_tags=exclude)
        ctx = ToolContext(
            requester=msg.user_id, chat_id=msg.chat_id, tier=tier.value, model=backend.model
        )
        if confirm is not None:
            ctx.confirm = confirm  # type: ignore[assignment]
        outcome = AgentOutcome()
        max_failures = cfg.routing.escalation.max_tool_failures
        for _ in range(cfg.agent.max_tool_rounds):
            buffer = ""
            result: ChatResult | None = None
            async for ev in backend.stream(messages, tools or None):
                if isinstance(ev, StreamDelta):
                    buffer += ev.text
                    await stream.update(buffer)
                elif isinstance(ev, StreamDone):
                    result = ev.result
            if result is None:
                raise BackendError(f"{backend.name}: stream ended without result")
            outcome.usage = outcome.usage + result.usage
            if not result.tool_calls:
                outcome.text = result.content.strip() or "(réponse vide)"
                outcome.finished = True
                await stream.finish(outcome.text)
                return outcome
            messages.append(Message("assistant", result.content, tool_calls=result.tool_calls))
            for call in result.tool_calls:
                tool = self.registry.get(call.name)
                r = await self.registry.execute(call.name, call.arguments, ctx)
                METRICS.inc("tool_calls_total", tool=call.name, outcome=r.outcome)
                if tool is None or "action" not in tool.tags:
                    await self.db.audit(
                        requester=ctx.requester, tier=ctx.tier, model=ctx.model, tool=call.name,
                        params=call.arguments, outcome=r.outcome, detail=r.error or "",
                    )  # fmt: skip
                if call.name == ESCALATE_TOOL_NAME and ctx.escalation:
                    outcome.escalation = ctx.escalation
                    await stream.finish("↗️ Je transmets à un modèle plus puissant…")
                    return outcome
                if not r.ok and r.outcome == "error":
                    outcome.tool_failures += 1
                messages.append(Message("tool", r.to_text(), tool_call_id=call.id, name=call.name))
            if outcome.tool_failures >= max_failures and tier == Tier.GPU:
                await stream.finish("⚠️ Les outils échouent, je passe la main…")
                return outcome
        outcome.text = "⚠️ Limite d'étapes atteinte sans réponse finale."
        outcome.finished = True
        await stream.finish(outcome.text)
        return outcome

    # ------------------------------------------------------------- CPU jobs
    async def run_job(self, chat_id: str, user_id: str, prompt: str, replier: Replier) -> str:
        backend, why = await self.router.pick(Tier.CPU)
        if backend is None:
            raise BackendUnavailable(why)
        msg = IncomingMessage(user_id=user_id, chat_id=chat_id, text=prompt)
        start = time.monotonic()
        try:
            outcome = await self.run_agent(backend, Tier.CPU, msg, None, replier.confirm)
        except BackendUnavailable:
            self.router.health.mark_down(backend.name)
            raise
        await self._account(chat_id, backend, Tier.CPU, outcome.usage, start, replier)
        await self.db.add_message(chat_id, "assistant", outcome.text)
        return outcome.text
