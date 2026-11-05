"""Client for OpenAI-compatible servers (llama-server, llama-swap, fake-llm)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from homelab_agent.config.models import OpenAICompatBackend, Tier
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
    estimate_messages_tokens,
    estimate_tokens,
)


def to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        item: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.role == "assistant" and m.tool_calls:
            item["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments_json()},
                }
                for c in m.tool_calls
            ]
        if m.role == "tool":
            item["tool_call_id"] = m.tool_call_id
            if m.name:
                item["name"] = m.name
        out.append(item)
    return out


def to_openai_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


class OpenAICompatClient(LLMBackend):
    def __init__(
        self,
        name: str,
        cfg: OpenAICompatBackend,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(name, cfg.tier, cfg.model)
        self.cfg = cfg
        self.max_context = cfg.max_context
        self.max_output_tokens = cfg.max_output_tokens
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=cfg.url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(cfg.timeout_s, connect=5.0),
            transport=transport,
        )

    @property
    def tier_is_local(self) -> bool:
        return self.tier in (Tier.GPU, Tier.CPU)

    def _payload(
        self, messages: list[Message], tools: list[ToolSpec] | None, max_tokens: int | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "max_tokens": max_tokens or self.cfg.max_output_tokens,
            "temperature": self.cfg.temperature,
        }
        if tools:
            payload["tools"] = to_openai_tools(tools)
            payload["tool_choice"] = "auto"
        return payload

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            resp = await self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise BackendTimeout(f"{self.name}: timeout") from exc
        except httpx.TransportError as exc:
            raise BackendUnavailable(f"{self.name}: {exc.__class__.__name__}") from exc
        self._raise_for_status(resp)
        return resp

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code >= 500 or resp.status_code in (408, 429):
            raise BackendUnavailable(f"{self.name}: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            body = resp.text[:300]
            low = body.lower()
            if resp.status_code in (400, 413) and (
                "context" in low or "too long" in low or "too many tokens" in low
            ):
                raise ContextOverflow(f"{self.name}: prompt exceeds the context window")
            raise BackendError(f"{self.name}: HTTP {resp.status_code}: {body[:200]}")

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> ChatResult:
        resp = await self._post(self._payload(messages, tools, max_tokens))
        data = resp.json()
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError) as exc:
            raise BackendError(f"{self.name}: malformed response") from exc
        calls = [
            ToolCall(
                id=c.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                name=c["function"]["name"],
                arguments=_parse_args(c["function"].get("arguments")),
            )
            for c in msg.get("tool_calls") or []
        ]
        content = msg.get("content") or ""
        u = data.get("usage") or {}
        usage = Usage(
            u.get("prompt_tokens") or estimate_messages_tokens(messages),
            u.get("completion_tokens") or estimate_tokens(content),
        )
        return ChatResult(
            content=content,
            tool_calls=calls,
            usage=usage,
            model=data.get("model", self.model),
            finish_reason=choice.get("finish_reason"),
        )

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        payload = self._payload(messages, tools, max_tokens)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        text_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage = Usage()
        finish: str | None = None
        model = self.model
        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    self._raise_for_status(resp)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    model = chunk.get("model", model)
                    if chunk.get("usage"):
                        u = chunk["usage"]
                        usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            text_parts.append(delta["content"])
                            yield StreamDelta(delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(
                                tc.get("index", 0), {"id": None, "name": "", "args": ""}
                            )
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
                        finish = choice.get("finish_reason") or finish
        except httpx.TimeoutException as exc:
            raise BackendTimeout(f"{self.name}: timeout") from exc
        except httpx.TransportError as exc:
            raise BackendUnavailable(f"{self.name}: {exc.__class__.__name__}") from exc
        content = "".join(text_parts)
        if not usage.input_tokens:
            usage = Usage(estimate_messages_tokens(messages), estimate_tokens(content))
        tool_calls = [
            ToolCall(
                id=s["id"] or f"call_{uuid.uuid4().hex[:8]}",
                name=s["name"],
                arguments=_parse_args(s["args"]),
            )
            for _, s in sorted(calls.items())
        ]
        yield StreamDone(ChatResult(content, tool_calls, usage, model, finish))

    async def health(self) -> bool:
        # llama-server exposes /health at the root, llama-swap and others /v1/models.
        try:
            resp = await self._client.get("/models", timeout=5.0)
        except httpx.HTTPError:
            return False
        return resp.status_code < 500

    async def aclose(self) -> None:
        await self._client.aclose()
