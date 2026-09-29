"""Search paths of the RX580 service (постановка лаборатории §44, §52–54): OpenSearch is the vector projection.

* dense — ``knn`` query on the dense field of the configured index (cosine space, vectors L2-normalised);
* hybrid — BM25 (``multi_match`` over the text fields) and dense k-NN, fused by RRF (k = 60, rank-based, no score
  mixing to tune on held-out data); every hit carries its per-stage ranks (explainability trace, §54);
* late — candidates (given ids or the hybrid top-N) re-scored with MaxSim of the late query vectors against the stored
  document token vectors (derived multivector artifacts; OpenSearch holds no token vectors);
* late targets — the late stage of the VKM API's hybrid search: pages (score = max MaxSim over the page's units) and
  figures/tables/formulas (their unit) scored against the token-vector pack (:mod:`vkm_corpus.embeddings.pack`,
  memory-mapped; the in-memory store is for tests and small sets only).

The OpenSearch transport is injected (``post(index, body) -> dict``) so the API contract is testable without a
cluster; production uses :class:`HttpSearchBackend` (httpx, loopback only).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence

import numpy as np

from vkm_corpus.embeddings.postprocess import maxsim


class SearchBackend(Protocol):
    def post(self, index: str, body: dict[str, Any]) -> dict[str, Any]: ...


class HttpSearchBackend:
    def __init__(self, url: str, *, timeout_s: float = 30.0) -> None:
        import httpx  # lazy

        self._client = httpx.Client(base_url=url.rstrip("/"), timeout=timeout_s)

    def post(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self._client.post(f"/{index}/_search", content=json.dumps(body), headers={"Content-Type": "application/json"})
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._client.close()


@dataclass
class Hit:
    object_id: str
    score: float
    source: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"object_id": self.object_id, "score": round(float(self.score), 6), "source": self.source,
                "trace": self.trace}


def _filters(filters: dict[str, Any] | None) -> list[dict[str, Any]]:
    out = []
    for k, v in (filters or {}).items():
        out.append({"terms": {k: list(v)}} if isinstance(v, (list, tuple)) else {"term": {k: v}})
    return out


def knn_body(field_name: str, vector: np.ndarray, k: int, *, filters: dict[str, Any] | None = None,
             source: Sequence[str] = ("id", "object_type", "source_id", "page_id")) -> dict[str, Any]:
    clause: dict[str, Any] = {"vector": [float(x) for x in np.asarray(vector).reshape(-1)], "k": int(k)}
    f = _filters(filters)
    if f:
        clause["filter"] = {"bool": {"filter": f}}
    return {"size": int(k), "_source": list(source), "query": {"knn": {field_name: clause}}}


def bm25_body(text: str, fields: Sequence[str], k: int, *, filters: dict[str, Any] | None = None,
              source: Sequence[str] = ("id", "object_type", "source_id", "page_id")) -> dict[str, Any]:
    q: dict[str, Any] = {"multi_match": {"query": text, "fields": list(fields), "type": "best_fields"}}
    f = _filters(filters)
    if f:
        q = {"bool": {"must": [q], "filter": f}}
    return {"size": int(k), "_source": list(source), "query": q}


def parse_hits(resp: dict[str, Any], id_field: str = "id") -> list[Hit]:
    hits = []
    for h in resp.get("hits", {}).get("hits", []):
        src = h.get("_source") or {}
        hits.append(Hit(str(src.get(id_field) or h.get("_id")), float(h.get("_score") or 0.0), src))
    return hits


def rrf(lists: dict[str, list[Hit]], k: int = 60) -> list[Hit]:
    """Reciprocal rank fusion; ties broken by object id (deterministic)."""
    fused: dict[str, Hit] = {}
    scores: dict[str, float] = {}
    for stage, hits in lists.items():
        for rank, h in enumerate(hits, start=1):
            scores[h.object_id] = scores.get(h.object_id, 0.0) + 1.0 / (k + rank)
            cur = fused.setdefault(h.object_id, Hit(h.object_id, 0.0, dict(h.source), {}))
            cur.trace[f"{stage}_rank"] = rank
            cur.trace[f"{stage}_score"] = round(h.score, 6)
    out = []
    for oid, s in scores.items():
        h = fused[oid]
        h.score = s
        out.append(h)
    out.sort(key=lambda h: (-h.score, h.object_id))
    for i, h in enumerate(out, start=1):
        h.trace["fused_rank"] = i
    return out


class MultiVectorStore(Protocol):
    def get(self, object_ids: Iterable[str]) -> dict[str, np.ndarray]: ...


class InMemoryMultiVectorStore:
    """Token matrices in RAM (tests and small sets; the corpus uses the memory-mapped pack). ``units`` (unit id →
    ``{"kind", "page_id", "object_ids"}``) enables page/object targets like :class:`~vkm_corpus.embeddings.pack.
    PackStore`."""

    def __init__(self, data: dict[str, np.ndarray] | None = None,
                 units: dict[str, dict[str, Any]] | None = None) -> None:
        self.data = {k: np.asarray(v, dtype=np.float16) for k, v in (data or {}).items()}
        self.units = dict(units or {})

    def get(self, object_ids: Iterable[str]) -> dict[str, np.ndarray]:
        return {o: self.data[o] for o in object_ids if o in self.data}

    def __len__(self) -> int:
        return len(self.data)

    def info(self) -> dict[str, Any]:
        return {"pack_id": None, "store": "memory", "count": len(self.data)}

    def rows_for(self, target_id: str, kind: str, *, page_exclude_kinds: Iterable[str] = ()) -> list[str]:
        if kind == "UNIT":
            return [target_id] if target_id in self.data else []
        if kind == "PAGE":
            skip = frozenset(page_exclude_kinds)
            return sorted(u for u, m in self.units.items() if m.get("page_id") == target_id and u in self.data
                          and m.get("kind") not in skip)
        return sorted(u for u, m in self.units.items() if m.get("kind") == kind and
                      list(m.get("object_ids") or []) == [target_id] and u in self.data)

    def kind_rows(self, kind: str) -> list[str]:
        return sorted(u for u, m in self.units.items() if m.get("kind") == kind and u in self.data)

    def scan(self, Q: np.ndarray, kind: str, *, top_pages: int = 100) -> list[dict[str, Any]]:
        """Pages ranked by their best unit of ``kind`` (MaxSim over every such unit), like ``PackStore.scan``."""
        best: dict[str, tuple[float, str]] = {}
        count: dict[str, int] = {}
        for u in self.kind_rows(kind):
            page = self.units[u].get("page_id")
            if not page:
                continue
            s = maxsim(Q, np.asarray(self.data[u], dtype=np.float32))
            count[page] = count.get(page, 0) + 1
            if page not in best or s > best[page][0]:          # unit ids ascending: a tie keeps the lower id
                best[page] = (s, u)
        ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[1][1]))[:top_pages]
        return [{"page_id": p, "late_score": round(float(s), 6), "best_unit_id": u, "units": count[p]}
                for p, (s, u) in ranked]

    def score_targets(self, Q: np.ndarray, targets: Sequence[tuple[str, str]], *,
                      page_exclude_kinds: Iterable[str] = ()) -> list[Any]:
        from vkm_corpus.embeddings.pack import TargetScore

        out = []
        for tid, kind in targets:
            units = self.rows_for(tid, kind, page_exclude_kinds=page_exclude_kinds)
            if not units:
                out.append(TargetScore(tid, kind, "NO_TOKENS"))
                continue
            scores = {u: maxsim(Q, np.asarray(self.data[u], dtype=np.float32)) for u in units}
            best = max(units, key=lambda u: (scores[u], u))
            out.append(TargetScore(tid, kind, "SCORED", scores[best], best, len(units),
                                   int(sum(self.data[u].shape[0] for u in units))))
        return out


def load_multivector_store(directory: str | Path) -> InMemoryMultiVectorStore:
    """Current rows of a multivector artifact directory as float16 token matrices, loaded into RAM — small sets and
    tests only (the whole corpus is ≈ 10 GB: the service uses :class:`~vkm_corpus.embeddings.pack.PackHandle`)."""
    from vkm_corpus.embeddings.artifacts import iter_current_vectors

    return InMemoryMultiVectorStore({oid: m for oid, m in iter_current_vectors(Path(directory))})


def current_store(store: Any) -> Any:
    """The store to use now: a :class:`~vkm_corpus.embeddings.pack.PackHandle` resolves to its current pack
    (``LookupError`` when none is loaded); any other store is used as is."""
    if store is None:
        raise LookupError("late-interaction token store is not configured (search.multivector_dir)")
    return store.current() if hasattr(store, "current") else store


def store_status(store: Any) -> dict[str, Any]:
    if store is None:
        return {"status": "NOT_CONFIGURED"}
    if hasattr(store, "status"):
        return store.status()
    return {"status": "READY", **(store.info() if hasattr(store, "info") else {"count": len(store)})}


def late_rerank(query_vectors: np.ndarray, candidates: list[Hit], store: MultiVectorStore) -> tuple[list[Hit], int]:
    """MaxSim re-scoring; candidates without stored token vectors keep their order after the scored ones."""
    # one BLAS product per candidate: with OpenBLAS numpy ≈ 6 ms per 100 candidates × 150 tokens × 128 dims (CORE);
    # stacking all candidates into one product was measured slower (concatenation + segment reduction)
    docs = store.get(h.object_id for h in candidates)
    scored, missing = [], []
    for h in candidates:
        m = docs.get(h.object_id)
        if m is None:
            missing.append(h)
            continue
        s = maxsim(query_vectors, np.asarray(m, dtype=np.float32))
        scored.append(Hit(h.object_id, s, h.source, {**h.trace, "late_score": round(s, 6)}))
    scored.sort(key=lambda h: (-h.score, h.object_id))
    for i, h in enumerate(scored, start=1):
        h.trace["late_rank"] = i
    return scored + missing, len(missing)


@dataclass
class Searcher:
    backend: SearchBackend | None
    text_index: str
    text_fields: Sequence[str]
    dense_index: str | None
    dense_field: str
    id_field: str = "id"
    rrf_k: int = 60

    def dense(self, vector: np.ndarray, k: int, filters: dict[str, Any] | None = None) -> list[Hit]:
        if self.backend is None or not self.dense_index:
            raise LookupError("dense index is not configured")
        return parse_hits(self.backend.post(self.dense_index, knn_body(self.dense_field, vector, k, filters=filters)),
                          self.id_field)

    def bm25(self, text: str, k: int, filters: dict[str, Any] | None = None) -> list[Hit]:
        if self.backend is None:
            raise LookupError("OpenSearch is not configured")
        return parse_hits(self.backend.post(self.text_index, bm25_body(text, self.text_fields, k, filters=filters)),
                          self.id_field)

    def hybrid(self, text: str, vector: np.ndarray, k: int, *, k_stage: int,
               filters: dict[str, Any] | None = None) -> list[Hit]:
        lists = {"bm25": self.bm25(text, k_stage, filters), "dense": self.dense(vector, k_stage, filters)}
        return rrf(lists, self.rrf_k)[:k]


Clock = Callable[[], float]
