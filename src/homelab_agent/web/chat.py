"""Web chat: a second gateway, in the panel, streamed with Server-Sent Events.

Each message sent from the page becomes a *turn*: the orchestrator runs it with a
``WebReplier`` whose output (messages, streamed edits, Yes/No questions) is pushed to the turn's
event stream. Out-of-band messages (finished CPU jobs, routine output for the web
conversation) go to an *inbox* stream every open page listens to.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from homelab_agent.gateway.base import (
    ActionButton,
    IncomingMessage,
    Notifier,
    Replier,
    StreamHandle,
)
from homelab_agent.gateway.commands import CommandTable

WEB_PREFIX = "web:"
WEB_CHAT = "web:panel"
WEB_USER = "panel"
Event = dict[str, Any]
Emit = Callable[[Event], None]


@dataclass
class Turn:
    id: str
    queue: asyncio.Queue[Event] = field(default_factory=asyncio.Queue)
    done: bool = False
    created: float = field(default_factory=time.monotonic)
    task: asyncio.Task[None] | None = None


class _WebStream:
    def __init__(self, replier: WebReplier) -> None:
        self.r = replier
        self.msg_id: int | None = None
        self._last = 0.0

    async def update(self, text: str) -> None:
        now = time.monotonic()
        if self.msg_id is not None and now - self._last < 0.1:
            return
        self._last = now
        self._show(text + " …")

    async def finish(self, text: str) -> None:
        self._show(text)

    def _show(self, text: str) -> None:
        if self.msg_id is None:
            self.msg_id = self.r.hub.next_id()
            self.r.emit({"type": "message", "id": self.msg_id, "text": text})
        else:
            self.r.emit({"type": "update", "id": self.msg_id, "text": text})


class WebReplier:
    def __init__(self, hub: WebChatHub, emit: Emit) -> None:
        self.hub = hub
        self.emit = emit

    async def send(self, text: str) -> None:
        self.emit({"type": "message", "id": self.hub.next_id(), "text": text})

    def stream(self) -> StreamHandle:
        return _WebStream(self)

    async def send_actions(self, text: str, actions: list[ActionButton]) -> None:
        buttons = []
        for action in actions:
            token = uuid.uuid4().hex[:12]
            self.hub.actions[token] = (action, time.monotonic() + 3600)
            buttons.append({"label": action.label, "token": token})
        self.emit({"type": "message", "id": self.hub.next_id(), "text": text, "actions": buttons})

    async def confirm(self, question: str) -> bool:
        token = uuid.uuid4().hex[:12]
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.hub.pending[token] = fut
        self.emit({"type": "confirm", "token": token, "question": question})
        try:
            return await asyncio.wait_for(fut, timeout=self.hub.confirm_timeout_s())
        except TimeoutError:
            expired = "⌛ Confirmation expirée."
            self.emit({"type": "message", "id": self.hub.next_id(), "text": expired})
            return False
        finally:
            self.hub.pending.pop(token, None)


class WebChatHub:
    def __init__(self, confirm_timeout_s: Callable[[], float] = lambda: 60.0) -> None:
        self.confirm_timeout_s = confirm_timeout_s
        self.turns: dict[str, Turn] = {}
        self.pending: dict[str, asyncio.Future[bool]] = {}
        self.actions: dict[str, tuple[ActionButton, float]] = {}
        self.inbox_subscribers: list[asyncio.Queue[Event]] = []
        self.inbox_backlog: list[Event] = []
        self._ids = itertools.count(1)

    def next_id(self) -> int:
        return next(self._ids)

    # ---------------------------------------------------------------- inbox
    def broadcast(self, event: Event) -> None:
        self.inbox_backlog = [*self.inbox_backlog, event][-50:]
        for q in list(self.inbox_subscribers):
            q.put_nowait(event)

    async def notify(self, chat_id: str, text: str) -> None:
        self.broadcast({"type": "message", "id": self.next_id(), "text": text})

    async def notify_actions(
        self, chat_id: str, text: str, actions: list[ActionButton], ttl_s: float
    ) -> None:
        buttons = []
        for action in actions:
            token = uuid.uuid4().hex[:12]
            self.actions[token] = (action, time.monotonic() + ttl_s)
            buttons.append({"label": action.label, "token": token})
        self.broadcast({"type": "message", "id": self.next_id(), "text": text, "actions": buttons})

    def replier_for(self, chat_id: str) -> Replier:
        return WebReplier(self, self.broadcast)

    async def run_action(self, token: str) -> str:
        entry = self.actions.pop(token, None)
        if entry is None or entry[1] < time.monotonic():
            return "Bouton expiré."
        return await entry[0].run(WEB_USER, WebReplier(self, self.broadcast))

    # ---------------------------------------------------------------- turns
    def start_turn(
        self,
        text: str,
        handler: Callable[[IncomingMessage, Replier], Awaitable[None]],
        commands: CommandTable | None = None,
    ) -> Turn:
        now = time.monotonic()
        self.turns = {k: t for k, t in self.turns.items() if now - t.created < 3600}
        turn = Turn(uuid.uuid4().hex[:12])
        self.turns[turn.id] = turn
        text = text.strip()
        table = commands if commands is not None else CommandTable()

        async def run() -> None:
            replier = WebReplier(self, turn.queue.put_nowait)
            try:
                # same /commands as on Telegram (/status, /routines, /cloud…)
                if not await table.handle(text, WEB_CHAT, WEB_USER, replier, handler):
                    await handler(IncomingMessage(WEB_USER, WEB_CHAT, text), replier)
            except Exception:
                turn.queue.put_nowait(
                    {"type": "message", "id": self.next_id(), "text": "❌ Erreur interne."}
                )
                raise
            finally:
                turn.done = True
                turn.queue.put_nowait({"type": "done"})

        turn.task = asyncio.create_task(run(), name=f"webchat-{turn.id}")
        return turn

    def answer(self, token: str, yes: bool) -> bool:
        fut = self.pending.get(token)
        if fut is None or fut.done():
            return False
        fut.set_result(yes)
        return True


def sse(event: Event) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def turn_events(turn: Turn) -> AsyncIterator[str]:
    while True:
        try:
            event = await asyncio.wait_for(turn.queue.get(), timeout=15)
        except TimeoutError:
            yield ": keep-alive\n\n"
            continue
        yield sse(event)
        if event["type"] == "done":
            return


async def inbox_events(hub: WebChatHub) -> AsyncIterator[str]:
    q: asyncio.Queue[Event] = asyncio.Queue()
    hub.inbox_subscribers.append(q)
    try:
        yield ": connected\n\n"
        for event in hub.inbox_backlog:  # the page dedupes by id after a reconnect
            yield sse(event)
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield sse(event)
    finally:
        hub.inbox_subscribers.remove(q)


class RoutingNotifier:
    """Sends notifications to the web chat for ``web:`` conversations, else to ``default``."""

    def __init__(self, default: Notifier, web: WebChatHub) -> None:
        self.default = default
        self.web = web

    def _pick(self, chat_id: str) -> Notifier:
        return self.web if chat_id.startswith(WEB_PREFIX) else self.default

    async def notify(self, chat_id: str, text: str) -> None:
        await self._pick(chat_id).notify(chat_id, text)

    async def notify_actions(
        self, chat_id: str, text: str, actions: list[ActionButton], ttl_s: float
    ) -> None:
        await self._pick(chat_id).notify_actions(chat_id, text, actions, ttl_s)

    def replier_for(self, chat_id: str) -> Replier:
        return self._pick(chat_id).replier_for(chat_id)
