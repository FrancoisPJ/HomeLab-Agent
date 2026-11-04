"""Transport-independent gateway interfaces (Telegram today; WhatsApp/voice later)."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Protocol

from homelab_agent.config.models import Tier


@dataclass(slots=True)
class IncomingMessage:
    user_id: str
    chat_id: str
    text: str
    forced_tier: Tier | None = None


class StreamHandle(Protocol):
    async def update(self, text: str) -> None:
        """Show the partial answer (rate-limited by the implementation)."""

    async def finish(self, text: str) -> None:
        """Show the final answer."""


class Replier(Protocol):
    async def send(self, text: str) -> None: ...

    def stream(self) -> StreamHandle: ...

    async def confirm(self, question: str) -> bool: ...


class Notifier(Protocol):
    """Sends out-of-band messages (finished jobs, budget alerts)."""

    async def notify(self, chat_id: str, text: str) -> None: ...

    def replier_for(self, chat_id: str) -> Replier: ...


class Gateway(abc.ABC):
    """A user-facing channel. Implementations must enforce their own allowlist."""

    name: str

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...


class BufferedReplier:
    """Collects output in memory (used for background jobs and tests)."""

    def __init__(self, auto_confirm: bool = False) -> None:
        self.messages: list[str] = []
        self.auto_confirm = auto_confirm
        self.questions: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)

    def stream(self) -> StreamHandle:
        outer = self

        class _H:
            partial: list[str] = []  # noqa: RUF012

            async def update(self, text: str) -> None:
                self.partial.append(text)

            async def finish(self, text: str) -> None:
                outer.messages.append(text)

        return _H()

    async def confirm(self, question: str) -> bool:
        self.questions.append(question)
        return self.auto_confirm
