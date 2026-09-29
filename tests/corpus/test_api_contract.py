"""VKM API contract on a synthetic canonical snapshot with fake backends (API-01…API-18 of the design; CP-19, H-07,
H-12, H-13, H-15, H-18, H-38, H-39, H-40, H-45, H-47, H-48). No live service, no corpus data."""
from __future__ import annotations

import hashlib
import io
import json
import logging

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("PIL")
pytest.importorskip("pytz", reason="DuckDB TIMESTAMPTZ values in Python need pytz (decision note: extra corpus)")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.canon import CanonStore  # noqa: E402
from vkm_corpus.api.envelope import Envelope, Geometry, Provenance  # noqa: E402
from vkm_corpus.api.errors import ApiFailure  # noqa: E402
from vkm_corpus.api.fixtures import (SNAPSHOT_ID, FakeGraph, FakeHybrid, FakeSearch, search_hits,  # noqa: E402
                                     synthetic_service)
from vkm_world.governance.leakage import FORBIDDEN_COLUMNS, _json_keys  # noqa: E402

READ, WRITE = "read-token-for-tests-0000000000000000", "write-token-for-tests-000000000000000"
HR = {"Authorization": f"Bearer {READ}"}
HW = {"Authorization": f"Bearer {WRITE}"}


@pytest.fixture()
def env(tmp_path):
    service, canon, fakes = synthetic_service(tmp_path)
    app = create_app(service, ApiConfig(read_tokens={READ: "read"}, write_tokens={WRITE: "write"}))
    return TestClient(app), canon, fakes, service


def _ok(resp, status=200):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["ok"] is True and body["meta"]["request_id"] and body["meta"]["canonical_snapshot_id"] == SNAPSHOT_ID
    for item in ([body["item"]] if body.get("item") else []) + (body.get("items") or []):
        Envelope.model_validate(item["envelope"])                                   # API-01/02: valid envelope
    return body


def _err(resp, status, code):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == code and body["error"]["log_ref"]
    assert body["error"]["log_ref"] == resp.headers["X-Request-Id"]
    return body["error"]


# ------------------------------------------------------------------------------------------------ service & auth
def test_health_needs_no_token_but_everything_else_does(env):
    client, *_ = env
    assert client.get("/v1/health").json() == {"status": "ok", "api_version": "0.1.0"}
    _err(client.get("/v1/source/VKM-SRC-001"), 401, "UNAUTHORIZED")
    _err(client.get("/v1/source/VKM-SRC-001", headers={"Authorization": "Bearer wrong"}), 401, "UNAUTHORIZED")
    _ok(client.get("/v1/source/VKM-SRC-001", headers=HW))                      # write token may read
    body = {"target_id": "VKM-SRC-001:p0001", "reason": "synthetic reprocess test"}
    _err(client.post("/v1/reprocess/page", json=body, headers=HR), 403, "FORBIDDEN")


def test_unknown_route_and_schema_errors_use_the_envelope(env):
    client, *_ = env
    _err(client.get("/v1/nothing", headers=HR), 404, "NOT_FOUND")
    err = _err(client.post("/v1/search", json={"query": ""}, headers=HR), 400, "INVALID_ARGUMENT")
    assert "body.query" in err["details"]["fields"]
    _err(client.post("/v1/search", json={"query": "x", "bogus": 1}, headers=HR), 400, "INVALID_ARGUMENT")


def test_status_names_roles_not_addresses(env):
    client, *_ = env
    body = client.get("/v1/status", headers=HR).json()["status"]
    assert body["canonical"]["snapshot_id"] == SNAPSHOT_ID and body["canonical"]["up_to_date"] is True
    assert body["canonical"]["counts"]["sources_total"] == 4
    assert set(body["host_roles"]) == {"CORE", "EDGE", "WORKSTATION"}
    assert body["dependencies"]["neo4j"]["state"] == "READY"
    assert body["dependencies"]["rerank"]["backends"]["visual"]["license"] == "none"
    text = json.dumps(body)
    assert "http://" not in text and "https://" not in text and "bolt://" not in text     # H-40


