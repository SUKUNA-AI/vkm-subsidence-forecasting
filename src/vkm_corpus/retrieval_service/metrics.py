"""Minimal Prometheus text exposition (no client library): counters and latency histograms per endpoint and model,
plus gauges collected at scrape time (VRAM, residency, keepalive). The small lock guards only the in-process counters;
it is never held while a model computes."""
from __future__ import annotations

import threading
from typing import Callable, Iterable

BUCKETS_MS = (2, 5, 10, 20, 35, 50, 75, 100, 150, 250, 500, 1000, 2500, 5000)


def _labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
                     for k, v in sorted(labels.items()))
    return "{" + inner + "}"


class Metrics:
    def __init__(self, prefix: str = "vkm_rx580") -> None:
        self.prefix = prefix
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple], float] = {}
        self._hist: dict[tuple[str, tuple], list[float]] = {}
        self._hist_sum: dict[tuple[str, tuple], float] = {}
        self._gauges: list[Callable[[], Iterable[tuple[str, dict[str, str], float]]]] = []

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe_ms(self, name: str, ms: float, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            b = self._hist.setdefault(key, [0.0] * (len(BUCKETS_MS) + 1))
            for i, edge in enumerate(BUCKETS_MS):
                if ms <= edge:
                    b[i] += 1
            b[-1] += 1
            self._hist_sum[key] = self._hist_sum.get(key, 0.0) + ms

    def gauge_source(self, fn: Callable[[], Iterable[tuple[str, dict[str, str], float]]]) -> None:
        self._gauges.append(fn)

    def render(self) -> str:
        p = self.prefix
        lines: list[str] = []
        with self._lock:
            counters = dict(self._counters)
            hist = {k: list(v) for k, v in self._hist.items()}
            sums = dict(self._hist_sum)
        for (name, labels), v in sorted(counters.items()):
            lines.append(f"{p}_{name}_total{_labels(dict(labels))} {v:g}")
        for (name, labels), b in sorted(hist.items()):
            lab = dict(labels)
            for edge, cnt in zip(BUCKETS_MS, b):
                lines.append(f"{p}_{name}_ms_bucket{_labels({**lab, 'le': str(edge)})} {cnt:g}")
            lines.append(f"{p}_{name}_ms_bucket{_labels({**lab, 'le': '+Inf'})} {b[-1]:g}")
            lines.append(f"{p}_{name}_ms_sum{_labels(lab)} {sums[(name, labels)]:.3f}")
            lines.append(f"{p}_{name}_ms_count{_labels(lab)} {b[-1]:g}")
        for fn in self._gauges:
            try:
                for name, lab, value in fn():
                    if value is not None:
                        lines.append(f"{p}_{name}{_labels(lab)} {float(value):g}")
            except Exception:  # a failing gauge must not break the scrape
                lines.append(f"{p}_gauge_errors_total 1")
        return "\n".join(lines) + "\n"
