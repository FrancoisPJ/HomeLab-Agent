"""Cloud clients tested with recorded/mocked responses — no real API key is ever used."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from google.genai import types

from homelab_agent.config.models import AnthropicBackend, GeminiBackend
from homelab_agent.llm.anthropic import AnthropicClient, to_anthropic
from homelab_agent.llm.base import BackendUnavailable, Message, StreamDone, ToolCall, ToolSpec
from homelab_agent.llm.gemini import GeminiClient, to_gemini

TOOLS = [
    ToolSpec(
        "ha_get_state", "read", {"type": "object", "properties": {"entity_id": {"type": "string"}}}
    )
]

ANTHROPIC_TOOL_RESPONSE = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "model": "claude-test",
    "content": [
        {"type": "text", "text": "Je regarde."},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "ha_get_state",
            "input": {"entity_id": "sensor.t"},
        },
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {"input_tokens": 120, "output_tokens": 30},
}


def anthropic_client(handler: Any) -> AnthropicClient:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return AnthropicClient(
        "claude", AnthropicBackend(model="claude-test"), "test-key", http_client=http
    )


async def test_anthropic_chat_with_tools() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.update(json.loads(request.content))
        return httpx2.Response(200, json=ANTHROPIC_TOOL_RESPONSE)

    c = anthropic_client(handler)
    res = await c.chat([Message("system", "sys"), Message("user", "température ?")], TOOLS)
    assert res.content == "Je regarde."
    assert res.tool_calls[0].name == "ha_get_state"
    assert res.tool_calls[0].arguments == {"entity_id": "sensor.t"}
    assert (res.usage.input_tokens, res.usage.output_tokens) == (120, 30)
    assert seen["system"] == "sys"
    assert seen["tools"][0]["input_schema"]["type"] == "object"


async def test_anthropic_errors() -> None:
    c = anthropic_client(
        lambda r: httpx2.Response(
            529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}}
        )
    )
    with pytest.raises(BackendUnavailable):
        await c.chat([Message("user", "x")])
    no_key = AnthropicClient("claude", AnthropicBackend(model="m"), None)
    with pytest.raises(BackendUnavailable):
        await no_key.chat([Message("user", "x")])
    assert await no_key.health() is False


async def test_anthropic_stream() -> None:
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    **ANTHROPIC_TOOL_RESPONSE,
                    "content": [],
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Bon"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "jour"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 5},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    body = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text=body, headers={"content-type": "text/event-stream"})

    out = [e async for e in anthropic_client(handler).stream([Message("user", "salut")])]
    assert isinstance(out[-1], StreamDone)
    assert out[-1].result.content == "Bonjour"
    assert out[-1].result.usage.output_tokens == 5


def test_anthropic_conversion_groups_tool_results() -> None:
    system, msgs = to_anthropic(
        [
            Message("system", "a"),
            Message("user", "u"),
            Message("assistant", "", tool_calls=[ToolCall("1", "t", {}), ToolCall("2", "t", {})]),
            Message("tool", "r1", tool_call_id="1"),
            Message("tool", "r2", tool_call_id="2"),
        ]
    )
    assert system == "a"
    assert msgs[1]["content"][0]["type"] == "tool_use"
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["1", "2"]


class FakeModels:
    def __init__(self, responses: list[types.GenerateContentResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kw: Any) -> types.GenerateContentResponse:
        self.calls.append(kw)
        return self.responses[0]

    async def generate_content_stream(self, **kw: Any) -> Any:
        self.calls.append(kw)

        async def gen() -> Any:
            for r in self.responses:
                yield r

        return gen()


class FakeGenai:
    def __init__(self, responses: list[types.GenerateContentResponse]) -> None:
        self.aio = type("Aio", (), {})()
        self.aio.models = FakeModels(responses)


def gemini_response(
    parts: list[types.Part], tin: int = 0, tout: int = 0
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=parts), finish_reason="STOP")
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=tin, candidates_token_count=tout
        ),
    )


async def test_gemini_chat_with_tool_call() -> None:
    fake = FakeGenai(
        [
            gemini_response(
                [
                    types.Part(
                        function_call=types.FunctionCall(
                            name="ha_get_state", args={"entity_id": "sensor.t"}
                        )
                    )
                ],
                50,
                7,
            )
        ]
    )
    c = GeminiClient("gemini", GeminiBackend(model="gemini-test"), None, client=fake)
    res = await c.chat([Message("system", "sys"), Message("user", "temp ?")], TOOLS)
    assert res.tool_calls[0].arguments == {"entity_id": "sensor.t"}
    assert (res.usage.input_tokens, res.usage.output_tokens) == (50, 7)
    cfg = fake.aio.models.calls[0]["config"]
    assert cfg.system_instruction == "sys"
    assert cfg.tools[0].function_declarations[0].name == "ha_get_state"


async def test_gemini_stream() -> None:
    fake = FakeGenai(
        [
            gemini_response([types.Part(text="Bon")]),
            gemini_response([types.Part(text="jour")], 9, 2),
        ]
    )
    c = GeminiClient("gemini", GeminiBackend(model="g"), None, client=fake)
    out = [e async for e in c.stream([Message("user", "x")])]
    assert isinstance(out[-1], StreamDone)
    assert out[-1].result.content == "Bonjour"
    assert out[-1].result.usage.input_tokens == 9


async def test_gemini_without_key_is_unavailable() -> None:
    c = GeminiClient("gemini", GeminiBackend(model="g"), None)
    with pytest.raises(BackendUnavailable):
        await c.chat([Message("user", "x")])


def test_gemini_conversion() -> None:
    system, contents = to_gemini(
        [
            Message("system", "s"),
            Message("user", "u"),
            Message("assistant", "", tool_calls=[ToolCall("1", "t", {"a": 1})]),
            Message("tool", "r", tool_call_id="1", name="t"),
        ]
    )
    assert system == "s"
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[2].parts[0].function_response.name == "t"
