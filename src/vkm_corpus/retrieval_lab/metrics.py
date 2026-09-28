"""Retrieval metrics (task §22) over graded qrels, slices, judged coverage and significance tests.

* Rankings of units are mapped to rankings of pages (first occurrence) or kept at object level.
* nDCG uses linear gain = grade and a log2 discount; the ideal ranking is built from all judged grades of the query.
* Binary relevance for Recall/P/MRR: ``grade ≥ threshold`` (default 2 — strongly relevant or central; 1 = lenient).
* Queries without a judged relevant item are excluded (they cannot be scored) and reported.
* Significance: paired sign-flip randomization test and bootstrap CI over queries (fixed seeds).
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

CUTOFFS_RECALL = (10, 20, 50, 100)
CUTOFFS_NDCG = (10, 20)
CUTOFFS_P = (5, 10)
MRR_CUTOFF = 10


def to_pages(ranked_units: Sequence[str], unit_page: Mapping[str, str | None]) -> list[str]:
    """Distinct pages in order of first appearance of their units."""
    seen: set[str] = set()
    out: list[str] = []
    for uid in ranked_units:
        page = unit_page.get(uid)
        if page and page not in seen:
            seen.add(page)
            out.append(page)
    return out


def to_objects(ranked_units: Sequence[str], unit_objects: Mapping[str, Sequence[str]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for uid in ranked_units:
        for oid in unit_objects.get(uid, ()):
            if oid not in seen:
                seen.add(oid)
                out.append(oid)
    return out


def dcg(gains: Sequence[float]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at(ranked: Sequence[str], judged: Mapping[str, int], k: int) -> float:
    gains = [float(judged.get(d, 0)) for d in ranked[:k]]
    ideal = sorted((float(g) for g in judged.values() if g > 0), reverse=True)[:k]
    idcg = dcg(ideal)
    return dcg(gains) / idcg if idcg > 0 else 0.0


def recall_at(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    return len(relevant.intersection(ranked[:k])) / len(relevant) if relevant else 0.0


def precision_at(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    return sum(1 for d in ranked[:k] if d in relevant) / k


def mrr_at(ranked: Sequence[str], relevant: set[str], k: int = MRR_CUTOFF) -> float:
    for i, d in enumerate(ranked[:k], 1):
        if d in relevant:
            return 1.0 / i
    return 0.0


def judged_at(ranked: Sequence[str], judged: Mapping[str, int], k: int = 10) -> float:
    top = ranked[:k]
    return sum(1 for d in top if d in judged) / len(top) if top else 1.0


def query_metrics(ranked: Sequence[str], judged: Mapping[str, int], *, threshold: int = 2,
                  hard_negatives: set[str] | None = None) -> dict[str, float]:
    relevant = {d for d, g in judged.items() if g >= threshold}
    m: dict[str, float] = {}
    for k in CUTOFFS_RECALL:
        m[f"recall@{k}"] = recall_at(ranked, relevant, k)
    for k in CUTOFFS_NDCG:
        m[f"ndcg@{k}"] = ndcg_at(ranked, judged, k)
    for k in CUTOFFS_P:
        m[f"p@{k}"] = precision_at(ranked, relevant, k)
    m[f"mrr@{MRR_CUTOFF}"] = mrr_at(ranked, relevant)
    m["judged@10"] = judged_at(ranked, judged, 10)
    if hard_negatives:
        first_rel = next((i for i, d in enumerate(ranked) if d in relevant), None)
        first_hn = next((i for i, d in enumerate(ranked[:10]) if d in hard_negatives), None)
        m["hn_above_first_rel@10"] = float(first_hn is not None and (first_rel is None or first_hn < first_rel))
    return m


@dataclass
class Evaluation:
    per_query: dict[str, dict[str, float]]
    excluded: list[str] = field(default_factory=list)      # queries without a judged relevant item

    def mean(self, query_ids: Iterable[str] | None = None) -> dict[str, float]:
        ids = [q for q in (query_ids if query_ids is not None else self.per_query) if q in self.per_query]
        if not ids:
            return {}
        keys = sorted({k for q in ids for k in self.per_query[q]})
        out = {}
        for k in keys:
            vals = [self.per_query[q][k] for q in ids if k in self.per_query[q]]
            out[k] = sum(vals) / len(vals) if vals else float("nan")
        out["n_queries"] = float(len(ids))
        return out

    def by_group(self, groups: Mapping[str, Iterable[str]]) -> dict[str, dict[str, float]]:
        """``groups``: group name → query IDs (slices, categories, splits)."""
        return {name: self.mean(list(ids)) for name, ids in groups.items()}


def evaluate(rankings: Mapping[str, Sequence[str]], judgments: Mapping[str, Mapping[str, int]], *,
             threshold: int = 2, hard_negatives: Mapping[str, set[str]] | None = None,
             query_ids: Iterable[str] | None = None) -> Evaluation:
    """``rankings``: query_id → ranked doc IDs (pages or objects); ``judgments``: query_id → {doc_id: grade}."""
    per_query: dict[str, dict[str, float]] = {}
    excluded: list[str] = []
    for qid in sorted(query_ids if query_ids is not None else rankings):
        judged = judgments.get(qid, {})
        if not any(g >= threshold for g in judged.values()):
            excluded.append(qid)
            continue
        per_query[qid] = query_metrics(rankings.get(qid, []), judged, threshold=threshold,
                                       hard_negatives=(hard_negatives or {}).get(qid))
    return Evaluation(per_query, excluded)


# ---------------------------------------------------------------- significance
def paired_randomization(a: Mapping[str, float], b: Mapping[str, float], *, n: int = 10000,
                         seed: int = 20260928) -> dict[str, float]:
    """Two-sided sign-flip randomization test of mean(a − b) over the queries both systems scored."""
    ids = sorted(set(a) & set(b))
    diffs = [a[q] - b[q] for q in ids]
    if not diffs:
        return {"n": 0, "delta": float("nan"), "p_value": float("nan")}
    observed = abs(sum(diffs) / len(diffs))
    rng = random.Random(seed)
    hits = 0
    for _ in range(n):
        s = sum(d if rng.random() < 0.5 else -d for d in diffs) / len(diffs)
        if abs(s) >= observed - 1e-12:
            hits += 1
    return {"n": len(diffs), "delta": sum(diffs) / len(diffs), "p_value": (hits + 1) / (n + 1)}


def bootstrap_ci(a: Mapping[str, float], b: Mapping[str, float] | None = None, *, n: int = 10000,
                 alpha: float = 0.05, seed: int = 20260928) -> dict[str, float]:
    """Percentile bootstrap CI of mean(a) or of mean(a − b) over queries."""
    ids = sorted(set(a) & set(b)) if b is not None else sorted(a)
    vals = [a[q] - (b[q] if b is not None else 0.0) for q in ids]
    if not vals:
        return {"n": 0, "mean": float("nan"), "lo": float("nan"), "hi": float("nan")}
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        sample = [vals[rng.randrange(len(vals))] for _ in vals]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int(math.floor(alpha / 2 * n))]
    hi = means[min(n - 1, int(math.ceil((1 - alpha / 2) * n)) - 1)]
    return {"n": len(vals), "mean": sum(vals) / len(vals), "lo": lo, "hi": hi}


def compare(eval_a: Evaluation, eval_b: Evaluation, metric: str = "ndcg@10",
            query_ids: Iterable[str] | None = None, *, min_gain: float = 0.02) -> dict[str, float | bool]:
    """Paired comparison A vs B on ``metric``; ``worth_it`` = mean gain ≥ ``min_gain`` and CI excludes 0."""
    ids = set(query_ids) if query_ids is not None else None
    a = {q: m[metric] for q, m in eval_a.per_query.items() if metric in m and (ids is None or q in ids)}
    b = {q: m[metric] for q, m in eval_b.per_query.items() if metric in m and (ids is None or q in ids)}
    test = paired_randomization(a, b)
    ci = bootstrap_ci(a, b)
    return {"metric": metric, "n": test["n"], "delta": test["delta"], "p_value": test["p_value"],
            "ci_lo": ci["lo"], "ci_hi": ci["hi"],
            "worth_it": bool(test["n"] and test["delta"] >= min_gain and ci["lo"] > 0)}


def group_queries(queries: Iterable, attr: str) -> dict[str, list[str]]:
    """Group query IDs by a scalar attribute (``category``) or a tuple attribute (``slices``)."""
    out: dict[str, list[str]] = defaultdict(list)
    for q in queries:
        value = getattr(q, attr)
        for v in (value if isinstance(value, (tuple, list)) else (value,)):
            out[str(v)].append(q.query_id)
    return dict(sorted(out.items()))
