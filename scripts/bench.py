#!/usr/bin/env python3
"""Benchmark an OpenAI-compatible endpoint: speed (tok/s, time to first token) and
tool-calling quality on home-automation prompts.

    uv run python scripts/bench.py --url http://127.0.0.1:8080/v1 --model fast
    uv run python scripts/bench.py --url ... --model heavy --runs 3 --json out.json

Run it on the real hardware to choose models (see docs/models.md). Against fake-llm it only
checks that the harness works: its numbers mean nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from homelab_agent.config.models import OpenAICompatBackend
from homelab_agent.llm.base import Message, StreamDelta, StreamDone, ToolSpec
from homelab_agent.llm.openai_compat import OpenAICompatClient

SYSTEM = (
    "Tu pilotes une maison avec les outils fournis. Entités : light.salon (Lumière salon), "
    "light.cuisine (Lumière cuisine), cover.volet_chambre (Volet chambre), "
    "sensor.temperature_salon (Température salon), scene.soiree (Soirée). "
    "Réponds en français, brièvement."
)

TOOLS = [
    ToolSpec(
        "ha_call_service",
        "Call a Home Assistant service.",
        {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
                "entity_id": {"type": "string"},
                "data": {"type": "object"},
            },
            "required": ["domain", "service"],
        },
    ),
    ToolSpec(
        "ha_get_state",
        "Read the state of one entity.",
        {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    ),
]

# (prompt, expected tool or None, expected argument subset)
TOOL_CASES: list[tuple[str, str | None, dict[str, Any]]] = [
    ("Éteins la lumière du salon", "ha_call_service",
     {"domain": "light", "service": "turn_off", "entity_id": "light.salon"}),
    ("allume la cuisine", "ha_call_service",
     {"domain": "light", "service": "turn_on", "entity_id": "light.cuisine"}),
    ("Ferme le volet de la chambre", "ha_call_service",
     {"domain": "cover", "service": "close_cover", "entity_id": "cover.volet_chambre"}),
    ("Quelle température fait-il dans le salon ?", "ha_get_state",
     {"entity_id": "sensor.temperature_salon"}),
    ("Lance la scène soirée", "ha_call_service", {"entity_id": "scene.soiree"}),
    ("Raconte-moi une blague courte", None, {}),
]  # fmt: skip

SPEED_PROMPT = "Explique en cinq phrases comment fonctionne un thermostat."


@dataclass
class SpeedResult:
    first_text_s: float  # time until the first visible text (includes any "thinking")
    total_s: float
    output_tokens: int
    end_to_end_tok_s: float  # output tokens / total time (prompt processing included)
    server_decode_tok_s: float | None  # llama-server's own measure, when reported
    server_prompt_tok_s: float | None


async def server_timings(url: str, model: str, max_tokens: int, timeout_s: float) -> dict[str, Any]:
    """llama-server adds a `timings` object to non-streamed answers."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": SPEED_PROMPT}],
        "max_tokens": max_tokens,
    }
    async with httpx.AsyncClient(timeout=timeout_s) as c:
        resp = await c.post(url.rstrip("/") + "/chat/completions", json=payload)
    data: dict[str, Any] = resp.json() if resp.status_code == 200 else {}
    return data.get("timings") or {}


async def speed_run(client: OpenAICompatClient, args: argparse.Namespace) -> SpeedResult:
    start = time.perf_counter()
    first: float | None = None
    usage_out = 0
    async for ev in client.stream([Message("user", SPEED_PROMPT)], max_tokens=args.max_tokens):
        if isinstance(ev, StreamDelta) and first is None:
            first = time.perf_counter()
        elif isinstance(ev, StreamDone):
            usage_out = ev.result.usage.output_tokens
    end = time.perf_counter()
    timings = await server_timings(args.url, args.model, args.max_tokens, args.timeout)
    return SpeedResult(
        first_text_s=(first or end) - start,
        total_s=end - start,
        output_tokens=usage_out,
        end_to_end_tok_s=usage_out / max(end - start, 1e-6),
        server_decode_tok_s=timings.get("predicted_per_second"),
        server_prompt_tok_s=timings.get("prompt_per_second"),
    )


async def tool_case(
    client: OpenAICompatClient, prompt: str, tool: str | None, args: dict[str, Any]
) -> dict[str, Any]:
    start = time.perf_counter()
    res = await client.chat([Message("system", SYSTEM), Message("user", prompt)], TOOLS)
    latency = time.perf_counter() - start
    got = res.tool_calls[0] if res.tool_calls else None
    if tool is None:
        ok = got is None
    else:
        ok = (
            got is not None
            and got.name == tool
            and all(
                str(got.arguments.get(k, "")).lower() == str(v).lower() for k, v in args.items()
            )
        )
    return {
        "prompt": prompt,
        "expected": tool,
        "got": got.name if got else None,
        "args": got.arguments if got else None,
        "ok": ok,
        "latency_s": round(latency, 3),
    }


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", required=True, help="base URL including /v1")
    ap.add_argument("--model", required=True)
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--max-tokens", type=int, default=200)
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--json", help="write the full results to this file")
    args = ap.parse_args()

    cfg = OpenAICompatBackend(tier="gpu", url=args.url, model=args.model, timeout_s=args.timeout)
    client = OpenAICompatClient("bench", cfg)
    try:
        speeds = [await speed_run(client, args) for _ in range(args.runs)]
        tools = [await tool_case(client, *case) for case in TOOL_CASES]
    finally:
        await client.aclose()

    print(f"\n# {args.model} @ {args.url}")
    print(f"first visible text  : {statistics.median(s.first_text_s for s in speeds):.2f} s")
    print(
        f"end-to-end speed    : {statistics.median(s.end_to_end_tok_s for s in speeds):.1f} tok/s"
    )
    decode = [s.server_decode_tok_s for s in speeds if s.server_decode_tok_s]
    prompt = [s.server_prompt_tok_s for s in speeds if s.server_prompt_tok_s]
    if decode:
        print(f"decode (server)     : {statistics.median(decode):.1f} tok/s")
    if prompt:
        print(f"prompt (server)     : {statistics.median(prompt):.1f} tok/s")
    score = sum(t["ok"] for t in tools)
    print(f"tool-calling        : {score}/{len(tools)}")
    for t in tools:
        mark = "✓" if t["ok"] else "✗"
        print(f"  {mark} {t['prompt'][:45]:45} → {t['got']} {t['args'] or ''} ({t['latency_s']} s)")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {"speed": [asdict(s) for s in speeds], "tools": tools},
                fh,
                ensure_ascii=False,
                indent=2,
            )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
