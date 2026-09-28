"""Candidate pipelines A–I (task §10), late-only and BGE-M3 unified baselines, with a per-query rank trace (§54).

All first stages see precomputed query representations (encoded once per model and run) and return
``[(unit_id, score)]``. Stages: first retrievers → fusion (RRF | weighted) → late MaxSim over the fused top-N →
EDGE reranker over the top-24. A stage that cannot run (no reranker configured) makes the whole pipeline ``NOT_RUN``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from vkm_corpus.retrieval_lab import fusion
from vkm_corpus.retrieval_lab.rerank import MAX_CANDIDATES, NOT_RUN, RerankUnavailable
from vkm_corpus.retrieval_lab.vectors import DenseIndex, LateIndex, SparseIndex, maxsim

Ranked = list[tuple[str, float]]


@dataclass(frozen=True)
class PipelineConfig:
    name: str
    first: tuple[str, ...]                      # retriever roles: bm25, dense, sparse, late, m3
    fusion: str | None = None                   # rrf | weighted | None (single first stage)
    rrf_k: int = 60
    weights: tuple[tuple[str, float], ...] = ()
    normalization: str = "minmax"
    late: bool = False
    late_mode: str = "replace"                  # replace: order by MaxSim; rrf: fuse MaxSim rank with fused rank
    rerank: bool = False
    first_depth: int = 100
    late_depth: int = 100
    rerank_depth: int = MAX_CANDIDATES
    final_depth: int = 100

    @classmethod
    def from_json(cls, name: str, obj: Mapping[str, Any]) -> "PipelineConfig":
        obj = dict(obj)
        obj["first"] = tuple(obj["first"])
        obj["weights"] = tuple(sorted((obj.get("weights") or {}).items()))
        return cls(name=name, **obj)


DEFAULT_PIPELINES: dict[str, PipelineConfig] = {
    "A": PipelineConfig("A", ("bm25",)),
    "B": PipelineConfig("B", ("dense",)),
    "B-late": PipelineConfig("B-late", ("late",)),
    "C": PipelineConfig("C", ("bm25", "dense"), fusion="rrf"),
    "D": PipelineConfig("D", ("bm25", "dense", "sparse"), fusion="rrf"),
    "E": PipelineConfig("E", ("bm25", "dense"), fusion="rrf", late=True),
    "F": PipelineConfig("F", ("bm25", "dense", "sparse"), fusion="rrf", late=True),
    "G": PipelineConfig("G", ("bm25", "dense"), fusion="rrf", rerank=True),
    "H": PipelineConfig("H", ("bm25", "dense"), fusion="rrf", late=True, rerank=True),
    "I": PipelineConfig("I", ("bm25", "dense", "sparse"), fusion="rrf", late=True, rerank=True),
    "M3-unified": PipelineConfig("M3-unified", ("m3",)),
}


# ---------------------------------------------------------------- retrievers over precomputed query representations
@dataclass
class TextRetriever:
    """BM25-like: searches with the query text."""
    backend: Any                                   # LocalBM25 | OpenSearchBM25
    texts: Mapping[str, str]

    def search(self, qid: str, k: int) -> Ranked:
        return self.backend.search(self.texts[qid], k)


@dataclass
class DenseRetriever:
    index: DenseIndex
    queries: Mapping[str, np.ndarray]

    def search(self, qid: str, k: int) -> Ranked:
        return self.index.search(self.queries[qid], k)


@dataclass
class SparseRetriever:
    index: SparseIndex
    queries: Mapping[str, Mapping[str, float]]

    def search(self, qid: str, k: int) -> Ranked:
        return self.index.search(self.queries[qid], k)


@dataclass
class LateRetriever:
    index: LateIndex
    queries: Mapping[str, np.ndarray]

    def search(self, qid: str, k: int) -> Ranked:
        return self.index.score(self.queries[qid])[:k]

    def rescore(self, qid: str, candidates: Sequence[str]) -> Ranked:
        return self.index.score(self.queries[qid], candidates)


@dataclass
class M3UnifiedRetriever:
    """BGE-M3 dense + sparse + multi-vector of one model: candidates = dense top-N ∪ sparse top-N, score =
    w_d·cos + w_s·lexical + w_m·MaxSim/|q| (weights: BGE-M3 paper start 0.4/0.2/0.4, tuned on train)."""
    dense: DenseRetriever
    sparse: SparseRetriever
    late: LateRetriever
    weights: tuple[float, float, float] = (0.4, 0.2, 0.4)

    def search(self, qid: str, k: int) -> Ranked:
        cands = {u for u, _ in self.dense.search(qid, k)} | {u for u, _ in self.sparse.search(qid, k)}
        if not cands:
            return []
        d = dict(self.dense.index.search(self.dense.queries[qid], len(self.dense.index.ids), allowed=cands))
        s_all = dict(self.sparse.index.search(self.sparse.queries[qid], len(self.sparse.index.ids)))
        q_tok = self.late.queries[qid]
        m = dict(self.late.index.score(q_tok, sorted(cands)))
        n_q = max(len(q_tok), 1)
        wd, ws, wm = self.weights
        scored = [(u, wd * d.get(u, 0.0) + ws * s_all.get(u, 0.0) + wm * m.get(u, 0.0) / n_q) for u in cands]
        return sorted(scored, key=lambda x: (-x[1], x[0]))[:k]


# ---------------------------------------------------------------- run
@dataclass
class PipelineResult:
    pipeline: str
    query_id: str
    status: str
    ranked: Ranked
    trace: dict[str, dict[str, Any]] = field(default_factory=dict)   # unit_id → {stage: [rank, score]}
    timings_ms: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _note_stage(trace: dict[str, dict[str, Any]], stage: str, ranked: Ranked) -> None:
    for rank, (uid, score) in enumerate(ranked, 1):
        trace.setdefault(uid, {})[stage] = [rank, round(float(score), 6)]


def run_pipeline(cfg: PipelineConfig, qid: str, query_text: str, retrievers: Mapping[str, Any], *,
                 reranker: Any = None, rerank_text: Callable[[str], str] | None = None) -> PipelineResult:
    """One query through one pipeline. ``retrievers``: role → retriever (``bm25``, ``dense``, ``sparse``, ``late``,
    ``m3``); ``rerank_text``: unit_id → passage text for the reranker (rule rerank_text_v1 of the unit's objects)."""
    trace: dict[str, dict[str, Any]] = {}
    timings: dict[str, float] = {}
    lists: dict[str, Ranked] = {}
    for role in cfg.first:
        if role not in retrievers:
            return PipelineResult(cfg.name, qid, NOT_RUN, [], notes=[f"missing retriever {role}"])
        t0 = time.perf_counter()
        lists[role] = retrievers[role].search(qid, cfg.first_depth)
        timings[role] = (time.perf_counter() - t0) * 1000
        _note_stage(trace, role, lists[role])
    if cfg.fusion and len(lists) > 1:
        t0 = time.perf_counter()
        if cfg.fusion == "rrf":
            fused = fusion.rrf(lists, k=cfg.rrf_k, weights=dict(cfg.weights) or None)
        elif cfg.fusion == "weighted":
            fused = fusion.weighted(lists, dict(cfg.weights) or {r: 1.0 / len(lists) for r in lists},
                                    normalization=cfg.normalization)
        else:
            raise ValueError(f"unknown fusion {cfg.fusion}")
        fused = fused[:cfg.first_depth]
        timings["fusion"] = (time.perf_counter() - t0) * 1000
        _note_stage(trace, "fused", fused)
    else:
        fused = next(iter(lists.values())) if lists else []
    current = fused
    if cfg.late:
        late = retrievers.get("late")
        if late is None:
            return PipelineResult(cfg.name, qid, NOT_RUN, [], trace, timings, ["missing late retriever"])
        t0 = time.perf_counter()
        head = [u for u, _ in current[:cfg.late_depth]]
        rescored = late.rescore(qid, head)
        if cfg.late_mode == "rrf":
            rescored = fusion.rrf({"fused": current[:cfg.late_depth], "late": rescored}, k=cfg.rrf_k)
        head_set = set(head)
        current = rescored + [x for x in current[cfg.late_depth:] if x[0] not in head_set]
        timings["late"] = (time.perf_counter() - t0) * 1000
        _note_stage(trace, "late", rescored)
    if cfg.rerank:
        if reranker is None or rerank_text is None:
            return PipelineResult(cfg.name, qid, NOT_RUN, [], trace, timings, ["reranker not configured"])
        t0 = time.perf_counter()
        head = current[:cfg.rerank_depth]
        try:
            reranked = reranker.rerank(query_text, [(u, rerank_text(u)) for u, _ in head], request_id=f"lab-{qid}")
        except RerankUnavailable as exc:
            return PipelineResult(cfg.name, qid, NOT_RUN, [], trace, timings, [str(exc)])
        seen = {u for u, _ in reranked}
        missing = [x for x in head if x[0] not in seen]          # empty-text candidates keep their order after
        current = reranked + missing + current[cfg.rerank_depth:]
        timings["rerank"] = (time.perf_counter() - t0) * 1000
        _note_stage(trace, "rerank", reranked)
    final = current[:cfg.final_depth]
    _note_stage(trace, "final", final)
    return PipelineResult(cfg.name, qid, "OK", final, {u: trace[u] for u, _ in final}, timings)
