"""Deterministic OpenAI-compatible server (``/v1/chat/completions``).

Directives inside the last user message drive its behaviour:

* ``[[tool:NAME {"json": "args"}]]`` → emits that tool call
* ``[[escalate:cpu]]`` / ``[[escalate:cloud]]`` → calls the ``escalate`` tool
* ``[[sleep:2.5]]`` → waits before answering
* ``[[error:503]]`` → answers with that HTTP status

Without directive, natural French home commands ("éteins la lumière du salon") are mapped to
``ha_call_service`` when the entity id appears in the system prompt. Otherwise it echoes.
``POST /_control`` with ``{"fail": true}``, ``{"delay": 1.0}`` or ``{"n_ctx": 512}`` simulates
outages, slowness or a small context window (HTTP 400, like llama-server).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

TOOL_NAME_RE = re.compile(r"[a-zA-Z0-9_]+")
ESC_RE = re.compile(r"\[\[escalate:(cpu|cloud)\]\]")
SLEEP_RE = re.compile(r"\[\[sleep:([0-9.]+)\]\]")
ERROR_RE = re.compile(r"\[\[error:([0-9]{3})\]\]")


def _tool_markers(text: str) -> list[tuple[str, str]]:
    """Find ``[[tool:name {json}]]`` markers (JSON optional) in one linear pass."""
    found: list[tuple[str, str]] = []
    pos = 0
    while (start := text.find("[[tool:", pos)) != -1:
        pos = start + len("[[tool:")
        name = TOOL_NAME_RE.match(text, pos)
        if name is None:
            continue
        i = name.end()
        while i < len(text) and text[i].isspace():
            i += 1
        if text.startswith("]]", i):
            found.append((name.group(), ""))
            pos = i + 2
        elif text.startswith("{", i) and (end := text.find("}]]", i)) != -1:
            found.append((name.group(), text[i : end + 1]))
            pos = end + 3
    return found


def _natural_tool(text: str, system: str) -> tuple[str, dict[str, Any]] | None:
    low = text.casefold()
    if re.search(r"\b(éteins|eteins|éteindre|coupe)\b", low):
        service = "turn_off"
    elif re.search(r"\b(allume|allumer)\b", low):
        service = "turn_on"
    else:
        return None
    for eid in re.findall(r"\b((?:light|switch)\.[a-z0-9_]+)\b", system):
        if eid.split(".", 1)[1].replace("_", " ") in low:
            return "ha_call_service", {
                "domain": eid.split(".")[0], "service": service, "entity_id": eid,
            }  # fmt: skip
    return None


def decide(body: dict[str, Any]) -> dict[str, Any]:
    """Return {'content': str, 'tool_calls': [...], 'sleep': s, 'error': code}."""
    messages = body.get("messages") or []
    tools = {t["function"]["name"] for t in body.get("tools") or []}
    model = body.get("model", "fake")
    last = messages[-1] if messages else {"role": "user", "content": ""}
    user = next((m for m in reversed(messages) if m.get("role") == "user"), {"content": ""})
    utext = str(user.get("content") or "")
    system = " ".join(str(m.get("content")) for m in messages if m.get("role") == "system")
    out: dict[str, Any] = {"content": "", "tool_calls": [], "sleep": 0.0, "error": None}
    if m := SLEEP_RE.search(utext):
        out["sleep"] = float(m.group(1))
    if m := ERROR_RE.search(utext):
        out["error"] = int(m.group(1))
        return out
    if last.get("role") == "tool":
        try:
            result = json.loads(last.get("content") or "{}")
        except json.JSONDecodeError:
            result = {}
        if result.get("ok"):
            out["content"] = f"C'est fait ({model})."
        else:
            out["content"] = f"Impossible : {result.get('error', 'erreur inconnue')}"
        return out
    if (m := ESC_RE.search(utext)) and "escalate" in tools:
        out["tool_calls"].append(("escalate", {"target": m.group(1), "reason": "test"}))
        return out
    for name, raw in _tool_markers(utext):
        if name in tools:
            out["tool_calls"].append((name, json.loads(raw) if raw else {}))
    if out["tool_calls"]:
        return out
    if "ha_call_service" in tools and (nat := _natural_tool(utext, system)):
        out["tool_calls"].append(nat)
        return out
    clean = ESC_RE.sub("", SLEEP_RE.sub("", utext)).strip()
    out["content"] = f"Réponse simulée ({model}) : {clean[:200]}"
    return out


def _calls(tool_calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)},
        }
        for n, a in tool_calls
    ]


def create_app() -> FastAPI:
    app = FastAPI(title="fake-llm")
    state: dict[str, Any] = {
        "fail": os.environ.get("FAKE_LLM_FAIL") == "1",
        "delay": float(os.environ.get("FAKE_LLM_DELAY", "0")),
        "n_ctx": int(os.environ.get("FAKE_LLM_N_CTX", "0")),  # 0 = unlimited
        "requests": 0,
    }

    @app.get("/health")
    async def health() -> JSONResponse:
        if state["fail"]:
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/v1/models")
    async def models() -> JSONResponse:
        if state["fail"]:
            return JSONResponse({"error": "down"}, status_code=503)
        return JSONResponse({"object": "list", "data": [{"id": "fake", "object": "model"}]})

    @app.post("/_control")
    async def control(request: Request) -> dict[str, Any]:
        state.update(await request.json())
        return state

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Any:
        state["requests"] += 1
        body = await request.json()
        if state["fail"]:
            return JSONResponse({"error": {"message": "simulated outage"}}, status_code=503)
        if state["n_ctx"]:
            prompt = sum(len(str(m.get("content") or "")) // 4 + 4 for m in body["messages"])
            if prompt + int(body.get("max_tokens") or 0) > state["n_ctx"]:
                return JSONResponse(
                    {"error": {"code": 400, "type": "exceed_context_size_error",
                               "message": "the request exceeds the available context size"}},
                    status_code=400,
                )  # fmt: skip
        d = decide(body)
        delay = d["sleep"] + float(state["delay"])
        if delay:
            await asyncio.sleep(delay)
        if d["error"]:
            return JSONResponse({"error": {"message": "simulated"}}, status_code=d["error"])
        model = body.get("model", "fake")
        calls = _calls(d["tool_calls"])
        prompt_tokens = sum(len(str(m.get("content") or "")) // 4 + 4 for m in body["messages"])
        completion_tokens = max(1, len(d["content"]) // 4)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        finish = "tool_calls" if calls else "stop"
        cid = f"chatcmpl-{uuid.uuid4().hex[:10]}"
        if not body.get("stream"):
            return {
                "id": cid,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": d["content"] or None,
                            "tool_calls": calls or None,
                        },
                        "finish_reason": finish,
                    }
                ],
                "usage": usage,
            }

        async def gen() -> AsyncIterator[str]:
            def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> str:
                payload = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                }
                return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            yield chunk({"role": "assistant"})
            for word in re.findall(r"\S+\s*", d["content"]):
                yield chunk({"content": word})
                await asyncio.sleep(0)
            for i, c in enumerate(calls):
                yield chunk({"tool_calls": [{"index": i, **c}]})
            yield chunk({}, finish)
            tail = {
                "id": cid,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [],
                "usage": usage,
            }
            yield f"data: {json.dumps(tail)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


app = create_app()
