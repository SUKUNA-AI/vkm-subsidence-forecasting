"""FastAPI application of the RX580 retrieval service (постановка лаборатории §56).

Endpoints: ``GET /health`` (no auth), ``GET /model-info``, ``POST /embed/query``, ``POST /search/dense``,
``POST /search/hybrid``, ``POST /search/late``, ``GET /metrics`` (no auth). With a configured token every other
endpoint needs ``Authorization: Bearer <token>`` (compared in constant time).

``POST /search/late`` with ``targets`` (``[{"id", "kind"}]``, kind UNIT | PAGE | FIGURE | TABLE | FORMULA |
BIB_ENTRY) is the late stage of the VKM API's hybrid search: the late query encoding and MaxSim against the token-vector
pack (``search.multivector_dir``: memory-mapped, hot-reloaded from ``packs/CURRENT``). No store, no pack or a pack of
another model → HTTP 503, never a silent fallback; ``/health`` reports the store as ``late_store``.

Every encode runs in a worker thread (``asyncio.to_thread``); dense and late encodes of one request run concurrently
(``asyncio.gather``) and requests of different clients are never serialised by the service — there is no global lock.

No ``from __future__ import annotations``: route functions are defined inside :func:`create_app` next to the lazily
imported FastAPI types and FastAPI must see evaluated annotations.
"""
import asyncio
import hmac
import json
import time
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from vkm_corpus.retrieval_service import SERVICE_NAME, SERVICE_VERSION
from vkm_corpus.retrieval_service.metrics import Metrics

MAX_QUERY_CHARS = 4096


class EmbedQueryRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    role: Literal["dense", "late", "both"] = "dense"
    include_vectors: bool = True


class SearchRequestBody(BaseModel):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    k: int = Field(default=20, ge=1, le=200)
    k_stage: int = Field(default=100, ge=1, le=1000)
    filters: Optional[dict[str, Any]] = None


