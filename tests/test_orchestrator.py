from __future__ import annotations

from pathlib import Path

import pytest

from homelab_agent.config.models import Tier
from homelab_agent.gateway.base import BufferedReplier, IncomingMessage

from .conftest import wait_for
from .harness import build_harness


def msg(text: str, tier: Tier | None = None) -> IncomingMessage:
    return IncomingMessage(user_id="111", chat_id="111", text=text, forced_tier=tier)


async def test_gpu_answers_directly(tmp_path: Path) -> None:
    h = await build_harness(tmp_path)
    r = BufferedReplier()
    await h.orchestrator.handle(msg("quelle heure est-il"), r)
    assert r.messages and "Réponse simulée (fast)" in r.messages[-1]
    inter = await h.db.recent_interactions()
    assert inter[0].tier == "gpu" and inter[0].backend == "gpu_fast" and inter[0].tokens_in > 0
    await h.close()


async def test_gpu_tool_call_reaches_home_assistant(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path,
        routing={
            "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]},
            "fast_path": False,
        },
    )
    r = BufferedReplier()
    await h.orchestrator.handle(msg("éteins la lumière de la cuisine stp, light cuisine"), r)
    assert h.ha_calls == [
        {"domain": "light", "service": "turn_off", "data": {"entity_id": ["light.cuisine"]}}
    ]
    assert "C'est fait" in r.messages[-1]
    audit = await h.db.audit_entries()
    assert (
        audit[0].tool == "ha_call_service" and audit[0].outcome == "ok" and audit[0].tier == "gpu"
    )
    await h.close()


async def test_fast_path_skips_llm(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path,
        routing={
            "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]},
            "fast_path": {"enabled": True, "aliases": {"salon": "light.salon"}},
        },
    )
    await h.aset_llm("gpu_fast", fail=True)  # proves no LLM is needed
    r = BufferedReplier()
    await h.orchestrator.handle(msg("Éteins le salon"), r)
    assert h.ha_calls[0]["service"] == "turn_off"
    assert r.messages[0].startswith("✅")
    assert (await h.db.recent_interactions())[0].tier == "fast_path"
    await h.close()


async def test_fast_path_respects_permissions(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path,
        home_assistant={"url": "http://ha", "rules": [{"domain": "light", "mode": "read_only"}]},
        routing={
            "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]},
            "fast_path": {"aliases": {"salon": "light.salon"}},
        },
    )
    r = BufferedReplier()
    await h.orchestrator.handle(msg("éteins le salon"), r)
    assert h.ha_calls == []
    assert "permission denied" in r.messages[0]
    assert (await h.db.audit_entries())[0].outcome == "denied"
    await h.close()


async def test_fallback_when_gpu_is_down(tmp_path: Path) -> None:
    h = await build_harness(tmp_path)
    await h.aset_llm("gpu_fast", fail=True)
    r = BufferedReplier()
    await h.orchestrator.handle(msg("bonjour"), r)
    # GPU failed → neighbour tier is CPU → asynchronous job
    assert any("ne répond pas" in m for m in r.messages)
    assert any("tâche #" in m for m in r.messages)
    assert not h.router.health.is_up("gpu_fast")
    await h.close()


async def test_cpu_job_answer_arrives_later(tmp_path: Path) -> None:
    h = await build_harness(tmp_path)
    await h.jobs.start()
    r = BufferedReplier()
    await h.orchestrator.handle(msg("hello", Tier.CPU), r)
    assert "Je m'en occupe" in r.messages[0]
    await wait_for(lambda: bool(h.notifier.sent))
    chat, text = h.notifier.sent[0]
    assert chat == "111" and "terminée" in text and "Réponse simulée (heavy)" in text
    jobs = await h.db.list_jobs()
    assert jobs[0].status == "done"
    await h.close()


async def test_keywords_route_to_cpu(tmp_path: Path) -> None:
    h = await build_harness(tmp_path)
    r = BufferedReplier()
    await h.orchestrator.handle(msg("analyse mes factures du mois"), r)
    assert "tâche #1" in r.messages[0]
    await h.close()


async def test_model_escalation_to_cloud(tmp_path: Path) -> None:
    h = await build_harness(tmp_path)
    r = BufferedReplier()
    await h.orchestrator.handle(msg("question difficile [[escalate:cloud]]"), r)
    assert any("Escalade" in m for m in r.messages)
    assert "Réponse simulée (big)" in r.messages[-1]
    inter = await h.db.recent_interactions()
    assert inter[0].tier == "cloud" and inter[0].cost_eur > 0
    await h.close()


async def test_cloud_cap_reached_falls_back_to_local(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path, cloud={"enabled": True, "limits": {"monthly_eur": 0, "daily_eur": 0}}
    )
    r = BufferedReplier()
    await h.orchestrator.handle(msg("/cloud x", Tier.CLOUD), r)
    joined = "\n".join(r.messages)
    assert "plafond" in joined
    assert "bascule" in joined
    await h.close()


async def test_cloud_disabled_message(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, cloud={"enabled": False})
    r = BufferedReplier()
    await h.orchestrator.handle(msg("x", Tier.CLOUD), r)
    assert "désactivé" in r.messages[0]
    await h.close()


