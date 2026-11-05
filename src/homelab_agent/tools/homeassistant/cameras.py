"""Camera snapshots: Home Assistant ``camera_proxy`` -> the chat that asked.

Privacy rules, enforced here whatever the entry point (/camera command, buttons, agent):
- off unless ``home_assistant.cameras.enabled``; a camera must also be readable by the
  permission rules (so one camera can be denied from the panel);
- the image goes only to the requesting chat, through a gateway able to show photos;
- the image is never given to a model: the agent tool only gets a short text back;
- every snapshot is written to the audit log, and requests are rate-limited per chat.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from homelab_agent.config.models import AppConfig, CameraSection
from homelab_agent.core.fastpath import normalize
from homelab_agent.gateway.base import ActionButton, Notifier, PhotoReplier, Replier
from homelab_agent.tools.homeassistant.client import EntityInfo, HAError, sanitize_label
from homelab_agent.tools.homeassistant.tools import HomeAssistantService
from homelab_agent.tools.registry import Tool, ToolContext, ToolRegistry, ToolResult

AUDIT_TOOL = "camera_snapshot"
ALL_WORDS = {"toutes", "tout", "toute", "tous", "all", "partout", "maison", "appartement", "appart"}
#: words ignored when matching a room ("la caméra de l'entrée" -> "entree")
FILLER = {
    "le", "la", "les", "l", "de", "du", "des", "d", "dans", "a", "au", "aux", "sur", "en",
    "camera", "cameras", "cam", "piece", "the", "in", "room",
}  # fmt: skip
NO_ROOM = "Sans pièce"


@dataclass(slots=True)
class CameraOutcome:
    sent: list[str] = field(default_factory=list)  # camera names
    problems: list[str] = field(default_factory=list)
    error: str | None = None
    denied: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.sent)


class CameraService:
    def __init__(
        self,
        ha: HomeAssistantService,
        get_config: Callable[[], AppConfig],
        get_notifier: Callable[[], Notifier],
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.ha = ha
        self._cfg = get_config
        self._notifier = get_notifier
        self.clock = clock
        self.now = now
        self._recent: dict[str, deque[float]] = {}

    @property
    def section(self) -> CameraSection:
        return self._cfg().home_assistant.cameras

    # ------------------------------------------------------------ discovery
    def cameras(self) -> list[EntityInfo]:
        """Cameras the permission rules let us read, by room then name."""
        cams = [
            e
            for e in self.ha.client.cache.entities.values()
            if e.domain == "camera" and self.ha.can_read(e.entity_id)
        ]
        return sorted(cams, key=lambda e: ((e.area_name or "~").casefold(), e.name.casefold()))

    def rooms(self) -> list[tuple[str, list[EntityInfo]]]:
        """(room label, cameras) for the rooms that have at least one readable camera."""
        out: dict[str, list[EntityInfo]] = {}
        for cam in self.cameras():
            out.setdefault(cam.area_name or NO_ROOM, []).append(cam)
        return list(out.items())

    def resolve(self, query: str | None) -> list[EntityInfo]:
        """Cameras for a room name, a camera name or an entity_id; all of them for "toutes"."""
        cams = self.cameras()
        raw = (query or "").strip()
        words = set(normalize(raw).split()) - FILLER
        if not raw or words & ALL_WORDS or (raw and not words and not raw.startswith("camera.")):
            return cams
        exact = [c for c in cams if c.entity_id == raw.casefold()]
        if exact:
            return exact

        def covers(label: str | None) -> bool:
            label_words = set(normalize(label or "").split()) - FILLER
            return bool(label_words) and label_words <= words

        by_room = [c for c in cams if any(covers(a) for a in c.areas)]
        if by_room:
            return by_room
        return [c for c in cams if covers(c.name) or covers(c.entity_id.split(".", 1)[1])]

    # ------------------------------------------------------------ rate limit
    def _take(self, chat_id: str, wanted: int) -> int:
        now = self.clock()
        recent = self._recent.setdefault(chat_id, deque())
        while recent and now - recent[0] > 60:
            recent.popleft()
        allowed = max(0, min(wanted, self.section.max_per_minute - len(recent)))
        recent.extend([now] * allowed)
        return allowed

    # -------------------------------------------------------------- sending
    async def send(
        self,
        query: str | None,
        *,
        chat_id: str,
        requester: str,
        replier: Replier | None = None,
        tier: str = "command",
        model: str = "-",
    ) -> CameraOutcome:
        section = self.section
        if not section.enabled:
            return CameraOutcome(
                error="Les caméras sont désactivées (home_assistant.cameras.enabled).", denied=True
            )
        target = replier if replier is not None else self._notifier().replier_for(chat_id)
        if not isinstance(target, PhotoReplier):
            return CameraOutcome(error="Les photos des caméras ne s'affichent que sur Telegram.")
        cams = self.resolve(query)
        if not cams:
            return self._not_found(query)
        cams = cams[: section.max_per_request]
        allowed = self._take(chat_id, len(cams))
        if allowed == 0:
            return CameraOutcome(error="Trop de photos demandées : réessaie dans une minute.")
        outcome = CameraOutcome()
        if allowed < len(cams):
            outcome.problems.append(f"limite atteinte : {allowed} photo(s) sur {len(cams)}")
        tz = ZoneInfo(self._cfg().agent.timezone)
        for cam in cams[:allowed]:
            name = sanitize_label(cam.name)
            try:
                image, _ctype = await self.ha.client.camera_snapshot(
                    cam.entity_id, max_bytes=section.max_bytes, timeout_s=section.timeout_s
                )
                await target.send_photo(image, f"📷 {name} · {self.now().astimezone(tz):%H:%M}")
            except HAError as exc:
                outcome.problems.append(f"{name} : image indisponible")
                await self._audit(requester, chat_id, cam, "error", str(exc), tier, model)
                continue
            outcome.sent.append(name)
            await self._audit(requester, chat_id, cam, "ok", "", tier, model)
        if not outcome.sent and not outcome.error:
            outcome.error = "Aucune image n'a pu être récupérée."
        return outcome

    def _not_found(self, query: str | None) -> CameraOutcome:
        raw = (query or "").strip()
        exists = self.ha.client.cache.entities.get(raw.casefold())
        if exists is not None and exists.domain == "camera":
            return CameraOutcome(error=f"permission refusée : {raw}", denied=True)
        rooms = ", ".join(label for label, _ in self.rooms())
        if not rooms:
            return CameraOutcome(error="Aucune caméra accessible.")
        label = sanitize_label(raw)
        return CameraOutcome(error=f"Aucune caméra pour « {label} ». Pièces : {rooms}.")

    async def _audit(
        self,
        requester: str,
        chat_id: str,
        cam: EntityInfo,
        outcome: str,
        detail: str,
        tier: str,
        model: str,
    ) -> None:
        if self.ha.db is None:
            return
        await self.ha.db.audit(
            requester=requester, tier=tier, model=model, tool=AUDIT_TOOL,
            params={"entity_id": cam.entity_id, "chat_id": chat_id}, outcome=outcome,
            detail=detail,
        )  # fmt: skip

    # --------------------------------------------------------- chat command
    async def command(self, chat_id: str, arg: str) -> str | None:
        """``/camera [pièce]``: photos of a room, or one button per room without argument."""
        if not self.section.enabled:
            return "📷 Les caméras sont désactivées (home_assistant.cameras.enabled)."
        if arg.strip():
            outcome = await self.send(arg, chat_id=chat_id, requester=chat_id)
            return self._summary(outcome)
        rooms = self.rooms()
        if not rooms:
            return "📷 Aucune caméra accessible."
        buttons = [self._button(chat_id, f"📷 {label}", label) for label, _ in rooms]
        if len(rooms) > 1:
            buttons.append(self._button(chat_id, "📷 Toutes", "toutes"))
        await self._notifier().notify_actions(chat_id, "📷 Quelle pièce ?", buttons, ttl_s=600)
        return None

    def _button(self, chat_id: str, label: str, query: str) -> ActionButton:
        # buttons only work in the chat they were sent to (checked by the gateway)
        async def run(user_id: str, replier: Replier) -> str:
            outcome = await self.send(query, chat_id=chat_id, requester=user_id, replier=replier)
            return self._summary(outcome) or "✔️"

        return ActionButton(label, run)

    @staticmethod
    def _summary(outcome: CameraOutcome) -> str | None:
        if outcome.error:
            return f"📷 {outcome.error}"
        if outcome.problems:
            return "⚠️ " + " ; ".join(outcome.problems)
        return None  # the photos speak for themselves


def register_camera_tools(registry: ToolRegistry, cams: CameraService) -> None:
    async def snapshot(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not cams.section.model_tool:
            return ToolResult(False, error="camera tool disabled", outcome="denied")
        outcome = await cams.send(
            args.get("room"), chat_id=ctx.chat_id, requester=ctx.requester,
            tier=ctx.tier, model=ctx.model,
        )  # fmt: skip
        if not outcome.ok:
            return ToolResult(
                False, error=outcome.error or "; ".join(outcome.problems),
                outcome="denied" if outcome.denied else "error",
            )  # fmt: skip
        # text only: the model never sees the image
        return ToolResult(
            True,
            data={
                "photos_sent_to_user": outcome.sent,
                "problems": outcome.problems,
                "note": "The user received the photos in the chat. You cannot see them: "
                "do not describe them, just say they were sent.",
            },
        )

    registry.register(
        Tool(
            "ha_camera_snapshot",
            "Send the user a live photo from the home cameras of a room (or of every camera "
            "when room is empty or 'toutes'). The photo goes straight to the user's chat; you "
            "do not receive the image and must never describe it.",
            {
                "type": "object",
                "properties": {
                    "room": {
                        "type": "string",
                        "description": "Room name (e.g. salon, entrée), camera name or "
                        "camera entity_id; empty for every camera.",
                    }
                },
                "additionalProperties": False,
            },
            snapshot,
            {"ha", "camera", "action"},
            available=lambda: cams.section.enabled and cams.section.model_tool,
        )
    )
