"""Undo the last Home Assistant action of a conversation.

Before an action runs, the previous state of every reversible target is noted. Undoing replays
the opposite services through the normal enforcement point (permissions, confirmations, audit).
Only on/off-like states are restored (brightness, colour or positions are not).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

REVERSIBLE = {"light", "switch", "fan", "input_boolean", "media_player", "cover", "lock"}
UNDO_TTL_S = 3600
MAX_PER_CHAT = 10


def restore_service(domain: str, previous_state: str) -> str | None:
    """Service that brings an entity back to ``previous_state`` (None: cannot)."""
    if domain in ("light", "switch", "fan", "input_boolean", "media_player"):
        if previous_state == "off":
            return "turn_off"
        if previous_state in ("on", "playing", "paused", "idle"):
            return "turn_on"
        return None
    if domain == "cover":
        if previous_state in ("open", "opening"):
            return "open_cover"
        if previous_state in ("closed", "closing"):
            return "close_cover"
        return None
    if domain == "lock":
        return {"locked": "lock", "unlocked": "unlock"}.get(previous_state)
    return None


@dataclass(slots=True)
class UndoRecord:
    description: str  # e.g. "light.turn_off"
    previous: dict[str, str]  # entity_id -> state before the action
    at: float


class UndoStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._records: dict[str, list[UndoRecord]] = {}

    def push(self, chat_id: str, record: UndoRecord) -> None:
        stack = [*self._records.get(chat_id, []), record][-MAX_PER_CHAT:]
        self._records[chat_id] = stack

    def pop(self, chat_id: str) -> UndoRecord | None:
        stack = self._records.get(chat_id, [])
        while stack:
            record = stack.pop()
            if self.clock() - record.at <= UNDO_TTL_S:
                return record
        return None

    def peek(self, chat_id: str) -> UndoRecord | None:
        stack = self._records.get(chat_id, [])
        if stack and self.clock() - stack[-1].at <= UNDO_TTL_S:
            return stack[-1]
        return None
