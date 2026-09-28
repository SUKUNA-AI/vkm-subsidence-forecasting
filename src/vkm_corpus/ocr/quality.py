"""Per-call anomaly flags and the emergency stop of CP-22.

Flags per call: ``TRUNCATED`` (finish_reason = length), ``EMPTY_ON_INK`` (empty output on a crop with ink),
``REPETITION`` (looping output). The run stops when, over the last ``window`` calls (default 200, evaluated once the
window is full), the share of truncations exceeds 2 %, of empty-on-ink 1 % or of repetitions 1 %.

Rule ``QUALITY_RULE`` (v2): the repetition test sees only the recognised text — HTML tags, table pipes and runs of one
punctuation mark (dot leaders, rules) are layout, not repeated text (v1 flagged every HTML table with many cells).
Counting loops (``N 225, N 226, …``) end at the token cap and are caught by ``TRUNCATED``. Flags are a function of the
raw output, so the assembler recomputes them with the current rule instead of trusting the flags stored at call time.
"""
from __future__ import annotations

import re
import zlib
from collections import deque
from dataclasses import dataclass, field

QUALITY_RULE = "ocr_quality_v2"

_TOKENS = re.compile(r"\w+|[^\w\s]", re.U)
_MARKUP = re.compile(r"<[^<>]{0,300}>")
_PUNCT_RUN = re.compile(r"([^\w\s])(?:\s*\1)+", re.U)


def recognised_text(content: str) -> str:
    """The text of an OCR output without markup (tags, table pipes, runs of one punctuation mark), spaces collapsed."""
    t = _MARKUP.sub(" ", content).replace("|", " ")
    t = _PUNCT_RUN.sub(r"\1", t)
    return " ".join(t.split())


def is_repetition(content: str | None, *, ngram: int = 6, min_repeats: int = 8, min_cover: float = 0.5,
                  min_len: int = 300, max_compress: float = 12.0) -> bool:
    """Looping output: one n-gram repeated many times covering most tokens, or an extreme compression ratio
    (both measured on ``recognised_text``)."""
    if not content:
        return False
    text = recognised_text(content)
    if len(text) < min_len:
        return False
    data = text.encode("utf-8")
    if len(data) / max(1, len(zlib.compress(data, 6))) > max_compress:
        return True
    toks = _TOKENS.findall(text)
    if len(toks) < ngram * min_repeats:
        return False
    counts: dict[tuple[str, ...], int] = {}
    for i in range(len(toks) - ngram + 1):
        g = tuple(toks[i:i + ngram])
        counts[g] = counts.get(g, 0) + 1
    g, n = max(counts.items(), key=lambda kv: kv[1])
    return n >= min_repeats and n * ngram / len(toks) >= min_cover


def call_flags(content: str | None, finish_reason: str | None, ink: float | None, *,
               ink_threshold: float = 0.01) -> list[str]:
    flags = []
    if finish_reason == "length":
        flags.append("TRUNCATED")
    if (content is None or not content.strip()) and ink is not None and ink >= ink_threshold:
        flags.append("EMPTY_ON_INK")
    if is_repetition(content):
        flags.append("REPETITION")
    return flags


class StopRun(RuntimeError):
    """Emergency stop of the model calls (CP-22 thresholds exceeded)."""


@dataclass
class StopWindow:
    window: int = 200
    max_truncated: float = 0.02
    max_empty_on_ink: float = 0.01
    max_repetition: float = 0.01
    recent: deque = field(default_factory=deque)
    totals: dict[str, int] = field(default_factory=lambda: {"calls": 0, "TRUNCATED": 0, "EMPTY_ON_INK": 0,
                                                            "REPETITION": 0, "ERRORS": 0})

    def add(self, flags: list[str], *, error: bool = False) -> None:
        self.recent.append(frozenset(flags))
        if len(self.recent) > self.window:
            self.recent.popleft()
        self.totals["calls"] += 1
        if error:
            self.totals["ERRORS"] += 1
        for f in flags:
            if f in self.totals:
                self.totals[f] += 1

    def shares(self) -> dict[str, float]:
        n = len(self.recent) or 1
        return {k: sum(1 for s in self.recent if k in s) / n for k in ("TRUNCATED", "EMPTY_ON_INK", "REPETITION")}

    def check(self) -> None:
        if len(self.recent) < self.window:
            return
        s = self.shares()
        if s["TRUNCATED"] > self.max_truncated or s["EMPTY_ON_INK"] > self.max_empty_on_ink \
                or s["REPETITION"] > self.max_repetition:
            raise StopRun(f"CP-22 stop over the last {self.window} calls: " +
                          ", ".join(f"{k}={v:.3f}" for k, v in s.items()))
