"""Status page numbers for the last 24 hours (pure: rows in, template data out)."""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Protocol

from homelab_agent.web.charts import TIER_COLORS, TIER_NAMES, donut, percentile, sparkline

HOURS = 24
WINDOW = timedelta(hours=HOURS)
TIER_ORDER = list(TIER_COLORS)  # fast_path, gpu, cpu, cloud
#: background jobs are slow on purpose: they would hide the latency people feel
SLOW_TIERS = {"cpu"}


class InteractionRow(Protocol):
    created_at: datetime
    tier: str
    latency_ms: int


def greeting(hour: int) -> str:
    if hour < 5:
        return "Bonne nuit"
    if hour < 12:
        return "Bonjour"
    return "Bon après-midi" if hour < 18 else "Bonsoir"


def latency_label(ms: float) -> tuple[str, str]:
    return (f"{ms / 1000:.1f}", "s") if ms >= 1000 else (f"{ms:.0f}", "ms")


def build_dashboard(
    rows: Iterable[InteractionRow],
    spend: Iterable[tuple[datetime, float]],
    *,
    now: datetime,
    tz: tzinfo,
    day_spend: float,
    daily_limit: float,
    house: dict[str, Any] | None,
) -> dict[str, Any]:
    """Hourly activity, latency, who answered and spend; times in the rows are naive UTC."""

    def hour_index(t: datetime) -> int:
        age_h = (now - t.replace(tzinfo=UTC)).total_seconds() / 3600
        return max(0, min(HOURS - 1, HOURS - 1 - int(age_h)))

    per_hour = [0.0] * HOURS
    latency_per_hour: list[list[float]] = [[] for _ in range(HOURS)]
    immediate: list[float] = []
    tiers: dict[str, int] = {}
    for r in rows:
        h = hour_index(r.created_at)
        per_hour[h] += 1
        tiers[r.tier] = tiers.get(r.tier, 0) + 1
        if r.tier not in SLOW_TIERS:
            latency_per_hour[h].append(r.latency_ms)
            immediate.append(r.latency_ms)
    spend_per_hour = [0.0] * HOURS
    for t, cost in spend:
        spend_per_hour[hour_index(t)] += cost

    segments = [
        (TIER_NAMES.get(t, t), float(tiers[t]), TIER_COLORS.get(t, "#94a3b8"))
        for t in sorted(tiers, key=lambda t: TIER_ORDER.index(t) if t in TIER_ORDER else 99)
    ]
    p95 = percentile(immediate, 0.95)
    p95_label, p95_unit = latency_label(p95)
    local = now.astimezone(tz)
    return {
        "greeting": greeting(local.hour),
        "date": f"{local:%d/%m/%Y · %H:%M}",
        "messages": int(sum(per_hour)),
        "spark_messages": sparkline(per_hour, TIER_COLORS["gpu"]),
        "p95": p95,
        "p95_label": p95_label,
        "p95_unit": p95_unit,
        "spark_latency": sparkline([percentile(v, 0.95) for v in latency_per_hour], "#f472b6"),
        "spark_spend": sparkline(list(itertools.accumulate(spend_per_hour)), TIER_COLORS["cloud"]),
        # the real ratio: one request can overshoot the cap, the page must say so
        "budget_pct": round(100 * day_spend / daily_limit) if daily_limit > 0 else 0,
        "house": house,
        "donut": donut(segments, center_label="réponses 24 h"),
        "segments": segments,
    }
