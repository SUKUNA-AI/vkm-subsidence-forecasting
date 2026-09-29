"""RX580 service, late targets (agent L): ``POST /search/late`` with page/object targets against a memory-mapped
token-vector pack, ``/health`` late_store, hot reload of a new pack, and HTTP 503/404 instead of a silent fallback.
Fake encoders (no GPU), a real pack on disk."""
from __future__ import annotations

import hashlib
import json

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pyarrow")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.embeddings.artifacts import ArtifactWriter, EmbeddingRow  # noqa: E402
from vkm_corpus.embeddings.fakes import FakeBackend, FakeTokenizer  # noqa: E402
from vkm_corpus.embeddings.pack import PackHandle, build_pack  # noqa: E402
from vkm_corpus.embeddings.postprocess import l2_normalize, maxsim  # noqa: E402
from vkm_corpus.embeddings.signature import EmbeddingConfig, text_hash  # noqa: E402
from vkm_corpus.embeddings.specs import get  # noqa: E402
from vkm_corpus.embeddings.tokenize import SpecTokenizer  # noqa: E402
from vkm_corpus.retrieval_service.app import create_app  # noqa: E402
from vkm_corpus.retrieval_service.config import ModelSlot  # noqa: E402
from vkm_corpus.retrieval_service.encoders import QueryEncoder  # noqa: E402

HID, TOK = 12, 8
P1, P2 = "VKM-SRC-001:p0001", "VKM-SRC-001:p0002"
UNITS = [("u1-0000000000000001", "BLOCK_GROUP", P1, ["VKM-SRC-001:p0001:b1"], 5),
         ("u1-0000000000000002", "FIGURE", P1, ["VKM-SRC-001:p0001:f1"], 3),
         ("u1-0000000000000003", "TABLE", P2, ["VKM-SRC-001:p0002:t1"], 4)]


def _late_encoder():
    spec = get("jina-colbert-v2")
    tok = SpecTokenizer(spec, FakeTokenizer(bos="<s>", eos="</s>", specials=(
        "<pad>", "<unk>", "<mask>", "[QueryMarker]", "[DocumentMarker]")))
    W = np.random.default_rng(5).standard_normal((TOK, HID)).astype(np.float32)
    return QueryEncoder(ModelSlot(role="late", key=spec.key, gguf="j-Q8_0.gguf", quant="Q8_0", gguf_sha256="b" * 64,
                                  port=2), spec, tok, FakeBackend(HID, pooled=False, name="late"),
                        heads={"colbert_w": W})


def _tokens(uid, n):
    seed = int.from_bytes(hashlib.sha256(uid.encode()).digest()[:8], "little")
    return l2_normalize(np.random.default_rng(seed).standard_normal((n, TOK)).astype(np.float32))


def _pack(root, units=UNITS, snapshot="SNAP-1", publish=True):
    cfg = EmbeddingConfig(model_id="jinaai/jina-colbert-v2", model_revision="r" * 40, weights_file="j-Q8_0.gguf",
                          weights_sha256="b" * 64, quantization="Q8_0", mode="multivector", dimension=TOK,
                          pooling="none", normalization="l2", text_rule="vkm-units-v1/A")
    w = ArtifactWriter(root, "multivector", cfg, writer_id=f"w-{snapshot}")
    have = set()
    try:
        from vkm_corpus.embeddings.artifacts import existing_hashes_in

        have = set(existing_hashes_in(w.dir))
    except FileNotFoundError:
        pass
    rows = [EmbeddingRow(object_id=u, text_hash=text_hash(u), page_id=p, object_type=k, vectors=_tokens(u, n))
            for u, k, p, _o, n in units if u not in have]
    if rows:
        w.write_part(rows)
    d = root / "units" / snapshot
    d.mkdir(parents=True, exist_ok=True)
    lines = "".join(json.dumps({"unit_id": u, "kind": k, "page_id": p, "source_id": "VKM-SRC-001", "object_ids": o,
                                "text_hash": text_hash(u)}) + "\n" for u, k, p, o, _n in units)
    (d / "units.jsonl").write_text(lines, encoding="utf-8", newline="\n")
    (d / "units.json").write_text(json.dumps({"schema": "vkm.embedding_units/1", "snapshot_id": snapshot,
                                              "text_rule": "vkm-units-v1/A", "count": len(units),
                                              "units_sha256": hashlib.sha256(lines.encode()).hexdigest()}),
                                  encoding="utf-8")
    return w.dir, build_pack(w.dir, d, publish_current=publish)


