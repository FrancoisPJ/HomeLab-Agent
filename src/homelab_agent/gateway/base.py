"""Transport-independent gateway interfaces (Telegram today; WhatsApp/voice later)."""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from homelab_agent.config.models import Tier


@dataclass(slots=True)
class IncomingMessage:
    user_id: str
    chat_id: str
    text: str
    forced_tier: Tier | None = None
    origin: str = "user"  # "user" or "routine" (routines may not edit routines)


class StreamHandle(Protocol):
    async def update(self, text: str) -> None:
        """Show the partial answer (rate-limited by the implementation)."""

    async def finish(self, text: str) -> None:
        """Show the final answer."""


class Replier(Protocol):
    async def send(self, text: str) -> None:
        """Send a text message to the conversation."""

    def stream(self) -> StreamHandle:
        """Start a message that is filled in as the answer streams."""

    async def confirm(self, question: str) -> bool:
        """Ask a yes/no question; True only if the user says yes in time."""


ActionCallback = Callable[[str, "Replier"], Awaitable[str]]


@dataclass(slots=True)
class ActionButton:
    """A one-shot button under a notification. ``run(user_id, replier)`` returns a status text."""

    label: str
    run: ActionCallback


class Notifier(Protocol):
    """Sends out-of-band messages (finished jobs, budget alerts, home alerts)."""

    async def notify(self, chat_id: str, text: str) -> None:
        """Send a message to a conversation, outside of any request."""

    async def notify_actions(
        self, chat_id: str, text: str, actions: list[ActionButton], ttl_s: float
    ) -> None:
        """Send a message with one-shot buttons, valid for ttl_s seconds."""

    def replier_for(self, chat_id: str) -> Replier:
        """Replier that answers in the given conversation."""


class Gateway(abc.ABC):
    """A user-facing channel. Implementations must enforce their own allowlist."""

    name: str

    @abc.abstractmethod
    async def start(self) -> None:
        """Start receiving messages."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop receiving messages and release resources."""


@runtime_checkable
class ActionReplier(Protocol):
    """A replier that can also show one-shot buttons under a message."""

    async def send_actions(self, text: str, actions: list[ActionButton]) -> None:
        """Send a message with one-shot buttons under it."""


@runtime_checkable
class PhotoReplier(Protocol):
    """A replier that can show an image (camera snapshots). Telegram only for now."""

    async def send_photo(self, image: bytes, caption: str) -> None:
        """Send an image with a caption."""


class BufferedReplier:
    """Collects output in memory (used for background jobs and tests)."""

    def __init__(self, auto_confirm: bool = False) -> None:
        self.messages: list[str] = []
        self.auto_confirm = auto_confirm
        self.questions: list[str] = []
        self.actions: list[ActionButton] = []
        self.photos: list[tuple[bytes, str]] = []

    async def send_photo(self, image: bytes, caption: str) -> None:
        self.photos.append((image, caption))

    async def send_actions(self, text: str, actions: list[ActionButton]) -> None:
        self.messages.append(text)
        self.actions.extend(actions)

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