async def test_budget_alert_is_sent(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path,
        cloud={
            "enabled": True,
            "pricing": {"cloud": {"in_per_mtok": 100000, "out_per_mtok": 100000}},
            "limits": {"monthly_eur": 100, "daily_eur": 30, "alert_at": 0.5},
        },
    )
    r = BufferedReplier()
    await h.orchestrator.handle(msg("dis bonjour", Tier.CLOUD), r)
    assert any("Budget cloud" in m for m in r.messages)
    await h.close()


async def test_nothing_available(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, cloud={"enabled": False})
    for name in ("gpu_fast", "cpu_heavy"):
        h.router.health.mark_down(name)
    r = BufferedReplier()
    await h.orchestrator.handle(msg("bonjour"), r)
    assert r.messages[0].startswith("❌ Aucun moteur disponible")
    await h.close()


async def test_repeated_tool_failures_hand_over(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path,
        routing={
            "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]},
            "fast_path": False,
            "escalation": {"max_tool_failures": 1},
        },
    )
    r = BufferedReplier()
    await h.orchestrator.handle(msg('[[tool:ha_get_state {"entity_id": "sensor.inexistant"}]]'), r)
    # house tools stay on the GPU: no other tier may use them, so no hand-over
    assert any("Les outils échouent" in m and "404" in m for m in r.messages)
    assert not any("passe la main" in m for m in r.messages)
    assert await h.db.list_jobs() == []
    await h.close()


async def test_repeated_tool_failures_hand_over_when_allowed(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path,
        routing={
            "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]},
            "fast_path": False,
            "escalation": {"max_tool_failures": 1},
        },
        tools={"cpu": ["ha", "memory"]},  # the CPU tier may read the house here
    )
    r = BufferedReplier()
    await h.orchestrator.handle(msg('[[tool:ha_get_state {"entity_id": "sensor.inexistant"}]]'), r)
    assert any("passe la main" in m for m in r.messages)
    assert any("tâche #" in m for m in r.messages)
    await h.close()


async def test_history_is_used_and_reset(tmp_path: Path) -> None:
    h = await build_harness(tmp_path)
    r = BufferedReplier()
    await h.orchestrator.handle(msg("premier"), r)
    msgs = await h.orchestrator.build_messages("111", Tier.GPU, "second")
    assert [m.role for m in msgs] == ["system", "user", "assistant", "user"]
    assert "Entités Home Assistant" in msgs[0].content
    await h.db.clear_history("111")
    msgs = await h.orchestrator.build_messages("111", Tier.GPU, "x")
    assert [m.role for m in msgs] == ["system", "user"]
    await h.close()


def _quality_overrides() -> dict[str, object]:
    return {
        "backends": {
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
                "model": "quality",
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
        "routing": {
            "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]},
            "fast_path": False,
            "gpu_quality": {
                "backend": "gpu_quality",
                "keywords": ["pourquoi"],
                "retry_after_s": 300,
            },
        },
    }


async def test_medium_request_uses_quality_model(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, **_quality_overrides())
    r = BufferedReplier()
    await h.orchestrator.handle(msg("pourquoi le chauffage s'allume ?"), r)
    assert "Réponse simulée (quality)" in r.messages[-1]
    await h.close()


async def test_quality_model_cannot_load_falls_back_to_fast(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, **_quality_overrides())
    await h.aset_llm("gpu_quality", fail=True)  # e.g. VRAM taken by Jellyfin
    r = BufferedReplier()
    await h.orchestrator.handle(msg("pourquoi le chauffage s'allume ?"), r)
    assert "Réponse simulée (fast)" in r.messages[-1]
    assert not any("tâche" in m for m in r.messages)  # stayed synchronous on the GPU tier
    assert not h.router.health.is_up("gpu_quality")
    await h.close()


async def test_history_is_trimmed_to_fit_context(tmp_path: Path) -> None:
    h = await build_harness(tmp_path)
    for i in range(10):
        await h.db.add_message("111", "user", f"question {i} " + "x" * 400)
        await h.db.add_message("111", "assistant", f"réponse {i} " + "y" * 400)
    backend = h.backends["gpu_fast"]
    full = await h.orchestrator.build_messages("111", Tier.GPU, "dernière question")
    backend.max_context = 1500
    backend.max_output_tokens = 200
    fitted = await h.orchestrator.build_messages("111", Tier.GPU, "dernière question", backend)
    assert len(fitted) < len(full)
    assert fitted[-1].content == "dernière question"
    assert fitted[-2].content.startswith("réponse 9")  # the most recent exchange is kept
    assert sum(len(m.content) // 3 + 5 for m in fitted) <= 1500 - 200
    await h.close()


async def test_context_overflow_goes_to_a_bigger_backend(tmp_path: Path) -> None:
    from homelab_agent.llm.base import ContextOverflow

    h = await build_harness(tmp_path)
    backend = h.backends["gpu_fast"]
    backend.max_context = 600
    backend.max_output_tokens = 100
    with pytest.raises(ContextOverflow):
        await h.orchestrator.build_messages("111", Tier.GPU, "z" * 3000, backend)
    # the server-side limit (HTTP 400 from llama-server) is handled the same way
    backend.max_context = None
    await h.aset_llm("gpu_fast", n_ctx=300)
    r = BufferedReplier()
    await h.orchestrator.handle(msg("dis bonjour " + "mot " * 100), r)
    assert any("trop long pour gpu_fast" in m for m in r.messages)
    assert any("tâche #" in m for m in r.messages)  # went on to the CPU tier
    assert h.router.health.is_up("gpu_fast")  # not marked down: it is healthy
    await h.close()
