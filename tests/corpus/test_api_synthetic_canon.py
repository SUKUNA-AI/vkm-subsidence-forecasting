"""VKM API over agent D's synthetic canon (``vkm_corpus.testing.synthetic_canon``): the real path register → STAGING →
CANONICAL → snapshot → DuckDB, read only through the API, so the API's SQL runs against D's actual views and macros
(H-07, H-11, H-13, H-16, H-38, H-47, H-48, H-49, H-50). Search, graph and rerank are fakes; no corpus data."""
from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("pytz", reason="DuckDB TIMESTAMPTZ values in Python need pytz (decision note: extra corpus)")

from fastapi.testclient import TestClient  # noqa: E402

from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.backends import ArtifactBlobs  # noqa: E402
from vkm_corpus.api.canon import CanonStore  # noqa: E402
from vkm_corpus.api.envelope import Envelope  # noqa: E402
from vkm_corpus.api.errors import ApiFailure  # noqa: E402
from vkm_corpus.api.fixtures import FakeControlPlane, FakeRerank, FakeSearch  # noqa: E402
from vkm_corpus.api.service import ApiDeps, ApiService  # noqa: E402
from vkm_corpus.testing import synthetic_canon  # noqa: E402

READ = "read-token-for-d-canon-tests-0000000000"
H = {"Authorization": f"Bearer {READ}"}


class _Graph:
    def __init__(self, snapshot_id: str) -> None:
        self.snapshot_id = snapshot_id

    def state(self):
        return {"state": "READY", "http_status": 200, "build_id": "g-d", "built_from_snapshot_id": self.snapshot_id}

    def page_neighbors(self, page_id):
        return None

    def citations(self, work_id, direction):
        return []


@pytest.fixture(scope="module")
def dcanon(tmp_path_factory):
    canon = synthetic_canon(tmp_path_factory.mktemp("dcanon") / "data", with_duckdb=True)
    ids = canon.ids
    hits = [{"id": ids["foreign_page"], "object_type": "PAGE", "index": "vkm-pages-b-d", "build_id": "b-test",
             "score": 3.0, "source_id": "VKM-SRC-005", "page_id": ids["foreign_page"]},
            {"id": ids["figure"], "object_type": "FIGURE", "index": "vkm-figures-b-d", "build_id": "b-test",
             "score": 2.0, "source_id": "VKM-SRC-025", "page_id": "VKM-SRC-025:p0001"},
            {"id": "VKM-SRC-001:p0009", "object_type": "PAGE", "index": "vkm-pages-b-d", "build_id": "b-test",
             "score": 1.0, "source_id": "VKM-SRC-001", "page_id": "VKM-SRC-001:p0009"}]
    rerank = FakeRerank()
    service = ApiService(ApiDeps(canon=CanonStore.from_data_root(canon.root),
                                 blobs=ArtifactBlobs(canon.layout.artifacts),
                                 search=FakeSearch(hits, canon.snapshot_id), graph=_Graph(canon.snapshot_id),
                                 rerank=rerank, control=FakeControlPlane()))
    client = TestClient(create_app(service, ApiConfig(read_tokens={READ: "read"})))
    return client, canon, rerank


def _ok(resp, canon):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["meta"]["canonical_snapshot_id"] == canon.snapshot_id
    for item in ([body["item"]] if body.get("item") else []) + (body.get("items") or []):
        Envelope.model_validate(item["envelope"])
    return body


def _get(dcanon, url):
    client, canon, _ = dcanon
    return _ok(client.get(url, headers=H), canon)


def _post(dcanon, url, payload):
    client, canon, _ = dcanon
    return _ok(client.post(url, json=payload, headers=H), canon)


def test_status_names_the_current_d_snapshot_and_its_counts(dcanon):
    client, canon, _ = dcanon
    resp = client.get("/v1/status", headers=H)
    assert resp.status_code == 200
    status = resp.json()["status"]["canonical"]
    assert status["snapshot_id"] == status["current_snapshot_id"] == canon.snapshot_id          # H-48
    assert status["up_to_date"] is True and status["root_kind"] == "CANONICAL"
    assert status["built_at"].endswith("Z") and "T" in status["built_at"]
    counts = status["counts"]
    assert (counts["sources_total"], counts["sources_complete"], counts["sources_partial"],
            counts["sources_not_processed"], counts["sources_skipped_by_register"]) == (11, 7, 1, 1, 2)


def test_staging_root_is_refused(dcanon):
    _client, canon, _ = dcanon
    with pytest.raises(ApiFailure) as exc:                                                     # H-07
        CanonStore.from_data_root(canon.staging.root)
    assert exc.value.code == "SNAPSHOT_UNAVAILABLE"


