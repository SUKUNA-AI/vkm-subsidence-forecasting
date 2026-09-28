"""Projection input from a CANONICAL snapshot of agent D's contract (offline): root guard (H-07), manifest check,
D's loader and SQL rules (H-17), the D → E mapping, plan-only runs of both projections."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from vkm_corpus.config import load_settings  # noqa: E402
from vkm_corpus.graph import schema as S  # noqa: E402
from vkm_corpus.graph.canon import E_RELATIONS, ProjectionInput  # noqa: E402
from vkm_corpus.graph.common import (ProjectionError, load_snapshot, require_canonical_root,  # noqa: E402
                                     verify_snapshot_files)
from vkm_corpus.graph.loader import RebuildOptions, rebuild  # noqa: E402
from vkm_corpus.graph.preflight import run_preflight  # noqa: E402
from vkm_corpus.graph.rows import iter_nodes, iter_rels  # noqa: E402
from vkm_corpus.graph.synthetic import write_synthetic_canonical_root  # noqa: E402
from vkm_corpus.search.documents import iter_documents  # noqa: E402
from vkm_corpus.search.indexer import BuildOptions, build  # noqa: E402
from vkm_corpus.search.mappings import INDEX_TYPES, properties  # noqa: E402


@pytest.fixture(scope="module")
def canon_root(tmp_path_factory):
    return write_synthetic_canonical_root(tmp_path_factory.mktemp("canon") / "data")


@pytest.fixture()
def settings(canon_root, monkeypatch):
    monkeypatch.delenv("VKM_DATA_ROLE", raising=False)
    return load_settings({"VKM_DATA_ROOT": str(canon_root), "VKM_DATA_ROLE": "canonical"})


@pytest.fixture(scope="module")
def inp(canon_root):
    projection = ProjectionInput.from_snapshot(load_snapshot(canon_root))
    yield projection
    projection.close()


def test_staging_roots_are_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("VKM_DATA_ROLE", raising=False)
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / ".vkm_root.json").write_text(json.dumps({"root_kind": "STAGING"}), encoding="utf-8")
    for env in ({"VKM_DATA_ROOT": str(staging), "VKM_DATA_ROLE": "producer"},
                {"VKM_DATA_ROOT": str(staging), "VKM_DATA_ROLE": "canonical"},
                {"VKM_DATA_ROOT": str(tmp_path / "no-marker"), "VKM_DATA_ROLE": "canonical"}):
        with pytest.raises(ProjectionError) as info:
            require_canonical_root(load_settings(env))
        assert info.value.code == "E_STAGING_ROOT"


def test_projectors_refuse_staging_before_any_io(tmp_path, monkeypatch):
    monkeypatch.delenv("VKM_DATA_ROLE", raising=False)
    settings = load_settings({"VKM_DATA_ROOT": str(tmp_path), "VKM_DATA_ROLE": "producer"})
    with pytest.raises(ProjectionError, match="E_STAGING_ROOT"):
        rebuild(settings, RebuildOptions(plan_only=True))
    with pytest.raises(ProjectionError, match="E_STAGING_ROOT"):
        build(settings, BuildOptions(plan_only=True))


def test_manifest_files_are_verified(tmp_path):
    root = write_synthetic_canonical_root(tmp_path / "data")
    snapshot = load_snapshot(root)
    assert verify_snapshot_files(snapshot)["files"] == sum(len(d.paths) for d in snapshot.datasets.values())
    victim = snapshot.datasets["pages"].paths[0]
    victim.write_bytes(victim.read_bytes() + b"x")
    with pytest.raises(ProjectionError) as info:
        verify_snapshot_files(snapshot)
    assert info.value.code == "E_CANON_MANIFEST_MISMATCH"


def test_every_relation_maps_from_d_contract(inp):
    assert set(inp.mapping_report["relations"]) == set(E_RELATIONS)
    assert inp.derived_sql and all(name.endswith(".sql") for name in inp.derived_sql)
    assert not [r for r in run_preflight(inp) if r.status == "FAIL"]


def test_derived_edges_equal_d_views_h17(inp):
    cites_graph = {(e.from_id, e.to_id) for e in iter_rels(inp, S.REL_BY_TYPE["CITES"], None)}
    cites_duckdb = {(r["citing_work_id"], r["cited_work_id"]) for r in inp.fetch("SELECT * FROM main.cites")}
    assert cites_graph == cites_duckdb and cites_graph
    precedes = {(e.from_id, e.to_id) for e in iter_rels(inp, S.REL_BY_TYPE["PRECEDES"], None)}
    assert precedes == {(r["from_page_id"], r["to_page_id"]) for r in inp.fetch("SELECT * FROM main.page_sequence")}
    dup = {e.from_id for e in iter_rels(inp, S.REL_BY_TYPE["DUPLICATE_CANDIDATE_OF"], None)}
    assert dup <= {r["page_id"] for r in inp.fetch("SELECT page_id FROM main.duplicate_page_candidates")}


def test_instance_of_equals_primary_links_of_d_work_sources_cp25(inp):
    edges = {(e.from_id, e.to_id) for e in iter_rels(inp, S.REL_BY_TYPE["INSTANCE_OF"], None)}
    d_rule = {(r["source_id"], r["work_id"]) for r in inp.fetch("SELECT * FROM main.work_sources WHERE is_primary")}
    assert edges == d_rule
    assert {("VKM-SRC-013", "VKM-WRK-013"), ("VKM-SRC-025", "VKM-WRK-013")} <= edges   # K-14 as refined by CP-25
    sources = {r.id: r.props for r in iter_nodes(inp, S.NODE_BY_LABEL["Source"], None)}
    assert sources["VKM-SRC-013"]["lifecycle_status"] == "ABSENT_BY_REGISTER"
    works = {r.id: r.props for r in iter_nodes(inp, S.NODE_BY_LABEL["Work"], None)}
    assert works["VKM-WRK-013"]["work_copy_count"] == 1 and works["VKM-WRK-001"]["work_copy_count"] == 2
    docs = {d["id"]: d for d in iter_documents(inp, "pages")}
    assert docs["VKM-SRC-025:p0001"]["work_copy_count"] == 1                     # ACTIVE copies in the index


def test_documents_fit_strict_mappings_and_rerank_ids_resolve(inp):
    rerank_ids = {r["object_id"] for r in inp.fetch("SELECT object_id FROM main.rerank_text")}
    for index_type in INDEX_TYPES:
        docs = list(iter_documents(inp, index_type))
        assert docs, index_type
        assert all(set(d) <= set(properties(index_type)) for d in docs)
        if index_type in ("pages", "blocks"):
            assert {d["id"] for d in docs} <= rerank_ids        # passage references are readable by F/G (H-04)


def test_plan_only_runs_need_no_server(settings, tmp_path):
    graph_plan = rebuild(settings, RebuildOptions(plan_only=True))
    assert graph_plan["status"] == "PLAN_ONLY"
    assert graph_plan["expected_counts"]["rels"]["INSTANCE_OF"] == 4
    assert graph_plan["input"]["snapshot_id"] == "snap-20260928T120000Z-5e5e5e5e"
    search_plan = build(settings, BuildOptions(plan_only=True))
    assert search_plan["status"] == "PLAN_ONLY" and search_plan["expected_counts"]["pages"] == 4