def _app(store, encoders=None):
    return create_app(None, encoders=encoders if encoders is not None else {"late": _late_encoder()}, store=store)


def test_late_targets_score_pages_and_objects_against_the_pack(tmp_path):
    art, rep = _pack(tmp_path)
    handle = PackHandle(art, expect={"model_id": "jinaai/jina-colbert-v2", "dimension": TOK})
    with TestClient(_app(handle)) as c:
        r = c.post("/search/late", json={"query": "subsidence over potash", "targets": [
            {"id": P1, "kind": "PAGE"}, {"id": "VKM-SRC-001:p0002:t1", "kind": "TABLE"},
            {"id": "VKM-SRC-009:p0001", "kind": "PAGE"}]})
        health = c.get("/health").json()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "late-targets" and body["store"]["pack_id"] == rep["pack_id"]
    assert body["scored"] == 2 and body["unscored"] == 1 and body["n_query_tokens"] > 0
    res = {x["id"]: x for x in body["results"]}
    q = _late_encoder().encode("subsidence over potash").vectors
    want_p1 = max(maxsim(q, _tokens("u1-0000000000000001", 5)), maxsim(q, _tokens("u1-0000000000000002", 3)))
    assert abs(res[P1]["late_score"] - want_p1) < 2e-2 and res[P1]["units"] == 2 and res[P1]["tokens"] == 8
    assert res["VKM-SRC-009:p0001"]["status"] == "NO_TOKENS" and res["VKM-SRC-009:p0001"]["late_rank"] is None
    assert sorted(x["late_rank"] for x in body["results"] if x["late_rank"]) == [1, 2]
    assert health["late_store"]["status"] == "READY" and health["late_store"]["pack_id"] == rep["pack_id"]


def test_pages_exclude_bibliography_units_by_default_cp42(tmp_path):
    units = [*UNITS, ("u1-0000000000000009", "BIB_ENTRY", P1, ["VKM-SRC-001:p0001:r1"], 6)]
    art, _rep = _pack(tmp_path, units=units)
    t = [{"id": P1, "kind": "PAGE"}, {"id": "VKM-SRC-001:p0001:r1", "kind": "BIB_ENTRY"}]
    with TestClient(_app(PackHandle(art))) as c:
        default = c.post("/search/late", json={"query": "x", "targets": t}).json()
        everything = c.post("/search/late", json={"query": "x", "targets": t, "page_exclude_kinds": []}).json()
        bad = c.post("/search/late", json={"query": "x", "targets": t, "page_exclude_kinds": ["PAGE"]})
    d = {r["id"]: r for r in default["results"]}
    e = {r["id"]: r for r in everything["results"]}
    assert default["page_exclude_kinds"] == ["BIB_ENTRY"] and d[P1]["units"] == 2 and e[P1]["units"] == 3
    assert d["VKM-SRC-001:p0001:r1"]["status"] == "SCORED" and bad.status_code == 422


