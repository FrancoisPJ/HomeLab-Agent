"""Regression tests for the findings of the second security audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab_agent.config.loader import expand_env
from homelab_agent.tools.registry import ToolContext

from .harness import build_harness


def test_escaped_env_reference_stays_literal() -> None:
    env = {"SECRET": "s3cr3t"}
    assert expand_env("a ${SECRET} b", env) == "a s3cr3t b"
    assert expand_env("a $${SECRET} b", env) == "a ${SECRET} b"


async def test_chat_cannot_store_env_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANEL_PASSWORD", "super-secret-panel")
    h = await build_harness(tmp_path)
    for args in (
        {"what": "reminder", "at": "08:00", "text": "code: ${PANEL_PASSWORD}"},
        {"what": "ask", "at": "08:00", "text": "dis ${HA_TOKEN:-x}"},
        {"what": "reminder", "at": "08:00", "text": "x", "name": "${A}"},
    ):
        with pytest.raises(ValueError, match="ne peut pas contenir"):
            h.routines.build(args, chat_id="111", requester="111")
    assert h.routines.routines == []
    await h.close()


async def test_admin_services_are_blocked(tmp_path: Path) -> None:
    h = await build_harness(tmp_path)  # default_policy: allow
    ctx = ToolContext(requester="111", chat_id="111")
    for domain, service in (
        ("hassio", "host_shutdown"),
        ("homeassistant", "restart"),
        ("recorder", "purge"),
        ("shell_command", "anything"),
    ):
        res = await h.ha.act(domain, service, [], {}, ctx)
        assert res.outcome == "denied"
        assert "blocked service" in (res.error or "")
    assert h.ha_calls == []
    await h.close()


async def test_blocked_service_can_be_explicitly_allowed(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path,
        home_assistant={
            "url": "http://ha",
            "allowed_services": ["backup.create"],
            "untargeted_domains": ["backup", "notify"],
        },
    )
    ctx = ToolContext(requester="111", chat_id="111")
    assert (await h.ha.act("backup", "create", [], {}, ctx)).ok
    assert (await h.ha.act("backup", "restore", [], {}, ctx)).outcome == "denied"
    await h.close()


async def test_untargeted_entity_services_are_refused(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path,
        home_assistant={"url": "http://ha", "rules": [{"entity": "light.salon", "mode": "deny"}]},
    )
    ctx = ToolContext(requester="111", chat_id="111")
    res = await h.ha.act("light", "turn_off", [], {}, ctx)
    assert not res.ok
    assert "explicit entity_id" in (res.error or "")
    assert not (await h.ha.act("homeassistant", "turn_off", [], {}, ctx)).ok
    assert (await h.ha.act("notify", "notify", [], {"message": "hi"}, ctx)).ok
    assert h.ha_calls == [{"domain": "notify", "service": "notify", "data": {"message": "hi"}}]
    await h.close()


async def test_alert_cooldown_survives_a_restart(tmp_path: Path) -> None:
    from homelab_agent.core.proactive import AlertEngine

    alerts = [{"name": "porte", "entity": "cover.*", "state": "open", "cooldown_s": 3600}]
    h = await build_harness(tmp_path, proactive={"enabled": True, "alerts": alerts})
    first = AlertEngine(lambda: h.cfg_store.config, h.ha, lambda: h.notifier, h.db)
    first.evaluate_all()  # cover.volet_chambre is already open
    await first.idle()
    assert len(h.notifier.sent) == 1
    # "restart": a brand new engine, same database
    second = AlertEngine(lambda: h.cfg_store.config, h.ha, lambda: h.notifier, h.db)
    second.evaluate_all()
    await second.idle()
    assert len(h.notifier.sent) == 1
    assert second.events[-1].outcome == "cooldown"
    await h.close()


async def test_missed_one_off_routines(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    h = await build_harness(tmp_path)
    created = datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
    h.routines.clock = lambda: created
    for text, at in (("Pilule", "08:00"), ("Vieux rappel", "07:15")):
        h.routines.add(
            h.routines.build(
                {"what": "reminder", "at": at, "date": "2026-11-05", "text": text},
                chat_id="111",
                requester="111",
            )
        )
    # the agent comes back at 09:30 Paris (08:30 UTC): 08:00 is 1h30 late, 07:15 is 2h15 late
    h.scheduler.clock = lambda: datetime(2026, 11, 5, 8, 30, tzinfo=UTC)
    handled = await h.scheduler.catch_up()
    assert sorted(handled) == ["pilule", "vieux_rappel"]
    texts = [t for _, t in h.notifier.sent]
    assert "⏰ Pilule (en retard : l'agent était arrêté)" in texts
    assert any(t.startswith("⚠️ Routine manquée (05/11 07:15") for t in texts)
    assert h.routines.routines == []  # both one-offs removed
    await h.close()
