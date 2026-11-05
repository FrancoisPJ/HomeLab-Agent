"""Claude client built on the official ``anthropic`` SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from homelab_agent.config.models import AnthropicBackend
from homelab_agent.llm.base import (
    BackendError,
    BackendTimeout,
    BackendUnavailable,
    ChatResult,
    ContextOverflow,
    LLMBackend,
    Message,
    StreamDelta,
    StreamDone,
    StreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
)


def to_anthropic(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Split the system prompt out and convert tool turns into content blocks."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
            continue
        if m.role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
            # consecutive tool results must live in a single user turn
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if m.role == "assistant" and m.tool_calls:
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            blocks += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in m.tool_calls
            ]
            out.append({"role": "assistant", "content": blocks})
            continue
        out.append({"role": m.role, "content": m.content})
    return "\n\n".join(system_parts), out


def to_anthropic_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
    ]


class AnthropicClient(LLMBackend):
    def __init__(
        self,
        name: str,
        cfg: AnthropicBackend,
        api_key: str | None,
        http_client: Any | None = None,
    ) -> None:
        super().__init__(name, cfg.tier, cfg.model)
        self.cfg = cfg
        self.max_output_tokens = cfg.max_output_tokens
        self.has_key = bool(api_key)
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or "missing",
            base_url=cfg.base_url,
            timeout=cfg.timeout_s,
            max_retries=1,
            http_client=http_client,
        )

    def _params(
        self, messages: list[Message], tools: list[ToolSpec] | None, max_tokens: int | None
    ) -> dict[str, Any]:
        system, msgs = to_anthropic(messages)
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.cfg.max_output_tokens,
            "messages": msgs,
        }  # sampling parameters are not accepted by the current Messages API
        if system:
            params["system"] = system
        if tools:
            params["tools"] = to_anthropic_tools(tools)
        return params

    @staticmethod
    def _wrap(exc: Exception, name: str) -> BackendError:
        if isinstance(exc, anthropic.APITimeoutError):
            return BackendTimeout(f"{name}: timeout")
        if isinstance(exc, anthropic.APIConnectionError | anthropic.RateLimitError):
            return BackendUnavailable(f"{name}: {exc.__class__.__name__}")
        if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
            return BackendUnavailable(f"{name}: HTTP {exc.status_code}")
        if isinstance(exc, anthropic.BadRequestError) and "too long" in str(exc).lower():
            return ContextOverflow(f"{name}: prompt exceeds the context window")
        return BackendError(f"{name}: {exc.__class__.__name__}")

    @staticmethod
    def _result(msg: Any) -> ChatResult:
        text: list[str] = []
        calls: list[ToolCall] = []
        for block in msg.content:
            if block.type == "text":
                text.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input or {})))
        return ChatResult(
            content="".join(text),
            tool_calls=calls,
            usage=Usage(msg.usage.input_tokens, msg.usage.output_tokens),
            model=msg.model,
            finish_reason=msg.stop_reason,
        )

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResult:
        if not self.has_key:
            raise BackendUnavailable(f"{self.name}: no API key configured")
        try:
            msg = await self._client.messages.create(**self._params(messages, tools, max_tokens))
        except anthropic.AnthropicError as exc:
            raise self._wrap(exc, self.name) from exc
        return self._result(msg)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if not self.has_key:
            raise BackendUnavailable(f"{self.name}: no API key configured")
        try:
            async with self._client.messages.stream(
                **self._params(messages, tools, max_tokens)
            ) as stream:
                async for text in stream.text_stream:
                    yield StreamDelta(text)
                final = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise self._wrap(exc, self.name) from exc
        yield StreamDone(self._result(final))

    async def health(self) -> bool:
        return self.has_key

    async def aclose(self) -> None:
        await self._client.close()
