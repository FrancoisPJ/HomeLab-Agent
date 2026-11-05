"""In-process metrics in the Prometheus text format (no client library needed).

* counters: ``inc(name, value, **labels)``
* histograms: ``observe(name, value, **labels)`` (latency buckets, for p50 / p95 in Grafana)
* gauges: computed at scrape time by collectors registered with ``add_collector``
  (backend health, Home Assistant, jobs, cloud budget...).
"""

from __future__ import annotations

import bisect
import logging
import threading
from collections import defaultdict
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

PREFIX = "hla_"
LabelKey = tuple[tuple[str, str], ...]
Sample = tuple[str, dict[str, str], float]  # (name without prefix, labels, value)
Collector = Callable[[], Awaitable[list[Sample]]]

#: latency buckets in milliseconds (fast path ~10 ms … CPU jobs ~minutes)
LATENCY_BUCKETS_MS = (25, 50, 100, 250, 500, 1000, 2500, 5000, 10_000, 30_000, 60_000, 300_000)

HELP = {
    "messages_total": "Messages received from users (all gateways).",
    "replies_total": "Answers produced, by tier (fast_path, gpu, cpu, cloud).",
    "latency_ms": "Time to answer, in milliseconds, by tier.",
    "tokens_total": "LLM tokens (input + output), by backend.",
    "tool_calls_total": "Tool calls made by the models, by tool and outcome.",
    "alerts_total": "Proactive alerts, by alert and outcome (sent, quiet, cooldown).",
    "routines_total": "Routine runs, by routine.",
    "telegram_rejected_total": "Telegram messages ignored (user not in the allowlist).",
    "voice_messages_total": "Voice notes transcribed.",
    "voice_replies_total": "Voice replies synthesized.",
    "telegram_attachments_total": "Photos and files received (not readable yet), by kind.",
    "backend_up": "1 when the backend is considered healthy.",
    "home_assistant_connected": "1 when the Home Assistant websocket is connected.",
    "home_assistant_entities": "Entities in the Home Assistant cache.",
    "jobs": "Background CPU jobs, by status.",
    "cloud_spend_eur": "Cloud spend in euros, by period (day, month).",
    "cloud_limit_eur": "Cloud spend caps in euros, by period.",
    "cloud_enabled": "1 when the cloud tier is enabled.",
    "build_info": "Version of homelab-agent (value is always 1).",
}


def _key(labels: dict[str, str]) -> LabelKey:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


def _fmt_labels(labels: LabelKey | dict[str, str], extra: tuple[tuple[str, str], ...] = ()) -> str:
    items = list(labels.items() if isinstance(labels, dict) else labels) + list(extra)
    if not items:
        return ""
    esc = [
        (k, str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")) for k, v in items
    ]
    return "{" + ",".join(f'{k}="{v}"' for k, v in esc) + "}"


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


class Metrics:
    def __init__(self, buckets: tuple[float, ...] = LATENCY_BUCKETS_MS) -> None:
        self._lock = threading.Lock()
        self.buckets = buckets
        self._counters: dict[str, dict[LabelKey, float]] = defaultdict(lambda: defaultdict(float))
        # name -> labels -> [bucket counts..., +Inf count], sum
        self._hist: dict[str, dict[LabelKey, tuple[list[int], float]]] = defaultdict(dict)
        self._collectors: list[Collector] = []

    # ------------------------------------------------------------ record
    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._counters[name][_key(labels)] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = _key(labels)
        with self._lock:
            counts, total = self._hist[name].get(key, ([0] * (len(self.buckets) + 1), 0.0))
            counts = list(counts)
            counts[bisect.bisect_left(self.buckets, value)] += 1
            self._hist[name][key] = (counts, total + value)

    def add_collector(self, collector: Collector) -> None:
        self._collectors.append(collector)

    # ------------------------------------------------------------- query
    def counter(self, name: str, **labels: str) -> float:
        return self._counters.get(name, {}).get(_key(labels), 0.0)

    def histogram_count(self, name: str, **labels: str) -> int:
        entry = self._hist.get(name, {}).get(_key(labels))
        return sum(entry[0]) if entry else 0

    # ------------------------------------------------------------ render
    async def render(self, collectors: list[Collector] | None = None) -> str:
        lines: list[str] = []

        def header(name: str, kind: str) -> None:
            if name in HELP:
                lines.append(f"# HELP {PREFIX}{name} {HELP[name]}")
            lines.append(f"# TYPE {PREFIX}{name} {kind}")

        with self._lock:
            counters = {n: dict(v) for n, v in self._counters.items()}
            hists = {n: dict(v) for n, v in self._hist.items()}
        for name in sorted(counters):
            header(name, "counter")
            for labels, value in sorted(counters[name].items()):
                lines.append(f"{PREFIX}{name}{_fmt_labels(labels)} {_num(value)}")
        for name in sorted(hists):
            header(name, "histogram")
            for labels, (counts, total) in sorted(hists[name].items()):
                cumulative = 0
                for bound, count in zip((*self.buckets, float("inf")), counts, strict=True):
                    cumulative += count
                    le = "+Inf" if bound == float("inf") else _num(bound)
                    lines.append(
                        f"{PREFIX}{name}_bucket{_fmt_labels(labels, (('le', le),))} {cumulative}"
                    )
                lines.append(f"{PREFIX}{name}_sum{_fmt_labels(labels)} {_num(total)}")
                lines.append(f"{PREFIX}{name}_count{_fmt_labels(labels)} {cumulative}")
        gauges: dict[str, list[tuple[dict[str, str], float]]] = defaultdict(list)
        for collector in [*self._collectors, *(collectors or [])]:
            try:
                for name, sample_labels, value in await collector():
                    gauges[name].append((sample_labels, value))
            except Exception:  # a broken collector must not break the scrape
                log.exception("metrics collector failed")
        for name in sorted(gauges):
            header(name, "gauge")
            for gauge_labels, value in gauges[name]:
                lines.append(f"{PREFIX}{name}{_fmt_labels(gauge_labels)} {_num(value)}")
        return "\n".join(lines) + "\n"


METRICS = Metrics()
