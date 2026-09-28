"""Contract of the RX580 retrieval service with fake backends (no GPU, no OpenSearch): endpoints, auth, trace, and
the absence of a global lock (dense and late encodes run concurrently)."""
from __future__ import annotations

import asyncio
import time

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.embeddings.fakes import FakeBackend, FakeSearchBackend, FakeTokenizer  # noqa: E402
from vkm_corpus.embeddings.postprocess import l2_normalize  # noqa: E402
from vkm_corpus.embeddings.specs import get  # noqa: E402
from vkm_corpus.embeddings.tokenize import SpecTokenizer  # noqa: E402
from vkm_corpus.retrieval_service.app import create_app  # noqa: E402
from vkm_corpus.retrieval_service.config import ModelSlot  # noqa: E402
from vkm_corpus.retrieval_service.encoders import QueryEncoder  # noqa: E402
from vkm_corpus.retrieval_service.search import InMemoryMultiVectorStore, Searcher  # noqa: E402

DIM, HID, TOK = 16, 12, 8
DOCS = {"d1": "salt creep under load", "d2": "subsidence over potash mine", "d3": "InSAR monitoring of subsidence",
        "d4": "backfill of rooms"}


def _encoders(delay_s: float = 0.0):
    dense_spec = get("granite-311m-r2")
    dtok = SpecTokenizer(dense_spec, FakeTokenizer(bos="<bos>", eos=None))
    dense = QueryEncoder(ModelSlot(role="dense", key=dense_spec.key, gguf="g-Q8_0.gguf", quant="Q8_0",
                                   gguf_sha256="a" * 64, port=1), dense_spec, dtok,
                         FakeBackend(DIM, pooled=True, delay_s=delay_s, name="dense"))
    late_spec = get("jina-colbert-v2")
    ltok = SpecTokenizer(late_spec, FakeTokenizer(bos="<s>", eos="</s>", specials=(
        "<pad>", "<unk>", "<mask>", "[QueryMarker]", "[DocumentMarker]")))
    W = np.random.default_rng(5).standard_normal((TOK, HID)).astype(np.float32)
    late = QueryEncoder(ModelSlot(role="late", key=late_spec.key, gguf="j-Q8_0.gguf", quant="Q8_0",
                                  gguf_sha256="b" * 64, port=2), late_spec, ltok,
                        FakeBackend(HID, pooled=False, delay_s=delay_s, name="late"), heads={"colbert_w": W})
    return {"dense": dense, "late": late}


def _app(token=None, delay_s=0.0):
    encs = _encoders(delay_s)
    rng = np.random.default_rng(3)
    vectors = {k: l2_normalize(rng.standard_normal(DIM).astype(np.float32)) for k in DOCS}
    searcher = Searcher(FakeSearchBackend(DOCS, vectors), "vkm-blocks", ("text",), "vkm-exp-dense", "vector")
    store = InMemoryMultiVectorStore({k: l2_normalize(rng.standard_normal((5, TOK)).astype(np.float32))
                                      for k in ("d1", "d2", "d3")})
    return create_app(None, encoders=encs, searcher=searcher, store=store, token=token), encs


def test_health_reports_every_model():
    app, _ = _app()
    with TestClient(app) as c:
        h = c.get("/health").json()
    assert h["status"] == "ok" and {m["role"] for m in h["models"]} == {"dense", "late"}
    assert all(m["loaded"] and m["resident"] for m in h["models"])


def test_model_info_has_signatures_and_licenses():
    app, encs = _app()
    with TestClient(app) as c:
        info = c.get("/model-info").json()
    by = {m["role"]: m for m in info["models"]}
    assert by["dense"]["query_signature"] == encs["dense"].signature and len(by["late"]["query_signature"]) == 64
    assert by["late"]["license"] == "cc-by-nc-4.0" and by["dense"]["weights_sha256"] == "a" * 64


def test_embed_query_dense_late_both():
    app, _ = _app()
    with TestClient(app) as c:
        r = c.post("/embed/query", json={"text": "ползучесть соли", "role": "both"}).json()
        bad = c.post("/embed/query", json={"text": "", "role": "dense"})
    assert len(r["dense"]["vector"]) == DIM and abs(np.linalg.norm(r["dense"]["vector"]) - 1) < 1e-4
    assert r["late"]["token_vectors"] == 32 and r["late"]["dimension"] == TOK   # query expanded to query_maxlen
    assert bad.status_code == 422


def test_search_dense_hybrid_late_with_trace():
    app, _ = _app()
    with TestClient(app) as c:
        dense = c.post("/search/dense", json={"query": "subsidence", "k": 3}).json()
        hyb = c.post("/search/hybrid", json={"query": "subsidence potash", "k": 4, "k_stage": 4}).json()
        late = c.post("/search/late", json={"query": "subsidence potash", "k": 3, "k_stage": 4}).json()
        given = c.post("/search/late", json={"query": "x", "k": 5, "candidates": ["d3", "d4", "d1"]}).json()
    assert len(dense["hits"]) == 3 and dense["hits"][0]["trace"]["dense_rank"] == 1
    top = hyb["hits"][0]
    assert hyb["fusion"] == "rrf" and "fused_rank" in top["trace"] and "bm25_rank" in top["trace"]
    assert late["mode"] == "late" and all("late_rank" in h["trace"] for h in late["hits"])
    assert "late_encode" in late["timings_ms"] and "dense_encode" in late["timings_ms"]
    assert given["candidates_without_tokens"] == 1 and given["hits"][-1]["object_id"] == "d4"


def test_bearer_token_protects_all_but_health_and_metrics():
    app, _ = _app(token="s3cret-token")
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/metrics").status_code == 200
        assert c.post("/embed/query", json={"text": "a"}).status_code == 401
        assert c.post("/embed/query", json={"text": "a"},
                      headers={"Authorization": "Bearer s3cret-token"}).status_code == 200


def test_metrics_exposition():
    app, _ = _app()
    with TestClient(app) as c:
        c.post("/embed/query", json={"text": "a", "role": "dense"})
        text = c.get("/metrics").text
    assert 'vkm_rx580_encode_ms_count{model="granite-311m-r2",role="dense"} 1' in text
    assert "vkm_rx580_http_requests_total" in text


def test_no_global_lock_dense_and_late_run_concurrently():
    app, encs = _app(delay_s=0.3)
    app.state.vkm["ready"] = True

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            t0 = time.perf_counter()
            both = await c.post("/embed/query", json={"text": "creep", "role": "both"})
            t_both = time.perf_counter() - t0
            t0 = time.perf_counter()
            rs = await asyncio.gather(*(c.post("/embed/query", json={"text": f"q{i}", "role": r})
                                        for i in range(3) for r in ("dense", "late")))
            t_many = time.perf_counter() - t0
        return both, t_both, rs, t_many

    both, t_both, rs, t_many = asyncio.run(run())
    assert both.status_code == 200 and all(r.status_code == 200 for r in rs)
    assert t_both < 0.55                                 # 0.3 s + 0.3 s would be serial
    assert t_many < 0.9                                  # six 0.3 s encodes, serial would be 1.8 s
    assert encs["dense"].backend.max_inflight >= 2 and encs["late"].backend.max_inflight >= 2
