"""Parity of RX580 outputs against the reference (постановка лаборатории §30–31). numpy only.

Bit-identical output is not required. Measured per model/quantisation:

* vector agreement — cosine between RX580 and reference vectors of the same item (dense: per item; ColBERT: per token,
  ids are identical by construction), mean / p1 / min;
* ranking agreement over the probe pool — for every query the reference ranking is the pseudo-truth: top-10 and
  top-50 overlap, Spearman and Kendall on the union of both top-100 lists, rank-agreement nDCG@10 (reference scores as
  gains) and its delta from the ideal 1.0;
* quality deltas on real qrels (Recall@k, nDCG@k) are computed by J's harness when the benchmark qrels exist; this
  module provides :func:`ndcg_at_k` / :func:`recall_at_k` for that.

The gate (:func:`gate`) is explicit and conservative; thresholds are MODEL_CHOICE recorded with every result.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

import numpy as np

from vkm_corpus.embeddings.postprocess import l2_normalize, maxsim

GATE = {"cos_mean_min": 0.995, "cos_p1_min": 0.98, "top10_overlap_min": 0.90, "top50_overlap_min": 0.90,
        "spearman_min": 0.95}


def _ranks(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), dtype=np.float64)
    ranks[order] = np.arange(len(x), dtype=np.float64)
    # average ranks for ties
    xs = x[order]
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[j + 1] == xs[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + j) / 2.0
        i = j + 1
    return ranks


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    if len(a) < 2:
        return 1.0
    ra, rb = _ranks(a), _ranks(b)
    ra -= ra.mean()
    rb -= rb.mean()
    den = np.sqrt((ra * ra).sum() * (rb * rb).sum())
    return float((ra * rb).sum() / den) if den > 0 else 1.0


def kendall(a: Sequence[float], b: Sequence[float]) -> float:
    """Kendall tau-b (O(n²); used on ≤ 200 items)."""
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    n = len(a)
    if n < 2:
        return 1.0
    da = np.sign(a[:, None] - a[None, :])
    db = np.sign(b[:, None] - b[None, :])
    iu = np.triu_indices(n, 1)
    x, y = da[iu], db[iu]
    conc_minus_disc = float((x * y).sum())
    n0 = len(x)
    tx = n0 - float((x == 0).sum())
    ty = n0 - float((y == 0).sum())
    den = np.sqrt(tx * ty)
    return conc_minus_disc / den if den > 0 else 1.0


def ndcg_at_k(ranking: Sequence[int], gains: dict[int, float], k: int) -> float:
    dcg = sum(gains.get(doc, 0.0) / np.log2(i + 2) for i, doc in enumerate(list(ranking)[:k]))
    ideal = sorted(gains.values(), reverse=True)[:k]
    idcg = sum(g / np.log2(i + 2) for i, g in enumerate(ideal))
    return float(dcg / idcg) if idcg > 0 else 0.0


def recall_at_k(ranking: Sequence[int], relevant: set[int], k: int) -> float:
    return len(set(list(ranking)[:k]) & relevant) / len(relevant) if relevant else 0.0


@dataclass(frozen=True)
class RankingAgreement:
    top10_overlap: float
    top50_overlap: float
    spearman_top100: float
    kendall_top100: float
    ndcg10_agreement: float
    n_queries: int

    def as_dict(self) -> dict:
        return asdict(self)


def ranking_agreement(ref_scores: np.ndarray, rx_scores: np.ndarray) -> RankingAgreement:
    """``*_scores[q, d]`` — query×doc score matrices of the reference and of the RX580."""
    t10, t50, sp, kt, nd = [], [], [], [], []
    for r, x in zip(np.asarray(ref_scores), np.asarray(rx_scores)):
        rr = np.argsort(-r, kind="mergesort")
        xr = np.argsort(-x, kind="mergesort")
        t10.append(len(set(rr[:10]) & set(xr[:10])) / 10.0)
        t50.append(len(set(rr[:50]) & set(xr[:50])) / 50.0)
        union = np.array(sorted(set(rr[:100]) | set(xr[:100])))
        sp.append(spearman(r[union], x[union]))
        kt.append(kendall(r[union], x[union]))
        lo = r.min()
        gains = {int(d): float(r[d] - lo) for d in rr[:10]}
        nd.append(ndcg_at_k(xr, gains, 10))
    return RankingAgreement(float(np.mean(t10)), float(np.mean(t50)), float(np.mean(sp)), float(np.mean(kt)),
                            float(np.mean(nd)), len(t10))


def vector_agreement(ref: np.ndarray, rx: np.ndarray) -> dict[str, float]:
    c = np.sum(l2_normalize(ref) * l2_normalize(rx), axis=-1)
    return {"cos_mean": float(c.mean()), "cos_p1": float(np.percentile(c, 1)), "cos_min": float(c.min()),
            "n": int(c.size)}


def dense_parity(q_ref: np.ndarray, d_ref: np.ndarray, q_rx: np.ndarray, d_rx: np.ndarray) -> dict:
    out = {"query_vectors": vector_agreement(q_ref, q_rx), "doc_vectors": vector_agreement(d_ref, d_rx)}
    ref_s = l2_normalize(q_ref) @ l2_normalize(d_ref).T
    rx_s = l2_normalize(q_rx) @ l2_normalize(d_rx).T
    out["ranking"] = ranking_agreement(ref_s, rx_s).as_dict()
    return out


def late_parity(q_ref: list[np.ndarray], d_ref: list[np.ndarray], q_rx: list[np.ndarray],
                d_rx: list[np.ndarray]) -> dict:
    tok_q = np.concatenate([np.sum(a * b, axis=1) for a, b in zip(q_ref, q_rx)])
    tok_d = np.concatenate([np.sum(a * b, axis=1) for a, b in zip(d_ref, d_rx)])
    ref_s = np.array([[maxsim(q, d) for d in d_ref] for q in q_ref])
    rx_s = np.array([[maxsim(q, d) for d in d_rx] for q in q_rx])
    return {"query_tokens": {"cos_mean": float(tok_q.mean()), "cos_p1": float(np.percentile(tok_q, 1)),
                             "cos_min": float(tok_q.min()), "n": int(tok_q.size)},
            "doc_tokens": {"cos_mean": float(tok_d.mean()), "cos_p1": float(np.percentile(tok_d, 1)),
                           "cos_min": float(tok_d.min()), "n": int(tok_d.size)},
            "ranking": ranking_agreement(ref_s, rx_s).as_dict()}


def gate(result: dict, thresholds: dict[str, float] | None = None) -> dict:
    """PASS/FAIL of one parity result against explicit thresholds (MODEL_CHOICE, recorded with the verdict)."""
    th = {**GATE, **(thresholds or {})}
    vec = result.get("doc_vectors") or result.get("doc_tokens") or {}
    rk = result.get("ranking", {})
    checks = {
        "cos_mean": vec.get("cos_mean", 0.0) >= th["cos_mean_min"],
        "cos_p1": vec.get("cos_p1", 0.0) >= th["cos_p1_min"],
        "top10_overlap": rk.get("top10_overlap", 0.0) >= th["top10_overlap_min"],
        "top50_overlap": rk.get("top50_overlap", 0.0) >= th["top50_overlap_min"],
        "spearman": rk.get("spearman_top100", 0.0) >= th["spearman_min"],
    }
    return {"verdict": "PASS" if all(checks.values()) else "FAIL", "checks": checks, "thresholds": th}
