"""Create and manage routines from a conversation ("tous les soirs à 23 h, vérifie…").

Routines live in ``config.yaml`` (single source of truth): every change goes through
``ConfigStore`` (validated, comments kept) and is applied without restart. Creating, deleting
or toggling a routine from the LLM always requires the user's Yes/No confirmation, and a
routine's own prompt can never edit routines (no self-replicating schedules).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from homelab_agent.config.loader import ConfigError, ConfigStore, contains_env_reference
from homelab_agent.config.models import WEEKDAYS, Routine, Weekday
from homelab_agent.core.proactive import RoutineScheduler
from homelab_agent.tools.registry import Tool, ToolContext, ToolRegistry, ToolResult

_DAY_ALIASES: dict[str, list[Weekday]] = {
    "lun": ["mon"], "lundi": ["mon"], "mon": ["mon"], "monday": ["mon"],
    "mar": ["tue"], "mardi": ["tue"], "tue": ["tue"], "tuesday": ["tue"],
    "mer": ["wed"], "mercredi": ["wed"], "wed": ["wed"], "wednesday": ["wed"],
    "jeu": ["thu"], "jeudi": ["thu"], "thu": ["thu"], "thursday": ["thu"],
    "ven": ["fri"], "vendredi": ["fri"], "fri": ["fri"], "friday": ["fri"],
    "sam": ["sat"], "samedi": ["sat"], "sat": ["sat"], "saturday": ["sat"],
    "dim": ["sun"], "dimanche": ["sun"], "sun": ["sun"], "sunday": ["sun"],
    "semaine": ["mon", "tue", "wed", "thu", "fri"], "weekdays": ["mon", "tue", "wed", "thu", "fri"],
    "ouvres": ["mon", "tue", "wed", "thu", "fri"],
    "weekend": ["sat", "sun"], "week-end": ["sat", "sun"],
    "tous": list(WEEKDAYS), "tout": list(WEEKDAYS), "everyday": list(WEEKDAYS),
    "daily": list(WEEKDAYS), "quotidien": list(WEEKDAYS), "jours": list(WEEKDAYS),
}  # fmt: skip
_DAY_LABELS = {"mon": "lun", "tue": "mar", "wed": "mer", "thu": "jeu", "fri": "ven",
               "sat": "sam", "sun": "dim"}  # fmt: skip


def _ascii(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def parse_days(value: Any) -> list[Weekday]:
    """'semaine', 'lun,mer', ['sat','sun'], 'tous les jours'… → sorted weekdays."""
    if value is None or value == "" or value == []:
        return list(WEEKDAYS)
    tokens = value if isinstance(value, list) else re.split(r"[\s,;/+&]+|\bet\b", str(value))
    days: set[str] = set()
    for tok in tokens:
        key = _ascii(str(tok)).strip(" .")
        if not key or key in ("les", "le", "du", "au", "de"):
            continue
        if key not in _DAY_ALIASES:
            raise ValueError(f"jour inconnu : {tok!r}")
        days.update(_DAY_ALIASES[key])
    return [d for d in WEEKDAYS if d in days]


def parse_time(value: Any) -> str:
    """'23:00', '23h', '7h30', '7.30', '0730' → 'HH:MM'."""
    text = _ascii(str(value)).replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?:[:h.](\d{2})?)?h?", text) or re.fullmatch(
        r"(\d{2})(\d{2})", text
    )
    if not m:
        raise ValueError(f"heure invalide : {value!r} (format HH:MM)")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        raise ValueError(f"heure invalide : {value!r}")
    return f"{hour:02d}:{minute:02d}"


def parse_date(value: Any, today: date) -> str | None:
    """'YYYY-MM-DD', 'demain', "aujourd'hui", 'today', 'tomorrow' → ISO date (or None)."""
    if value is None or value == "":
        return None
    key = _ascii(str(value)).strip()
    if key in ("aujourd'hui", "aujourdhui", "today", "ce soir", "ce matin"):
        return today.isoformat()
    if key in ("demain", "tomorrow"):
        return (today + timedelta(days=1)).isoformat()
    if key in ("apres-demain", "apres demain"):
        return (today + timedelta(days=2)).isoformat()
    try:
        return date.fromisoformat(key).isoformat()
    except ValueError as exc:
        raise ValueError(f"date invalide : {value!r} (format AAAA-MM-JJ ou 'demain')") from exc


def fr_when(dt: datetime) -> str:
    """'mar 10/11 20:00' (French weekday, no locale needed)."""
    return f"{_DAY_LABELS[WEEKDAYS[dt.weekday()]]} {dt:%d/%m %H:%M}"


def slug(text: str, fallback: str = "routine") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", _ascii(text)).strip("_")[:30].strip("_")
    return s or fallback


def describe(r: Routine) -> str:
    if r.date is not None:
        when = f"le {r.date} à {r.at}"
    elif len(r.days) == 7:
        when = f"tous les jours à {r.at}"
    elif r.days == ["mon", "tue", "wed", "thu", "fri"]:
        when = f"en semaine à {r.at}"
    elif r.days == ["sat", "sun"]:
        when = f"le week-end à {r.at}"
    else:
        when = f"{', '.join(_DAY_LABELS[d] for d in r.days)} à {r.at}"
    if r.message is not None:
        what = f"rappel « {r.message} »"
    elif r.prompt is not None:
        what = f"demande « {r.prompt} »"
    else:
        assert r.check is not None
        what = f"vérification {'/'.join(r.check.domains)} ∈ {'/'.join(r.check.states)}"
    state = "" if r.enabled else " (désactivée)"
    return f"{r.name} : {what}, {when}{state}"


class RoutineError(Exception):
    pass


class RoutineManager:
    def __init__(
        self,
        store: ConfigStore,
        scheduler: RoutineScheduler,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.scheduler = scheduler
        self.clock = clock or scheduler.clock

    @property
    def routines(self) -> list[Routine]:
        return list(self.store.config.proactive.routines)

    def get(self, name: str) -> Routine | None:
        return next((r for r in self.routines if r.name == name), None)

    def today(self) -> date:
        return self.clock().astimezone(ZoneInfo(self.store.config.agent.timezone)).date()

    def _save(self, routines: list[Routine]) -> None:
        data = [
            r.model_dump(mode="json", exclude_none=True, exclude_defaults=True) for r in routines
        ]
        for d, r in zip(data, routines, strict=True):  # keep the essentials explicit
            d["name"], d["at"] = r.name, r.at
        try:
            self.store.update_section(["proactive", "routines"], data)
        except ConfigError as exc:
            raise RoutineError(str(exc)) from exc

    def build(self, args: dict[str, Any], *, chat_id: str | None, requester: str) -> Routine:
        for key, value in args.items():
            # text saved in config.yaml would be expanded from the environment on reload:
            # never let a conversation write ${VAR} (it could exfiltrate a secret)
            if contains_env_reference(json.dumps(value, ensure_ascii=False)):
                raise ValueError(f"'{key}' ne peut pas contenir '${{' (variables d'environnement)")
        what = _ascii(str(args.get("what") or "")).strip()
        text = str(args.get("text") or "").strip()
        fields: dict[str, Any] = {"at": parse_time(args.get("at", ""))}
        when_date = parse_date(args.get("date"), self.today())
        if when_date is not None:
            fields["date"] = when_date
        else:
            fields["days"] = parse_days(args.get("days"))
        if what in ("reminder", "rappel", "message"):
            if not text:
                raise ValueError("un rappel a besoin d'un texte")
            fields["message"] = text
        elif what in ("ask", "prompt", "demande", "question"):
            if not text:
                raise ValueError("une demande a besoin d'un texte")
            fields["prompt"] = text
            if args.get("tier") in ("gpu", "cpu", "cloud"):
                fields["tier"] = args["tier"]
        elif what in ("check", "verification", "verifier"):
            check: dict[str, Any] = {}
            if args.get("check_domains"):
                check["domains"] = [str(d) for d in args["check_domains"]]
            if args.get("check_states"):
                check["states"] = [str(s) for s in args["check_states"]]
            fields["check"] = check
            fields["only_if_found"] = bool(args.get("only_if_found", True))
        else:
            raise ValueError("what doit valoir reminder, ask ou check")
        if chat_id and chat_id.lstrip("-").isdigit():
            fields["chat_ids"] = [int(chat_id)]
        fields["created_by"] = requester
        base = slug(str(args.get("name") or text or what), fallback=what or "routine")
        name, i = base, 2
        existing = {r.name for r in self.routines}
        while name in existing:
            name, i = f"{base}_{i}", i + 1
        try:
            return Routine(name=name, **fields)
        except ValidationError as exc:
            raise ValueError("; ".join(e["msg"] for e in exc.errors())) from exc

    def add(self, routine: Routine) -> None:
        current = self.routines
        if len(current) >= self.store.config.proactive.max_routines:
            raise RoutineError("nombre maximal de routines atteint (proactive.max_routines)")
        if not self.store.config.proactive.enabled:
            try:
                self.store.update_section(["proactive", "enabled"], True)
            except ConfigError as exc:
                raise RoutineError(str(exc)) from exc
        self._save([*current, routine])

    def remove(self, name: str) -> bool:
        current = self.routines
        kept = [r for r in current if r.name != name]
        if len(kept) == len(current):
            return False
        self._save(kept)
        return True

    def set_enabled(self, name: str, enabled: bool) -> bool:
        current = self.routines
        if not any(r.name == name for r in current):
            return False
        self._save(
            [r.model_copy(update={"enabled": enabled}) if r.name == name else r for r in current]
        )
        return True

    def next_label(self, r: Routine) -> str:
        nxt = self.scheduler.next_run(r)
        if nxt is None or not r.enabled:
            return "—"
        tz = ZoneInfo(self.store.config.agent.timezone)
        return fr_when(nxt.astimezone(tz))

    # ---------------------------------------------------------- Telegram
    async def command(self, chat_id: str, arg: str) -> str:
        """/routine on|off|delete <name>  (explicit command: no confirmation)."""
        parts = arg.split()
        if len(parts) != 2 or parts[0] not in ("on", "off", "delete", "supprimer"):
            return (
                "Usage : /routine on|off|delete <nom>\n"
                "Pour en créer une, écris-la simplement, par ex. « rappelle-moi de sortir "
                "les poubelles tous les mardis à 20h »."
            )
        action, name = parts
        try:
            if action in ("delete", "supprimer"):
                ok = self.remove(name)
                return f"🗑️ Routine {name} supprimée." if ok else f"Routine inconnue : {name}"
            ok = self.set_enabled(name, action == "on")
        except RoutineError as exc:
            return f"❌ {exc}"
        if not ok:
            return f"Routine inconnue : {name}"
        if action == "on":
            return f"▶️ Routine {name} activée."
        return f"⏸️ Routine {name} désactivée."


def register_routine_tools(registry: ToolRegistry, manager: RoutineManager) -> None:
    async def create(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            routine = manager.build(args, chat_id=ctx.chat_id, requester=ctx.requester)
        except ValueError as exc:
            return ToolResult(False, error=str(exc))
        summary = describe(routine)
        if not await ctx.confirm(f"Créer la routine {summary} ?"):
            return ToolResult(False, error="creation declined by the user", outcome="declined")
        try:
            manager.add(routine)
        except RoutineError as exc:
            return ToolResult(False, error=str(exc))
        return ToolResult(True, data={"created": summary, "next_run": manager.next_label(routine)})

    async def list_(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(
            True,
            data=[
                {"routine": describe(r), "next_run": manager.next_label(r)}
                for r in manager.routines
            ],
        )

    async def delete(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = manager.get(str(args["name"]))
        if r is None:
            return ToolResult(False, error=f"unknown routine {args['name']!r}; call routine_list")
        if not await ctx.confirm(f"Supprimer la routine {describe(r)} ?"):
            return ToolResult(False, error="deletion declined by the user", outcome="declined")
        manager.remove(r.name)
        return ToolResult(True, data={"deleted": r.name})

    async def toggle(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        r = manager.get(str(args["name"]))
        if r is None:
            return ToolResult(False, error=f"unknown routine {args['name']!r}; call routine_list")
        enabled = bool(args["enabled"])
        verb = "Réactiver" if enabled else "Mettre en pause"
        if not await ctx.confirm(f"{verb} la routine {describe(r)} ?"):
            return ToolResult(False, error="declined by the user", outcome="declined")
        manager.set_enabled(r.name, enabled)
        return ToolResult(True, data={"name": r.name, "enabled": enabled})

    registry.register(
        Tool(
            "routine_create",
            "Schedule something at a time: a reminder (what=reminder, text sent as is), a "
            "request to yourself (what=ask, e.g. 'fais le point sur la maison') or a check of "
            "what is still on/open (what=check). Use date for a one-off (e.g. 'demain'), "
            "days for a repeating one. The user confirms before it is saved.",
            {
                "type": "object",
                "properties": {
                    "what": {"type": "string", "enum": ["reminder", "ask", "check"]},
                    "at": {"type": "string", "description": "Local time HH:MM, e.g. 23:00"},
                    "text": {"type": "string", "description": "Reminder text or request."},
                    "days": {
                        "type": "string",
                        "description": "e.g. 'tous', 'semaine', 'weekend', 'lun,mer,ven'.",
                    },
                    "date": {"type": "string", "description": "One-off: YYYY-MM-DD or 'demain'."},
                    "name": {"type": "string", "description": "Short id (optional)."},
                    "check_domains": {"type": "array", "items": {"type": "string"}},
                    "check_states": {"type": "array", "items": {"type": "string"}},
                    "tier": {"type": "string", "enum": ["gpu", "cpu", "cloud"]},
                },
                "required": ["what", "at"],
            },
            create,
            {"routines", "routine_edit"},
        )
    )
    registry.register(
        Tool(
            "routine_list",
            "List the scheduled routines and reminders with their next run.",
            {"type": "object", "properties": {}},
            list_,
            {"routines"},
        )
    )
    registry.register(
        Tool(
            "routine_delete",
            "Delete a routine by name (see routine_list). The user confirms.",
            {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            delete,
            {"routines", "routine_edit"},
        )
    )
    registry.register(
        Tool(
            "routine_set_enabled",
            "Pause (enabled=false) or resume (enabled=true) a routine. The user confirms.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}, "enabled": {"type": "boolean"}},
                "required": ["name", "enabled"],
            },
            toggle,
            {"routines", "routine_edit"},
        )
    )
