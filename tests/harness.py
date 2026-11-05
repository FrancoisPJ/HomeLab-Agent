"""In-process wiring of the agent with fake backends (no network, no hardware)."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from ruamel.yaml import YAML

from homelab_agent.config.loader import ConfigStore
from homelab_agent.config.models import AppConfig, OpenAICompatBackend
from homelab_agent.core.budget import BudgetManager
from homelab_agent.core.jobs import JobQueue
from homelab_agent.core.memory import MemoryService, register_memory_tools
from homelab_agent.core.orchestrator import Orchestrator
from homelab_agent.core.proactive import RoutineScheduler
from homelab_agent.core.router import HealthTracker, Router
from homelab_agent.core.routines import RoutineManager, register_routine_tools
from homelab_agent.fakes.ha import AREAS
from homelab_agent.fakes.ha import create_app as fake_ha_app
from homelab_agent.fakes.llm import create_app as fake_llm_app
from homelab_agent.gateway.base import ActionButton, BufferedReplier, Replier
from homelab_agent.llm.base import LLMBackend
from homelab_agent.llm.openai_compat import OpenAICompatClient
from homelab_agent.store.db import Database
from homelab_agent.tools.homeassistant.client import HomeAssistantClient
from homelab_agent.tools.homeassistant.permissions import PermissionEngine
from homelab_agent.tools.homeassistant.tools import HomeAssistantService, register_ha_tools
from homelab_agent.tools.registry import ToolRegistry, escalate_tool

from .conftest import base_config


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.actions: list[ActionButton] = []
        self.replies: list[str] = []
        self.photos: list[tuple[str, bytes, str]] = []  # (chat_id, image, caption)

    async def notify(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))

    async def notify_actions(
        self, chat_id: str, text: str, actions: list[ActionButton], ttl_s: float
    ) -> None:
        self.sent.append((chat_id, text))
        self.actions.extend(actions)

    def replier_for(self, chat_id: str) -> Replier:
        notifier = self

        class _Recording(BufferedReplier):
            async def send(self, text: str) -> None:
                await super().send(text)
                notifier.replies.append(text)

            async def send_photo(self, image: bytes, caption: str) -> None:
                await super().send_photo(image, caption)
                notifier.photos.append((chat_id, image, caption))

            def stream(self):  # type: ignore[no-untyped-def]
                handle = super().stream()
                orig = handle.finish

                async def finish(text: str) -> None:
                    await orig(text)
                    notifier.replies.append(text)

                handle.finish = finish  # type: ignore[method-assign]
                return handle

        return _Recording(auto_confirm=False)


@dataclass
class Harness:
    cfg_store: ConfigStore
    db: Database
    llm_apps: dict[str, Any]
    ha_app: Any
    backends: dict[str, LLMBackend]
    router: Router
    orchestrator: Orchestrator
    ha: HomeAssistantService
    notifier: RecordingNotifier
    jobs: JobQueue
    scheduler: RoutineScheduler
    routines: RoutineManager

    @property
    def ha_calls(self) -> list[dict[str, Any]]:
        return list(self.ha_app.state.ha.calls)

    def set_llm(self, backend: str, **state: Any) -> None:
        # fake-llm keeps its state in a closure; reach it through the control route
        import asyncio

        async def go() -> None:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.llm_apps[backend]), base_url="http://x"
            ) as c:
                await c.post("/_control", json=state)

        loop = asyncio.get_event_loop()
        if loop.is_running():
            raise RuntimeError("use await h.aset_llm()")
        loop.run_until_complete(go())

    async def aset_llm(self, backend: str, **state: Any) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.llm_apps[backend]), base_url="http://x"
        ) as c:
            await c.post("/_control", json=state)

    async def close(self) -> None:
        await self.jobs.stop()
        await self.db.close()


async def build_harness(tmp_path: Path, **overrides: Any) -> Harness:
    cfg: AppConfig = base_config(**overrides)
    path = tmp_path / "config.yaml"
    buf = io.StringIO()
    YAML(typ="safe").dump(cfg.model_dump(mode="json", exclude_none=True), buf)
    path.write_text(buf.getvalue(), encoding="utf-8")
    store = ConfigStore(path)
    db = Database(str(tmp_path / "agent.db"))
    await db.init()
    llm_apps: dict[str, Any] = {}
    backends: dict[str, LLMBackend] = {}
    for name, b in cfg.backends.items():
        assert isinstance(b, OpenAICompatBackend)
        llm_apps[name] = fake_llm_app()
        backends[name] = OpenAICompatClient(
            name, b, transport=httpx.ASGITransport(app=llm_apps[name])
        )
    health = HealthTracker(cooldown_s=60)
    budget = BudgetManager(lambda: store.config, db)
    router = Router(lambda: store.config, backends, health, budget)
    notifier = RecordingNotifier()
    registry = ToolRegistry()
    registry.register(escalate_tool())
    ha_app = fake_ha_app()
    client = HomeAssistantClient("http://ha", "token", transport=httpx.ASGITransport(app=ha_app))
    fake = ha_app.state.ha
    client.cache.load(list(fake.states.values()), AREAS, fake.entity_registry(), [])
    ha = HomeAssistantService(
        client,
        PermissionEngine(lambda: store.config.home_assistant),
        lambda: store.config.home_assistant,
        db,
    )
    register_ha_tools(registry, ha)
    orch = Orchestrator(lambda: store.config, db, router, registry, budget, ha)
    orch.memory = MemoryService(lambda: store.config, db)
    register_memory_tools(registry, orch.memory)
    scheduler = RoutineScheduler(lambda: store.config, orch.handle, lambda: notifier, ha, db)
    manager = RoutineManager(store, scheduler)

    async def once_done(name: str) -> None:
        manager.remove(name)

    scheduler.on_once_done = once_done
    register_routine_tools(registry, manager)
    jobs = JobQueue(db, orch.run_job, notifier, poll_interval_s=0.05, retry_delay_s=0.01)
    orch.jobs = jobs
    return Harness(
        store, db, llm_apps, ha_app, backends, router, orch, ha, notifier, jobs, scheduler, manager
    )
