"""Shared services built at start-up (kept apart from app.py so the web panel and the
/commands can import the type without importing the whole start-up module)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from homelab_agent.config.loader import ConfigStore
from homelab_agent.config.models import AppConfig, Tier
from homelab_agent.core.budget import BudgetManager
from homelab_agent.core.jobs import JobQueue
from homelab_agent.core.orchestrator import Orchestrator
from homelab_agent.core.proactive import AlertEngine, RoutineScheduler
from homelab_agent.core.router import HealthTracker, Router
from homelab_agent.core.routines import RoutineManager
from homelab_agent.gateway.base import ActionButton, BufferedReplier, Notifier, Replier
from homelab_agent.gateway.commands import CommandTable
from homelab_agent.llm.base import LLMBackend
from homelab_agent.store.db import Database
from homelab_agent.tools.homeassistant.cameras import CameraService
from homelab_agent.tools.homeassistant.tools import HomeAssistantService
from homelab_agent.tools.registry import ToolRegistry
from homelab_agent.voice.stt import SpeechToText
from homelab_agent.voice.tts import TextToSpeech
from homelab_agent.web.chat import WebChatHub

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
    tts: TextToSpeech | None = None
    cameras: CameraService | None = None
    webchat: WebChatHub = field(default_factory=WebChatHub)
    #: /commands shared by Telegram and the web chat (Telegram adds its own, e.g. /maison)
    commands: CommandTable = field(default_factory=CommandTable)
    started: bool = False
    #: long-lived tasks started on the side (kept here so they are not garbage collected)
    background: list[asyncio.Task[None]] = field(default_factory=list)

    @property
    def cfg(self) -> AppConfig:
        return self.store.config

    # ------------------------------------------------------------- metrics
    async def metrics_samples(self) -> list[tuple[str, dict[str, str], float]]:
        """Gauges computed at scrape time."""
        from importlib.metadata import PackageNotFoundError, version

        try:
            ver = version("homelab-agent")
        except PackageNotFoundError:  # pragma: no cover - source checkout without install
            ver = "dev"
        samples: list[tuple[str, dict[str, str], float]] = [("build_info", {"version": ver}, 1)]
        for tier in Tier:
            for name in self.cfg.backends_for(tier):
                up = float(self.health.available(name))
                samples.append(("backend_up", {"backend": name, "tier": tier.value}, up))
        if self.ha is not None:
            samples.append(("home_assistant_connected", {}, float(self.ha.client.connected)))
            samples.append(
                ("home_assistant_entities", {}, float(len(self.ha.client.cache.entities)))
            )
        counts = await self.db.count_jobs()
        for status in ("queued", "running", "done", "failed"):
            samples.append(("jobs", {"status": status}, float(counts.get(status, 0))))
        day, month = await self.budget.totals()
        lim = self.cfg.cloud.limits
        samples += [
            ("cloud_spend_eur", {"period": "day"}, day),
            ("cloud_spend_eur", {"period": "month"}, month),
            ("cloud_limit_eur", {"period": "day"}, lim.daily_eur),
            ("cloud_limit_eur", {"period": "month"}, lim.monthly_eur),
            ("cloud_enabled", {}, float(self.cfg.cloud.enabled)),
        ]
        return samples
