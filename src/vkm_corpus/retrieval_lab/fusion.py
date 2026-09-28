"""Fusion of candidate lists (task §24): reciprocal-rank fusion and score-normalised weighted fusion.

A ranking is a list of ``(item_id, score)`` in rank order (best first). Ties are broken by ``item_id`` so fused
orders are deterministic. Weights and ``k`` are tuned only on train/dev queries (design §11).
"""
from __future__ import annotations

import math
from typing import Mapping, Sequence

Ranking = Sequence[tuple[str, float]]
NORMALIZATIONS = ("minmax", "zscore", "rank")


def rrf(rankings: Mapping[str, Ranking], *, k: int = 60, weights: Mapping[str, float] | None = None,
        depth: int | None = None) -> list[tuple[str, float]]:
    """``score(d) = Σ_r w_r / (k + rank_r(d))`` over the rankings that contain ``d`` (rank from 1)."""
    fused: dict[str, float] = {}
    for name, ranking in rankings.items():
        w = 1.0 if weights is None else float(weights.get(name, 0.0))
        if w == 0.0:
            continue
        for rank, (item, _score) in enumerate(ranking[:depth] if depth else ranking, 1):
            fused[item] = fused.get(item, 0.0) + w / (k + rank)
    return sorted(fused.items(), key=lambda x: (-x[1], x[0]))


def _normalize(ranking: Ranking, method: str) -> dict[str, float]:
    if not ranking:
        return {}
    if method == "rank":
        n = len(ranking)
        return {item: 1.0 - i / n for i, (item, _s) in enumerate(ranking)}
    scores = [s for _i, s in ranking]
    if method == "minmax":
        lo, hi = min(scores), max(scores)
        span = hi - lo
        return {item: ((s - lo) / span if span > 0 else 1.0) for item, s in ranking}
    if method == "zscore":
        mean = sum(scores) / len(scores)
        sd = math.sqrt(sum((s - mean) ** 2 for s in scores) / len(scores))
        return {item: ((s - mean) / sd if sd > 0 else 0.0) for item, s in ranking}
    raise ValueError(f"normalization must be one of {NORMALIZATIONS}")


def weighted(rankings: Mapping[str, Ranking], weights: Mapping[str, float], *, normalization: str = "minmax",
             depth: int | None = None, missing: float | None = None) -> list[tuple[str, float]]:
    """``score(d) = Σ_r w_r · norm_r(d)``; an item absent from a list gets ``missing`` for it (default: the minimum
    normalised value of that list, i.e. 0 for minmax/rank)."""
    fused: dict[str, float] = {}
    normed = {name: _normalize(list(r[:depth] if depth else r), normalization) for name, r in rankings.items()}
    items = set().union(*[set(n) for n in normed.values()]) if normed else set()
    for name, n in normed.items():
        w = float(weights.get(name, 0.0))
        if w == 0.0:
            continue
        floor = missing if missing is not None else (min(n.values()) if n else 0.0)
        for item in items:
            fused[item] = fused.get(item, 0.0) + w * n.get(item, floor)
    return sorted(fused.items(), key=lambda x: (-x[1], x[0]))


def weight_grid(names: Sequence[str], step: float = 0.1) -> list[dict[str, float]]:
    """All weight vectors on a simplex grid (non-negative, sum 1) — the train-split search space."""
    n = round(1 / step)
    out: list[dict[str, float]] = []

    def rec(prefix: list[int], left: int, slots: int) -> None:
        if slots == 1:
            out.append({name: v * step for name, v in zip(names, prefix + [left])})
            return
        for v in range(left + 1):
            rec(prefix + [v], left - v, slots - 1)

    rec([], n, len(names))
    return out
