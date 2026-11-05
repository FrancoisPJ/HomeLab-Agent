"""Application wiring: builds every service from the config and runs them together."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn

from homelab_agent.config.loader import ConfigError, ConfigStore
from homelab_agent.config.models import AppConfig, Tier
from homelab_agent.core.budget import BudgetManager
from homelab_agent.core.jobs import JobQueue
from homelab_agent.core.orchestrator import Orchestrator
from homelab_agent.core.proactive import AlertEngine, RoutineScheduler
from homelab_agent.core.router import TIER_LABELS, HealthTracker, Router
from homelab_agent.core.routines import RoutineManager, describe, register_routine_tools
from homelab_agent.gateway.base import ActionButton, BufferedReplier, Notifier, Replier
from homelab_agent.gateway.home_remote import HomeRemote
from homelab_agent.gateway.telegram import TelegramFrontend
from homelab_agent.llm.base import LLMBackend
from homelab_agent.llm.factory import build_backends
from homelab_agent.observability.logging import MASKER, setup_logging
from homelab_agent.store.db import Database
from homelab_agent.tools.homeassistant.client import HomeAssistantClient
from homelab_agent.tools.homeassistant.permissions import PermissionEngine
from homelab_agent.tools.homeassistant.tools import HomeAssistantService, register_ha_tools
from homelab_agent.tools.registry import ToolRegistry, escalate_tool
from homelab_agent.voice.stt import SpeechToText

log = logging.getLogger(__name__)


class LogNotifier:
    """Fallback notifier when no gateway is running."""

    async def notify(self, chat_id: str, text: str) -> None:
        log.info("notification for %s: %s", chat_id, text)

    async def notify_actions(
        self, chat_id: str, text: str, actions: list[ActionButton], ttl_s: float
    ) -> None:
        log.info("notification for %s: %s", chat_id, text)

    def replier_for(self, chat_id: str) -> Replier:
        return BufferedReplier()


@dataclass
class Services:
    store: ConfigStore
    db: Database
    backends: dict[str, LLMBackend]
    health: HealthTracker
    budget: BudgetManager
    router: Router
    registry: ToolRegistry
    orchestrator: Orchestrator
    ha: HomeAssistantService | None
    jobs: JobQueue | None = None
    notifier: Notifier = field(default_factory=LogNotifier)
    alerts: AlertEngine | None = None
    routines: RoutineScheduler | None = None
    routine_manager: RoutineManager | None = None
    stt: SpeechToText | None = None
    started: bool = False

    @property
    def cfg(self) -> AppConfig:
        return self.store.config

    # ---------------------------------------------------------- text views
    async def status_text(self) -> str:
        lines = ["État des moteurs :"]
        for tier in Tier:
            for name in self.cfg.backends_for(tier):
                b = self.backends.get(name)
                up = self.health.is_up(name) and self.health.last_probe.get(name, True)
                icon = "🟢" if up else "🔴"
                lines.append(f"{icon} {TIER_LABELS[tier]} · {name} ({b.model if b else '?'})")
        if self.ha is not None:
            ha_ok = self.ha.client.connected or bool(self.ha.client.cache.entities)
            lines.append(f"{'🟢' if ha_ok else '🔴'} Home Assistant")
        day, month = await self.budget.totals()
        lim = self.cfg.cloud.limits
        state = "activé" if self.cfg.cloud.enabled else "désactivé"
        lines.append(
            f"\nCloud {state} · aujourd'hui {day:.2f} € / {lim.daily_eur:.2f} € · "
            f"mois {month:.2f} € / {lim.monthly_eur:.2f} €"
        )
        if self.stt is not None and self.cfg.voice.enabled:
            ok = await self.stt.health()
            lines.append(f"{'🟢' if ok else '🔴'} Voix (transcription)")
        if self.jobs is not None:
            active = await self.db.list_jobs(active_only=True)
            lines.append(f"Tâches CPU actives : {len(active)}")
        return "\n".join(lines)

    async def model_text(self) -> str:
        lines = ["Moteurs par niveau :"]
        for tier in Tier:
            names = self.cfg.backends_for(tier)
            desc = ", ".join(f"{n} → {self.backends[n].model}" for n in names if n in self.backends)
            lines.append(f"• {TIER_LABELS[tier]} : {desc or '—'}")
        return "\n".join(lines)

    async def jobs_text(self, chat_id: str) -> str:
        jobs = [j for j in await self.db.list_jobs(limit=20) if j.chat_id == chat_id][:10]
        if not jobs:
            return "Aucune tâche."
        icons = {"queued": "⏳", "running": "⚙️", "done": "✅", "failed": "❌"}
        return "\n".join(
            f"{icons.get(j.status, '?')} #{j.id} {j.status} — {j.prompt[:60]}" for j in jobs
        )

    async def reset_chat(self, chat_id: str) -> str:
        await self.db.clear_history(chat_id)
        return "🧹 Contexte effacé."

    # ------------------------------------------------------------ proactive
    async def routines_text(self, chat_id: str, arg: str) -> str:
        if self.routine_manager is None:
            return "Les routines sont indisponibles."
        routines = self.routine_manager.routines
        if not routines:
            return (
                "Aucune routine. Écris par exemple : « rappelle-moi de sortir les poubelles "
                "tous les mardis à 20h » ou « tous les soirs à 23h, vérifie ce qui est allumé »."
            )
        lines = ["Routines :"]
        if not self.cfg.proactive.routines_active:
            lines.insert(
                0, "⚠️ Routines désactivées (proactive.routines_enabled) : rien ne s'exécute."
            )
        for r in routines:
            nxt = self.routine_manager.next_label(r)
            lines.append(f"• {describe(r)} — prochaine : {nxt}")
        lines.append("/run <nom> pour lancer, /routine on|off|delete <nom> pour gérer.")
        return "\n".join(lines)

    async def run_routine(self, chat_id: str, arg: str) -> str:
        if self.routines is None:
            return "Les routines sont désactivées."
        routine = self.routines.get(arg.strip())
        if routine is None:
            return "Usage : /run <nom>. Voir /routines."
        sent = await self.routines.run(routine, [chat_id])
        if routine.check is not None and not sent:
            return f"✅ {routine.name} : rien à signaler."
        return f"▶️ {routine.name} lancée." if routine.prompt else "✔️"

    async def routine_cmd(self, chat_id: str, arg: str) -> str:
        if self.routine_manager is None:
            return "Les routines sont indisponibles."
        return await self.routine_manager.command(chat_id, arg)

    async def alerts_text(self, chat_id: str, arg: str) -> str:
        if self.alerts is None or not self.cfg.proactive.alerts_active:
            return "Les alertes sont désactivées (proactive.alerts_enabled / enabled)."
        rules = self.cfg.proactive.alerts
        lines = [f"Alertes configurées : {len(rules)}"]
        lines += [f"• {r.name} ({r.severity}) sur {r.entity}" for r in rules]
        recent = self.alerts.events[-5:]
        if recent:
            lines.append("Dernières :")
            tz = ZoneInfo(self.cfg.agent.timezone)
            lines += [
                f"• {e.at.astimezone(tz):%d/%m %H:%M} {e.alert} {e.entity_id} → {e.outcome}"
                for e in reversed(recent)
            ]
        return "\n".join(lines)


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

        asyncio.create_task(initial_scan(), name="alerts-initial-scan")  # noqa: RUF006


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


async def run(config_path: str) -> None:
    store = ConfigStore(config_path)
    cfg = store.config
    setup_logging(cfg.agent.log_level, cfg.agent.log_format)
    services = await build_services(store)
    tasks: list[Any] = []
    gateway = None

    if cfg.telegram.enabled:
        token = AppConfig.secret(cfg.telegram.token_env)
        if not token:
            raise SystemExit(f"Telegram is enabled but {cfg.telegram.token_env} is not set")
        MASKER.add(token)
        from homelab_agent.gateway.aiogram_adapter import TelegramGateway

        gateway = TelegramGateway(token, cfg.telegram.api_base)
        if cfg.voice.enabled:
            stt_key = AppConfig.secret(cfg.voice.api_key_env)
            MASKER.add(stt_key)
            services.stt = SpeechToText(cfg.voice, stt_key)
        frontend = TelegramFrontend(
            gateway.api,
            lambda: store.config.telegram,
            services.orchestrator.handle,
            status_text=services.status_text,
            model_text=services.model_text,
            jobs_text=services.jobs_text,
            reset_chat=services.reset_chat,
            commands={
                "routines": ("routines planifiées", services.routines_text),
                "run": ("lancer une routine maintenant", services.run_routine),
                "routine": ("activer / désactiver / supprimer une routine", services.routine_cmd),
                "alerts": ("alertes et derniers déclenchements", services.alerts_text),
            },
            voice=lambda: store.config.voice,
            stt=services.stt,
        )
        gateway.attach(frontend)
        if services.ha is not None:
            HomeRemote(frontend, services.ha, lambda: store.config.telegram).attach()
        services.notifier = frontend
        if not cfg.telegram.allowed_user_ids:
            log.warning("telegram.allowed_user_ids is empty: every message will be ignored")

    await start_jobs(services)
    await start_proactive(services)
    maintenance = start_maintenance(services)
    if gateway is not None:
        await gateway.start()

    server = None
    if cfg.web.enabled:
        from homelab_agent.web.app import create_app, require_panel_secret

        require_panel_secret(cfg)
        bind = os.environ.get("HLA_WEB_BIND", cfg.web.bind)
        app = create_app(services)
        server = uvicorn.Server(
            uvicorn.Config(app, host=bind, port=cfg.web.port, log_level="warning", lifespan="off")
        )
        tasks.append(asyncio.create_task(server.serve(), name="web"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    services.started = True
    log.info("homelab-agent started")
    await stop.wait()
    log.info("shutting down")
    if server is not None:
        server.should_exit = True
    if gateway is not None:
        await gateway.stop()
    for t in tasks:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(t, timeout=5)
    for task in maintenance:
        task.cancel()
    await close_services(services)
