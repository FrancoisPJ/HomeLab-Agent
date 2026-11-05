"""Application wiring: builds every service from the config and runs them together."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from homelab_agent.commands import AgentCommands
from homelab_agent.config.loader import ConfigError, ConfigStore
from homelab_agent.config.models import AppConfig
from homelab_agent.core.budget import BudgetManager
from homelab_agent.core.jobs import JobQueue
from homelab_agent.core.memory import MemoryService, register_memory_tools
from homelab_agent.core.orchestrator import Orchestrator
from homelab_agent.core.proactive import AlertEngine, RoutineScheduler
from homelab_agent.core.router import HealthTracker, Router
from homelab_agent.core.routines import RoutineManager, register_routine_tools
from homelab_agent.gateway.home_remote import HomeRemote
from homelab_agent.gateway.telegram import TelegramFrontend
from homelab_agent.llm.factory import build_backends
from homelab_agent.observability.logging import MASKER, setup_logging
from homelab_agent.services import LogNotifier, Services
from homelab_agent.store.db import Database
from homelab_agent.tools.homeassistant.cameras import CameraService, register_camera_tools
from homelab_agent.tools.homeassistant.client import HomeAssistantClient
from homelab_agent.tools.homeassistant.permissions import PermissionEngine
from homelab_agent.tools.homeassistant.tools import HomeAssistantService, register_ha_tools
from homelab_agent.tools.registry import ToolRegistry, escalate_tool
from homelab_agent.voice.stt import SpeechToText
from homelab_agent.voice.tts import TextToSpeech
from homelab_agent.web.chat import RoutingNotifier, WebChatHub

if TYPE_CHECKING:
    from homelab_agent.gateway.aiogram_adapter import TelegramGateway

log = logging.getLogger(__name__)


async def build_services(store: ConfigStore, *, start_background: bool = True) -> Services:
    cfg = store.config
    db = Database(cfg.store.path)
    await db.init()
    backends = build_backends(cfg)
    health = HealthTracker(cfg.routing.unhealthy_cooldown_s)
    budget = BudgetManager(lambda: store.config, db)
    router = Router(lambda: store.config, backends, health, budget)
    registry = ToolRegistry()
    registry.register(escalate_tool())
    ha: HomeAssistantService | None = None
    if cfg.home_assistant.enabled:
        token = AppConfig.secret(cfg.home_assistant.token_env)
        MASKER.add(token)
        if not token:
            log.warning("home assistant token %s is not set", cfg.home_assistant.token_env)
        client = HomeAssistantClient(
            cfg.home_assistant.url,
            token,
            timeout_s=cfg.home_assistant.request_timeout_s,
            verify_ssl=cfg.home_assistant.verify_ssl,
        )
        engine = PermissionEngine(lambda: store.config.home_assistant)
        ha = HomeAssistantService(client, engine, lambda: store.config.home_assistant, db)
        register_ha_tools(registry, ha)
    orch = Orchestrator(lambda: store.config, db, router, registry, budget, ha)
    services = Services(store, db, backends, health, budget, router, registry, orch, ha)
    services.webchat = WebChatHub(lambda: store.config.telegram.confirm_timeout_s)
    services.notifier = RoutingNotifier(LogNotifier(), services.webchat)
    orch.memory = MemoryService(lambda: store.config, db)
    register_memory_tools(registry, orch.memory)
    services.routines = RoutineScheduler(
        lambda: store.config, orch.handle, lambda: services.notifier, ha, db
    )
    services.routine_manager = RoutineManager(store, services.routines)
    manager = services.routine_manager

    async def once_done(name: str) -> None:
        manager.remove(name)  # one-off routine: remove it once it has run

    services.routines.on_once_done = once_done
    register_routine_tools(registry, manager)
    if ha is not None:
        services.alerts = AlertEngine(lambda: store.config, ha, lambda: services.notifier, db)
        services.alerts.attach()
        services.cameras = CameraService(ha, lambda: store.config, lambda: services.notifier)
        register_camera_tools(registry, services.cameras)
    services.commands = AgentCommands(services).table()
    if start_background:
        health.start(backends, cfg.routing.health_interval_s)
        if ha is not None:
            ha.client.start()
    return services


async def start_proactive(services: Services) -> None:
    if services.routines is not None:
        services.routines.start()
    if services.alerts is not None and services.ha is not None:
        alerts = services.alerts

        async def initial_scan() -> None:
            # wait for the first Home Assistant snapshot, then arm already-true conditions
            for _ in range(120):
                if services.ha is not None and services.ha.client.cache.entities:
                    break
                await asyncio.sleep(1)
            alerts.evaluate_all()

        services.background.append(asyncio.create_task(initial_scan(), name="alerts-scan"))


async def start_jobs(services: Services) -> None:
    cfg = services.cfg
    services.jobs = JobQueue(
        services.db,
        services.orchestrator.run_job,
        services.notifier,
        max_concurrent=cfg.jobs.max_concurrent,
        poll_interval_s=cfg.jobs.poll_interval_s,
        max_attempts=cfg.jobs.max_attempts,
    )
    services.orchestrator.jobs = services.jobs
    await services.jobs.start()


HEARTBEAT = Path(os.environ.get("HLA_HEARTBEAT", "/tmp/hla-heartbeat"))  # noqa: S108


def start_maintenance(services: Services) -> list[asyncio.Task[None]]:
    """Heartbeat (container healthcheck), config hot reload, daily database purge."""

    async def heartbeat() -> None:
        while True:
            with contextlib.suppress(OSError):
                HEARTBEAT.write_text(str(time.time()), encoding="utf-8")  # noqa: ASYNC240 (tiny tmpfs write)
            await asyncio.sleep(15)

    async def watch_config() -> None:
        while True:
            await asyncio.sleep(5)
            try:
                if services.store.reload_if_changed():
                    log.info("config.yaml changed on disk: reloaded")
            except ConfigError as exc:
                log.error("config.yaml changed but is invalid, keeping the previous one: %s", exc)

    async def purge() -> None:
        while True:
            days = services.cfg.store.retention_days
            if days > 0:
                try:
                    counts = await services.db.purge(days)
                    if any(counts.values()):
                        log.info("database purge (> %d days): %s", days, counts)
                except Exception:
                    log.exception("database purge failed")
            await asyncio.sleep(24 * 3600)

    return [
        asyncio.create_task(heartbeat(), name="heartbeat"),
        asyncio.create_task(watch_config(), name="config-watch"),
        asyncio.create_task(purge(), name="db-purge"),
    ]


async def close_services(services: Services) -> None:
    if services.stt:
        await services.stt.aclose()
    if services.tts:
        await services.tts.aclose()
    if services.routines:
        await services.routines.stop()
    if services.alerts:
        await services.alerts.stop()
    if services.jobs:
        await services.jobs.stop()
    await services.health.stop()
    if services.ha:
        await services.ha.client.aclose()
    for b in services.backends.values():
        with contextlib.suppress(Exception):
            await b.aclose()
    await services.db.close()


def attach_voice(services: Services) -> None:
    """Speech-to-text, and text-to-speech when spoken replies are wanted."""
    cfg = services.cfg
    if not cfg.voice.enabled:
        return
    key = AppConfig.secret(cfg.voice.api_key_env)
    MASKER.add(key)
    services.stt = SpeechToText(cfg.voice, key)
    if cfg.voice.reply_voice != "never":
        services.tts = TextToSpeech(cfg.voice, key)


def build_telegram(services: Services) -> TelegramGateway:
    """Telegram gateway (long polling) with the shared commands, /maison and voice notes."""
    store, cfg = services.store, services.cfg
    token = AppConfig.secret(cfg.telegram.token_env)
    if not token:
        raise SystemExit(f"Telegram is enabled but {cfg.telegram.token_env} is not set")
    MASKER.add(token)
    from homelab_agent.gateway.aiogram_adapter import TelegramGateway

    gateway = TelegramGateway(token, cfg.telegram.api_base)
    attach_voice(services)
    frontend = TelegramFrontend(
        gateway.api,
        lambda: store.config.telegram,
        services.orchestrator.handle,
        commands=services.commands,
        voice=lambda: store.config.voice,
        stt=services.stt,
        tts=services.tts,
    )
    gateway.attach(frontend)
    if services.ha is not None:
        HomeRemote(frontend, services.ha, lambda: store.config.telegram).attach()
    services.notifier = RoutingNotifier(frontend, services.webchat)
    if not cfg.telegram.allowed_user_ids:
        log.warning("telegram.allowed_user_ids is empty: every message will be ignored")
    return gateway


def build_web_server(services: Services) -> uvicorn.Server:
    from homelab_agent.web.app import create_app, require_panel_secret

    cfg = services.cfg
    require_panel_secret(cfg)
    bind = os.environ.get("HLA_WEB_BIND", cfg.web.bind)
    config = uvicorn.Config(
        create_app(services), host=bind, port=cfg.web.port, log_level="warning", lifespan="off"
    )
    return uvicorn.Server(config)


async def wait_for_stop_signal() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()


async def run(config_path: str) -> None:
    store = ConfigStore(config_path)
    cfg = store.config
    setup_logging(cfg.agent.log_level, cfg.agent.log_format)
    services = await build_services(store)
    gateway = build_telegram(services) if cfg.telegram.enabled else None

    await start_jobs(services)
    await start_proactive(services)
    maintenance = start_maintenance(services)
    if gateway is not None:
        await gateway.start()
    server = build_web_server(services) if cfg.web.enabled else None
    web = asyncio.create_task(server.serve(), name="web") if server is not None else None

    services.started = True
    log.info("homelab-agent started")
    await wait_for_stop_signal()
    log.info("shutting down")
    if server is not None:
        server.should_exit = True
    if gateway is not None:
        await gateway.stop()
    if web is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(web, timeout=5)
    for task in [*maintenance, *services.background]:
        task.cancel()
    await close_services(services)
