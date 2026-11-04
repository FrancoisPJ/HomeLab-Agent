"""Gemini client built on the official ``google-genai`` SDK."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from homelab_agent.config.models import GeminiBackend
from homelab_agent.llm.base import (
    BackendError,
    BackendUnavailable,
    ChatResult,
    LLMBackend,
    Message,
    StreamDelta,
    StreamDone,
    StreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
)


def to_gemini(messages: list[Message]) -> tuple[str, list[types.Content]]:
    system: list[str] = []
    contents: list[types.Content] = []
    for m in messages:
        if m.role == "system":
            system.append(m.content)
        elif m.role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=m.content)]))
        elif m.role == "assistant":
            parts: list[types.Part] = []
            if m.content:
                parts.append(types.Part(text=m.content))
            parts += [
                types.Part(function_call=types.FunctionCall(name=c.name, args=c.arguments))
                for c in m.tool_calls
            ]
            contents.append(types.Content(role="model", parts=parts))
        elif m.role == "tool":
            part = types.Part(
                function_response=types.FunctionResponse(
                    name=m.name or "tool", response={"result": m.content}
                )
            )
            if (
                contents
                and contents[-1].role == "user"
                and contents[-1].parts
                and all(p.function_response for p in contents[-1].parts)
            ):
                contents[-1].parts.append(part)
            else:
                contents.append(types.Content(role="user", parts=[part]))
    return "\n\n".join(system), contents


def to_gemini_tools(tools: list[ToolSpec]) -> list[types.Tool]:
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=t.name, description=t.description, parameters_json_schema=t.parameters
                )
                for t in tools
            ]
        )
    ]


class GeminiClient(LLMBackend):
    def __init__(
        self, name: str, cfg: GeminiBackend, api_key: str | None, client: Any | None = None
    ) -> None:
        super().__init__(name, cfg.tier, cfg.model)
        self.cfg = cfg
        self.has_key = bool(api_key) or client is not None
        self._client: Any = client
        if self._client is None and api_key:
            self._client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=int(cfg.timeout_s * 1000)),
            )

    def _config(self, system: str, tools: list[ToolSpec] | None, max_tokens: int | None) -> Any:
        return types.GenerateContentConfig(
            system_instruction=system or None,
            tools=to_gemini_tools(tools) if tools else None,
            max_output_tokens=max_tokens or self.cfg.max_output_tokens,
            temperature=self.cfg.temperature,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    @staticmethod
    def _wrap(exc: Exception, name: str) -> BackendError:
        code = getattr(exc, "code", None)
        if isinstance(exc, genai_errors.ServerError) or code in (429, 503):
            return BackendUnavailable(f"{name}: HTTP {code}")
        return BackendError(f"{name}: {exc.__class__.__name__}")

    def _result(self, resp: Any) -> ChatResult:
        text: list[str] = []
        calls: list[ToolCall] = []
        finish = None
        if resp.candidates:
            cand = resp.candidates[0]
            finish = str(cand.finish_reason) if cand.finish_reason else None
            for part in (cand.content.parts if cand.content else None) or []:
                if part.text:
                    text.append(part.text)
                if part.function_call:
                    fc = part.function_call
                    calls.append(
                        ToolCall(
                            fc.id or f"call_{uuid.uuid4().hex[:8]}", fc.name, dict(fc.args or {})
                        )
                    )
        um = resp.usage_metadata
        usage = Usage(
            (um.prompt_token_count or 0) if um else 0,
            (um.candidates_token_count or 0) if um else 0,
        )
        return ChatResult("".join(text), calls, usage, self.model, finish)

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResult:
        if not self.has_key:
            raise BackendUnavailable(f"{self.name}: no API key configured")
        system, contents = to_gemini(messages)
        try:
            resp = await self._client.aio.models.generate_content(
                model=self.model, contents=contents, config=self._config(system, tools, max_tokens)
            )
        except genai_errors.APIError as exc:
            raise self._wrap(exc, self.name) from exc
        return self._result(resp)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if not self.has_key:
            raise BackendUnavailable(f"{self.name}: no API key configured")
        system, contents = to_gemini(messages)
        text: list[str] = []
        calls: list[ToolCall] = []
        usage = Usage()
        try:
            it = await self._client.aio.models.generate_content_stream(
                model=self.model, contents=contents, config=self._config(system, tools, max_tokens)
            )
            async for chunk in it:
                part_result = self._result(chunk)
                if part_result.content:
                    text.append(part_result.content)
                    yield StreamDelta(part_result.content)
                calls += part_result.tool_calls
                if part_result.usage.input_tokens or part_result.usage.output_tokens:
                    usage = part_result.usage
        except genai_errors.APIError as exc:
            raise self._wrap(exc, self.name) from exc
        yield StreamDone(ChatResult("".join(text), calls, usage, self.model, None))

    async def health(self) -> bool:
        return self.has_key
