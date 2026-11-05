"""Slash commands shared by every gateway (Telegram, web chat).

A gateway only has to call :meth:`CommandTable.handle` before handing plain text to the
agent: parsing (``/cmd@bot arg``), help, forced-tier prefixes (``/cloud``, ``/deep``),
unknown commands and failing commands are handled here, the same way everywhere.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homelab_agent.config.models import Tier
from homelab_agent.gateway.base import IncomingMessage, Replier

log = logging.getLogger(__name__)

#: (chat_id, argument) -> reply text, or None when the command already replied by itself
CommandFn = Callable[[str, str], Awaitable[str | None]]
#: what a gateway does with a message for the agent
MessageRunner = Callable[[IncomingMessage, Replier], Awaitable[None]]

#: listed first in the help, in this order; the others follow alphabetically
CORE_ORDER = ("status", "model", "cloud", "deep", "jobs", "reset")
UNKNOWN = "Commande inconnue. /help pour la liste."
FAILED = "❌ Erreur interne, voir les journaux."


@dataclass(frozen=True, slots=True)
class Command:
    description: str
    run: CommandFn | None = None
    #: forced-tier prefix: the argument goes to the agent on this tier
    tier: Tier | None = None
    usage: str = ""


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    name: str
    arg: str


def parse_command(text: str) -> ParsedCommand | None:
    """``/Cmd@bot  argument`` -> ("cmd", "argument"); None for plain text."""
    text = text.strip()
    if not text.startswith("/"):
        return None
    head, *rest = text.split(maxsplit=1)
    return ParsedCommand(head[1:].split("@", 1)[0].lower(), rest[0].strip() if rest else "")


def tier_commands() -> dict[str, Command]:
    return {
        "cloud": Command("forcer le cloud", tier=Tier.CLOUD, usage="<texte>"),
        "deep": Command("forcer le CPU (réponse différée)", tier=Tier.CPU, usage="<texte>"),
    }


class CommandTable:
    def __init__(self, commands: dict[str, Command] | None = None) -> None:
        self.commands: dict[str, Command] = {**tier_commands(), **(commands or {})}

    def add(self, name: str, description: str, run: CommandFn) -> None:
        self.commands[name] = Command(description, run)

    def copy(self) -> CommandTable:
        """Independent table: a gateway can add its own commands (e.g. /maison)."""
        return CommandTable(dict(self.commands))

    def help_text(self) -> str:
        core = [n for n in CORE_ORDER if n in self.commands]
        names = core + sorted(n for n in self.commands if n not in CORE_ORDER)
        lines = ["Commandes :"]
        for name in names:
            cmd = self.commands[name]
            usage = f" {cmd.usage}" if cmd.usage else ""
            lines.append(f"/{name}{usage} – {cmd.description}")
        lines += ["/help – cette aide", "Sinon, écris simplement ta demande."]
        return "\n".join(lines)

    async def handle(
        self, text: str, chat_id: str, user_id: str, replier: Replier, run: MessageRunner
    ) -> bool:
        """Run ``text`` if it is a command; False means plain text for the agent."""
        parsed = parse_command(text)
        if parsed is None:
            return False
        if parsed.name in ("start", "help"):
            await replier.send(self.help_text())
            return True
        cmd = self.commands.get(parsed.name)
        if cmd is None:
            await replier.send(UNKNOWN)
        elif cmd.tier is not None:
            if not parsed.arg:
                await replier.send(f"Usage : /{parsed.name} <ta demande>")
            else:
                await run(IncomingMessage(user_id, chat_id, parsed.arg, cmd.tier), replier)
        elif cmd.run is not None:
            try:
                reply = await cmd.run(chat_id, parsed.arg)
            except Exception:
                log.exception("command /%s failed", parsed.name)
                reply = FAILED
            if reply is not None:
                await replier.send(reply)
        return True
