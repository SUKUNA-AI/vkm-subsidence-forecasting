"""Hybrid retrieval: BM25 (E's indices) + dense k-NN (the vectors alias), fused by RRF, with a per-stage trace
(постановка лаборатории §52, §54; J's design §16).

* The query vector comes from the RX580 retrieval service (``POST /embed/query``, role ``dense``; ``VKM_EMBED_URL``).
  Its model key and dimension must match the ``_meta`` of the vectors build (the document encoder) — otherwise the
  request fails: a query encoded by another model is never compared with these vectors.
* Missing prerequisites fail loudly: no embed service, no vectors alias or a build that is not COMPLETE →
  :class:`HybridError` ``DEPENDENCY_UNAVAILABLE``. BM25-only results are never substituted silently (``search_text``
  is the BM25 path).
* Fusion keys are stable canonical ids: ``PAGE`` → page id (units of the page map to it, first occurrence wins; with
  duplicate collapsing, one page per ``dup_group_id`` as in the BM25 path); ``FIGURE`` / ``TABLE`` / ``FORMULA`` → the
  object id of the unit (one unit = one object). Per kind the BM25 list and the dense list are ranked separately and
  one RRF (k = 60 by default, J's design §16) sums the reciprocal ranks — kinds never compare raw scores (H-44).
* Filters: E's whitelist (``vkm_corpus.search.query.FILTERS``) on page-level fields, applied to both legs (vector
  documents carry the same fields); object-type filters (block_type, figure_type, layout_class, text_layer) have no
  page-level meaning and are refused.
* Late interaction (MaxSim over token vectors, RX580 ``/search/late``) is the next stage and is not run here; the
  EDGE text reranker stays the last stage (``rerank_text`` over the returned ``rerank_candidate``).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from vkm_corpus.retrieval_lab.fusion import rrf
from vkm_corpus.search.query import (MAX_SIZE, MAX_WINDOW, RRF_K, SearchRequest, SearchRequestError,
                                     compile_filters, search)
from vkm_corpus.search.vectors import (COMPLETE, UNIT_SOURCE_FIELDS, VECTOR_FIELD, alias_indices,
                                      list_vector_builds, vectors_alias)

HYBRID_KINDS: tuple[str, ...] = ("PAGE", "FIGURE", "TABLE", "FORMULA")
UNIT_KINDS: dict[str, tuple[str, ...] | None] = {"PAGE": None, "FIGURE": ("FIGURE",), "TABLE": ("TABLE",),
                                                 "FORMULA": ("FORMULA",)}
UNSUPPORTED_FILTERS = frozenset({"block_type", "figure_type", "layout_class", "text_layer"})
MAX_CANDIDATES = MAX_SIZE                 # per leg and kind (E's single-kind size limit)
PAGE_OVERSAMPLE = 3                       # units fetched per wanted page (several units of one page)
MAX_KNN = 1000
EMBED_TIMEOUT_S = 15.0


class HybridError(RuntimeError):
    """``code`` is an API error code: DEPENDENCY_UNAVAILABLE | DEPENDENCY_ERROR | DEPENDENCY_TIMEOUT."""

    def __init__(self, code: str, message: str, *, stage: str, tool: str,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.stage, self.tool = code, message, stage, tool
        self.details = dict(details or {})


# ---------------------------------------------------------------- query encoder (RX580 service)
@dataclass
class QueryVector:
    vector: list[float]
    model: str | None
    signature: str | None
    dimension: int
    encode_ms: float | None = None


class EmbedClient:
    """Client of the RX580 retrieval service ``POST /embed/query`` (httpx, sync; bearer token optional)."""

    def __init__(self, url: str | None, token: str | None = None, *, timeout: float = EMBED_TIMEOUT_S,
                 transport: Any = None) -> None:
        self.url = url
        self._http = None
        if url:
            import httpx

            headers = {"Accept": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            self._http = httpx.Client(base_url=url.rstrip("/"), headers=headers, transport=transport,
                                      timeout=httpx.Timeout(timeout, connect=3.0), trust_env=False)

    def embed_dense(self, text: str) -> QueryVector:
        import httpx

        if self._http is None:
            raise HybridError("DEPENDENCY_UNAVAILABLE", "VKM_EMBED_URL is not configured (RX580 retrieval service)",
                              stage="embed", tool="rx580-retrieval")
        try:
            r = self._http.post("/embed/query", json={"text": text, "role": "dense", "include_vectors": True})
        except httpx.TimeoutException as exc:
            raise HybridError("DEPENDENCY_TIMEOUT", "query encoder did not answer in time", stage="embed",
                              tool="rx580-retrieval") from exc
        except httpx.HTTPError as exc:
            raise HybridError("DEPENDENCY_UNAVAILABLE", f"query encoder not reachable ({type(exc).__name__})",
                              stage="embed", tool="rx580-retrieval") from exc
        if r.status_code in (401, 403):
            raise HybridError("DEPENDENCY_ERROR", "query encoder refused the token (VKM_EMBED_TOKEN_FILE)",
                              stage="embed", tool="rx580-retrieval", details={"status": r.status_code})
        if r.status_code != 200:
            code = "DEPENDENCY_UNAVAILABLE" if r.status_code in (404, 502, 503) else "DEPENDENCY_ERROR"
            raise HybridError(code, f"query encoder answered HTTP {r.status_code}", stage="embed",
                              tool="rx580-retrieval", details={"status": r.status_code})
        try:
            dense = r.json()["dense"]
            vector = [float(x) for x in dense["vector"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise HybridError("DEPENDENCY_ERROR", "query encoder answer has no dense vector", stage="embed",
                              tool="rx580-retrieval") from exc
        return QueryVector(vector, dense.get("model"), dense.get("signature"), len(vector), dense.get("encode_ms"))

    def health(self) -> dict[str, Any]:
        """``GET /health`` of the service (no token): status and the resident models (role, key, quant)."""
        import httpx

        if self._http is None:
            return {"available": False, "error": "NOT_CONFIGURED"}
        try:
            body = self._http.get("/health", timeout=3.0).json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"available": False, "error": type(exc).__name__}
        return {"available": True, "status": body.get("status"),
                "models": [{k: m.get(k) for k in ("role", "key", "quant", "loaded", "resident")}
                           for m in body.get("models") or []]}

    def close(self) -> None:
        if self._http is not None:
            self._http.close()


# ---------------------------------------------------------------- request
@dataclass
class HybridRequest:
    query: str
    kinds: tuple[str, ...] = ("PAGE",)
    filters: dict[str, Any] = field(default_factory=dict)
    size: int = 20
    offset: int = 0
    candidates: int = 100                  # k per leg and kind (J's design §16: first stages top-100)
    rrf_k: int = RRF_K
    include_duplicates: bool = False
    exact: bool = False

    def validate(self) -> None:
        if not self.query or not self.query.strip() or len(self.query) > 512:
            raise SearchRequestError("E_BAD_QUERY", "query must be 1..512 characters")
        self.kinds = tuple(k.upper() for k in self.kinds)
        if not self.kinds or any(k not in HYBRID_KINDS for k in self.kinds) or len(set(self.kinds)) != len(self.kinds):
            raise SearchRequestError("E_BAD_KIND", f"hybrid kinds must be distinct values of {HYBRID_KINDS} (blocks "
                                                   "are fused at page level: use PAGE)")
        if not 1 <= self.candidates <= MAX_CANDIDATES or not 1 <= self.rrf_k <= 1000:
            raise SearchRequestError("E_BAD_SIZE", f"candidates 1..{MAX_CANDIDATES}, rrf_k 1..1000")
        if not 1 <= self.size <= 50 or self.offset < 0 or self.offset + self.size > MAX_WINDOW:
            raise SearchRequestError("E_BAD_SIZE", f"size 1..50 and offset + size ≤ {MAX_WINDOW}")
        bad = sorted(set(self.filters) & UNSUPPORTED_FILTERS)
        if bad:
            raise SearchRequestError("E_BAD_FILTER", f"filters {bad} are object-type fields; hybrid search "
                                                             "filters on page-level fields only")
        compile_filters(self.filters)


# ---------------------------------------------------------------- vectors build
def vectors_meta(client: Any, prefix: str) -> dict[str, Any]:
    """``_meta`` of the aliased vectors build (+ index name); DEPENDENCY_UNAVAILABLE if absent or incomplete."""
    alias = vectors_alias(prefix)
    try:
        targets = alias_indices(client, alias)
        builds = {b["index"]: b["meta"] for b in list_vector_builds(client, prefix)} if targets else {}
    except HybridError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported without the address
        raise HybridError("DEPENDENCY_UNAVAILABLE", f"search index not reachable ({type(exc).__name__})",
                          stage="vectors", tool="opensearch") from exc
    if len(targets) != 1:
        raise HybridError("DEPENDENCY_UNAVAILABLE", f"vectors alias {alias} is not built yet (search build-vectors)",
                          stage="vectors", tool="opensearch", details={"alias": alias, "indices": targets})
    meta = builds.get(targets[0]) or {}
    if meta.get("build_status") != COMPLETE or not meta.get("dimension"):
        raise HybridError("DEPENDENCY_UNAVAILABLE", "the aliased vectors build is not COMPLETE", stage="vectors",
                          tool="opensearch", details={"index": targets[0]})
    return {**meta, "index": targets[0], "alias": alias}


def check_encoder(meta: dict[str, Any], q: QueryVector) -> None:
    if int(meta["dimension"]) != q.dimension:
        raise HybridError("DEPENDENCY_ERROR", f"query vector has {q.dimension} dimensions, the vectors build "
                          f"{meta['dimension']}", stage="embed", tool="rx580-retrieval",
                          details={"query_model": q.model, "index_model": meta.get("model_key")})
    if meta.get("model_key") and q.model and meta["model_key"] != q.model:
        raise HybridError("DEPENDENCY_ERROR", f"query encoder {q.model} differs from the document encoder "
                          f"{meta['model_key']} of the vectors build", stage="embed", tool="rx580-retrieval",
                          details={"query_model": q.model, "index_model": meta.get("model_key")})


def knn_body(vector: list[float], k: int, filters: dict[str, Any], unit_kinds: tuple[str, ...] | None) -> dict[str, Any]:
    clauses, must_not = compile_filters(filters)
    if unit_kinds:
        clauses = [*clauses, {"terms": {"unit_kind": list(unit_kinds)}}]
    knn: dict[str, Any] = {"vector": vector, "k": int(k)}
    if clauses or must_not:
        knn["filter"] = {"bool": {"filter": clauses, "must_not": must_not}}
    return {"size": int(k), "_source": {"includes": list(UNIT_SOURCE_FIELDS)}, "query": {"knn": {VECTOR_FIELD: knn}}}


@dataclass
class _Dense:
    key: str
    score: float
    unit_id: str
    unit_kind: str
    object_ids: list[str]
    source: dict[str, Any]


def dense_ranking(resp: dict[str, Any], kind: str, limit: int, *, collapse_duplicates: bool) -> list[_Dense]:
    """Units → fusion keys of ``kind`` (first occurrence wins), at most ``limit``."""
    out: list[_Dense] = []
    seen: set[str] = set()
    for h in resp.get("hits", {}).get("hits", []):
        src = h.get("_source") or {}
        objects = list(src.get("object_ids") or [])
        if kind == "PAGE":
            key = src.get("page_id")
            group = (src.get("dup_group_id") or key) if collapse_duplicates else key
        else:
            key = objects[0] if len(objects) == 1 else None
            group = key
        if not key or group in seen:
            continue
        seen.add(group)
        out.append(_Dense(key, float(h.get("_score") or 0.0), str(src.get("id") or h.get("_id")),
                          str(src.get("unit_kind")), objects, src))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- search
def hybrid_search(client: Any, embed: EmbedClient, req: HybridRequest, prefix: str, *,
                  meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """BM25 + dense → RRF; hits carry ids, the per-stage trace and E's highlights/best blocks when BM25 found them."""
    req.validate()
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    meta = meta or vectors_meta(client, prefix)
    timings["vectors_meta"] = round((time.perf_counter() - t0) * 1e3, 2)
    q = embed.embed_dense(req.query)
    check_encoder(meta, q)
    timings["embed"] = round((time.perf_counter() - t0) * 1e3 - timings["vectors_meta"], 2)
    rankings: dict[str, list[tuple[str, float]]] = {}
    bm25_hits: dict[str, Any] = {}
    dense_hits: dict[str, _Dense] = {}
    kind_of: dict[str, str] = {}
    totals: dict[str, dict[str, int]] = {}
    warnings: list[str] = []
    for kind in req.kinds:
        t1 = time.perf_counter()
        bm = search(client, SearchRequest(query=req.query, kinds=(kind,), filters=dict(req.filters),
                                          size=req.candidates, include_duplicates=req.include_duplicates,
                                          exact=req.exact), prefix)
        timings[f"bm25_{kind.lower()}"] = round((time.perf_counter() - t1) * 1e3, 2)
        warnings += bm.warnings
        bm_list = []
        for h in bm.hits:
            bm25_hits.setdefault(h.id, h)
            kind_of.setdefault(h.id, kind)
            bm_list.append((h.id, h.score))
        rankings[f"bm25:{kind}"] = bm_list
        t1 = time.perf_counter()
        k = min(MAX_KNN, req.candidates * (PAGE_OVERSAMPLE if kind == "PAGE" else 1))
        resp = client.search(index=meta["alias"], body=knn_body(q.vector, k, req.filters, UNIT_KINDS[kind]))
        timings[f"dense_{kind.lower()}"] = round((time.perf_counter() - t1) * 1e3, 2)
        dense = dense_ranking(resp, kind, req.candidates, collapse_duplicates=not req.include_duplicates)
        for d in dense:
            dense_hits.setdefault(d.key, d)
            kind_of.setdefault(d.key, kind)
        rankings[f"dense:{kind}"] = [(d.key, d.score) for d in dense]
        totals[kind] = {"bm25": int(bm.totals.get(kind, len(bm.hits))), "bm25_returned": len(bm.hits),
                        "dense_returned": len(dense)}
    fused = rrf(rankings, k=req.rrf_k)
    ranks = {name: {key: i for i, (key, _s) in enumerate(lst, 1)} for name, lst in rankings.items()}
    scores = {name: dict(lst) for name, lst in rankings.items()}
    hits = []
    for rank, (key, score) in enumerate(fused, 1):
        if rank <= req.offset:
            continue
        if len(hits) >= req.size:
            break
        kind = kind_of[key]
        bmh, dh = bm25_hits.get(key), dense_hits.get(key)
        trace = {"bm25_rank": ranks[f"bm25:{kind}"].get(key), "bm25_score": scores[f"bm25:{kind}"].get(key),
                 "dense_rank": ranks[f"dense:{kind}"].get(key), "dense_score": scores[f"dense:{kind}"].get(key),
                 "fused_rank": rank, "rrf_score": round(score, 8), "rrf_k": req.rrf_k,
                 "late_rank": None, "rerank_rank": None}
        if dh is not None:
            trace["dense_unit"] = {"unit_id": dh.unit_id, "unit_kind": dh.unit_kind, "object_ids": dh.object_ids}
        src = dh.source if dh is not None else {}
        hit: dict[str, Any] = {
            "id": key, "object_type": kind, "rank": rank, "score": round(score, 8),
            "page_id": (bmh.page_id if bmh else None) or (key if kind == "PAGE" else src.get("page_id")),
            "source_id": (bmh.source_id if bmh else None) or src.get("source_id"),
            "work_id": (bmh.work_id if bmh else None) or src.get("work_id"),
            "page_index": (bmh.page_index if bmh else None) or src.get("page_index"),
            "index": bmh.index if bmh else meta["index"], "build_id": bmh.build_id if bmh else meta["build_id"],
            "vectors_index": meta["index"], "vectors_build_id": meta["build_id"],
            "highlights": list(bmh.highlights) if bmh else [], "best_blocks": list(bmh.best_blocks) if bmh else [],
            "duplicates": list(bmh.duplicates) if bmh else [], "trace": trace}
        hits.append(hit)
    timings["total"] = round((time.perf_counter() - t0) * 1e3, 2)
    return {"query": req.query, "kinds": list(req.kinds), "hits": hits, "fusion": "RRF", "rrf_k": req.rrf_k,
            "candidates": req.candidates, "fused_total": len(fused), "totals": totals, "warnings": warnings,
            "stages": {"bm25": {"engine": "opensearch", "indices": "per-kind aliases of E"},
                       "dense": {"engine": "opensearch-knn", "alias": meta["alias"], "index": meta["index"],
                                 "build_id": meta["build_id"], "built_from_snapshot_id":
                                 meta.get("built_from_snapshot_id"), "config_signature": meta.get("config_signature"),
                                 "model_key": meta.get("model_key"), "query_model": q.model,
                                 "query_signature": q.signature, "dimension": q.dimension,
                                 "space_type": meta.get("space_type"), "encode_ms": q.encode_ms},
                       "late": "NOT_RUN (next stage: MaxSim over token vectors on the RX580)",
                       "rerank": "NOT_RUN here (EDGE text reranker: rerank_text over rerank_candidate)"},
            "timings_ms": timings}