def test_sources_absent_or_retired_by_register_are_200_with_lifecycle(dcanon):
    client, *_ = dcanon
    for source_id, lifecycle, reason in (("VKM-SRC-013", "ABSENT_BY_REGISTER", "ARCHIVE_DELETED_AFTER_ASSEMBLY"),
                                         ("VKM-SRC-022", "RETIRED", "RETIRED_NOT_EVIDENCE")):
        item = _get(dcanon, f"/v1/source/{source_id}")["item"]                             # H-38: 200, not 410
        envelope, record = item["envelope"], item["record"]
        assert envelope["lifecycle_status"] == lifecycle and envelope["processing_status"] == "SKIPPED_BY_REGISTER"
        assert "REGISTER_NOTES_NOT_EVIDENCE" in envelope["flags"] and record["register_notes_are_evidence"] is False
        assert record["processing_summary"]["register_skip_reason"] == reason
    missing = client.get("/v1/source/VKM-SRC-250", headers=H)
    assert missing.status_code == 404 and missing.json()["error"]["hint"]


def test_foreign_content_page_keeps_the_instance_work_and_is_flagged(dcanon):
    _client, canon, _ = dcanon
    page = _get(dcanon, f"/v1/page/{canon.ids['foreign_page']}")["item"]                      # H-16
    assert page["envelope"]["work_id"] == "VKM-WRK-005" and "PAGE_HAS_FOREIGN_CONTENT" in page["envelope"]["flags"]
    assert page["record"]["foreign_content"] == {"work_ids": [], "unidentified_work": True}
    entry = _get(dcanon, f"/v1/object/{canon.ids['foreign_entry']}")["item"]
    assert "PAGE_HAS_FOREIGN_CONTENT" in entry["envelope"]["flags"]
    assert entry["record"]["citing_work_id"] is None and entry["record"]["citing_work_resolution"] == "FOREIGN_CONTENT"
    host = _get(dcanon, f"/v1/object/{canon.ids['host_entry']}")["item"]
    assert "PAGE_HAS_FOREIGN_CONTENT" not in host["envelope"]["flags"] and host["record"]["foreign_content"] is None
    assert host["record"]["citing_work_id"] == "VKM-WRK-005"


def test_search_hydrates_d_ids_flags_foreign_pages_and_drops_stale(dcanon):
    _client, canon, _ = dcanon
    body = _post(dcanon, "/v1/search", {"query": "синтетический", "kinds": ["PAGE", "FIGURE"]})
    assert [i["envelope"]["object_id"] for i in body["items"]] == [canon.ids["foreign_page"], canon.ids["figure"]]
    assert "STALE_PROJECTION" in {w["code"] for w in body["meta"]["warnings"]}
    page, figure = body["items"]
    assert page["envelope"]["work_id"] == page["record"]["work_id"] == "VKM-WRK-005"
    assert "PAGE_HAS_FOREIGN_CONTENT" in page["envelope"]["flags"] and page["record"]["foreign_content"]
    assert figure["envelope"]["work_id"] == "VKM-WRK-013" and figure["envelope"]["projection"][
        "matches_canonical_snapshot"] is True


def test_document_scoped_docx_and_epub_objects(dcanon):
    _client, canon, _ = dcanon
    for key, kind, layer in (("docx_block", "BLOCK", "DOCX_XML"), ("docx_formula", "FORMULA", "DOCX_XML"),
                             ("epub_block", "BLOCK", "EPUB_XHTML")):
        envelope = _get(dcanon, f"/v1/object/{canon.ids[key]}")["item"]["envelope"]
        assert envelope["object_kind"] == kind and envelope["text_layer"] == layer and envelope["geometry"] is None
        assert envelope["review_status"] == "AUTO_EXTRACTED_UNREVIEWED"
        if key.startswith("docx"):                                                              # H-50
            assert ":doc:" in envelope["object_id"] and envelope["page_id"].startswith("VKM-SRC-023:r")
            assert envelope["work_id"] == "VKM-WRK-023"
    blocks = _post(dcanon, "/v1/objects/query", {"kinds": ["BLOCK"], "source_ids": ["VKM-SRC-023"]})["items"]
    assert len(blocks) == 4 and all(":doc:b" in i["envelope"]["object_id"] for i in blocks)


def test_text_layers_and_models_reach_the_envelope(dcanon):
    _client, canon, _ = dcanon
    ocr = _get(dcanon, f"/v1/object/{canon.ids['ocr_block']}")["item"]["envelope"]
    embedded = _get(dcanon, f"/v1/object/{canon.ids['embedded_block']}")["item"]["envelope"]
    assert (ocr["origin"], ocr["text_layer"]) == ("OCR", "GLM_OCR")
    assert {m["role"] for m in ocr["provenance"]["models"]} == {"LAYOUT", "RECOGNITION"}
    assert (embedded["origin"], embedded["text_layer"]) == ("EMBEDDED_OCR", "PDF_EMBEDDED_OCR_LAYER")
    assert embedded["provenance"]["models"] == [] and ocr["geometry"]["bbox_space"] == "PAGE_PT_TL"


def test_work_group_copies_and_objects_of_a_derived_copy(dcanon):
    work = _get(dcanon, "/v1/work/VKM-WRK-013")["item"]                                        # H-49
    assert "WORK_HAS_MULTIPLE_COPIES" in work["envelope"]["flags"] and work["record"]["work_copy_count"] == 2
    assert [s["source_id"] for s in work["record"]["sources"]] == ["VKM-SRC-013", "VKM-SRC-025", "VKM-SRC-202"]
    objects = _post(dcanon, "/v1/objects/query", {"kinds": ["FIGURE", "TABLE", "FORMULA"],
                                                  "source_ids": ["VKM-SRC-025"]})["items"]
    assert sorted(i["envelope"]["object_kind"] for i in objects) == ["FIGURE", "FORMULA", "TABLE"]
    assert {i["envelope"]["work_id"] for i in objects} == {"VKM-WRK-013"}


