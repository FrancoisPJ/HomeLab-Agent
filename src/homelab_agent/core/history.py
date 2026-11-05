"""Conversation history as the model sees it, and the guard against invented actions.

Actions done in past turns are replayed as real tool calls (assistant ``tool_calls`` + ``tool``
results), never as free text: when they were written as text (``[actions effectuées : …]``),
small models copied that text instead of calling the tool, and claimed actions they never did.
"""

from __future__ import annotations

import json
import re
from typing import Any

from homelab_agent.llm.base import Message, ToolCall

#: suffix written in the history by versions ≤ 0.5.1, and copied by the models
FAKE_ACTIONS_RE = re.compile(r"\s*\[\s*actions?\s+effectu[ée]es?\s*:[^\]]*(?:\]|$)", re.IGNORECASE)

_DONE = (
    r"allum|[ée]tein|baiss|r[ée]dui|augment|mont[ée]|ferm|ouvert|r[ée]gl|mis\b|mise|lanc|"
    r"activ|d[ée]sactiv|verrouill|d[ée]verrouill|coup[ée]|arr[êe]t|d[ée]marr|tamis"
)
#: a reply that says the house was changed (first person, passive or "c'est fait")
CLAIM_RE = re.compile(
    rf"\bc['’]est fait\b"
    rf"|\bj['’]ai (?:bien |d[ée]j[àa] )?(?:{_DONE})"
    rf"|\b(?:a|ont) (?:bien )?[ée]t[ée] (?:{_DONE})"
    rf"|\b(?:est|sont) (?:maintenant|d[ée]sormais) (?:{_DONE})",
    re.IGNORECASE,
)

#: sent (as a user turn, never stored) when the model claims an action it did not do
NO_ACTION_NUDGE = (
    "[Système] Tu n'as appelé aucun outil dans ce tour : aucune action n'a été faite. Si "
    "l'utilisateur demande une action, appelle l'outil maintenant (ha_call_service…). Sinon, "
    "réponds sans prétendre avoir agi."
)
NO_ACTION_REPLY = (
    "⚠️ Je n'ai rien fait : aucune action n'a été exécutée. Redis-moi ce que tu veux que je fasse."
)

#: size kept of a tool result in the history
RESULT_MAX_CHARS = 500


def strip_fake_actions(text: str) -> str:
    return FAKE_ACTIONS_RE.sub("", text).strip()


def claims_action(text: str) -> bool:
    return bool(FAKE_ACTIONS_RE.search(text) or CLAIM_RE.search(text))


def action_record(name: str, arguments: dict[str, Any], result: str) -> dict[str, Any]:
    """What is stored for one action really executed (column ``chat_messages.tool_calls``)."""
    return {"name": name, "arguments": arguments, "result": result[:RESULT_MAX_CHARS]}


def replay(
    row_id: int,
    role: str,
    content: str,
    actions: list[dict[str, Any]] | None,
    offered: set[str] | None = None,
) -> list[Message]:
    """Messages of one stored row; actions as real tool calls when the tools are offered.

    A backend may refuse tool calls to tools it was not given (Anthropic does): when one of the
    tools is not offered (``offered`` set), the actions are left out and only the text remains.
    """
    text = strip_fake_actions(content) if role == "assistant" else content
    if role != "assistant" or not actions:
        return [Message("user" if role == "user" else "assistant", text)]
    calls = [
        ToolCall(f"h{row_id}_{i}", str(a.get("name", "")), dict(a.get("arguments") or {}))
        for i, a in enumerate(actions)
    ]
    if any(not c.name for c in calls) or (
        offered is not None and any(c.name not in offered for c in calls)
    ):
        return [Message("assistant", text)]
    out = [Message("assistant", "", tool_calls=calls)]
    for call, action in zip(calls, actions, strict=True):
        result = action.get("result") or json.dumps({"ok": True})
        out.append(Message("tool", str(result), tool_call_id=call.id, name=call.name))
    out.append(Message("assistant", text or "C'est fait."))
    return out
