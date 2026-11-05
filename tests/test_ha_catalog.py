from __future__ import annotations

from pathlib import Path

from homelab_agent.config.models import Tier
from homelab_agent.fakes.ha import SERVICES
from homelab_agent.gateway.base import BufferedReplier, IncomingMessage
from homelab_agent.tools.homeassistant.catalog import ServiceCatalog
from homelab_agent.tools.registry import ToolContext

from .harness import build_harness

ROUTING = {
    "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]},
    "fast_path": False,
}


def catalog() -> ServiceCatalog:
    c = ServiceCatalog()
    c.load([{"domain": d, "services": s} for d, s in SERVICES.items()])
    return c


def test_load_and_describe() -> None:
    c = catalog()
    assert c.loaded
    # most useful fields first, sections (advanced_fields) flattened
    assert c.describe("light").startswith("turn_on (brightness_pct, color_name, color_temp_kelvin")
    assert "turn_off (transition, flash)" in c.describe("light")
    info = c.get("cover", "set_cover_position")
    assert info is not None and info.required == {"position"} and info.fields[0] == "position"
    # the WebSocket shape (dict) works too, invalid names are skipped
    ws = ServiceCatalog()
    ws.load({"switch": {"turn_on": {"fields": {}}, "bad name": {}}, "Bad-Domain": {}})
    assert list(ws.domains) == ["switch"] and list(ws.domains["switch"]) == ["turn_on"]


def test_check_gives_the_fix() -> None:
    c = catalog()
    err = c.check("light", "dim", {}, fields=True)
    assert err is not None
    assert err.startswith("light.dim n'existe pas")
    assert "turn_on (brightness_pct, color_name" in err and "turn_off" in err and "toggle" in err
    err = c.check("light", "turn_off", {"brightness_pct": 30}, fields=True)
    assert err is not None and "n'accepte pas brightness_pct" in err and "turn_on (" in err
    assert c.check("light", "turn_off", {"brightness_pct": 30}, fields=False) is None
    assert (
        c.check("light", "turn_on", {"brightness_pct": 30, "color_name": "red"}, fields=True)
        is None
    )
    err = c.check("lampe", "turn_on", {}, fields=True)
    assert err is not None and "domaine 'lampe' n'existe pas" in err and "light" in err
    # services without declared fields accept any data (custom integrations)
    assert c.check("switch", "turn_on", {"whatever": 1}, fields=True) is None
    # not loaded yet: no opinion, Home Assistant decides
    assert ServiceCatalog().check("light", "dim", {}, fields=True) is None


def test_summary_respects_the_budget() -> None:
    c = catalog()
    text = c.summary(["light", "cover", "unknown"], 1000)
    assert text.splitlines()[0].startswith("light: turn_on (brightness_pct")
    assert "cover: open_cover" in text
    assert len(c.summary(["light", "cover"], 60)) <= 60


async def test_invented_service_is_refused_before_home_assistant(tmp_path: Path) -> None:
    h = await build_harness(tmp_path)
    await h.ha.client.refresh_services()
    ctx = ToolContext(requester="1", chat_id="1")
    res = await h.ha.act("light", "dim", ["light.salon"], {"brightness_pct": 30}, ctx)
    assert not res.ok and "light.dim n'existe pas" in (res.error or "")
    assert "turn_on (brightness_pct" in (res.error or "")
    res = await h.ha.act("light", "turn_off", ["light.salon"], {"brightness": 10}, ctx)
    assert not res.ok and "n'accepte pas brightness" in (res.error or "")
    assert h.ha_calls == [] and h.ha_app.state.ha.rejected == []
    res = await h.ha.act("light", "turn_on", ["light.salon"], {"brightness_pct": 30}, ctx)
    assert res.ok
    await h.close()


async def test_home_assistant_4xx_comes_with_the_hint(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path, home_assistant={"url": "http://ha", "service_validation": "services"}
    )
    await h.ha.client.refresh_services()
    ctx = ToolContext(requester="1", chat_id="1")
    res = await h.ha.act("light", "turn_off", ["light.salon"], {"brightness": 10}, ctx)
    assert not res.ok
    assert "HTTP 400: extra keys not allowed" in (res.error or "")
    assert "services possibles pour light : turn_on (" in (res.error or "")
    assert h.ha_app.state.ha.rejected[0]["service"] == "turn_off"
    # validation off: Home Assistant answers, with the hint
    h.cfg_store.config.home_assistant.service_validation = "off"
    res = await h.ha.act("light", "dim", ["light.salon"], {}, ctx)
    assert "Service light.dim not found" in (res.error or "") and "turn_on (" in (res.error or "")
    await h.close()


async def test_services_are_shown_to_the_house_model(tmp_path: Path) -> None:
    h = await build_harness(tmp_path)
    msgs = await h.orchestrator.build_messages("111", Tier.GPU, "x")
    assert "Services Home Assistant" not in msgs[0].content  # not loaded yet
    await h.ha.client.refresh_services()
    msgs = await h.orchestrator.build_messages("111", Tier.GPU, "x")
    assert "Services Home Assistant" in msgs[0].content
    assert "light: turn_on (brightness_pct, color_name" in msgs[0].content
    assert "light.salon" in msgs[0].content  # entities still there
    h.cfg_store.config.home_assistant.services_max_chars = 0
    msgs = await h.orchestrator.build_messages("111", Tier.GPU, "x")
    assert "Services Home Assistant" not in msgs[0].content
    await h.close()


async def test_model_retries_once_after_a_tool_error(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, routing=ROUTING)
    await h.ha.client.refresh_services()
    text = (
        '[[tool:ha_call_service {"domain": "light", "service": "dim", "entity_id": "light.salon",'
        ' "data": {"brightness_pct": 30}}]] [[retry:ha_call_service {"domain": "light",'
        ' "service": "turn_on", "entity_id": "light.salon", "data": {"brightness_pct": 30}}]]'
    )
    r = BufferedReplier()
    await h.orchestrator.handle(IncomingMessage("111", "111", text), r)
    assert h.ha_calls == [
        {
            "domain": "light",
            "service": "turn_on",
            "data": {"brightness_pct": 30, "entity_id": ["light.salon"]},
        }
    ]
    assert "C'est fait" in r.messages[-1]
    outcomes = [(a.tool, a.outcome) for a in reversed(await h.db.audit_entries())]
    assert outcomes == [("ha_call_service", "error"), ("ha_call_service", "ok")]
    # the nudge is not stored in the conversation
    history = await h.db.history("111", 10)
    assert not any("[Système]" in m.content for m in history)
    await h.close()


async def test_retry_nudge_is_sent_only_once(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, routing=ROUTING)
    await h.ha.client.refresh_services()
    text = (
        '[[tool:ha_call_service {"domain": "light", "service": "dim", "entity_id": "light.salon"}]]'
    )
    r = BufferedReplier()
    await h.orchestrator.handle(IncomingMessage("111", "111", text), r)
    assert r.messages[-1].startswith("Impossible : light.dim n'existe pas")
    assert h.ha_calls == []
    await h.close()
