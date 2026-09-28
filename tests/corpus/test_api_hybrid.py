"""VKM API ``/v1/search/hybrid`` (retrieval lab stage 2): envelope contract, hydration from the canon, stage trace,
DEPENDENCY_UNAVAILABLE without the encoder or the vectors build (never a silent BM25 substitute), same auth as search.
Synthetic canon; the hybrid backend is a fake or the real adapter over a fake OpenSearch and a fake RX580 service."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("PIL")
pytest.importorskip("pytz", reason="DuckDB TIMESTAMPTZ values in Python need pytz (decision note: extra corpus)")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.envelope import Envelope  # noqa: E402
from vkm_corpus.api.errors import ApiFailure  # noqa: E402
from vkm_corpus.api.fixtures import SNAPSHOT_ID, FakeHybrid, hybrid_hits, synthetic_service  # noqa: E402

READ = "read-token-for-hybrid-tests-000000000000"
H = {"Authorization": f"Bearer {READ}"}


@pytest.fixture()
def env(tmp_path):
    service, canon, fakes = synthetic_service(tmp_path)
    service.deps.hybrid = FakeHybrid(hybrid_hits(canon))
    return TestClient(create_app(service, ApiConfig(read_tokens={READ: "read"}))), canon, service


def _ok(resp):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] and body["meta"]["canonical_snapshot_id"] == SNAPSHOT_ID
    for item in [body["item"], *body["items"]]:
        Envelope.model_validate(item["envelope"])
    return body


def test_hybrid_hits_are_hydrated_with_the_stage_trace(env):
    client, canon, service = env
    body = _ok(client.post("/v1/search/hybrid", headers=H, json={"query": "оседание земной поверхности",
                                                                 "limit": 10, "candidates": 50}))
    ids = [it["envelope"]["object_id"] for it in body["items"]]
    assert ids == ["VKM-SRC-001:p0001", "VKM-SRC-002:p0001", canon.ids["figure"]]       # stale id dropped
    assert any(w["code"] == "STALE_PROJECTION" for w in body["meta"]["warnings"])
    first = body["items"][0]
    trace = first["record"]["trace"]
    assert (trace["bm25_rank"], trace["dense_rank"], trace["fused_rank"]) == (1, 1, 1)
    assert first["record"]["rrf_score"] == trace["rrf_score"] and first["record"]["dense_score"] == 0.91
    assert first["envelope"]["projection"]["build_id"] == "b-test"
    dense_only = body["items"][1]
    assert dense_only["record"]["trace"]["bm25_rank"] is None
    proj = dense_only["envelope"]["projection"]
    assert proj["build_id"] == "v-test" and proj["built_from_snapshot_id"] == SNAPSHOT_ID
    assert proj["matches_canonical_snapshot"] is True
    # a dense-only page is reranked on the passage of its matching unit, not on the whole page
    assert dense_only["record"]["rerank_candidate"]["object_ids"] == ["VKM-SRC-002:p0001:b000000000001"]
    run = body["item"]
    assert run["envelope"]["object_kind"] == "SEARCH_RESULT" and run["envelope"]["layer"] == "SERVICE"
    assert run["record"]["fusion"] == "RRF" and run["record"]["stages"]["dense"]["model_key"] == "granite-311m-r2"
    sent = service.deps.hybrid.requests[-1]
    assert sent["candidates"] == 50 and sent["kinds"] == ("PAGE",) and sent["size"] == 10


def test_hybrid_get_form_auth_and_schema(env):
    client, *_ = env
    assert _ok(client.get("/v1/search/hybrid", params={"q": "ползучесть", "limit": 2}, headers=H))["items"]
    assert client.get("/v1/search/hybrid", params={"q": "x"}).status_code == 401
    bad = client.post("/v1/search/hybrid", headers=H, json={"query": "x", "kinds": ["BLOCK"]})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_hybrid_is_unavailable_without_its_backend_or_dependencies(env):
    client, _canon, service = env
    service.deps.hybrid = FakeHybrid([], fail=ApiFailure("DEPENDENCY_UNAVAILABLE", "vectors alias vkm-vectors is "
                                                         "not built yet", stage="hybrid_vectors", tool="opensearch"))
    resp = client.post("/v1/search/hybrid", headers=H, json={"query": "мульда сдвижения"})
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert resp.json()["error"]["stage"] == "hybrid_vectors" and not resp.json().get("items")
    service.deps.hybrid = None
    resp = client.post("/v1/search/hybrid", headers=H, json={"query": "мульда сдвижения"})
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


def test_real_adapter_maps_encoder_and_index_failures(env):
    from vkm_corpus.api.backends import HybridBackend, OpenSearchBackend
    from vkm_corpus.config import load_settings
    from vkm_corpus.search.fakes import FakeOpenSearch
    from vkm_corpus.search.hybrid import EmbedClient

    client, _canon, service = env
    settings = load_settings({"VKM_OPENSEARCH_URL": "http://opensearch:9200"})
    search = OpenSearchBackend(settings)
    search._client = FakeOpenSearch()                                     # no vectors alias built

    def refuse(request):
        raise httpx.ConnectError("refused")

    embed = EmbedClient("http://rx580-retrieval:8790", None, transport=httpx.MockTransport(refuse))
    service.deps.hybrid = HybridBackend(settings, search, embed=embed)
    resp = client.post("/v1/search/hybrid", headers=H, json={"query": "оседание"})
    assert resp.status_code == 503 and resp.json()["error"]["stage"] == "hybrid_vectors"
    name = "vkm-vectors-m1-b1"
    search._client.indices.create(index=name, body={"mappings": {"_meta": {
        "build_id": "b1", "build_status": "COMPLETE", "dimension": 4, "model_key": "granite-311m-r2"}}})
    search._client.indices.update_aliases({"actions": [{"add": {"index": name, "alias": "vkm-vectors"}}]})
    service.deps.hybrid._meta = None
    resp = client.post("/v1/search/hybrid", headers=H, json={"query": "оседание"})
    err = resp.json()["error"]
    assert resp.status_code == 503 and err["stage"] == "hybrid_embed" and err["tool"] == "rx580-retrieval"
    assert "http://" not in err["message"]                                 # no addresses in messages
    bad = client.post("/v1/search/hybrid", headers=H,
                      json={"query": "оседание", "filters": {"figure_type": ["MAP"]}})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_ARGUMENT"
    status = client.get("/v1/status", headers=H).json()["status"]["dependencies"]
    assert status["query_encoder"] == {"available": False, "error": "ConnectError"}
