"""/maison: a Telegram remote control for the house, made of inline keyboards.

Rooms (Home Assistant areas) → devices with their live state → one tap toggles. No LLM is
involved, and every tap goes through ``HomeAssistantService.act`` (permissions, confirmations,
audit) like any other action. Read-only devices are shown with a padlock.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from homelab_agent.config.models import TelegramSection
from homelab_agent.gateway.telegram import Button, TelegramFrontend, TelegramReplier
from homelab_agent.tools.homeassistant.client import EntityInfo, sanitize_label
from homelab_agent.tools.homeassistant.tools import HomeAssistantService
from homelab_agent.tools.registry import ToolContext

ICONS = {
    "light": "💡", "switch": "🔌", "fan": "🌀", "cover": "🪟", "lock": "🔐", "scene": "🎬",
    "script": "📜", "media_player": "📺", "input_boolean": "🔘", "button": "🔘",
    "sensor": "📟", "binary_sensor": "🔎", "climate": "🌡️",
}  # fmt: skip
STATES = {
    "on": "allumé", "off": "éteint", "open": "ouvert", "closed": "fermé", "opening": "s'ouvre",
    "closing": "se ferme", "locked": "verrouillé", "unlocked": "déverrouillé",
    "playing": "lecture", "paused": "pause", "idle": "inactif", "unavailable": "indisponible",
}  # fmt: skip
PAGE_SIZE = 10
NO_AREA = "__none__"
SCENES = "__scenes__"
TOKEN_TTL_S = 6 * 3600


def toggle_service(info: EntityInfo) -> str | None:
    """Service that flips the entity, or None when it cannot be toggled from a button."""
    d, state = info.domain, info.state
    if d in ("light", "switch", "fan", "input_boolean", "media_player"):
        return "turn_off" if state not in ("off", "unavailable", "standby") else "turn_on"
    if d == "cover":
        return "close_cover" if state in ("open", "opening") else "open_cover"
    if d == "lock":
        return "unlock" if state == "locked" else "lock"
    if d in ("scene", "script"):
        return "turn_on"
    if d == "button":
        return "press"
    return None


def state_label(info: EntityInfo) -> str:
    unit = info.attributes.get("unit_of_measurement") or ""
    label = STATES.get(info.state, info.state)
    return sanitize_label(f"{label}{(' ' + unit) if unit else ''}", 20)


@dataclass(slots=True)
class _Token:
    kind: str  # home | area | toggle | close
    value: str
    page: int
    expires: float


class HomeRemote:
    def __init__(
        self,
        frontend: TelegramFrontend,
        ha: HomeAssistantService,
        get_section: Callable[[], TelegramSection],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.f = frontend
        self.ha = ha
        self._section = get_section
        self.clock = clock
        self._tokens: dict[str, _Token] = {}

    def attach(self) -> None:
        self.f.callbacks["hm"] = self.on_callback
        self.f.commands["maison"] = ("télécommande de la maison (boutons)", self.command)

    # ------------------------------------------------------------- tokens
    def _token(self, kind: str, value: str = "", page: int = 0) -> str:
        now = self.clock()
        if len(self._tokens) > 2000:
            self._tokens = {k: t for k, t in self._tokens.items() if t.expires > now}
        token = uuid.uuid4().hex[:12]
        self._tokens[token] = _Token(kind, value, page, now + TOKEN_TTL_S)
        return f"hm:{token}"  # well under Telegram's 64-byte limit

    # -------------------------------------------------------------- model
    def _controllable(self) -> list[EntityInfo]:
        domains = set(self._section().remote_domains)
        return [e for e in self.ha.readable_entities() if e.domain in domains]

    def _areas(self) -> list[tuple[str, str, int]]:
        """(key, label, count) of rooms that have devices, sorted by name."""
        counts: dict[str, int] = {}
        for e in self._controllable():
            if e.domain == "scene":
                continue
            key = e.area_id or NO_AREA
            counts[key] = counts.get(key, 0) + 1
        names = self.ha.client.cache.areas
        rows = [
            (key, sanitize_label(names.get(key, key), 24), n)
            for key, n in counts.items()
            if key != NO_AREA
        ]
        rows.sort(key=lambda r: r[1].casefold())
        if NO_AREA in counts:
            rows.append((NO_AREA, "Sans pièce", counts[NO_AREA]))
        return rows

    def _devices(self, area: str) -> list[EntityInfo]:
        items = self._controllable()
        if area == SCENES:
            return [e for e in items if e.domain == "scene"]
        key = None if area == NO_AREA else area
        return [e for e in items if e.domain != "scene" and e.area_id == key]

    def _sensors(self, area: str) -> list[EntityInfo]:
        if area in (SCENES, NO_AREA):
            return []
        return [
            e
            for e in self.ha.readable_entities(area=area)
            if e.domain in ("sensor", "binary_sensor", "climate")
        ][:8]

    # -------------------------------------------------------------- views
    def home_view(self) -> tuple[str, list[list[Button]]]:
        areas = self._areas()
        rows: list[list[Button]] = []
        line: list[Button] = []
        for key, label, n in areas:
            line.append((f"{label} ({n})", self._token("area", key)))
            if len(line) == 2:
                rows.append(line)
                line = []
        if line:
            rows.append(line)
        if any(e.domain == "scene" for e in self._controllable()):
            rows.append([("🎬 Scènes", self._token("area", SCENES))])
        rows.append([("✖️ Fermer", self._token("close"))])
        text = "🏠 Maison — choisis une pièce" if areas else "🏠 Aucun appareil pilotable."
        return text, rows

    def area_view(self, area: str, page: int = 0) -> tuple[str, list[list[Button]]]:
        devices = self._devices(area)
        title = (
            "🎬 Scènes"
            if area == SCENES
            else "📦 Sans pièce"
            if area == NO_AREA
            else f"🚪 {sanitize_label(self.ha.client.cache.areas.get(area, area), 30)}"
        )
        lines = [title]
        for s in self._sensors(area):
            lines.append(
                f"{ICONS.get(s.domain, '•')} {sanitize_label(s.name, 30)} : {state_label(s)}"
            )
        pages = max(1, -(-len(devices) // PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        rows: list[list[Button]] = []
        for e in devices[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
            decision = self.ha.permissions.evaluate(e.entity_id, "control", areas=e.areas)
            icon = ICONS.get(e.domain, "•") if decision.allowed else "🔒"
            name = sanitize_label(e.name, 28)
            label = (
                name if e.domain in ("scene", "script", "button") else f"{name} · {state_label(e)}"
            )
            if decision.allowed and decision.needs_confirm:
                label += " ⚠️"
            rows.append([(f"{icon} {label}", self._token("toggle", e.entity_id, page))])
        nav: list[Button] = [("⬅️ Pièces", self._token("home"))]
        if pages > 1:
            if page > 0:
                nav.append(("◀️", self._token("area", area, page - 1)))
            nav.append((f"{page + 1}/{pages}", self._token("area", area, page)))
            if page < pages - 1:
                nav.append(("▶️", self._token("area", area, page + 1)))
        else:
            nav.append(("🔄", self._token("area", area, page)))
        rows.append(nav)
        if not devices:
            lines.append("(aucun appareil pilotable)")
        return "\n".join(lines), rows

    # ---------------------------------------------------------- handlers
    async def command(self, chat_id: str, arg: str) -> str | None:
        text, rows = self.home_view()
        await self.f.api.send_message(chat_id, text, buttons=rows)
        return None

    async def on_callback(
        self, user_id: str, chat_id: str, message_id: int | None, payload: str, callback_id: str
    ) -> None:
        token = self._tokens.get(payload)
        if token is None or token.expires < self.clock() or message_id is None:
            await self.f.api.answer_callback(callback_id, "Menu expiré : relance /maison.")
            return
        if token.kind == "close":
            await self.f.api.answer_callback(callback_id)
            await self.f.api.edit_message_text(chat_id, message_id, "🏠 Télécommande fermée.")
            return
        if token.kind == "home":
            await self.f.api.answer_callback(callback_id)
            text, rows = self.home_view()
            await self.f.api.edit_message_text(chat_id, message_id, text, buttons=rows)
            return
        if token.kind == "area":
            await self.f.api.answer_callback(callback_id)
            text, rows = self.area_view(token.value, token.page)
            await self.f.api.edit_message_text(chat_id, message_id, text, buttons=rows)
            return
        # toggle
        info = self.ha.client.cache.entities.get(token.value)
        if info is None:
            await self.f.api.answer_callback(callback_id, "Appareil introuvable.")
            return
        service = toggle_service(info)
        if service is None:
            await self.f.api.answer_callback(callback_id, f"{state_label(info)} (lecture seule)")
            return
        decision = self.ha.permissions.evaluate(info.entity_id, "control", areas=info.areas)
        if not decision.allowed:
            await self.f.api.answer_callback(callback_id, "🔒 Lecture seule (permissions)")
            return
        await self.f.api.answer_callback(callback_id, "⏳")
        ctx = ToolContext(
            requester=user_id, chat_id=chat_id, tier="remote", model="-",
            confirm=TelegramReplier(self.f, chat_id, user_id).confirm,
        )  # fmt: skip
        res = await self.ha.act(
            info.domain, service, [info.entity_id], {}, ctx, tool_name="home_remote"
        )
        area = info.area_id or (SCENES if info.domain == "scene" else NO_AREA)
        text, rows = self.area_view(area, token.page)
        if not res.ok:
            text += "\n" + ("👍 Annulé." if res.outcome == "declined" else f"⛔ {res.error}")
        await self.f.api.edit_message_text(chat_id, message_id, text, buttons=rows)
