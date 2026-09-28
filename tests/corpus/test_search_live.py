"""Live OpenSearch tests (marker ``services``; without the service they are skipped as NOT_RUN).

Environment (never committed): ``VKM_TEST_OPENSEARCH_URL`` (e.g. a local SSH-tunnel port). Every test uses its own
index prefix ``vkmtest<8 hex>`` — never the production prefix — and deletes its indices by exact name afterwards (the
cluster refuses wildcard deletes).
"""
from __future__ import annotations

import os
import secrets
import threading
import time

import pytest

pytestmark = pytest.mark.services

pytest.importorskip("opensearchpy")
pytest.importorskip("duckdb")

from vkm_corpus.config import load_settings  # noqa: E402
from vkm_corpus.graph.common import ProjectionError  # noqa: E402
from vkm_corpus.graph.synthetic import synthetic_input, write_synthetic_canonical_root  # noqa: E402
from vkm_corpus.search.client import connect  # noqa: E402
from vkm_corpus.search.indexer import (BuildOptions, alias_targets, build, delete_indices, list_builds,  # noqa: E402
                                       rollback, status)
from vkm_corpus.search.mappings import index_body  # noqa: E402
from vkm_corpus.search.query import SearchRequest, rerank_candidates, search  # noqa: E402


@pytest.fixture(scope="module")
def os_client():
    url = os.environ.get("VKM_TEST_OPENSEARCH_URL", "").strip()
    if not url:
        pytest.skip("NOT_RUN: VKM_TEST_OPENSEARCH_URL is not set")
    try:
        return connect(url=url)
    except ProjectionError as exc:
        pytest.skip(f"NOT_RUN: {exc.code}")


@pytest.fixture()
def prefix(os_client):
    value = f"vkmtest{secrets.token_hex(4)}"
    yield value
    names = [b["index"] for builds in list_builds(os_client, value).values() for b in builds]
    delete_indices(os_client, names)
    assert all(not b for b in list_builds(os_client, value).values())


def test_build_smoke_repeat_and_rollback(os_client, prefix, tmp_path):
    settings = load_settings({})
    first = build(settings, BuildOptions(prefix=prefix, smoke=True), client=os_client, inp=synthetic_input(),
                  data_root=tmp_path)
    assert first["status"] == "COMPLETE", first.get("checks")
    assert {c["check_id"]: c["status"] for c in first["checks"]} == {
        k: "PASS" for k in ("S1", "S3", "S4", "Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8", "Q9", "Q10")}
    time.sleep(1.1)                                   # build ids have one-second resolution
    second = build(settings, BuildOptions(prefix=prefix), client=os_client, inp=synthetic_input(), data_root=tmp_path)
    assert second["doc_stream_sha256"] == first["doc_stream_sha256"] and second["counts"] == first["counts"]
    st = status(os_client, prefix)
    assert st["consistent_snapshot"] and st["aliases"]["pages"]["build_id"] == second["build_id"]
    assert len(st["group_alias"]["indices"]) == 3
    back = rollback(os_client, prefix)
    assert back["targets"]["pages"] == first["indices"]["pages"]
    assert alias_targets(os_client, f"{prefix}-objects") == sorted(first["indices"][t]
                                                                   for t in ("figures", "formulas", "tables"))


def test_build_from_a_canonical_snapshot(os_client, prefix, tmp_path, monkeypatch):
    monkeypatch.delenv("VKM_DATA_ROLE", raising=False)
    root = write_synthetic_canonical_root(tmp_path / "data")
    settings = load_settings({"VKM_DATA_ROOT": str(root), "VKM_DATA_ROLE": "canonical"})
    receipt = build(settings, BuildOptions(prefix=prefix), client=os_client)
    assert receipt["status"] == "COMPLETE"
    assert receipt["counts"] == receipt["expected_counts"] == {"pages": 4, "blocks": 4, "figures": 1, "tables": 1,
                                                                "formulas": 1}
    assert receipt["input"]["snapshot_id"] == "snap-20260928T120000Z-5e5e5e5e"
    assert (root / receipt["receipt_ref"]).is_file()
    hits = search(os_client, SearchRequest("сильвинит", kinds=("BLOCK",)), prefix).hits
    assert [h.page_id for h in hits] == ["VKM-SRC-001:p0002"]              # «сильвинита» found by «сильвинит»


def test_search_answers_ids_filters_and_candidates(os_client, prefix, tmp_path):
    build(load_settings({}), BuildOptions(prefix=prefix), client=os_client, inp=synthetic_input(), data_root=tmp_path)
    resp = search(os_client, SearchRequest("маркшейдерские наблюдения", kinds=("BLOCK",)), prefix)
    assert resp.hits and all(h.id.startswith(h.page_id) for h in resp.hits)
    cand = rerank_candidates(resp)
    assert cand["candidates"][0]["passage"]["object_ids"]
    scoped = search(os_client, SearchRequest("оседание", filters={"source_scope": ["SKRU1"]}), prefix)
    assert all("SKRU1" in h.fields["source_site_scope"] for h in scoped.hits)
    secondary = search(os_client, SearchRequest("сильвин", kinds=("BLOCK",), include_secondary_layers=True), prefix)
    primary = search(os_client, SearchRequest("сильвин", kinds=("BLOCK",)), prefix)
    assert secondary.totals["BLOCK"] > primary.totals["BLOCK"]            # H-30: second layer only on request


def test_strict_mapping_and_duplicate_ids_are_rejected(os_client, prefix):
    from opensearchpy.exceptions import ConflictError, RequestError

    name = f"{prefix}-pages-m1-probe"
    os_client.indices.create(index=name, body=index_body("pages", {"build_id": "probe"}))
    try:
        with pytest.raises(RequestError):
            os_client.index(index=name, id="VKM-SRC-001:p0001",
                            body={"id": "VKM-SRC-001:p0001", "unexpected_field": 1})
        os_client.create(index=name, id="VKM-SRC-001:p0001", body={"id": "VKM-SRC-001:p0001"})
        with pytest.raises(ConflictError):
            os_client.create(index=name, id="VKM-SRC-001:p0001", body={"id": "VKM-SRC-001:p0001"})
    finally:
        delete_indices(os_client, [name])


def test_alias_swap_never_breaks_readers(os_client, prefix, tmp_path):
    settings = load_settings({})
    build(settings, BuildOptions(prefix=prefix), client=os_client, inp=synthetic_input(), data_root=tmp_path)
    errors: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                if not search(os_client, SearchRequest("оседание"), prefix).hits:
                    errors.append("empty")
            except Exception as exc:  # noqa: BLE001 - any reader failure is the finding
                errors.append(type(exc).__name__)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        time.sleep(1.1)
        build(settings, BuildOptions(prefix=prefix), client=os_client, inp=synthetic_input(), data_root=tmp_path)
    finally:
        stop.set()
        thread.join()
    assert errors == []