class LateTarget(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    kind: Literal["UNIT", "PAGE", "FIGURE", "TABLE", "FORMULA", "BIB_ENTRY"] = "UNIT"


class LateSearchBody(SearchRequestBody):
    candidates: Optional[list[str]] = Field(default=None, max_length=1000)
    targets: Optional[list[LateTarget]] = Field(default=None, max_length=1000)
    # CP-42: a page scores over its units except these kinds (a reference list is not the page's topic)
    page_exclude_kinds: list[Literal["BLOCK_GROUP", "FIGURE", "TABLE", "FORMULA", "BIB_ENTRY"]] = \
        Field(default_factory=lambda: ["BIB_ENTRY"], max_length=5)


def _round(v, nd: int = 6):
    return [round(float(x), nd) for x in v]


def create_app(config=None, *, encoders: Optional[dict] = None, residency=None, searcher=None, store=None,
               start_residency: bool = True, token: Optional[str] = None):
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse, PlainTextResponse

    metrics = Metrics()
    state: dict[str, Any] = {"config": config, "encoders": encoders or {}, "residency": residency,
                             "searcher": searcher, "store": store, "started_at": time.time(), "ready": False}
    auth_token = token if token is not None else (config.token if config is not None else None)

    def _gauges():
        res = state["residency"]
        if res is None:
            return []
        h = res.health()
        out = [("device_vram_used_mib", {}, h["device"]["vram_used_mib"]),
               ("device_vram_total_mib", {}, h["device"]["vram_total_mib"]),
               ("device_runtime_active", {}, 1.0 if h["device"]["runtime_status"] == "active" else 0.0)]
        for m in h["models"]:
            lab = {"role": m["role"], "model": m["key"]}
            out += [("model_loaded", lab, 1.0 if m["loaded"] else 0.0),
                    ("model_resident", lab, 1.0 if m["resident"] else 0.0),
                    ("model_vram_mib", lab, m["vram_mib"]), ("model_restarts", lab, m["restarts"]),
                    ("keepalive_sent", lab, m["keepalive_sent"]), ("keepalive_failed", lab, m["keepalive_failed"])]
        return out

    metrics.gauge_source(_gauges)

    async def lifespan(app):
        res = state["residency"]
        if res is not None and start_residency:
            await asyncio.to_thread(res.start)
        state["ready"] = True
        try:
            yield
        finally:
            if res is not None:
                await asyncio.to_thread(res.stop)

    from contextlib import asynccontextmanager

    app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION, lifespan=asynccontextmanager(lifespan))

    def require_token(request: Request) -> None:
        if not auth_token:
            return
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(supplied.encode(), auth_token.encode()):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    def encoder(role: str):
        enc = state["encoders"].get(role)
        if enc is None:
            raise HTTPException(status_code=404, detail=f"no {role} model is configured")
        return enc

    async def encode(role: str, text: str):
        enc = encoder(role)
        res = state["residency"]
        if res is not None:
            res.note_request(role)
        t0 = time.perf_counter()
        try:
            out = await asyncio.to_thread(enc.encode, text)
        except Exception as exc:
            metrics.inc("encode_errors", role=role, model=enc.spec.key)
            raise HTTPException(status_code=503, detail=f"{role} encoder unavailable: {str(exc)[:200]}") from exc
        metrics.observe_ms("encode", (time.perf_counter() - t0) * 1e3, role=role, model=enc.spec.key)
        return out

    @app.middleware("http")
    async def count_requests(request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        path = request.url.path
        metrics.inc("http_requests", path=path, status=str(response.status_code))
        metrics.observe_ms("http", (time.perf_counter() - t0) * 1e3, path=path)
        return response

    @app.get("/health")
    async def health():
        res = state["residency"]
        body: dict[str, Any] = {"service": SERVICE_NAME, "version": SERVICE_VERSION, "ready": state["ready"],
                                "uptime_s": round(time.time() - state["started_at"], 1)}
        if res is not None:
            h = await asyncio.to_thread(res.health)
            body.update(h)
            models = h["models"]
        else:
            models = []
            for role, enc in state["encoders"].items():
                try:
                    ok = bool(enc.backend.health().get("http_status") == 200)
                except Exception:
                    ok = False
                models.append({"role": role, "key": enc.spec.key, "quant": enc.slot.quant, "loaded": ok,
                               "resident": ok, "vram_mib": None})
            body["models"] = models
        from vkm_corpus.retrieval_service.search import store_status

        body["late_store"] = await asyncio.to_thread(store_status, state["store"])
        all_ok = bool(models) and all(m["loaded"] and m["resident"] for m in models)
        body["status"] = "ok" if all_ok and state["ready"] else ("degraded" if any(m["loaded"] for m in models)
                                                                  else "down")
        return JSONResponse(body, status_code=200 if body["status"] != "down" else 503)

    @app.get("/model-info", dependencies=[Depends(require_token)])
    async def model_info():
        parity = {}
        cfg = state["config"]
        if cfg is not None and cfg.parity_receipt and Path(cfg.parity_receipt).is_file():
            parity = json.loads(Path(cfg.parity_receipt).read_text(encoding="utf-8"))
        out = []
        for role, enc in state["encoders"].items():
            spec = enc.spec
            out.append({"role": role, "key": spec.key, "model_id": spec.model_id, "model_revision": spec.model_revision,
                        "license": spec.license, "family": spec.family, "arch": spec.arch,
                        "quant": enc.slot.quant, "weights_file": Path(enc.slot.gguf).name,
                        "weights_sha256": enc.slot.gguf_sha256, "dimension": enc.qconfig.dimension,
                        "query_signature": enc.signature, "query_config": enc.qconfig.as_dict(),
                        "parity": parity.get(spec.key)})
        return {"service": SERVICE_NAME, "version": SERVICE_VERSION, "models": out}

    @app.post("/embed/query", dependencies=[Depends(require_token)])
    async def embed_query(body: EmbedQueryRequest):
        roles = ["dense", "late"] if body.role == "both" else [body.role]
        outs = await asyncio.gather(*(encode(r, body.text) for r in roles))
        resp: dict[str, Any] = {"layer": "SERVICE", "payload_form": "vectors"}
        for r, o in zip(roles, outs):
            item: dict[str, Any] = {"model": o.key, "signature": o.signature, "n_tokens": o.n_tokens,
                                    "encode_ms": o.encode_ms}
            if o.vector is not None:
                item["dimension"] = int(o.vector.shape[-1])
                if body.include_vectors:
                    item["vector"] = _round(o.vector)
            else:
                item["token_vectors"] = int(o.vectors.shape[0])
                item["dimension"] = int(o.vectors.shape[1])
                if body.include_vectors:
                    item["vectors"] = [_round(row, 5) for row in o.vectors]
            resp[r] = item
        return resp

    def searcher_or_503():
        s = state["searcher"]
        if s is None:
            raise HTTPException(status_code=503, detail="search backend is not configured")
        return s

    @app.post("/search/dense", dependencies=[Depends(require_token)])
    async def search_dense(body: SearchRequestBody):
        s = searcher_or_503()
        q = await encode("dense", body.query)
        try:
            hits = await asyncio.to_thread(s.dense, q.vector, body.k, body.filters)
        except LookupError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        for i, h in enumerate(hits, start=1):
            h.trace["dense_rank"] = i
        return {"mode": "dense", "model": q.key, "query_signature": q.signature,
                "timings_ms": {"encode": q.encode_ms}, "hits": [h.as_dict() for h in hits]}

    async def _hybrid(body: SearchRequestBody, k: int):
        s = searcher_or_503()
        q = await encode("dense", body.query)
        t0 = time.perf_counter()
        try:
            hits = await asyncio.to_thread(s.hybrid, body.query, q.vector, k, k_stage=body.k_stage,
                                           filters=body.filters)
        except LookupError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return q, hits, round((time.perf_counter() - t0) * 1e3, 2)

    @app.post("/search/hybrid", dependencies=[Depends(require_token)])
    async def search_hybrid(body: SearchRequestBody):
        q, hits, ms = await _hybrid(body, body.k)
        return {"mode": "hybrid", "fusion": "rrf", "model": q.key, "query_signature": q.signature,
                "timings_ms": {"encode": q.encode_ms, "search": ms}, "hits": [h.as_dict() for h in hits]}

    @app.post("/search/late", dependencies=[Depends(require_token)])
    async def search_late(body: LateSearchBody):
        from vkm_corpus.retrieval_service.search import Hit, current_store, late_rerank

        encoder("late")                                   # 404 before any work when no late model is configured
        try:
            store = await asyncio.to_thread(current_store, state["store"])
        except LookupError as exc:
            raise HTTPException(status_code=503, detail=str(exc)[:300]) from exc
        timings: dict[str, float] = {}
        if body.targets is not None:
            lq = await encode("late", body.query)
            t0 = time.perf_counter()
            targets = [(t.id, t.kind) for t in body.targets]
            scored = await asyncio.to_thread(store.score_targets, lq.vectors, targets,
                                             page_exclude_kinds=tuple(body.page_exclude_kinds))
            ms = round((time.perf_counter() - t0) * 1e3, 2)
            metrics.observe_ms("maxsim", ms, model=lq.key)
            order = sorted((r for r in scored if r.status == "SCORED"), key=lambda r: (-r.late_score, r.id))
            rank = {r.id: i for i, r in enumerate(order, start=1)}
            info = store.info() if hasattr(store, "info") else {}
            return {"mode": "late-targets", "model": lq.key, "query_signature": lq.signature,
                    "n_query_tokens": int(lq.vectors.shape[0]), "store": info,
                    "timings_ms": {"late_encode": lq.encode_ms, "maxsim": ms},
                    "targets": len(targets), "scored": len(order), "unscored": len(targets) - len(order),
                    "page_exclude_kinds": list(body.page_exclude_kinds),
                    "results": [{**r.as_dict(), "late_rank": rank.get(r.id)} for r in scored]}
        if body.candidates:
            cands = [Hit(c, 0.0, {}, {"candidate_rank": i}) for i, c in enumerate(body.candidates, start=1)]
            lq = await encode("late", body.query)
        else:   # hybrid candidates and the late query encoding run concurrently (dense + late on the RX580)
            (dq, cands, ms), lq = await asyncio.gather(_hybrid(body, body.k_stage), encode("late", body.query))
            timings.update({"dense_encode": dq.encode_ms, "search": ms})
        timings["late_encode"] = lq.encode_ms
        t0 = time.perf_counter()
        ranked, missing = await asyncio.to_thread(late_rerank, lq.vectors, cands, store)
        timings["maxsim"] = round((time.perf_counter() - t0) * 1e3, 2)
        return {"mode": "late", "model": lq.key, "query_signature": lq.signature, "timings_ms": timings,
                "candidates": len(cands), "candidates_without_tokens": missing,
                "hits": [h.as_dict() for h in ranked[: body.k]]}

    @app.get("/metrics")
    async def prom_metrics():
        text = await asyncio.to_thread(metrics.render)
        return PlainTextResponse(text, media_type="text/plain; version=0.0.4")

    app.state.vkm = state
    app.state.metrics = metrics
    return app


def build_production_app():
    """App from ``VKM_RX580_CONFIG``: spawns the resident llama-servers, builds encoders, search and token store."""
    from vkm_corpus.retrieval_service.config import load_config
    from vkm_corpus.retrieval_service.encoders import QueryEncoder
    from vkm_corpus.retrieval_service.residency import ResidencyManager
    from vkm_corpus.retrieval_service.search import HttpSearchBackend, Searcher

    from vkm_corpus.embeddings.specs import get

    cfg = load_config()
    poolings = {slot.role: ("none" if (slot.role == "late" or get(slot.key).pooling == "none")
                            else get(slot.key).pooling) for slot in cfg.models}
    residency = ResidencyManager(cfg, poolings=poolings, keepalive_ids={})
    residency.build()
    encs: dict[str, QueryEncoder] = {}
    for slot in cfg.models:
        enc = QueryEncoder.from_slot(slot, residency.processes[slot.role].client)
        if enc.pooling != poolings[slot.role]:
            raise RuntimeError(f"pooling mismatch for {slot.role}: {enc.pooling} != {poolings[slot.role]}")
        residency.keepalive_ids[slot.role] = enc.keepalive_ids()
        encs[slot.role] = enc
    searcher = None
    if cfg.search.opensearch_url:
        searcher = Searcher(HttpSearchBackend(cfg.search.opensearch_url), cfg.search.text_index,
                            cfg.search.text_fields, cfg.search.dense_index, cfg.search.dense_field,
                            cfg.search.id_field, cfg.search.rrf_k)
    store = None
    if cfg.search.multivector_dir:        # memory-mapped pack (never the Parquet rows in RAM), hot-reloaded
        from vkm_corpus.embeddings.pack import PackHandle

        late = encs.get("late")
        expect = None
        if late is not None:
            q = late.qconfig
            expect = {"model_id": q.model_id, "model_revision": q.model_revision, "weights_sha256": q.weights_sha256,
                      "quantization": q.quantization, "tokenizer_sha256": q.tokenizer_sha256,
                      "heads_sha256": q.heads_sha256, "dimension": q.dimension}
        store = PackHandle(cfg.search.multivector_dir, expect=expect, check_s=cfg.search.multivector_check_s,
                           rss_budget_bytes=cfg.search.multivector_rss_budget_mib << 20)
    return create_app(cfg, encoders=encs, residency=residency, searcher=searcher, store=store)
