"""Proactive home automation: alerts on Home Assistant state changes, scheduled routines.

* Alerts watch the live entity cache (WebSocket ``state_changed``). A rule fires when its
  condition becomes true and stays true for ``for_s``; it then respects ``cooldown_s`` and the
  quiet hours (only ``critical`` alerts get through at night). An optional button runs a Home
  Assistant service through the same permission engine as everything else.
* Routines run at a local time on given days: either an LLM-free ``check`` (what is still on /
  open) or a ``prompt`` handled by the agent as if the user had sent it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from fnmatch import fnmatchcase
from zoneinfo import ZoneInfo

from homelab_agent.config.models import (
    WEEKDAYS,
    AlertRule,
    AppConfig,
    ProactiveSection,
    Routine,
)
from homelab_agent.gateway.base import ActionButton, IncomingMessage, Notifier, Replier
from homelab_agent.observability.metrics import METRICS
from homelab_agent.store.db import Database
from homelab_agent.tools.homeassistant.client import EntityInfo, sanitize_label
from homelab_agent.tools.homeassistant.tools import HomeAssistantService
from homelab_agent.tools.registry import ToolContext

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Handler = Callable[[IncomingMessage, Replier], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def in_quiet_hours(section: ProactiveSection, local: datetime) -> bool:
    q = section.quiet_hours
    if q is None:
        return False
    start, end, now = _hhmm(q.start), _hhmm(q.end), local.time()
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # spans midnight


def recipients(cfg: AppConfig) -> list[str]:
    ids = cfg.proactive.notify_chat_ids or cfg.telegram.allowed_user_ids
    return [str(i) for i in ids]


def condition(rule: AlertRule, info: EntityInfo | None) -> bool:
    if info is None:
        return False
    if rule.state is not None and info.state in rule.state:
        return True
    if rule.above is not None or rule.below is not None:
        try:
            value = float(info.state)
        except ValueError:
            return False
        if rule.above is not None and value > rule.above:
            return True
        if rule.below is not None and value < rule.below:
            return True
    return False


def render_message(rule: AlertRule, info: EntityInfo) -> str:
    fields = {
        "name": sanitize_label(info.name),
        "entity_id": info.entity_id,
        "state": sanitize_label(info.state, 30),
        "unit": info.attributes.get("unit_of_measurement", "") or "",
        "area": sanitize_label(info.area_name or "-", 30),
        "alert": rule.name,
    }
    try:
        return rule.message.format(**fields)
    except (KeyError, IndexError, ValueError):
        return f"⚠️ {fields['name']} : {fields['state']}{fields['unit']}"


@dataclass(slots=True)
class AlertEvent:
    alert: str
    entity_id: str
    state: str
    outcome: str  # sent | quiet | cooldown
    at: datetime


class AlertEngine:
    def __init__(
        self,
        get_config: Callable[[], AppConfig],
        ha: HomeAssistantService,
        notifier: Callable[[], Notifier],
        db: Database,
        clock: Clock = _utcnow,
    ) -> None:
        self._cfg = get_config
        self.ha = ha
        self._notifier = notifier
        self.db = db
        self.clock = clock
        self._timers: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._last_sent: dict[tuple[str, str], datetime] = {}
        self.events: list[AlertEvent] = []  # recent, in memory (DB keeps the history)
        self._attached = False

    @property
    def section(self) -> ProactiveSection:
        return self._cfg().proactive

    def attach(self) -> None:
        if not self._attached:
            self.ha.client.cache.listeners.append(self.on_change)
            self._attached = True

    def rules_for(self, entity_id: str) -> list[AlertRule]:
        if not self.section.enabled:
            return []
        return [r for r in self.section.alerts if fnmatchcase(entity_id, r.entity)]

    # ------------------------------------------------------------- events
    def on_change(self, old: EntityInfo | None, new: EntityInfo | None) -> None:
        info = new or old
        if info is None:
            return
        for rule in self.rules_for(info.entity_id):
            key = (rule.name, info.entity_id)
            now_true = condition(rule, new)
            was_true = condition(rule, old)
            if now_true and not was_true:
                self._arm(rule, info.entity_id)
            elif not now_true:
                self._disarm(key)

    def evaluate_all(self) -> None:
        """Arm rules whose condition is already true (e.g. at startup)."""
        for info in list(self.ha.client.cache.entities.values()):
            for rule in self.rules_for(info.entity_id):
                if condition(rule, info) and (rule.name, info.entity_id) not in self._timers:
                    self._arm(rule, info.entity_id)

    def _arm(self, rule: AlertRule, entity_id: str) -> None:
        key = (rule.name, entity_id)
        self._disarm(key)
        self._timers[key] = asyncio.create_task(
            self._wait_and_fire(rule, entity_id), name=f"alert-{rule.name}-{entity_id}"
        )

    def _disarm(self, key: tuple[str, str]) -> None:
        task = self._timers.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    async def _wait_and_fire(self, rule: AlertRule, entity_id: str) -> None:
        try:
            if rule.for_s:
                await asyncio.sleep(rule.for_s)
            info = self.ha.client.cache.entities.get(entity_id)
            if info is not None and condition(rule, info):
                await self.fire(rule, info)
        finally:
            key = (rule.name, entity_id)
            if self._timers.get(key) is asyncio.current_task():
                del self._timers[key]

    async def fire(self, rule: AlertRule, info: EntityInfo) -> str:
        cfg = self._cfg()
        now = self.clock()
        key = (rule.name, info.entity_id)
        last = self._last_sent.get(key)
        if last is not None and now - last < timedelta(seconds=rule.cooldown_s):
            outcome = "cooldown"
        elif rule.severity != "critical" and in_quiet_hours(
            cfg.proactive, now.astimezone(ZoneInfo(cfg.agent.timezone))
        ):
            outcome = "quiet"
        else:
            outcome = "sent"
            self._last_sent[key] = now
            text = render_message(rule, info)
            if rule.severity == "critical":
                text = "🚨 " + text
            actions = [self._action_button(rule, info)] if rule.action else []
            notifier = self._notifier()
            for chat in recipients(cfg):
                if actions:
                    await notifier.notify_actions(chat, text, actions, cfg.proactive.action_ttl_s)
                else:
                    await notifier.notify(chat, text)
        self.events.append(AlertEvent(rule.name, info.entity_id, info.state, outcome, now))
        self.events = self.events[-200:]
        METRICS.inc("alerts_total", alert=rule.name, outcome=outcome)
        await self.db.audit(
            requester="proactive", tier="alert", model="-", tool=f"alert:{rule.name}",
            params={"entity_id": info.entity_id, "state": info.state}, outcome=outcome,
        )  # fmt: skip
        return outcome

    def _action_button(self, rule: AlertRule, info: EntityInfo) -> ActionButton:
        assert rule.action is not None
        action = rule.action
        target = action.entity or info.entity_id

        async def run(user_id: str, replier: Replier) -> str:
            ctx = ToolContext(
                requester=user_id, chat_id=user_id, tier="alert", model="-",
                confirm=replier.confirm,
            )  # fmt: skip
            res = await self.ha.act(
                action.domain, action.service, [target], {}, ctx, tool_name=f"alert:{rule.name}"
            )
            if res.ok:
                return f"✅ {action.label} : {action.domain}.{action.service} → {target}"
            if res.outcome == "declined":
                return "👍 Action annulée."
            return f"⛔ {res.error}"

        return ActionButton(action.label, run)

    async def idle(self) -> None:
        """Wait until every pending alert has fired or been cancelled."""
        while self._timers:
            await asyncio.gather(*self._timers.values(), return_exceptions=True)

    async def stop(self) -> None:
        tasks = list(self._timers.values())
        for key in list(self._timers):
            self._disarm(key)
        await asyncio.gather(*tasks, return_exceptions=True)


class RoutineScheduler:
    def __init__(
        self,
        get_config: Callable[[], AppConfig],
        handler: Handler,
        notifier: Callable[[], Notifier],
        ha: HomeAssistantService | None,
        db: Database,
        clock: Clock = _utcnow,
        on_once_done: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._cfg = get_config
        self.on_once_done = on_once_done
        self.handler = handler
        self._notifier = notifier
        self.ha = ha
        self.db = db
        self.clock = clock
        self._task: asyncio.Task[None] | None = None
        self._last_run: dict[str, datetime] = {}

    @property
    def section(self) -> ProactiveSection:
        return self._cfg().proactive

    def get(self, name: str) -> Routine | None:
        return next((r for r in self.section.routines if r.name == name), None)

    def next_run(self, routine: Routine, after: datetime | None = None) -> datetime | None:
        """Next local occurrence strictly after ``after`` (UTC), returned in UTC."""
        tz = ZoneInfo(self._cfg().agent.timezone)
        local = (after or self.clock()).astimezone(tz)
        at = _hhmm(routine.at)
        if routine.date is not None:
            once = datetime.combine(
                datetime.strptime(routine.date, "%Y-%m-%d").date(), at, tzinfo=tz
            )
            return once.astimezone(UTC) if once > local else None
        for day in range(8):
            d = (local + timedelta(days=day)).date()
            candidate = datetime.combine(d, at, tzinfo=tz)
            if candidate > local and WEEKDAYS[candidate.weekday()] in routine.days:
                return candidate.astimezone(UTC)
        return None

    def due(self, now: datetime, previous: datetime) -> list[Routine]:
        """Routines whose scheduled time is in (previous, now]."""
        out = []
        for r in self.section.routines:
            if not r.enabled:
                continue
            nxt = self.next_run(r, previous)
            if nxt is not None and nxt <= now:
                out.append(r)
        return out

    async def run(self, routine: Routine, chats: list[str] | None = None) -> list[str]:
        """Run one routine now and return the messages sent (check routines)."""
        cfg = self._cfg()
        chats = chats or [str(c) for c in routine.chat_ids] or recipients(cfg)
        self._last_run[routine.name] = self.clock()
        METRICS.inc("routines_total", routine=routine.name)
        sent: list[str] = []
        if routine.prompt is not None:
            for chat in chats:
                msg = IncomingMessage(
                    chat, chat, routine.prompt, forced_tier=routine.tier, origin="routine"
                )
                await self.handler(msg, self._notifier().replier_for(chat))
        else:
            text = (
                f"⏰ {routine.message}"
                if routine.message is not None
                else self.check_report(routine)
            )
            if text is not None:
                for chat in chats:
                    await self._notifier().notify(chat, text)
                sent = [text]
        await self.db.audit(
            requester="proactive", tier="routine", model="-", tool=f"routine:{routine.name}",
            params={"kind": routine.kind, "chats": chats}, outcome="ok",
        )  # fmt: skip
        if routine.date is not None and self.on_once_done is not None:
            await self.on_once_done(routine.name)
        return sent

    def check_report(self, routine: Routine) -> str | None:
        assert routine.check is not None
        if self.ha is None:
            return f"⚠️ {routine.name} : Home Assistant n'est pas configuré."
        chk = routine.check
        found = [
            e
            for e in self.ha.readable_entities()
            if e.domain in chk.domains
            and e.state in chk.states
            and (not chk.entities or any(fnmatchcase(e.entity_id, g) for g in chk.entities))
        ]
        if not found:
            return None if routine.only_if_found else f"✅ {routine.name} : rien à signaler."
        lines = [f"🔎 {routine.name} : {len(found)} élément(s)"]
        lines += [f"• {sanitize_label(e.name)} — {sanitize_label(e.state, 20)}" for e in found]
        return "\n".join(lines)

    def start(self, tick_s: float = 30.0) -> None:
        async def loop() -> None:
            previous = self.clock()
            while True:
                await asyncio.sleep(tick_s)
                now = self.clock()
                if self.section.enabled:
                    for routine in self.due(now, previous):
                        try:
                            await self.run(routine)
                        except Exception:
                            log.exception("routine %s failed", routine.name)
                previous = now

        if self._task is None:
            self._task = asyncio.create_task(loop(), name="routines")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
