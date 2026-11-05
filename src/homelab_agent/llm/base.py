"""Backend-independent chat interface: messages, tools, streaming, token usage."""

from __future__ import annotations

import abc
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from homelab_agent.config.models import Tier

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def arguments_json(self) -> str:
        return json.dumps(self.arguments, ensure_ascii=False)


@dataclass(slots=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # tool name for role="tool"


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )


@dataclass(slots=True)
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str | None = None


@dataclass(slots=True)
class StreamDelta:
    """A piece of text produced while streaming."""

    text: str


@dataclass(slots=True)
class StreamDone:
    """End of stream, with the full aggregated result."""

    result: ChatResult


StreamEvent = StreamDelta | StreamDone


class BackendError(Exception):
    """The backend answered with an error."""


class BackendUnavailable(BackendError):
    """The backend cannot be reached (connection refused, DNS, 5xx, overloaded...)."""


class BackendTimeout(BackendUnavailable):
    """The backend did not answer in time."""


class ContextOverflow(BackendError):
    """The prompt does not fit in the backend's context window (the backend itself is fine)."""


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) when the backend reports nothing."""
    return max(1, len(text) // 4) if text else 0


def budget_tokens(text: str) -> int:
    """Conservative estimate used to fit prompts in a context window (~3 chars/token,
    French and JSON tokenize worse than English prose)."""
    return len(text) // 3 + 1


def estimate_messages_tokens(messages: list[Message]) -> int:
    return sum(estimate_tokens(m.content) + 4 for m in messages)


class LLMBackend(abc.ABC):
    """Common interface implemented by every engine client."""

    #: context window in tokens, when known (local engines); None = large enough
    max_context: int | None = None
    max_output_tokens: int = 1024

    def __init__(self, name: str, tier: Tier, model: str) -> None:
        self.name = name
        self.tier = tier
        self.model = model

    @property
    def provider(self) -> str:
        """Key used for pricing/budget (the backend name)."""
        return self.name

    @abc.abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResult: ...

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Default: no real streaming, yield the full answer at once."""
        result = await self.chat(messages, tools, max_tokens=max_tokens)
        if result.content:
            yield StreamDelta(result.content)
        yield StreamDone(result)

    @abc.abstractmethod
    async def health(self) -> bool: ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None
