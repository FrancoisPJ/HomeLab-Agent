from __future__ import annotations

import json
from pathlib import Path

from homelab_agent.gateway.base import BufferedReplier, IncomingMessage
from homelab_agent.tools.homeassistant.undo import UndoRecord, UndoStore, restore_service
from homelab_agent.tools.registry import ToolContext

from .harness import build_harness


def test_restore_service() -> None:
    assert restore_service("light", "on") == "turn_on"
    assert restore_service("switch", "off") == "turn_off"
    assert restore_service("cover", "closed") == "close_cover"
    assert restore_service("lock", "locked") == "lock"
    assert restore_service("light", "unavailable") is None
    assert restore_service("scene", "scening") is None


def test_undo_store_ttl_and_size() -> None:
    now = [0.0]
    store = UndoStore(clock=lambda: now[0])
    for i in range(12):
        store.push("c", UndoRecord(f"a{i}", {"light.x": "on"}, now[0]))
    last = store.pop("c")
    assert last is not None
    assert last.description == "a11"
    now[0] = 4000
    expired = store.pop("c")
    assert expired is None  # older than one hour


async def test_undo_restores_previous_states(tmp_path: Path) -> None:
    h = await build_harness(tmp_path)
    ctx = ToolContext(requester="111", chat_id="111")
    await h.ha.act("light", "turn_off", ["light.salon"], {}, ctx)  # on -> off
    assert h.ha.client.cache.entities["light.salon"].state == "off"
    res = await h.ha.undo_last(ctx)
    assert res.ok and res.data["restored"] == ["light.salon → on"]
    assert h.ha_calls[-1]["service"] == "turn_on"
    assert not (await h.ha.undo_last(ctx)).ok  # an undo is not itself undoable
    other = ToolContext(requester="222", chat_id="222")
    assert not (await h.ha.undo_last(other)).ok  # per conversation
    await h.close()


async def test_undo_goes_through_permissions(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path, home_assistant={"url": "http://ha", "confirm": [{"domain": "cover"}]}
    )
    yes = ToolContext(requester="111", chat_id="111", confirm=_yes)
    await h.ha.act("cover", "close_cover", ["cover.volet_chambre"], {}, yes)
    no = ToolContext(requester="111", chat_id="111", confirm=_no)
    res = await h.ha.undo_last(no)
    assert not res.ok and "declined" in (res.error or "")
    assert h.ha_calls[-1]["service"] == "close_cover"
    await h.close()


async def _yes(_: str) -> bool:
    return True


async def _no(_: str) -> bool:
    return False


async def test_fast_path_offers_an_undo_button(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path,
        routing={
            "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]},
            "fast_path": {"aliases": {"salon": "light.salon"}},
        },
    )
    r = BufferedReplier()
    await h.orchestrator.handle(IncomingMessage("111", "111", "éteins le salon"), r)
    assert r.messages[-1].startswith("✅ light.turn_off") and r.actions[0].label == "↩️ Annuler"
    assert await r.actions[0].run("111", BufferedReplier()) == "↩️ Annulé."
    assert h.ha.client.cache.entities["light.salon"].state == "on"
    await h.close()


async def test_llm_can_undo(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path,
        routing={
            "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]},
            "fast_path": False,
        },
    )
    r = BufferedReplier()
    call = {"domain": "switch", "service": "turn_off", "entity_id": "switch.prise_bureau"}
    await h.orchestrator.handle(
        IncomingMessage("111", "111", f"[[tool:ha_call_service {json.dumps(call)}]]"), r
    )
    await h.orchestrator.handle(IncomingMessage("111", "111", "[[tool:ha_undo]]"), r)
    assert h.ha_calls[-1]["service"] == "turn_on"
    tools = [a.tool for a in await h.db.audit_entries()]
    assert tools[:2] == ["ha_undo", "undo"]  # the tool call and the restoring action
    await h.close()