# ------------------------------------------------------------------------------------------------ objects
def test_source_register_row_lifecycle_and_scope(env):
    client, *_ = env
    body = _ok(client.get("/v1/source/VKM-SRC-001", headers=HR))
    env_ = body["item"]["envelope"]
    assert (env_["object_kind"], env_["layer"], env_["payload_form"]) == ("SOURCE", "CANONICAL", "REFERENCE")
    assert env_["review_status"] == "FULLY_REVIEWED" and env_["origin"] == "REGISTRY"
    assert "REGISTER_NOTES_NOT_EVIDENCE" in env_["flags"] and body["item"]["record"]["register_notes_are_evidence"] \
        is False                                                                                       # H-47
    assert env_["source_scope"] == {"raw": "VKM_regional", "values": ["VKM_REGIONAL"], "mapping": "CASE",
                                    "inherited_from_source": False}
    assert env_["object_version"].endswith("@CMT-00000000000000f0") and env_["provenance"]["input_ref"]
    assert body["item"]["record"]["work_links"][0]["work_id"] == "VKM-WRK-001"
    for sid, lifecycle in (("VKM-SRC-013", "ABSENT_BY_REGISTER"), ("VKM-SRC-022", "RETIRED")):     # API-17, CP-05
        env_ = _ok(client.get(f"/v1/source/{sid}", headers=HR))["item"]["envelope"]
        assert env_["lifecycle_status"] == lifecycle and env_["processing_status"] == "SKIPPED_BY_REGISTER"
        assert env_["review_status"] == "NOT_APPLICABLE"
    err = _err(client.get("/v1/source/VKM-SRC-999", headers=HR), 404, "NOT_FOUND")                 # H-38: no 410
    assert "register" in err["hint"]
    _err(client.get("/v1/source/SRC-1", headers=HR), 400, "INVALID_ID")


