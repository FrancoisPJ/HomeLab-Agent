"""Slash commands: the shared table and the agent's replies."""

from __future__ import annotations

from pathlib import Path

import pytest

from homelab_agent.app import Services
from homelab_agent.commands import RESET_DONE, AgentCommands
from homelab_agent.config.models import Tier
from homelab_agent.gateway.base import BufferedReplier, IncomingMessage, Replier
from homelab_agent.gateway.commands import Command, CommandTable, parse_command

from .test_web import services


# ------------------------------------------------------------------- table
def test_parse_command() -> None:
    assert parse_command("bonjour") is None
    p = parse_command("  /Status@MonBot   ")
    assert p is not None and (p.name, p.arg) == ("status", "")
    p = parse_command("/deep\nrédige  un poème")
    assert p is not None and (p.name, p.arg) == ("deep", "rédige  un poème")


async def test_table_dispatch() -> None:
    ran: list[tuple[str, str]] = []
    sent: list[IncomingMessage] = []

    async def echo(chat: str, arg: str) -> str:
        ran.append((chat, arg))
        return f"echo {arg}"

    async def silent(chat: str, arg: str) -> None:
        return None

    async def boom(chat: str, arg: str) -> str:
        raise RuntimeError("x")

    async def run(msg: IncomingMessage, replier: Replier) -> None:
        sent.append(msg)

    table = CommandTable(
        {
            "echo": Command("répète", echo),
            "chut": Command("rien", silent),
            "boom": Command("x", boom),
        }
    )
    r = BufferedReplier()
    assert not await table.handle("pas une commande", "c", "u", r, run)
    for text in ("/echo a b", "/chut", "/boom", "/nope", "/deep", "/cloud explique"):
        assert await table.handle(text, "c", "u", r, run)
    assert ran == [("c", "a b")]
    assert r.messages == [
        "echo a b",
        "❌ Erreur interne, voir les journaux.",
        "Commande inconnue. /help pour la liste.",
        "Usage : /deep <ta demande>",
    ]
    assert [(m.text, m.forced_tier, m.chat_id, m.user_id) for m in sent] == [
        ("explique", Tier.CLOUD, "c", "u")
    ]


def test_help_order_and_copies() -> None:
    async def f(chat: str, arg: str) -> str:
        return ""

    table = CommandTable({"zeta": Command("z", f), "status": Command("état", f)})
    telegram = table.copy()
    telegram.add("maison", "télécommande", f)
    lines = telegram.help_text().splitlines()
    assert lines[:4] == ["Commandes :", "/status – état", "/cloud <texte> – forcer le cloud",
                         "/deep <texte> – forcer le CPU (réponse différée)"]  # fmt: skip
    assert lines[4:6] == ["/maison – télécommande", "/zeta – z"]
    assert "maison" not in table.commands  # a gateway's own commands stay its own


# ------------------------------------------------------------- agent replies
@pytest.fixture
async def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Services:
    monkeypatch.setenv("PANEL_PASSWORD", "correct-horse-battery")
    return await services(tmp_path)


async def test_status_model_jobs_reset(svc: Services) -> None:
    cmds = AgentCommands(svc)
    status = await cmds.status()
    assert status.startswith("État des moteurs :")
    assert "Home Assistant" in status and "Cloud activé" in status
    svc.health.mark_down("gpu_fast")
    assert "🔴" in await cmds.status()
    model = await cmds.model()
    assert "• rapide : gpu_fast → fast" in model and "• lourd : cpu_heavy" in model
    assert await cmds.jobs("chat-1") == "Aucune tâche."
    await svc.db.enqueue_job("chat-1", "u", "rédige un rapport " * 10)
    jobs = await cmds.jobs("chat-1")
    assert jobs.startswith("⏳ #") and len(jobs) < 100
    await svc.db.add_message("chat-1", "user", "salut")
    assert await cmds.reset("chat-1") == RESET_DONE
    assert await svc.db.history("chat-1", 10) == []


async def test_routine_commands(svc: Services) -> None:
    cmds = AgentCommands(svc)
    listing = await cmds.routines("c")
    assert "• soir" in listing and "/run <nom>" in listing
    assert (await cmds.run_routine("c", "inconnue")).startswith("Usage")
    # a check routine notifies by itself; the command only acknowledges
    assert await cmds.run_routine("c", "soir") in ("✔️", "✅ soir : rien à signaler.")
    assert "désactivée" in await cmds.routine("c", "off soir")
    svc.routine_manager = None
    assert "indisponibles" in await cmds.routines("c")
    assert "indisponibles" in await cmds.routine("c", "on soir")


async def test_alerts_memory_undo(svc: Services) -> None:
    cmds = AgentCommands(svc)
    alerts = await cmds.alerts()
    assert alerts.startswith("Alertes configurées : 2") and "fuite (critical)" in alerts
    assert "rien à annuler" in await cmds.undo("c")
    assert "rien mémorisé" in await cmds.memory_list("c")
    assert await cmds.memory_forget("c", "") != ""
    svc.alerts = None
    svc.ha = None
    svc.orchestrator.memory = None
    assert "désactivées" in await cmds.alerts()
    assert "pas configuré" in await cmds.undo("c")
    assert "indisponible" in await cmds.memory_list("c")
    assert "indisponible" in await cmds.memory_forget("c", "1")


async def test_every_command_is_listed(svc: Services) -> None:
    help_text = AgentCommands(svc).table().help_text()
    for name in ("status", "model", "jobs", "reset", "routines", "run", "routine", "annuler",
                 "memoire", "oublie", "alerts", "cloud", "deep"):  # fmt: skip
        assert f"/{name}" in help_text, name
