"""The bot must never claim an action it did not do (bug seen with Qwen3-8B in 0.5.1).

Past actions were written in the history as text (``[actions effectuées : …]``); the model
copied that text instead of calling the tool. Actions are now replayed as real tool calls, and
an answer that claims an action without any tool call is retried once, then replaced.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from homelab_agent.config.models import Tier
from homelab_agent.core.history import (
    NO_ACTION_REPLY,
    claims_action,
    replay,
    strip_fake_actions,
)
from homelab_agent.gateway.base import BufferedReplier, IncomingMessage
from homelab_agent.store.db import SCHEMA_VERSION, Database

from .harness import build_harness

ROUTING = {
    "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]},
    "fast_path": False,
}


def msg(text_: str) -> IncomingMessage:
    return IncomingMessage("111", "111", text_)


@pytest.mark.parametrize(
    "answer",
    [
        "La lumière du salon a été réduite à 2 %.",
        "C'est fait !",
        "J'ai éteint la cuisine.",
        "J’ai bien allumé le salon",
        "Les volets ont été fermés.",
        "Le salon est maintenant éteint.",
        'OK.\n[actions effectuées : ha_call_service {"domain": "light"}]',
    ],
)
def test_claims(answer: str) -> None:
    assert claims_action(answer)


@pytest.mark.parametrize(
    "answer",
    [
        "Il fait 21,5 °C dans le salon.",
        "Veux-tu que j'éteigne la cuisine ?",
        "La lumière du salon est allumée.",
        "Je ne peux pas le faire : permission refusée.",
    ],
)
def test_no_claim(answer: str) -> None:
    assert not claims_action(answer)


def test_strip_fake_suffix() -> None:
    assert strip_fake_actions("Fait.\n[actions effectuées : ha_call_service {...}]") == "Fait."
    assert strip_fake_actions("Fait. [actions effectuées : coupé en plein") == "Fait."
    assert strip_fake_actions("rien à voir [x]") == "rien à voir [x]"


def test_replay_shapes() -> None:
    actions = [
        {"name": "ha_call_service", "arguments": {"domain": "light"}, "result": '{"ok": true}'}
    ]
    msgs = replay(7, "assistant", "C'est fait.", actions)
    assert [m.role for m in msgs] == ["assistant", "tool", "assistant"]
    assert msgs[0].tool_calls[0].id == "h7_0" and msgs[0].tool_calls[0].name == "ha_call_service"
    assert msgs[1].tool_call_id == "h7_0" and msgs[1].content == '{"ok": true}'
    # a tier without that tool (e.g. a cloud backend) only gets the text
    assert [m.role for m in replay(7, "assistant", "C'est fait.", actions, {"web_search"})] == [
        "assistant"
    ]
    # rows written by 0.5.1: the text suffix is removed, never shown again to the model
    old = replay(3, "assistant", "Fait.\n[actions effectuées : ha_call_service {}]", None)
    assert [(m.role, m.content) for m in old] == [("assistant", "Fait.")]


async def test_actions_are_replayed_as_real_tool_calls(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, routing=ROUTING)
    r = BufferedReplier()
    await h.orchestrator.handle(msg("éteins la lumière de la cuisine stp, light cuisine"), r)
    assert "C'est fait" in r.messages[-1]
    rows = await h.db.history("111", 10)
    assert rows[-1].role == "assistant" and "[actions" not in rows[-1].content
    assert rows[-1].tool_calls is not None
    assert rows[-1].tool_calls[0]["name"] == "ha_call_service"
    assert rows[-1].tool_calls[0]["arguments"]["entity_id"] == "light.cuisine"
    tools = h.orchestrator.registry.specs()
    msgs = await h.orchestrator.build_messages("111", Tier.GPU, "et le salon ?", None, tools)
    assert [m.role for m in msgs] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert msgs[2].tool_calls[0].name == "ha_call_service"
    assert all("actions effectuées" not in m.content for m in msgs)
    await h.close()


async def test_invented_action_is_retried_then_told_honestly(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, routing=ROUTING)
    r = BufferedReplier()
    await h.orchestrator.handle(msg("[[lie]] baisse le salon à 2 %"), r)
    assert h.ha_calls == []
    assert r.messages[-1] == "D'accord, je n'ai rien changé."
    assert all("actions effectuées" not in m for m in r.messages)
    await h.close()


async def test_stubborn_liar_gets_replaced(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, routing=ROUTING)
    r = BufferedReplier()
    await h.orchestrator.handle(msg("[[stubborn]] baisse le salon à 2 %"), r)
    assert h.ha_calls == []
    assert r.messages[-1] == NO_ACTION_REPLY
    rows = await h.db.history("111", 10)
    assert rows[-1].content == NO_ACTION_REPLY and rows[-1].tool_calls is None
    # the nudge itself is never stored
    assert not any("[Système]" in row.content for row in rows)
    await h.close()


async def test_nudge_makes_the_model_call_the_tool(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, routing=ROUTING)
    r = BufferedReplier()
    await h.orchestrator.handle(
        msg(
            '[[lie]] [[retry:ha_call_service {"domain": "light", "service": "turn_on",'
            ' "entity_id": "light.salon", "data": {"brightness_pct": 2}}]]'
        ),
        r,
    )
    assert h.ha_calls == [
        {
            "domain": "light",
            "service": "turn_on",
            "data": {"brightness_pct": 2, "entity_id": ["light.salon"]},
        }
    ]
    assert "C'est fait" in r.messages[-1]
    assert (await h.db.audit_entries())[0].outcome == "ok"
    await h.close()


async def test_migration_adds_the_column(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = Database(str(path))
    async with old.engine.begin() as conn:  # the 0.5.1 schema of chat_messages
        await conn.execute(
            text(
                "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, chat_id VARCHAR(64), "
                "role VARCHAR(16), content TEXT, created_at DATETIME)"
            )
        )
        await conn.execute(text("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO schema_version VALUES (1)"))
        await conn.execute(
            text(
                "INSERT INTO chat_messages (chat_id, role, content, created_at) VALUES "
                "('1', 'assistant', 'ok [actions effectuées : x]', '2026-11-05 10:00:00')"
            )
        )
    await old.close()
    db = Database(str(path))
    await db.init()
    rows = await db.history("1", 5)
    assert rows[0].tool_calls is None and rows[0].content.startswith("ok")
    await db.add_message("1", "assistant", "fait", [{"name": "x", "arguments": {}, "result": ""}])
    assert (await db.history("1", 5))[-1].tool_calls == [
        {"name": "x", "arguments": {}, "result": ""}
    ]
    async with db.engine.connect() as conn:
        version = (await conn.execute(text("SELECT max(version) FROM schema_version"))).scalar()
    assert version == SCHEMA_VERSION == 2
    await db.close()
