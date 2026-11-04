"""Tiny in-process metrics (counters + latency summaries), Prometheus text format."""

from __future__ import annotations

import threading
from collections import defaultdict


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._sums: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, int]] = {}

    @staticmethod
    def _key(name: str, labels: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted(labels.items()))

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = self._key(name, labels)
        with self._lock:
            s, c = self._sums.get(key, (0.0, 0))
            self._sums[key] = (s + value, c + 1)

    def counter(self, name: str, **labels: str) -> float:
        return self._counters.get(self._key(name, labels), 0.0)

    def render(self) -> str:
        def fmt(labels: tuple[tuple[str, str], ...]) -> str:
            if not labels:
                return ""
            return "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}"

        lines: list[str] = []
        with self._lock:
            for (name, labels), v in sorted(self._counters.items()):
                lines.append(f"hla_{name}{fmt(labels)} {v}")
            for (name, labels), (s, c) in sorted(self._sums.items()):
                lines.append(f"hla_{name}_sum{fmt(labels)} {s}")
                lines.append(f"hla_{name}_count{fmt(labels)} {c}")
        return "\n".join(lines) + "\n"


METRICS = Metrics()