def test_page_objects_are_never_reviewed_and_inherit_scope(env):
    client, canon, *_ = env
    body = _ok(client.get("/v1/page/VKM-SRC-001:p0001", headers=HR))
    env_, record = body["item"]["envelope"], body["item"]["record"]
    assert env_["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"                 # API-16: source is FULLY_REVIEWED
    assert env_["source_scope"]["inherited_from_source"] and "SCOPE_INHERITED_FROM_SOURCE" in env_["flags"]
    assert env_["origin"] == "NATIVE" and env_["processing_status"] == "NATIVE_OK" and env_["page_index"] == 1
    assert env_["geometry"] == {"bbox_space": "PAGE_PT_TL", "bbox": [0.0, 0.0, 595.3, 841.9], "unit": "pt",
                                "origin": "TOP_LEFT", "crs_status": None, "epsg": None}
    assert record["normalized_text"].startswith("Оседание") and {o["object_id"] for o in record["objects"]} >= {
        canon.ids["block"]}
    assert "content_sha256" not in record and "processing_run_id" not in record     # envelope columns not duplicated
    short = _ok(client.get("/v1/page/VKM-SRC-001:p0001", params={"max_chars": 5, "text_offset": 2}, headers=HR))
    assert short["item"]["record"]["normalized_text"] == "едани" and "TEXT_TRUNCATED" in short["item"]["envelope"][
        "flags"]
    assert short["item"]["record"]["text_window"] == {"offset": 2, "chars_total": len(record["normalized_text"]),
                                                      "truncated": True}


def test_figure_table_formula_entry_records(env):
    client, canon, *_ = env
    fig = _ok(client.get(f"/v1/figure/{canon.ids['figure']}", headers=HR))["item"]
    assert fig["envelope"]["geometry"]["bbox"] == [100.0, 300.0, 400.0, 500.0]
    assert fig["envelope"]["geometry"]["crs_status"] is None                              # page space is not a CRS
    assert fig["envelope"]["region_origin"] == "LAYOUT_MODEL"
    assert [m["role"] for m in fig["envelope"]["provenance"]["models"]] == ["LAYOUT"]
    assert fig["record"]["image"]["artifact_id"] == canon.ids["figure_crop"]
    _err(client.get(f"/v1/figure/{canon.ids['table']}", headers=HR), 400, "INVALID_ID")
    formula = _ok(client.get(f"/v1/formula/{canon.ids['formula']}", headers=HR))["item"]
    assert formula["envelope"]["origin"] == "OCR" and formula["envelope"]["text_layer"] == "GLM_OCR"
    assert formula["envelope"]["provenance"]["model_id"] == "zai-org/GLM-OCR"
    table = _ok(client.get(f"/v1/object/{canon.ids['table']}", headers=HR))["item"]["record"]
    assert table["raw_output"].startswith("<table>") and table["cells"]
    entry = _ok(client.get(f"/v1/object/{canon.ids['entry']}", headers=HR))["item"]["record"]
    assert entry["links"][0]["cited_work_id"] == "VKM-WRK-050" and entry["links"][0]["match_status"] == \
        "AUTO_EXACT_ID_MATCH"
    err = _err(client.get("/v1/object/VKM-SRC-001:p0001:f000000000000", headers=HR), 404, "NOT_FOUND")
    assert "older extraction" in err["hint"]


def test_work_authors_copies_and_tombstone_semantics(env):
    client, *_ = env
    item = _ok(client.get("/v1/work/VKM-WRK-001", headers=HR))["item"]
    assert "WORK_HAS_MULTIPLE_COPIES" in item["envelope"]["flags"] and item["record"]["work_copy_count"] == 2
    assert item["envelope"]["review_status"] == "NOT_APPLICABLE"
    authors = item["record"]["authors"]
    assert [a["name_as_listed"] for a in authors] == ["Альфаев А.А.", "Бетин Б.Б."]                       # H-15
    assert all(a["identity_status"] == "NAME_KEY_ONLY" and a["identity_note"] for a in authors)
    assert {s["source_id"] for s in item["record"]["sources"]} == {"VKM-SRC-001", "VKM-SRC-002"}


def test_images_and_artifacts(env):
    client, canon, *_ = env
    resp = client.get(f"/v1/object/{canon.ids['figure']}/image", params={"max_side": 150}, headers=HR)
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    meta = json.loads(resp.headers["X-VKM-Image-Meta"])
    from PIL import Image

    size = Image.open(io.BytesIO(resp.content)).size
    assert tuple(meta["px"]) == size and max(size) == 150 and meta["original_px"] == [300, 200]
    assert meta["pixel_to_page"] == {"x0": 100.0, "y0": 300.0, "sx": round(300 / 150, 6), "sy": round(200 / 100, 6)}
    assert meta["served_sha256"] == hashlib.sha256(resp.content).hexdigest()
    page = client.get("/v1/page/VKM-SRC-001:p0001/image", headers=HR)                     # H-45 preview ≤ 1024
    assert page.status_code == 200 and json.loads(page.headers["X-VKM-Image-Meta"])["artifact_id"] == \
        canon.ids["preview"]
    _err(client.get("/v1/page/VKM-SRC-001:p0002/image", headers=HR), 409, "ARTIFACT_NOT_MATERIALIZED")
    _err(client.get("/v1/page/VKM-SRC-001:p0003/image", headers=HR), 422, "NO_IMAGE_ARTIFACT")
    item = _ok(client.get(f"/v1/artifact/{canon.ids['figure_crop']}", headers=HR))["item"]
    assert item["envelope"]["layer"] == "ARTIFACT" and item["record"]["artifact_kind"] == "FIGURE_CROP"
    content = client.get(f"/v1/artifact/{canon.ids['figure_crop']}/content", headers=HR)
    assert "sha256:" + hashlib.sha256(content.content).hexdigest() == canon.ids["figure_crop"]
    rel = item["record"]["storage_relpath"]
    (canon.artifacts_root / rel).write_bytes(b"tampered")
    _err(client.get(f"/v1/artifact/{canon.ids['figure_crop']}/content", headers=HR), 500, "ARTIFACT_HASH_MISMATCH")


# ------------------------------------------------------------------------------------------------ search
def test_search_hydrates_from_canon_and_flags_projections(env):
    client, canon, fakes, _ = env
    body = _ok(client.post("/v1/search", json={"query": "оседание", "kinds": ["BLOCK", "PAGE", "FIGURE"],
                                               "filters": {"source_scope": ["VKM_REGIONAL"], "year_from": 2000}},
                           headers=HR))
    ids = [i["envelope"]["object_id"] for i in body["items"]]
    assert ids == [canon.ids["block"], "VKM-SRC-002:p0001", canon.ids["figure"]]              # stale id dropped
    warnings = {w["code"]: w for w in body["meta"]["warnings"]}
    assert warnings["STALE_PROJECTION"]["count"] == 1 and "WORK_HAS_MULTIPLE_COPIES" in warnings
    first = body["items"][0]
    assert first["envelope"]["layer"] == "CANONICAL" and first["envelope"]["projection"]["engine"] == "opensearch"
    assert first["envelope"]["projection"]["matches_canonical_snapshot"] is True
    assert first["record"]["highlight_origin"].startswith("SEARCH_INDEX")
    assert first["record"]["rerank_candidate"] == {"candidate_id": "VKM-SRC-001:p0001",
                                                   "object_ids": [canon.ids["block"], canon.ids["block2"]],
                                                   "rule": "rerank_text_v1"}
    assert fakes["search"].requests[-1]["filters"] == {"source_scope": ["VKM_REGIONAL"], "year": {"gte": 2000}}
    _err(client.post("/v1/search", json={"query": "x", "filters": {"available_until": "2020-01-01"}}, headers=HR),
         400, "INVALID_ARGUMENT")                                                               # H-19


def test_search_build_mismatch_warning(tmp_path):
    service, canon, _f = synthetic_service(tmp_path, search=None)
    service.deps.search = FakeSearch(search_hits(canon), built_from_snapshot_id="snap-20200101T000000Z-00000000")
    client = TestClient(create_app(service, ApiConfig(read_tokens={READ: "read"})))
    body = _ok(client.get("/v1/search", params={"q": "оседание"}, headers=HR))
    assert "PROJECTION_BUILD_MISMATCH" in {w["code"] for w in body["meta"]["warnings"]}


def test_objects_query_is_typed_and_canonical(env):
    client, canon, *_ = env
    body = _ok(client.post("/v1/objects/query", json={"kinds": ["FIGURE", "TABLE"], "source_ids": ["VKM-SRC-001"],
                                                      "has_image": True, "caption_query": "схема"}, headers=HR))
    assert [i["envelope"]["object_id"] for i in body["items"]] == [canon.ids["figure"]]
    body = _ok(client.post("/v1/objects/query", json={"kinds": ["FORMULA"], "origin": ["OCR"]}, headers=HR))
    assert len(body["items"]) == 1 and "raw_output" not in body["items"][0]["record"]
    _err(client.post("/v1/objects/query", json={"kinds": ["PAGE"]}, headers=HR), 400, "INVALID_ARGUMENT")


# ------------------------------------------------------------------------------------------------ rerank
def test_rerank_text_uses_canonical_text_and_limits(env):
    client, canon, fakes, _ = env
    too_many = {"query": "q", "candidate_ids": [f"VKM-SRC-001:p{i:04d}" for i in range(1, 26)]}
    err = _err(client.post("/v1/rerank/text", json=too_many, headers=HR), 413, "PAYLOAD_TOO_LARGE")      # H-13
    assert err["details"]["given"] == 25
    body = _ok(client.post("/v1/rerank/text", json={
        "query": "мульда сдвижения", "candidate_ids": ["VKM-SRC-001:p0001", "VKM-SRC-001:p0002", "VKM-SRC-001:p0003",
                                                     "VKM-SRC-002:p0009"],
        "passages": [{"candidate_id": "VKM-SRC-001:p0001", "object_ids": [canon.ids["block"],
                                                                          canon.ids["block2"]]}]}, headers=HR))
    sent = dict(fakes["rerank"].text_calls[-1])
    texts = service_texts(env, [canon.ids["block"], canon.ids["block2"], "VKM-SRC-001:p0002"])
    assert sent["VKM-SRC-001:p0001"] == texts[canon.ids["block"]] + "\n" + texts[canon.ids["block2"]]
    assert sent["VKM-SRC-001:p0002"] == texts["VKM-SRC-001:p0002"]                        # the view rerank_text
    service_item = body["item"]
    assert service_item["envelope"]["layer"] == "SERVICE" and service_item["envelope"]["review_status"] == \
        "NOT_APPLICABLE"
    assert {r["id"]: r["code"] for r in service_item["record"]["rejected"]} == {
        "VKM-SRC-001:p0003": "NO_RERANK_TEXT", "VKM-SRC-002:p0009": "NOT_FOUND"}
    ranked = body["items"]
    assert ranked[0]["envelope"]["object_id"] == "VKM-SRC-001:p0002" and ranked[0]["record"]["rank"] == 1
    assert ranked[0]["record"]["sent_text_sha256"] == hashlib.sha256(sent["VKM-SRC-001:p0002"].encode()).hexdigest()
    _err(client.post("/v1/rerank/text", json={"query": "q", "candidate_ids": ["VKM-SRC-001:p0003"]}, headers=HR),
         422, "NO_RERANK_TEXT")


def test_rerank_text_late_backend_scores_through_the_token_store(tmp_path, monkeypatch):
    """With hybrid search configured rerank_text re-scores by mLateOn MaxSim (the EDGE text reranker was retired
    29.09); a block is scored through its page, absent ids are rejected, ``gateway`` still selects the old path."""
    monkeypatch.delenv("VKM_RERANK_TEXT_BACKEND", raising=False)
    service, canon, fakes = synthetic_service(tmp_path, hybrid=FakeHybrid([]))
    client = TestClient(create_app(service, ApiConfig(read_tokens={READ: "read"}, write_tokens={WRITE: "write"})))
    ids = ["VKM-SRC-001:p0001", "VKM-SRC-001:p0002", canon.ids["figure"], canon.ids["block"], "VKM-SRC-002:p0009"]
    body = _ok(client.post("/v1/rerank/text", json={"query": "мульда сдвижения", "candidate_ids": ids}, headers=HR))
    ranked = [it["envelope"]["object_id"] for it in body["items"]]
    assert ranked[0] == canon.ids["figure"] and set(ranked) == set(ids[:4])       # figure: the last target, top
    assert all(it["record"]["rule"] == "rerank_text_late_v1" for it in body["items"])
    block = next(it["record"] for it in body["items"] if it["envelope"]["object_id"] == canon.ids["block"])
    assert block["scored_as"]["kind"] == "PAGE"
    targets = fakes["hybrid"].late_calls[-1][1]
    assert len(targets) == len({t["id"] for t in targets})                         # a page is scored once
    assert body["item"]["record"]["rule"] == "rerank_text_late_v1"
    assert {r["id"]: r["code"] for r in body["item"]["record"]["rejected"]} == {"VKM-SRC-002:p0009": "NOT_FOUND"}
    assert not fakes["rerank"].text_calls                                           # EDGE is not called
    top = _ok(client.post("/v1/rerank/text", json={"query": "q", "candidate_ids": ids[:3], "top_n": 2}, headers=HR))
    assert len(top["items"]) == 2
    monkeypatch.setenv("VKM_RERANK_TEXT_BACKEND", "gateway")
    _ok(client.post("/v1/rerank/text", json={"query": "q", "candidate_ids": ["VKM-SRC-001:p0002"]}, headers=HR))
    assert fakes["rerank"].text_calls                                               # the old path on request


def test_hybrid_backend_late_rerank_calls_the_token_store_and_maps_errors():
    """The real HybridBackend carries late_rerank (29.09: the method first landed on the BM25 backend and the fake
    hid it) and maps a late-stage failure to the API error of that stage."""
    from types import SimpleNamespace

    from vkm_corpus.api.backends import HybridBackend
    from vkm_corpus.api.errors import ApiFailure
    from vkm_corpus.search.hybrid import HybridError

    class Embed:
        def __init__(self, fail=None):
            self.fail, self.calls = fail, []

        def late_scores(self, query, targets):
            self.calls.append((query, targets))
            if self.fail is not None:
                raise self.fail
            return SimpleNamespace(results={t["id"]: {"status": "SCORED", "late_score": 1.0} for t in targets})

    settings = SimpleNamespace(opensearch_index_prefix="vkm")
    visual = SimpleNamespace(enabled=False, mode="exact", ef_search=None)
    target = [{"id": "VKM-SRC-001:p0001", "kind": "PAGE"}]
    ok = HybridBackend(settings, search=object(), embed=Embed(), late_default=True, visual=visual)
    assert ok.late_rerank("q", target).results["VKM-SRC-001:p0001"]["status"] == "SCORED"
    down = HybridError("DEPENDENCY_UNAVAILABLE", "token store down", stage="late", tool="rx580-retrieval")
    bad = HybridBackend(settings, search=object(), embed=Embed(fail=down), late_default=True, visual=visual)
    with pytest.raises(ApiFailure) as err:
        bad.late_rerank("q", target)
    assert err.value.code == "DEPENDENCY_UNAVAILABLE"


def service_texts(env, ids):
    service = env[3]
    return {k: v["text"] for k, v in service.canon.rerank_texts(ids).items()}


def test_rerank_visual_sends_only_images(env):
    client, canon, fakes, _ = env
    nine = {"query": "q", "candidate_ids": [f"VKM-SRC-001:p{i:04d}" for i in range(1, 10)]}
    _err(client.post("/v1/rerank/visual", json=nine, headers=HR), 413, "PAYLOAD_TOO_LARGE")
    body = _ok(client.post("/v1/rerank/visual", json={
        "query": "схема", "candidate_ids": [canon.ids["figure"], "VKM-SRC-001:p0001", canon.ids["block"],
                                            canon.ids["table_crop"]], "max_side": 256}, headers=HR))
    sent = dict(fakes["rerank"].visual_calls[-1])
    assert set(sent) == {canon.ids["figure"], "VKM-SRC-001:p0001", canon.ids["table_crop"]}
    from PIL import Image

    assert all(max(Image.open(io.BytesIO(data)).size) <= 256 for data in sent.values())
    assert {r["id"]: r["code"] for r in body["item"]["record"]["rejected"]} == {canon.ids["block"]:
                                                                                 "NO_IMAGE_ARTIFACT"}
    kinds = {i["envelope"]["object_kind"] for i in body["items"]}
    assert kinds == {"FIGURE", "PAGE", "ARTIFACT"}
    assert all(i["record"]["image_artifact_id"].startswith("sha256:") for i in body["items"])
    _err(client.post("/v1/rerank/visual", json={"query": "q", "candidate_ids": [canon.ids["block"]]}, headers=HR),
         422, "NO_IMAGE_ARTIFACT")


# ------------------------------------------------------------------------------------------------ graph, provenance
def test_neighbors_come_from_graph_and_are_hydrated(env, tmp_path):
    client, canon, fakes, service = env
    body = _ok(client.get("/v1/neighbors/VKM-SRC-001:p0002", headers=HR))
    rels = {(i["envelope"]["object_id"], i["record"]["relationship"]) for i in body["items"]}
    assert ("VKM-SRC-001:p0001", "PRECEDES") in rels and (canon.ids["figure"], "HAS_FIGURE") in rels
    assert ("VKM-WRK-001", "INSTANCE_OF") in rels
    assert body["items"][0]["envelope"]["projection"]["engine"] == "neo4j"
    only = _ok(client.get("/v1/neighbors/VKM-SRC-001:p0002", params={"rel_types": ["HAS_TABLE"]}, headers=HR))
    assert [i["envelope"]["object_id"] for i in only["items"]] == [canon.ids["table"]]
    work = _ok(client.get("/v1/neighbors/VKM-WRK-050", headers=HR))
    assert [(i["envelope"]["object_id"], i["record"]["direction"]) for i in work["items"]] == [("VKM-WRK-001", "in")]
    service.deps.graph = FakeGraph(canon, state="REBUILDING")                                            # H-43
    _err(client.get("/v1/neighbors/VKM-SRC-001:p0002", headers=HR), 503, "DEPENDENCY_UNAVAILABLE")


def test_citations_from_canonical_rules(env):
    client, canon, *_ = env
    cites = _ok(client.get("/v1/citations/VKM-WRK-001", params={"direction": "cites"}, headers=HR))["items"]
    assert [(i["envelope"]["object_id"], i["record"]["status"]) for i in cites] == [(canon.ids["entry"], "LINKED")]
    everything = _ok(client.get("/v1/citations/VKM-WRK-001", params={"direction": "cites", "include_unlinked":
                                                                     True}, headers=HR))["items"]
    assert {i["record"]["status"] for i in everything} == {"LINKED", "UNLINKED"}
    cited_by = _ok(client.get("/v1/citations/VKM-WRK-050", params={"direction": "cited_by"}, headers=HR))["items"]
    assert [(i["envelope"]["object_id"], i["record"]["n_citing_entries"]) for i in cited_by] == [("VKM-WRK-001", 1)]


def test_provenance_answers_section_50(env):
    client, canon, *_ = env
    record = _ok(client.get(f"/v1/provenance/{canon.ids['figure']}", headers=HR))["item"]["record"]
    answers = record["interpretability"]
    for key in ("what", "original", "created_by", "native_or_ocr", "auto_or_reviewed", "pipeline_version",
                "rebuildable", "projections"):
        assert answers[key] not in (None, "", []), key                                                   # K-15
    assert answers["auto_or_reviewed"] == "AUTO_EXTRACTED_UNREVIEWED"
    assert answers["original"]["source_id"] == "VKM-SRC-001" and answers["original"]["page_index"] == 2
    assert answers["native_or_ocr"]["region_origin"] == "LAYOUT_MODEL"
    assert all(p["deletable_without_canonical_loss"] for p in answers["projections"])
    steps = [s["step"] for s in record["chain"]]
    assert steps == ["RAW_SOURCE", "PROCESSING_RUN", "PRODUCER", "RAW_OUTPUT", "CANONICAL_COMMIT"]
    assert record["chain"][0]["logical_path"].startswith("PRIVATE:") and record["chain"][4]["subject"] == \
        "CMT-00000000000000a1"
    source = _ok(client.get("/v1/provenance/VKM-SRC-001", headers=HR))["item"]["record"]
    assert source["chain"][0]["step"] == "REGISTRY_INPUT" and source["chain"][0]["subject"].startswith("PRIVATE:")
    _err(client.get(f"/v1/provenance/{canon.ids['figure_crop']}", headers=HR), 400, "INVALID_ARGUMENT")


# ------------------------------------------------------------------------------------------------ processing & write
def test_processing_status_degrades_without_control_plane(env):
    client, canon, fakes, service = env
    body = _ok(client.get("/v1/processing/status", params={"source_id": "VKM-SRC-001"}, headers=HR))
    assert body["item"]["record"]["summary"]["source_rollup"] and body["item"]["record"]["errors"][0]["code"] == \
        "OCR_FAILED"
    assert body["item"]["record"]["jobs"] == []
    fakes["control"].available = False                                                                   # API-15
    body = _ok(client.get("/v1/processing/status", params={"page_id": "VKM-SRC-001:p0003"}, headers=HR))
    assert body["item"]["record"]["jobs"] is None and body["item"]["record"]["page_status"] == "NEEDS_REVIEW"
    assert "DEGRADED_DEPENDENCY" in {w["code"] for w in body["meta"]["warnings"]}
    run = _ok(client.get("/v1/processing/status", params={"run_id": canon.ids["run"]}, headers=HR))["item"]
    assert run["envelope"]["object_kind"] == "PROCESSING_RUN" and run["record"]["step_outcomes"] == {"EXECUTED": 3}
    _err(client.get("/v1/processing/status", headers=HR), 400, "INVALID_ARGUMENT")


def test_reprocess_is_plan_first_and_never_touches_the_canon(env):
    client, canon, fakes, _ = env
    before = hashlib.sha256(canon.duckdb_path.read_bytes()).hexdigest()
    body = {"target_id": "VKM-SRC-001:p0002", "reason": "re-run OCR after a synthetic fix", "recall_model": True}
    first = _ok(client.post("/v1/reprocess/page", json=body, headers=HW), 202)["item"]
    job_id = first["record"]["job_id"]
    assert first["record"]["state"] == "PLAN_REQUESTED" and first["envelope"]["layer"] == "OPERATIONAL"
    request = fakes["control"].jobs[job_id]["request"]           # the worker executes only request.options
    assert first["record"]["requested_by"] == "write"
    assert request["options"] == {"force": False, "recall_model": True, "no_ocr": False}
    again = _ok(client.post("/v1/reprocess/page", json=body, headers=HW))["item"]["record"]
    assert again["job_id"] == job_id and again["deduplicated"] is True
    other = _err(client.post("/v1/reprocess/page", json={**body, "recall_model": False}, headers=HW), 409,
                 "JOB_STATE_CONFLICT")
    assert other["details"]["job_id"] == job_id
    _err(client.post("/v1/reprocess/page", json={**body, "stages": ["ocr"]}, headers=HW), 400, "INVALID_ARGUMENT")
    confirm = {**body, "job_id": job_id, "plan_sha256": "0" * 64}
    _err(client.post("/v1/reprocess/page", json=confirm, headers=HW), 409, "PLAN_NOT_READY")
    digest = fakes["control"].worker_plans(job_id, {"pages": ["VKM-SRC-001:p0002"], "model_calls": 1})
    _err(client.post("/v1/reprocess/page", json=confirm, headers=HW), 409, "PLAN_CHANGED")                 # H-12
    done = _ok(client.post("/v1/reprocess/page", json={**confirm, "plan_sha256": digest}, headers=HW))["item"]
    assert done["record"]["state"] == "CONFIRMED" and done["record"]["confirmed_plan_sha256"] == digest
    job = _ok(client.get(f"/v1/jobs/{job_id}", headers=HR))["item"]["record"]
    assert job["state"] == "CONFIRMED"
    _err(client.post("/v1/reprocess/source", json={"target_id": "VKM-SRC-013", "reason": "should be refused"},
                     headers=HW), 409, "NOT_REPROCESSABLE")
    fakes["control"].jobs.update({100 + i: {"job_id": 100 + i, "kind": "REPROCESS_SOURCE", "state": "RUNNING",
                                            "source_id": f"VKM-SRC-1{i:02d}"} for i in range(3)})
    _err(client.post("/v1/reprocess/source", json={"target_id": "VKM-SRC-002", "reason": "quota exceeded now"},
                     headers=HW), 429, "RATE_LIMITED")
    assert hashlib.sha256(canon.duckdb_path.read_bytes()).hexdigest() == before                          # API-11


def test_cancel_job_is_a_write_and_only_before_running(env):
    client, _canon, fakes, _ = env
    body = {"target_id": "VKM-SRC-001", "reason": "synthetic source reprocess"}
    job_id = _ok(client.post("/v1/reprocess/source", json=body, headers=HW), 202)["item"]["record"]["job_id"]
    reason = {"reason": "the human declined the plan"}
    _err(client.post(f"/v1/jobs/{job_id}/cancel", json=reason, headers=HR), 403, "FORBIDDEN")
    done = _ok(client.post(f"/v1/jobs/{job_id}/cancel", json=reason, headers=HW))["item"]["record"]
    assert done["state"] == "CANCELLED" and "declined" in done["note"]
    _err(client.post(f"/v1/jobs/{job_id}/cancel", json=reason, headers=HW), 409, "JOB_STATE_CONFLICT")
    _err(client.post("/v1/jobs/999/cancel", json=reason, headers=HW), 404, "NOT_FOUND")
    fakes["control"].jobs[500] = {"job_id": 500, "kind": "RECONCILE", "state": "PLAN_REQUESTED"}
    _err(client.post("/v1/jobs/500/cancel", json=reason, headers=HW), 403, "FORBIDDEN")


# ------------------------------------------------------------------------------------------------ roots, openapi, logs
def test_canon_unavailable_and_staging_root_refused(tmp_path):
    from vkm_corpus.parquet.layout import init_root

    staging = init_root(tmp_path / "staging", "STAGING")
    with pytest.raises(ApiFailure) as exc:
        CanonStore.from_data_root(staging.root)                                                          # H-07
    assert exc.value.code == "SNAPSHOT_UNAVAILABLE"
    service, canon, _ = synthetic_service(tmp_path / "c")
    client = TestClient(create_app(service, ApiConfig(read_tokens={READ: "read"})))
    canon.duckdb_path.unlink()                   # nothing opened yet: the store opens lazily
    _err(client.get("/v1/source/VKM-SRC-001", headers=HR), 503, "SNAPSHOT_UNAVAILABLE")


def test_openapi_has_minimum_endpoints_and_no_forbidden_keys(env):
    client, *_ = env
    spec = client.get("/v1/openapi.json").json()
    for path in ("/v1/health", "/v1/status", "/v1/search", "/v1/source/{source_id}", "/v1/work/{work_id}",
                 "/v1/page/{page_id}", "/v1/artifact/{artifact_id}", "/v1/rerank/text", "/v1/rerank/visual",
                 "/v1/figure/{object_id}", "/v1/table/{object_id}", "/v1/formula/{object_id}",
                 "/v1/page/{page_id}/image", "/v1/neighbors/{object_id}", "/v1/citations/{work_id}",
                 "/v1/provenance/{object_id}", "/v1/processing/status", "/v1/reprocess/page"):
        assert path in spec["paths"], path
    assert not (FORBIDDEN_COLUMNS & _json_keys(spec))                                                    # API-13


def test_request_log_has_context_and_no_secrets(env, caplog):
    client, *_ = env
    with caplog.at_level(logging.INFO, logger="vkm.api"):
        client.get("/v1/source/VKM-SRC-001", headers=HR)
        client.post("/v1/search", json={"query": "секретный запрос"}, headers=HR)
    records = [r for r in caplog.records if r.name == "vkm.api"]
    ctx = [r.vkm for r in records]
    assert ctx[0]["route"] == "/v1/source/{source_id}" and ctx[0]["http_status"] == 200 and ctx[0]["token_label"] == \
        "read"
    assert ctx[1]["query_sha256"] == hashlib.sha256("секретный запрос".encode()).hexdigest()
    dumped = json.dumps(ctx, ensure_ascii=False)
    assert READ not in dumped and "секретный" not in dumped and "Bearer" not in dumped                    # API-14


# ------------------------------------------------------------------------------------------------ envelope rules
def test_envelope_invariants():
    base = {"object_id": "VKM-SRC-001:p0001", "object_kind": "PAGE", "source_id": "VKM-SRC-001",
            "page_id": "VKM-SRC-001:p0001",
            "review_status": "AUTO_EXTRACTED_UNREVIEWED", "layer": "CANONICAL", "payload_form": "NORMALIZED",
            "canonical_snapshot_id": SNAPSHOT_ID}
    Envelope.model_validate(base)
    bad = [{"canonical_snapshot_id": None}, {"review_status": "FULLY_REVIEWED"}, {"payload_form": "RAW"},
           {"layer": "PROJECTION"}, {"source_id": None}, {"flags": ["MADE_UP"]}, {"review_status": "FACT"}]
    for change in bad:
        with pytest.raises(ValueError):
            Envelope.model_validate({**base, **change})
    Envelope.model_validate({**base, "payload_form": "RAW", "provenance": {"raw_artifact_id": "sha256:" + "a" * 64}})
    with pytest.raises(ValueError):
        Geometry(bbox_space="DRAWING_UNITS")                                                            # H-20
    with pytest.raises(ValueError):
        Geometry(bbox_space="PAGE_PT_TL", crs_status="UNKNOWN_CRS")
    with pytest.raises(ValueError):
        Geometry(bbox_space="DRAWING_UNITS", crs_status="UNKNOWN_CRS", epsg=4326)
    Geometry(bbox_space="DRAWING_UNITS", crs_status="UNKNOWN_CRS")
    assert Provenance().trace is None