def test_provenance_chain_reaches_the_register_and_the_run(dcanon):
    _client, canon, _ = dcanon
    record = _get(dcanon, f"/v1/provenance/{canon.ids['figure']}")["item"]["record"]
    steps = [s["step"] for s in record["chain"]]
    assert steps[0] == "RAW_SOURCE" and {"PROCESSING_RUN", "PRODUCER"} <= set(steps)
    raw = record["chain"][0]
    assert raw["sha256_matches_register"] is True and raw["logical_path"].startswith("PRIVATE:")
    run = next(s for s in record["chain"] if s["step"] == "PROCESSING_RUN")
    assert run["subject"] == canon.ids["extraction_run"]


def test_failed_page_and_crashed_run_are_visible(dcanon):
    _client, canon, _ = dcanon
    page = _get(dcanon, f"/v1/processing/status?page_id={canon.ids['failed_page']}")["item"]["record"]
    assert page["page_status"] == "FAILED" and [e["code"] for e in page["errors"]] == ["RENDER_FAILED"]
    run = _get(dcanon, f"/v1/processing/status?run_id={canon.ids['crashed_run']}")["item"]["record"]
    assert run["run"]["has_end"] is False and run["run"]["status"] == "STARTED"                # H-11


def test_citations_count_entries_and_skip_foreign_pages(dcanon):
    items = _get(dcanon, "/v1/citations/VKM-WRK-001")["items"]
    entries = [i["record"] for i in items if i["envelope"]["object_kind"] == "BIBLIOGRAPHY_ENTRY"]
    assert sorted(e["status"] for e in entries) == ["CANDIDATE", "LINKED"]
    cited_by = [i for i in items if i["envelope"]["object_kind"] == "WORK"]
    # 005 cites 001 twice (host page and foreign page); only the host entry cites on behalf of 005
    assert [(i["envelope"]["object_id"], i["record"]["n_citing_entries"]) for i in cited_by] == [("VKM-WRK-005", 1)]


def test_rerank_text_sends_exactly_the_rerank_text_view(dcanon):
    _client, canon, rerank = dcanon
    candidates = [canon.ids["page_001_3"], canon.ids["figure"], canon.ids["docx_block"], canon.ids["epub_block"]]
    calls = len(rerank.text_calls)
    body = _post(dcanon, "/v1/rerank/text", {"query": "синтетический абзац", "candidate_ids": candidates})
    sent = dict(rerank.text_calls[calls])
    con = canon.connect()
    try:
        view = dict(con.execute("SELECT object_id, text FROM rerank_text WHERE object_id IN "
                                "(SELECT unnest(?::VARCHAR[]))", [candidates]).fetchall())
    finally:
        con.close()
    assert sent == view and len(sent) == 4                                                  # H-13, CP-16
    assert body["items"][0]["envelope"]["object_id"] == canon.ids["page_001_3"]


def test_artifact_bytes_are_verified_and_unstored_render_is_409(dcanon):
    client, canon, _ = dcanon
    figure = _get(dcanon, f"/v1/object/{canon.ids['figure']}")["item"]
    raw_id = figure["envelope"]["provenance"]["raw_artifact_id"]
    resp = client.get(f"/v1/artifact/{raw_id}/content", headers=H)
    assert resp.status_code == 200 and hashlib.sha256(resp.content).hexdigest() == raw_id.removeprefix("sha256:")
    page = _get(dcanon, "/v1/page/VKM-SRC-025:p0001")["item"]["record"]
    resp = client.get(f"/v1/artifact/{page['render_artifact_id']}/content", headers=H)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "ARTIFACT_NOT_MATERIALIZED"


def test_undecodable_stored_image_is_a_data_defect(dcanon):
    client, canon, _ = dcanon                     # D's synthetic crops are JSON bytes registered as image/png
    resp = client.get(f"/v1/figure/{canon.ids['figure']}/image", headers=H)
    error = resp.json()["error"]
    assert resp.status_code == 500 and error["code"] == "ARTIFACT_NOT_DECODABLE" and error["retryable"] is False
    assert error["object_id"].startswith("sha256:")
    wrong_kind = client.get(f"/v1/figure/{canon.ids['table']}/image", headers=H)
    assert wrong_kind.status_code == 400 and wrong_kind.json()["error"]["code"] == "INVALID_ID"
    resp = client.post("/v1/rerank/visual", json={"query": "схема мульды",
                                                  "candidate_ids": [canon.ids["figure"], canon.ids["table"]]},
                       headers=H)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "NO_IMAGE_ARTIFACT"
    assert {r["code"] for r in resp.json()["error"]["details"]["rejected"]} == {"ARTIFACT_NOT_DECODABLE"}
