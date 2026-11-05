"""Regression tests for the remaining (non critical) findings of the second audit."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from homelab_agent.config.loader import ConfigError, ConfigStore
from homelab_agent.config.models import OpenAICompatBackend, TelegramSection
from homelab_agent.fakes.ha import create_app as fake_ha
from homelab_agent.gateway.base import BufferedReplier, IncomingMessage
from homelab_agent.gateway.telegram import TelegramFrontend
from homelab_agent.llm.base import Message
from homelab_agent.llm.openai_compat import OpenAICompatClient
from homelab_agent.observability.logging import MASKER, MaskingTextFormatter
from homelab_agent.store.db import Database
from homelab_agent.tools.homeassistant.client import HomeAssistantClient

from .conftest import ROOT, wait_for
from .harness import build_harness
from .test_telegram import TABLE, FakeBot


async def test_only_the_requester_can_confirm_in_a_group() -> None:
    bot = FakeBot()
    section = TelegramSection(allowed_user_ids=[111, 222], confirm_timeout_s=5)
    f = TelegramFrontend(
        bot, lambda: section, None,  # type: ignore[arg-type]
        commands=TABLE,
    )  # fmt: skip
    task = asyncio.create_task(f.ask_confirmation("-100", "Déverrouiller ?", "111"))
    await wait_for(lambda: bool(bot.ops))
    yes = bot.ops[0][1][2][0][1]
    await f.on_callback(222, -100, "cb1", yes)  # another allowed member of the group
    assert not task.done()
    assert ("answer", ("cb1", "Seul l'auteur de la demande peut répondre.")) in bot.ops
    await f.on_callback(111, -100, "cb2", yes)
    assert await task is True


def test_login_is_rate_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from homelab_agent.web.app import create_app

    from .test_web import PASSWORD, services

    monkeypatch.setenv("PANEL_PASSWORD", PASSWORD)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    svc = asyncio.new_event_loop().run_until_complete(services(tmp_path))
    with TestClient(create_app(svc)) as c:
        for _ in range(5):
            assert c.post("/login", data={"password": "nope"}).status_code == 200
        blocked = c.post("/login", data={"password": PASSWORD})  # even the right one
        assert blocked.status_code == 429


async def _no_sleep(*_: Any) -> None:
    return None


def test_text_log_tracebacks_are_masked() -> None:
    MASKER.add("very-secret-token-123")
    fmt = MaskingTextFormatter("%(message)s")
    try:
        raise RuntimeError("auth failed with very-secret-token-123")
    except RuntimeError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, "f", 1, "boom", None, sys.exc_info())
    out = fmt.format(record)
    assert "very-secret-token-123" not in out and "***" in out


async def test_backend_concurrency_is_enforced() -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        body = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        return httpx.Response(200, json=body)

    cfg = OpenAICompatBackend(tier="cpu", url="http://x/v1", model="m", concurrency=2)
    client = OpenAICompatClient("b", cfg, transport=httpx.MockTransport(handler))
    await asyncio.gather(*(client.chat([Message("user", "x")]) for _ in range(6)))
    assert peak == 2


async def test_registry_change_triggers_a_refresh(run_server: Any) -> None:
    app = fake_ha()
    srv = run_server(app)
    c = HomeAssistantClient(srv.url, "tok")
    c.start()
    await wait_for(lambda: c.connected, timeout=10)
    first = c.cache.updated_at
    # rename an area in the fake, then announce it like Home Assistant does
    from homelab_agent.fakes import ha as fake_module

    old = list(fake_module.AREAS)
    fake_module.AREAS[0] = {"area_id": "salon", "name": "Séjour"}
    try:
        app.state.ha.emit("area_registry_updated", {"action": "update", "area_id": "salon"})
        await wait_for(lambda: c.cache.entities["light.salon"].area_name == "Séjour", timeout=10)
        assert c.cache.updated_at != first
    finally:
        fake_module.AREAS[:] = old
        await c.aclose()


async def test_history_remembers_actions(tmp_path: Path) -> None:
    h = await build_harness(
        tmp_path,
        routing={
            "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]},
            "fast_path": False,
        },
    )
    r = BufferedReplier()
    await h.orchestrator.handle(IncomingMessage("111", "111", "peux-tu éteindre la cuisine"), r)
    history = await h.db.history("111", 5)
    assert history[-1].role == "assistant"
    # stored as structured actions (replayed as real tool calls), never as text
    assert "actions effectuées" not in history[-1].content
    assert history[-1].tool_calls is not None
    assert history[-1].tool_calls[0]["arguments"]["entity_id"] == "light.cuisine"
    assert "actions effectuées" not in r.messages[-1]  # not shown to the user
    await h.close()


async def test_purge_respects_retention(db: Database) -> None:
    now = datetime(2026, 11, 5, tzinfo=UTC)
    old = (now - timedelta(days=200)).replace(tzinfo=None)
    recent = (now - timedelta(days=10)).replace(tzinfo=None)
    await db.add_message("c", "user", "old")
    await db.add_message("c", "user", "recent")
    await db.add_spend("p", 1, 1, 1.0, at=old)
    async with db.session() as s, s.begin():
        from sqlalchemy import update

        from homelab_agent.store.db import ChatMessage

        await s.execute(
            update(ChatMessage).where(ChatMessage.content == "old").values(created_at=old)
        )
        await s.execute(
            update(ChatMessage).where(ChatMessage.content == "recent").values(created_at=recent)
        )
    counts = await db.purge(180, now=now)
    assert counts["chat_messages"] == 1
    assert counts["spend"] == 0  # spend kept >= 400 days for budgets
    assert [m.content for m in await db.history("c", 10)] == ["recent"]


async def test_config_hand_edits_are_reloaded(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("routing:\n  tiers: {}\nagent:\n  history_messages: 5\n", encoding="utf-8")
    store = ConfigStore(p)
    assert store.reload_if_changed() is False
    p.write_text("routing:\n  tiers: {}\nagent:\n  history_messages: 9\n", encoding="utf-8")
    os.utime(p, (time.time() + 1, time.time() + 1))
    assert store.reload_if_changed() is True
    assert store.config.agent.history_messages == 9
    p.write_text("agent:\n  history_messages: -1\n", encoding="utf-8")
    os.utime(p, (time.time() + 2, time.time() + 2))
    with pytest.raises(ConfigError):
        store.reload_if_changed()
    assert store.config.agent.history_messages == 9  # invalid edit: previous config kept
    assert store.reload_if_changed() is False  # not retried until the file changes again


def test_healthcheck_without_web_uses_the_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import homelab_agent.app as app_module
    from homelab_agent.cli import healthcheck
    from homelab_agent.config.loader import load_config

    cfg = load_config(ROOT / "deploy" / "config.dev.yaml")
    cfg = cfg.model_copy(update={"web": cfg.web.model_copy(update={"enabled": False})})
    beat = tmp_path / "beat"
    monkeypatch.setattr(app_module, "HEARTBEAT", beat)
    assert healthcheck(cfg) == 1  # no heartbeat yet
    beat.write_text("x", encoding="utf-8")
    assert healthcheck(cfg) == 0
    os.utime(beat, (time.time() - 300, time.time() - 300))
    assert healthcheck(cfg) == 1  # stale


def test_fast_path_ignores_ambiguous_or_odd_names() -> None:
    from homelab_agent.config.models import FastPathSection
    from homelab_agent.core.fastpath import match
    from homelab_agent.tools.homeassistant.client import EntityCache

    cache = EntityCache()
    cache.load(
        [
            {"entity_id": "light.salon", "state": "on", "attributes": {"friendly_name": "Salon"}},
            {"entity_id": "light.copie", "state": "on", "attributes": {"friendly_name": "salon"}},
            {"entity_id": "switch.tv", "state": "on", "attributes": {"friendly_name": "Télé"}},
            {"entity_id": "switch.x", "state": "on",
             "attributes": {"friendly_name": "nom beaucoup trop long pour etre un vrai nom de piece"}},
        ]
    )  # fmt: skip
    assert match("éteins le salon", FastPathSection(), cache) is None  # two "salon": ambiguous
    m = match("éteins le salon", FastPathSection(aliases={"salon": "light.salon"}), cache)
    assert m is not None and m.entity_ids == ["light.salon"]  # alias wins
    assert match("éteins la télé", FastPathSection(), cache) is not None
    assert match("éteins la télé", FastPathSection(match_names=False), cache) is None
    long_text = "allume nom beaucoup trop long pour etre un vrai nom de piece"
    assert match(long_text, FastPathSection(), cache) is None


def test_routine_times_across_dst_changes() -> None:
    from zoneinfo import ZoneInfo

    from homelab_agent.core.proactive import local_at

    tz = ZoneInfo("Europe/Paris")
    from datetime import date, time

    # 2026-03-29: 02:00 -> 03:00 (02:30 does not exist): moved to 03:30 local = 01:30 UTC
    spring = local_at(date(2026, 3, 29), time(2, 30), tz)
    assert spring.astimezone(UTC) == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert (spring.hour, spring.minute) == (3, 30)
    # 2026-10-25: 03:00 -> 02:00 (02:30 happens twice): the first one, 00:30 UTC
    autumn = local_at(date(2026, 10, 25), time(2, 30), tz)
    assert autumn.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


async def test_routine_runs_once_on_the_autumn_change(tmp_path: Path) -> None:
    routines = [{"name": "nuit", "at": "02:30", "message": "ping"}]
    h = await build_harness(tmp_path, proactive={"enabled": True, "routines": routines})
    r = h.cfg_store.config.proactive.routines[0]
    first = h.scheduler.next_run(r, datetime(2026, 10, 24, 23, 0, tzinfo=UTC))
    assert first == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    after = h.scheduler.next_run(r, first)
    assert after == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)  # next day, not 01:30 UTC same night
    await h.close()
