from __future__ import annotations

import httpx
import pytest

from homelab_agent.config.models import OpenAICompatBackend
from homelab_agent.llm.base import (
    BackendError,
    BackendUnavailable,
    Message,
    StreamDelta,
    StreamDone,
    ToolCall,
    ToolSpec,
)
from homelab_agent.llm.openai_compat import OpenAICompatClient, to_openai_messages

CFG = OpenAICompatBackend(tier="gpu", url="http://fake/v1", model="fast", timeout_s=5)
TOOLS = [ToolSpec("ha_call_service", "call", {"type": "object", "properties": {}})]


def client(transport: httpx.AsyncBaseTransport) -> OpenAICompatClient:
    return OpenAICompatClient("gpu_fast", CFG, transport=transport)


async def test_chat_plain(llm_transport: httpx.ASGITransport) -> None:
    res = await client(llm_transport).chat([Message("user", "bonjour")])
    assert "bonjour" in res.content
    assert res.usage.input_tokens > 0 and res.usage.output_tokens > 0
    assert res.tool_calls == []


async def test_chat_tool_call(llm_transport: httpx.ASGITransport) -> None:
    msg = Message("user", 'fais [[tool:ha_call_service {"domain": "light", "service": "turn_on"}]]')
    res = await client(llm_transport).chat([msg], TOOLS)
    assert res.tool_calls[0].name == "ha_call_service"
    assert res.tool_calls[0].arguments == {"domain": "light", "service": "turn_on"}


async def test_stream_text_and_tools(llm_transport: httpx.ASGITransport) -> None:
    c = client(llm_transport)
    events = [e async for e in c.stream([Message("user", "un deux trois")])]
    deltas = "".join(e.text for e in events if isinstance(e, StreamDelta))
    done = events[-1]
    assert isinstance(done, StreamDone)
    assert deltas == done.result.content and "un deux trois" in deltas
    assert done.result.usage.input_tokens > 0
    msg = Message("user", '[[tool:ha_call_service {"domain": "x", "service": "y"}]]')
    events = [e async for e in c.stream([msg], TOOLS)]
    assert isinstance(events[-1], StreamDone)
    assert events[-1].result.tool_calls[0].arguments == {"domain": "x", "service": "y"}


async def test_errors_are_classified(llm_transport: httpx.ASGITransport) -> None:
    c = client(llm_transport)
    with pytest.raises(BackendUnavailable):
        await c.chat([Message("user", "[[error:503]]")])
    with pytest.raises(BackendError) as err:
        await c.chat([Message("user", "[[error:400]]")])
    assert not isinstance(err.value, BackendUnavailable)
    with pytest.raises(BackendUnavailable):
        _ = [e async for e in c.stream([Message("user", "[[error:500]]")])]


async def test_connection_refused_is_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    c = client(httpx.MockTransport(refuse))
    with pytest.raises(BackendUnavailable):
        await c.chat([Message("user", "x")])
    assert await c.health() is False


async def test_timeout_is_unavailable() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    from homelab_agent.llm.base import BackendTimeout

    with pytest.raises(BackendTimeout):
        await client(httpx.MockTransport(slow)).chat([Message("user", "x")])


async def test_health(llm_transport: httpx.ASGITransport) -> None:
    assert await client(llm_transport).health() is True


def test_message_conversion() -> None:
    msgs = [
        Message("system", "s"),
        Message("assistant", "", tool_calls=[ToolCall("c1", "t", {"a": 1})]),
        Message("tool", '{"ok": true}', tool_call_id="c1", name="t"),
    ]
    out = to_openai_messages(msgs)
    assert out[1]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'
    assert out[2]["tool_call_id"] == "c1"
