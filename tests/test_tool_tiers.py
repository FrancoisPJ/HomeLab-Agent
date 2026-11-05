"""Strict separation of tools by tier: the house model acts on the house, the deep models
read the Internet and never reach Home Assistant."""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab_agent.config.models import AppConfig, Tier, ToolsSection
from homelab_agent.core.orchestrator import AgentOutcome
from homelab_agent.gateway.base import BufferedReplier, IncomingMessage
from homelab_agent.llm.base import Message, ToolCall
from homelab_agent.tools.registry import ToolContext

from .conftest import wait_for
from .harness import build_harness

ROUTING = {
    "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]},
    "fast_path": False,
}


def names(h: object, tier: Tier) -> set[str]:
    msg = IncomingMessage("111", "111", "x")
    return {t.name for t in h.orchestrator.offered_tools(tier, msg)}  # type: ignore[attr-defined]


def test_defaults() -> None:
    tools = ToolsSection()
    assert tools.allows(Tier.GPU, {"ha", "action"})
    assert not tools.allows(Tier.CPU, {"ha"})
    assert not tools.allows(Tier.CLOUD, {"ha", "action"})
    assert tools.allows(Tier.CPU, {"web"}) and tools.allows(Tier.CLOUD, {"web"})
    assert not tools.allows(Tier.GPU, {"web"})
    # every tag of a tool must be allowed: memory edits need memory_edit too
    assert tools.allows(Tier.CPU, {"memory"}) and not tools.allows(
        Tier.CPU, {"memory", "memory_edit"}
    )


async def test_tools_offered_per_tier(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, web_tools={"enabled": True})
    gpu = names(h, Tier.GPU)
    assert {"ha_call_service", "ha_get_state", "ha_undo", "escalate", "memory_remember"} <= gpu
    assert {"routine_create"} <= gpu
    assert not any(n.startswith("web_") for n in gpu)
    for tier in (Tier.CPU, Tier.CLOUD):
        tools = names(h, tier)
        assert tools == {"web_search", "web_fetch", "memory_list"}, tier
    await h.close()


async def test_web_tools_off_by_default(tmp_path: Path) -> None:
    h = await build_harness(tmp_path)
    assert names(h, Tier.CPU) == {"memory_list"}
    await h.close()


async def test_custom_allowlist(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, tools={"gpu": ["ha"], "cpu": []})
    assert names(h, Tier.CPU) == set()
    gpu = names(h, Tier.GPU)
    assert "ha_get_state" in gpu and "ha_call_service" not in gpu  # action tag not listed
    await h.close()


async def test_deep_prompt_has_no_house(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, web_tools={"enabled": True})
    gpu = (await h.orchestrator.build_messages("111", Tier.GPU, "x"))[0].content
    cpu = (await h.orchestrator.build_messages("111", Tier.CPU, "x"))[0].content
    assert "Entités Home Assistant" in gpu and "light.salon" in gpu and "outils ha_*" in gpu
    assert "web_search" not in gpu
    assert "Entités Home Assistant" not in cpu and "light.salon" not in cpu
    assert "aucun accès à la maison" in cpu and "web_search" in cpu and "NON FIABLE" in cpu
    assert "escalate" not in cpu
    await h.close()


async def test_tool_of_another_tier_is_never_run(tmp_path: Path) -> None:
    """Even if a deep model (fed by a hostile page) calls a house tool, nothing happens."""
    h = await build_harness(tmp_path, web_tools={"enabled": True})
    msg = IncomingMessage("111", "111", "x")
    offered = {t.name for t in h.orchestrator.offered_tools(Tier.CPU, msg)}
    ctx = ToolContext(requester="111", chat_id="111", tier="cpu", model="heavy")
    messages: list[Message] = []
    call = ToolCall(
        "c1", "ha_call_service",
        {"domain": "lock", "service": "unlock", "entity_id": "lock.porte_entree"},
    )  # fmt: skip
    outcome = AgentOutcome()
    await h.orchestrator._run_tools([call], ctx, messages, outcome, offered)
    assert h.ha_calls == []
    assert "indisponible" in messages[0].content
    entry = (await h.db.audit_entries())[0]
    assert entry.tool == "ha_call_service" and entry.outcome == "denied" and entry.tier == "cpu"
    await h.close()


async def test_deep_job_cannot_reach_the_house(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, routing=ROUTING)
    await h.jobs.start()
    r = BufferedReplier()
    text = (
        '[[tool:ha_call_service {"domain": "light", "service": "turn_off",'
        ' "entity_id": "light.salon"}]]'
    )
    await h.orchestrator.handle(IncomingMessage("111", "111", text, Tier.CPU), r)
    await wait_for(lambda: bool(h.notifier.sent))
    assert h.ha_calls == []  # the tool was not even offered to the CPU model
    assert "Réponse simulée (heavy)" in h.notifier.sent[0][1]
    await h.close()


async def test_deep_job_uses_the_web(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, routing=ROUTING, web_tools={"enabled": True})
    await h.jobs.start()
    r = BufferedReplier()
    text = '[[tool:web_fetch {"url": "https://example.org/crepes"}]]'
    await h.orchestrator.handle(IncomingMessage("111", "111", text, Tier.CPU), r)
    await wait_for(lambda: bool(h.notifier.sent))
    assert "C'est fait (heavy)" in h.notifier.sent[0][1]
    entry = (await h.db.audit_entries())[0]
    assert entry.tool == "web_fetch" and entry.outcome == "ok" and entry.tier == "cpu"
    await h.close()


async def test_house_model_has_no_web(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, routing=ROUTING, web_tools={"enabled": True})
    r = BufferedReplier()
    text = '[[tool:web_fetch {"url": "https://example.org/crepes"}]]'
    await h.orchestrator.handle(IncomingMessage("111", "111", text), r)
    assert "Réponse simulée (fast)" in r.messages[-1]
    assert await h.db.audit_entries() == []
    await h.close()


def test_unknown_tier_tag_rejected_by_schema() -> None:
    with pytest.raises(ValueError, match="tools"):
        AppConfig.model_validate({"tools": {"nas": ["web"]}})


async def test_past_actions_are_not_replayed_as_tool_calls_to_the_deep_model(
    tmp_path: Path,
) -> None:
    h = await build_harness(tmp_path, routing=ROUTING, web_tools={"enabled": True})
    r = BufferedReplier()
    await h.orchestrator.handle(
        IncomingMessage("111", "111", "éteins la lumière de la cuisine stp, light cuisine"), r
    )
    msg = IncomingMessage("111", "111", "x")
    gpu_tools = h.orchestrator.offered_tools(Tier.GPU, msg)
    gpu = await h.orchestrator.build_messages("111", Tier.GPU, "x", None, gpu_tools)
    assert any(m.tool_calls for m in gpu)
    cpu_tools = h.orchestrator.offered_tools(Tier.CPU, msg)
    cpu = await h.orchestrator.build_messages("111", Tier.CPU, "x", None, cpu_tools)
    assert not any(m.tool_calls or m.role == "tool" for m in cpu)
    assert [m.role for m in cpu] == ["system", "user", "assistant", "user"]
    await h.close()
