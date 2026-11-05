"""Application wiring: every service built from the dev config, without any network."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from homelab_agent import app as app_module
from homelab_agent.app import (
    Services,
    attach_voice,
    build_services,
    build_telegram,
    build_web_server,
    close_services,
    start_maintenance,
)
from homelab_agent.config.loader import ConfigStore
from homelab_agent.web.chat import RoutingNotifier

from .conftest import ROOT


@pytest.fixture
async def services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Services:
    monkeypatch.setenv("HA_TOKEN", "fake-ha-token")
    text = (ROOT / "deploy" / "config.dev.yaml").read_text(encoding="utf-8")
    db = tmp_path / "agent.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text.replace("store: { path: /data/agent.db }", f"store: {{ path: {db} }}"))
    svc = await build_services(ConfigStore(cfg), start_background=False)
    yield svc
    await close_services(svc)


async def test_build_services_wires_everything(services: Services) -> None:
    names = {spec.name for spec in services.registry.specs()}
    assert {"ha_call_service", "memory_remember", "routine_create", "escalate"} <= names
    assert services.ha is not None and services.alerts is not None
    assert services.routines is not None and services.routine_manager is not None
    assert isinstance(services.notifier, RoutingNotifier)
    help_text = services.commands.help_text()
    assert "/status" in help_text and "/routines" in help_text and "/maison" not in help_text


async def test_metrics_samples(services: Services) -> None:
    samples = await services.metrics_samples()
    by_name: dict[str, list[tuple[dict[str, str], float]]] = {}
    for name, labels, value in samples:
        by_name.setdefault(name, []).append((labels, value))
    assert by_name["build_info"][0][1] == 1
    backends = {labels["backend"]: value for labels, value in by_name["backend_up"]}
    assert backends == {"gpu_fast": 1.0, "cpu_heavy": 1.0, "cloud_fake": 1.0}
    services.health.mark_down("gpu_fast")
    samples = await services.metrics_samples()
    assert ("backend_up", {"backend": "gpu_fast", "tier": "gpu"}, 0.0) in samples
    assert {labels["status"] for labels, _ in by_name["jobs"]} == {
        "queued",
        "running",
        "done",
        "failed",
    }
    assert ("cloud_limit_eur", {"period": "day"}, 0.05) in samples
    assert ("home_assistant_connected", {}, 0.0) in samples  # not started in this test


async def test_voice_and_gateways(services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    attach_voice(services)
    assert services.stt is not None and services.tts is not None  # reply_voice: when_voice

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN"):
        build_telegram(services)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake-dev-token")
    gateway = build_telegram(services)
    try:
        assert gateway.dp.message.handlers  # the frontend is attached to the dispatcher
        assert isinstance(services.notifier, RoutingNotifier)
        assert "/maison" not in services.commands.help_text()  # Telegram keeps its own copy
    finally:
        await gateway.bot.session.close()

    monkeypatch.delenv("PANEL_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        build_web_server(services)
    monkeypatch.setenv("PANEL_PASSWORD", "correct-horse-battery")
    monkeypatch.setenv("HLA_WEB_BIND", "127.0.0.2")
    server = build_web_server(services)
    assert (server.config.host, server.config.port) == ("127.0.0.2", 8099)


async def test_maintenance_tasks(
    services: Services, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    beat = tmp_path / "heartbeat"
    monkeypatch.setattr(app_module, "HEARTBEAT", beat)
    tasks = start_maintenance(services)
    try:
        for _ in range(50):
            if beat.exists():
                break
            await asyncio.sleep(0.02)
        assert beat.exists()
        assert {t.get_name() for t in tasks} == {"heartbeat", "config-watch", "db-purge"}
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_camera_command_is_shared_but_photos_need_telegram(services: Services) -> None:
    from homelab_agent.gateway.base import BufferedReplier, IncomingMessage, Replier
    from homelab_agent.web.chat import WEB_CHAT, WEB_USER

    assert "/camera" in services.commands.help_text()  # cameras are on in the dev config
    replier = BufferedReplier()

    async def agent(msg: IncomingMessage, r: Replier) -> None:
        raise AssertionError("a command must not reach the agent")

    assert await services.commands.handle("/camera salon", WEB_CHAT, WEB_USER, replier, agent)
    assert replier.messages == ["📷 Les photos des caméras ne s'affichent que sur Telegram."]
