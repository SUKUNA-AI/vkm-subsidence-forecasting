"""Live check of the vector mapping and the k-NN query DSL against the CORE OpenSearch (marker ``services``).

Needs ``VKM_OPENSEARCH_URL`` (loopback of CORE, e.g. through an SSH tunnel). Creates only ``vkm-vtest-*`` indices and
deletes them by exact name; production aliases and indices are never touched."""
from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.services
httpx = pytest.importorskip("httpx")
np = pytest.importorskip("numpy")

from vkm_corpus.embeddings.signature import EmbeddingConfig  # noqa: E402
from vkm_corpus.search.hybrid import dense_ranking, knn_body  # noqa: E402
from vkm_corpus.search.vectors import vectors_body  # noqa: E402


@pytest.fixture(scope="module")
def os_http():
    url = os.environ.get("VKM_OPENSEARCH_URL", "").strip()
    if not url:
        pytest.skip("NOT_RUN: VKM_OPENSEARCH_URL is not set")
    client = httpx.Client(base_url=url.rstrip("/"), timeout=60.0, trust_env=False)
    try:
        info = client.get("/").json()
    except httpx.HTTPError:
        pytest.skip("NOT_RUN: OpenSearch is not reachable")
    yield client, info
    client.close()


def _cfg(**kw):
    base = dict(model_id="org/enc", model_revision="1" * 40, weights_file="enc-Q8_0.gguf", weights_sha256="a" * 64,
                quantization="Q8_0", mode="dense", dimension=8, pooling="cls", normalization="l2",
                tokenizer_sha256="b" * 64)
    base.update(kw)
    return EmbeddingConfig(**base)


def test_vector_index_and_filtered_knn_on_the_live_cluster(os_http):
    http, info = os_http
    name = f"vkm-vtest-{uuid.uuid4().hex[:10]}"
    cfg = _cfg()
    try:
        r = http.put(f"/{name}", json=vectors_body(cfg, {"build_id": "t", "build_status": "BUILDING"}))
        assert r.status_code == 200, r.text
        rng = np.random.default_rng(7)
        vecs = rng.standard_normal((20, 8)).astype(np.float32)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
        lines = []
        for i, v in enumerate(vecs):
            page = f"VKM-SRC-00{1 + i % 2}:p{i // 4 + 1:04d}"
            doc = {"id": f"u1-{i:016x}", "unit_kind": "FIGURE" if i % 5 == 0 else "BLOCK_GROUP",
                   "object_ids": [f"{page}:b{i:012x}"], "page_id": page, "source_id": page.split(":")[0],
                   "work_id": "VKM-WRK-001", "dup_group_id": page, "year": 2000 + i, "snapshot_id": "S",
                   "config_signature": cfg.signature(), "text_hash": "h", "vector": v.tolist()}
            lines += [{"create": {"_index": name, "_id": doc["id"]}}, doc]
        body = "\n".join(__import__("json").dumps(x) for x in lines) + "\n"
        r = http.post("/_bulk", content=body, headers={"Content-Type": "application/x-ndjson"},
                      params={"refresh": "true"})
        assert r.status_code == 200 and not r.json()["errors"], r.text[:500]
        q = vecs[3].tolist()
        resp = http.post(f"/{name}/_search", json=knn_body(q, 10, {}, None)).json()
        assert resp["hits"]["hits"][0]["_id"] == "u1-0000000000000003"          # inner product: self first
        pages = dense_ranking(resp, "PAGE", 10, collapse_duplicates=True)
        assert len({p.key for p in pages}) == len(pages) and pages[0].key == "VKM-SRC-002:p0001"
        resp = http.post(f"/{name}/_search", json=knn_body(q, 10, {"source_id": ["VKM-SRC-001"],
                                                                   "year": {"gte": 2004}}, ("BLOCK_GROUP",))).json()
        hits = resp["hits"]["hits"]
        assert hits and all(h["_source"]["source_id"] == "VKM-SRC-001" and h["_source"]["unit_kind"] == "BLOCK_GROUP"
                            for h in hits)
        mapping = http.get(f"/{name}/_mapping").json()[name]["mappings"]
        assert mapping["properties"]["vector"]["method"]["space_type"] == "innerproduct"
        byte_name = f"{name}-byte"
        r = http.put(f"/{byte_name}", json=vectors_body(_cfg(storage_precision="int8", normalization="none"), {}))
        assert r.status_code == 200, r.text
    finally:
        for n in (name, f"{name}-byte"):
            http.delete(f"/{n}")
    assert http.get(f"/{name}").status_code == 404