def test_scan_of_bibliography_entries_ranks_pages(tmp_path):
    units = [*UNITS, ("u1-0000000000000009", "BIB_ENTRY", P1, ["VKM-SRC-001:p0001:r1"], 6),
             ("u1-000000000000000a", "BIB_ENTRY", P2, ["VKM-SRC-001:p0002:r1"], 4),
             ("u1-000000000000000b", "BIB_ENTRY", P2, ["VKM-SRC-001:p0002:r2"], 5)]
    art, _rep = _pack(tmp_path, units=units)
    with TestClient(_app(PackHandle(art))) as c:
        scan = c.post("/search/late", json={"query": "Барях ползучесть", "scan_kind": "BIB_ENTRY", "scan_top": 5})
        both = c.post("/search/late", json={"query": "x", "scan_kind": "BIB_ENTRY", "scan_top": 1,
                                            "targets": [{"id": P2, "kind": "PAGE"}], "page_exclude_kinds": []})
        bad = c.post("/search/late", json={"query": "x", "scan_kind": "PAGE"})
    body = scan.json()
    assert scan.status_code == 200 and body["mode"] == "late-scan" and body["scan_units"] == 3
    assert {s["page_id"] for s in body["scan"]} == {P1, P2} and [s["scan_rank"] for s in body["scan"]] == [1, 2]
    q = _late_encoder().encode("Барях ползучесть").vectors
    best = {P1: maxsim(q, _tokens("u1-0000000000000009", 6)),
            P2: max(maxsim(q, _tokens("u1-000000000000000a", 4)), maxsim(q, _tokens("u1-000000000000000b", 5)))}
    for s in body["scan"]:
        assert abs(s["late_score"] - best[s["page_id"]]) < 2e-2
    assert body["scan"][0]["late_score"] >= body["scan"][1]["late_score"] and "scan" in body["timings_ms"]
    b2 = both.json()
    assert len(b2["scan"]) == 1 and b2["results"][0]["units"] == 3 and b2["page_exclude_kinds"] == []
    assert bad.status_code == 422
    from vkm_corpus.retrieval_service.search import InMemoryMultiVectorStore

    mem = InMemoryMultiVectorStore({u: _tokens(u, n) for u, _k, _p, _o, n in units},
                                   units={u: {"kind": k, "page_id": p, "object_ids": o} for u, k, p, o, _n in units})
    got = mem.scan(q, "BIB_ENTRY", top_pages=5)
    assert [g["page_id"] for g in got] == [s["page_id"] for s in body["scan"]]


def test_missing_store_or_model_is_an_error_not_a_fallback(tmp_path):
    empty = tmp_path / "multivector" / "m" / "r" / "sig"
    empty.mkdir(parents=True)
    for store, code in ((None, 503), (PackHandle(empty), 503)):
        with TestClient(_app(store)) as c:
            r = c.post("/search/late", json={"query": "x", "targets": [{"id": P1, "kind": "PAGE"}]})
            assert r.status_code == code
            assert c.get("/health").json()["late_store"]["status"] in ("NOT_CONFIGURED", "MISSING")
    art, _rep = _pack(tmp_path / "b")
    with TestClient(_app(PackHandle(art), encoders={})) as c:
        assert c.post("/search/late", json={"query": "x", "targets": []}).status_code == 404
    other = PackHandle(art, expect={"model_id": "lightonai/mLateOn"})
    with TestClient(_app(other)) as c:
        r = c.post("/search/late", json={"query": "x", "targets": [{"id": P1, "kind": "PAGE"}]})
        assert r.status_code == 503 and "MISMATCH" in r.json()["detail"]


def test_a_new_pack_is_served_without_a_restart(tmp_path):
    art, rep1 = _pack(tmp_path)
    now = [0.0]
    handle = PackHandle(art, check_s=5.0, clock=lambda: now[0])
    extra = [*UNITS, ("u1-0000000000000004", "BIB_ENTRY", P2, ["VKM-SRC-001:p0002:r1"], 2)]
    with TestClient(_app(handle)) as c:
        _art, rep2 = _pack(tmp_path, units=extra, snapshot="SNAP-2")
        assert rep2["status"] == "BUILT" and rep2["pack_id"] != rep1["pack_id"]
        t = {"query": "x", "targets": [{"id": "VKM-SRC-001:p0002:r1", "kind": "BIB_ENTRY"}]}
        assert c.post("/search/late", json=t).json()["results"][0]["status"] == "NO_TOKENS"
        now[0] = 6.0
        body = c.post("/search/late", json=t).json()
        assert body["store"]["pack_id"] == rep2["pack_id"] and body["results"][0]["status"] == "SCORED"


def test_unit_candidates_still_work_with_the_pack(tmp_path):
    art, _rep = _pack(tmp_path)
    with TestClient(_app(PackHandle(art))) as c:
        r = c.post("/search/late", json={"query": "x", "k": 5,
                                         "candidates": ["u1-0000000000000003", "nope", "u1-0000000000000001"]})
    body = r.json()
    assert r.status_code == 200 and body["candidates_without_tokens"] == 1
    assert body["hits"][-1]["object_id"] == "nope" and "late_rank" in body["hits"][0]["trace"]
