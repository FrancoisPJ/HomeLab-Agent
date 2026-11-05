"""The agent's chat commands (/status, /routines, /memoire…), the same for every gateway."""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from homelab_agent.config.models import Tier
from homelab_agent.core.router import TIER_LABELS
from homelab_agent.core.routines import describe
from homelab_agent.gateway.commands import Command, CommandTable
from homelab_agent.tools.registry import ToolContext

if TYPE_CHECKING:
    from homelab_agent.app import Services

RESET_DONE = "🧹 Contexte effacé."
JOB_ICONS = {"queued": "⏳", "running": "⚙️", "done": "✅", "failed": "❌"}


class AgentCommands:
    """Text replies built from the running services (read at call time, never cached)."""

    def __init__(self, services: Services) -> None:
        self.s = services

    def table(self) -> CommandTable:
        table = CommandTable(
            {
                "status": Command("état des moteurs et du budget", self.status),
                "model": Command("moteurs et modèles configurés", self.model),
                "jobs": Command("tâches en cours", self.jobs),
                "reset": Command("oublier la conversation", self.reset),
                "routines": Command("routines planifiées", self.routines),
                "run": Command("lancer une routine maintenant", self.run_routine),
                "routine": Command("activer / désactiver / supprimer une routine", self.routine),
                "annuler": Command("annuler la dernière action sur la maison", self.undo),
                "memoire": Command("ce que je retiens de toi", self.memory_list),
                "oublie": Command("oublier une note (/oublie <n°> ou tout)", self.memory_forget),
                "alerts": Command("alertes et derniers déclenchements", self.alerts),
            }
        )
        if self.s.cameras is not None:  # photos only show on Telegram; it says so elsewhere
            table.add("camera", "photo des caméras d'une pièce", self.s.cameras.command)
        return table

    # ------------------------------------------------------------- engines
    async def status(self, chat_id: str = "", arg: str = "") -> str:
        s, cfg = self.s, self.s.cfg
        lines = ["État des moteurs :"]
        for tier in Tier:
            for name in cfg.backends_for(tier):
                b = s.backends.get(name)
                icon = "🟢" if s.health.available(name) else "🔴"
                lines.append(f"{icon} {TIER_LABELS[tier]} · {name} ({b.model if b else '?'})")
        if s.ha is not None:
            ha_ok = s.ha.client.connected or bool(s.ha.client.cache.entities)
            lines.append(f"{'🟢' if ha_ok else '🔴'} Home Assistant")
        day, month = await s.budget.totals()
        lim = cfg.cloud.limits
        state = "activé" if cfg.cloud.enabled else "désactivé"
        lines.append(
            f"\nCloud {state} · aujourd'hui {day:.2f} € / {lim.daily_eur:.2f} € · "
            f"mois {month:.2f} € / {lim.monthly_eur:.2f} €"
        )
        if s.stt is not None and cfg.voice.enabled:
            ok = await s.stt.health()
            lines.append(f"{'🟢' if ok else '🔴'} Voix (transcription)")
        if s.jobs is not None:
            active = await s.db.list_jobs(active_only=True)
            lines.append(f"Tâches CPU actives : {len(active)}")
        return "\n".join(lines)

    async def model(self, chat_id: str = "", arg: str = "") -> str:
        lines = ["Moteurs par niveau :"]
        for tier in Tier:
            names = self.s.cfg.backends_for(tier)
            desc = ", ".join(
                f"{n} → {self.s.backends[n].model}" for n in names if n in self.s.backends
            )
            lines.append(f"• {TIER_LABELS[tier]} : {desc or '—'}")
        return "\n".join(lines)

    async def jobs(self, chat_id: str, arg: str = "") -> str:
        jobs = [j for j in await self.s.db.list_jobs(limit=20) if j.chat_id == chat_id][:10]
        if not jobs:
            return "Aucune tâche."
        return "\n".join(
            f"{JOB_ICONS.get(j.status, '?')} #{j.id} {j.status} — {j.prompt[:60]}" for j in jobs
        )

    async def reset(self, chat_id: str, arg: str = "") -> str:
        await self.s.db.clear_history(chat_id)
        return RESET_DONE

    # ------------------------------------------------------------- routines
    async def routines(self, chat_id: str, arg: str = "") -> str:
        manager = self.s.routine_manager
        if manager is None:
            return "Les routines sont indisponibles."
        if not manager.routines:
            return (
                "Aucune routine. Écris par exemple : « rappelle-moi de sortir les poubelles "
                "tous les mardis à 20h » ou « tous les soirs à 23h, vérifie ce qui est allumé »."
            )
        lines = []
        if not self.s.cfg.proactive.routines_active:
            lines.append("⚠️ Routines désactivées (proactive.routines_enabled) : rien ne s'exécute.")
        lines.append("Routines :")
        lines += [
            f"• {describe(r)} — prochaine : {manager.next_label(r)}" for r in manager.routines
        ]
        lines.append("/run <nom> pour lancer, /routine on|off|delete <nom> pour gérer.")
        return "\n".join(lines)

    async def run_routine(self, chat_id: str, arg: str) -> str:
        scheduler = self.s.routines
        if scheduler is None:
            return "Les routines sont désactivées."
        routine = scheduler.get(arg.strip())
        if routine is None:
            return "Usage : /run <nom>. Voir /routines."
        sent = await scheduler.run(routine, [chat_id])
        if routine.check is not None and not sent:
            return f"✅ {routine.name} : rien à signaler."
        return f"▶️ {routine.name} lancée." if routine.prompt else "✔️"

    async def routine(self, chat_id: str, arg: str) -> str:
        if self.s.routine_manager is None:
            return "Les routines sont indisponibles."
        return await self.s.routine_manager.command(chat_id, arg)

    # ---------------------------------------------------------------- house
    async def undo(self, chat_id: str, arg: str = "") -> str:
        if self.s.ha is None:
            return "Home Assistant n'est pas configuré."
        replier = self.s.notifier.replier_for(chat_id)
        ctx = ToolContext.for_command(chat_id, replier.confirm)
        res = await self.s.ha.undo_last(ctx)
        if not res.ok:
            return f"↩️ {res.error}"
        lines = ["↩️ Annulé :"] + [f"• {r}" for r in res.data["restored"]]
        lines += [f"⚠️ {p}" for p in res.data["problems"]]
        return "\n".join(lines)

    async def alerts(self, chat_id: str = "", arg: str = "") -> str:
        cfg = self.s.cfg
        if self.s.alerts is None or not cfg.proactive.alerts_active:
            return "Les alertes sont désactivées (proactive.alerts_enabled / enabled)."
        rules = cfg.proactive.alerts
        lines = [f"Alertes configurées : {len(rules)}"]
        lines += [f"• {r.name} ({r.severity}) sur {r.entity}" for r in rules]
        recent = self.s.alerts.events[-5:]
        if recent:
            tz = ZoneInfo(cfg.agent.timezone)
            lines.append("Dernières :")
            lines += [
                f"• {e.at.astimezone(tz):%d/%m %H:%M} {e.alert} {e.entity_id} → {e.outcome}"
                for e in reversed(recent)
            ]
        return "\n".join(lines)

    # --------------------------------------------------------------- memory
    async def memory_list(self, chat_id: str, arg: str = "") -> str:
        memory = self.s.orchestrator.memory
        if memory is None:
            return "La mémoire est indisponible."
        return await memory.list_text(chat_id)

    async def memory_forget(self, chat_id: str, arg: str) -> str:
        memory = self.s.orchestrator.memory
        if memory is None:
            return "La mémoire est indisponible."
        return await memory.forget_command(chat_id, arg)
