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
    ttft_s: float
    total_s: float
    output_tokens: int
    tok_per_s: float


async def speed_run(client: OpenAICompatClient, max_tokens: int) -> SpeedResult:
    start = time.perf_counter()
    first: float | None = None
    usage_out = 0
    async for ev in client.stream([Message("user", SPEED_PROMPT)], max_tokens=max_tokens):
        if isinstance(ev, StreamDelta) and first is None:
            first = time.perf_counter()
        elif isinstance(ev, StreamDone):
            usage_out = ev.result.usage.output_tokens
    end = time.perf_counter()
    first = first or end
    gen = max(end - first, 1e-6)
    return SpeedResult(first - start, end - start, usage_out, usage_out / gen)


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
        speeds = [await speed_run(client, args.max_tokens) for _ in range(args.runs)]
        tools = [await tool_case(client, *case) for case in TOOL_CASES]
    finally:
        await client.aclose()

    print(f"\n# {args.model} @ {args.url}")
    print(f"time to first token : {statistics.median(s.ttft_s for s in speeds):.2f} s (median)")
    print(
        f"generation speed    : {statistics.median(s.tok_per_s for s in speeds):.1f} tok/s (median)"
    )
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
