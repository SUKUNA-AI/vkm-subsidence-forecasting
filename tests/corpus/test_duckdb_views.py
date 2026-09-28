"""DuckDB query layer over the synthetic snapshot: views/macros exist, derived rules give the expected answers,
SQL rerank text = Python rule, BML ids = Python ids, provenance trace is complete, the build verifies fingerprints
(H-48) and refuses STAGING roots (H-07)."""
from __future__ import annotations

import pytest

pytest.importorskip("pyarrow")
duckdb = pytest.importorskip("duckdb")

from vkm_corpus import ids  # noqa: E402
from vkm_corpus.contracts.datasets import DATASETS  # noqa: E402
from vkm_corpus.contracts.text_rules import rerank_text_v1, sha256_text  # noqa: E402

VIEWS = ["sources", "works", "works_all", "documents", "pages", "blocks", "figures", "tables", "formulas",
         "bibliography_entries", "bibliography", "bibliography_links", "cites", "work_sources", "foreign_content_pages",
         "work_of_page", "work_copy_counts", "page_sequence", "duplicate_page_candidates", "page_coverage",
         "processing_status", "source_status_summary", "corpus_counts", "all_objects", "rerank_text",
         "works_availability", "artifacts", "processing_runs", "processing_steps", "errors", "source_work_links",
         "work_relations", "source_relations", "authors", "work_authors", "venues"]
MACROS = ["provenance_trace", "registry_trace", "objects_on_page", "objects_by_source", "resolve_work"]


@pytest.fixture(scope="module")
def canon(tmp_path_factory):
    from vkm_corpus.testing.synthetic import synthetic_canon

    return synthetic_canon(tmp_path_factory.mktemp("duck") / "data", with_duckdb=True)


@pytest.fixture(scope="module")
def con(canon):
    c = duckdb.connect(str(canon.duckdb_path), read_only=True)
    yield c
    c.close()


def q(con, sql, *params):
    return con.execute(sql, list(params)).to_arrow_table().to_pylist()


def test_views_and_macros_exist(con):
    names = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE schema_name = 'main'").fetchall()}
    assert set(VIEWS) <= names, set(VIEWS) - names
    macros = {r[0] for r in con.execute("SELECT function_name FROM duckdb_functions() "
                                        "WHERE function_type = 'table_macro'").fetchall()}
    assert set(MACROS) <= macros


def test_meta_snapshot_matches_manifest(con, canon):
    snap = q(con, "SELECT snapshot_id, manifest_sha256 FROM meta.snapshot")[0]
    assert snap["snapshot_id"] == canon.snapshot_id and len(snap["manifest_sha256"]) == 64
    heads = {r["commit_key"]: r["commit_id"] for r in q(con, "SELECT * FROM meta.commits")}
    assert heads["REGISTRY"] == canon.manifest["registry_head"]


def test_corpus_counts_roll_up(con):
    c = q(con, "SELECT * FROM corpus_counts")[0]
    assert c["sources_total"] == 11 and c["rollup_closed"]
    assert (c["sources_complete"], c["sources_partial"], c["sources_not_processed"],
            c["sources_skipped_by_register"]) == (7, 1, 1, 2)
    assert c["pages_failed"] == 1 and c["figures"] == 1 and c["tables"] == 1 and c["formulas"] == 2
    skipped = q(con, "SELECT source_id, register_skip_reason FROM source_status_summary "
                     "WHERE source_rollup = 'SKIPPED_BY_REGISTER' ORDER BY 1")
    assert [(r["source_id"], r["register_skip_reason"]) for r in skipped] == [
        ("VKM-SRC-013", "ARCHIVE_DELETED_AFTER_ASSEMBLY"), ("VKM-SRC-022", "RETIRED_NOT_EVIDENCE")]


def test_foreign_content_never_cites_for_the_host(con, canon):
    rows = {r["object_id"]: r for r in q(con, "SELECT object_id, citing_work_id, citing_work_resolution, "
                                              "citing_work_is_container FROM bibliography")}
    foreign = rows[canon.ids["foreign_entry"]]
    assert foreign["citing_work_id"] is None and foreign["citing_work_resolution"] == "FOREIGN_CONTENT"
    host = rows[canon.ids["host_entry"]]
    assert host["citing_work_id"] == "VKM-WRK-005" and host["citing_work_resolution"] == "UNIQUE_LINK"


def test_cites_only_from_exact_ids(con, canon):
    cites = {(r["citing_work_id"], r["cited_work_id"]): r for r in q(con, "SELECT * FROM cites")}
    assert set(cites) == {("VKM-WRK-001", "VKM-WRK-005"), ("VKM-WRK-005", "VKM-WRK-001")}
    edge = cites[("VKM-WRK-005", "VKM-WRK-001")]
    assert edge["n_citing_entries"] == 1 and edge["n_citing_sources"] == 1 and edge["rule_version"] == "cites_v1"
    links = q(con, "SELECT * FROM bibliography_links ORDER BY object_id")
    cand = [r for r in links if r["entry_id"] == canon.ids["candidate_entry"]]
    assert cand and cand[0]["match_status"] == "CANDIDATE" and cand[0]["cited_work_id"] == "VKM-WRK-249"
    for r in links:
        assert r["object_id"] == ids.bml_id(r["entry_id"], r["cited_work_id"], r["match_method"])
        assert r["review_status"] == "AUTO_EXTRACTED_UNREVIEWED" and r["origin"] == "DERIVED"
    assert [c for c in q(con, "DESCRIBE bibliography_links")] and \
        [d["column_name"] for d in q(con, "DESCRIBE bibliography_links")] == list(DATASETS["bibliography_links"].fields)


def test_instance_of_excludes_foreign_content(con):
    ws = q(con, "SELECT source_id, work_id, link_type FROM work_sources ORDER BY source_id")
    assert all(r["link_type"] != "FOREIGN_CONTENT" for r in ws)
    grp = sorted(r["source_id"] for r in ws if r["work_id"] == "VKM-WRK-013")
    assert grp == ["VKM-SRC-013", "VKM-SRC-025", "VKM-SRC-202"]
    counts = q(con, "SELECT * FROM work_copy_counts WHERE work_id = 'VKM-WRK-013'")[0]
    assert (counts["n_sources_total"], counts["n_sources_active"], counts["work_copy_count"]) == (3, 2, 2)  # CP-25


def test_duplicates_and_page_sequence(con, canon):
    dups = q(con, "SELECT dup_group_id, page_id, n_sources FROM duplicate_page_candidates ORDER BY page_id")
    assert [d["page_id"] for d in dups] == [canon.ids["page_001_3"], "VKM-SRC-005:p0001"]
    assert len({d["dup_group_id"] for d in dups}) == 1 and dups[0]["n_sources"] == 2
    seq = q(con, "SELECT from_page_id, to_page_id, rule_version FROM page_sequence WHERE source_id = 'VKM-SRC-001' "
                 "ORDER BY 1")
    assert [(s["from_page_id"], s["to_page_id"]) for s in seq] == [("VKM-SRC-001:p0001", "VKM-SRC-001:p0002"),
                                                                    ("VKM-SRC-001:p0002", "VKM-SRC-001:p0003")]


def test_resolve_work_and_availability(con):
    assert q(con, "SELECT * FROM resolve_work('VKM-WRK-013')")[0]["resolved_work_id"] == "VKM-WRK-013"
    av = {r["work_id"]: r for r in q(con, "SELECT * FROM works_availability")}
    assert av["VKM-WRK-001"]["available_basis"] == "ASSUMED_FROM_PUBLICATION"
    assert str(av["VKM-WRK-001"]["available_latest_day"]) == "2020-12-31"
    assert av["VKM-WRK-022"]["available_basis"] == "UNKNOWN" and av["VKM-WRK-022"]["available_latest_day"] is None


def test_provenance_trace_answers_section_50(con, canon):
    for key in ("figure", "table", "formula", "ocr_block", "docx_block", "epub_block", "embedded_block"):
        t = q(con, "SELECT * FROM provenance_trace(?)", canon.ids[key])
        assert len(t) == 1, key
        t = t[0]
        for col in ("object_kind", "source_id", "origin", "review_status", "processing_run_id", "run_kind",
                    "pipeline_version", "extractor_id", "extractor_version", "raw_artifact_id",
                    "raw_artifact_relpath", "source_canonical_path", "register_sha256", "commit_id",
                    "object_version", "snapshot_id"):
            assert t[col] is not None, (key, col)
        assert t["record_role"] == "CANONICAL" and t["source_sha256_matches_register"] is True
    ocr = q(con, "SELECT * FROM provenance_trace(?)", canon.ids["formula"])[0]
    assert ocr["origin"] == "OCR" and ocr["model_id"] == "zai-org/GLM-OCR" and ocr["region_origin"] == "LAYOUT_MODEL"
    emb = q(con, "SELECT * FROM provenance_trace(?)", canon.ids["embedded_block"])[0]
    assert emb["origin"] == "EMBEDDED_OCR" and emb["text_layer"] == "PDF_EMBEDDED_OCR_LAYER"
    reg = q(con, "SELECT * FROM registry_trace('VKM-SRC-013')")[0]
    assert reg["input_ref"].startswith("PRIVATE:") and reg["commit_id"]


def test_objects_on_page_and_status(con, canon):
    objs = q(con, "SELECT object_kind FROM objects_on_page('VKM-SRC-025:p0001')")
    assert {o["object_kind"] for o in objs} == {"BLOCK", "FIGURE", "TABLE", "FORMULA"}
    st = q(con, "SELECT * FROM processing_status WHERE page_id = ?", canon.ids["failed_page"])[0]
    assert st["last_attempt_status"] == "FAILED" and st["committed_page_status"] == "FAILED"
    runs = {r["processing_run_id"]: r for r in q(con, "SELECT processing_run_id, has_end, status FROM processing_runs")}
    assert runs[canon.ids["crashed_run"]]["has_end"] is False


def test_rerank_text_sql_equals_python(con):
    tables = {"PAGE": "pages", "BLOCK": "blocks", "FIGURE": "figures", "TABLE": '"tables"', "FORMULA": "formulas",
              "BIBLIOGRAPHY_ENTRY": "bibliography_entries"}
    sql = {r["object_id"]: r for r in q(con, "SELECT * FROM rerank_text")}
    n = 0
    for kind, table in tables.items():
        for row in q(con, f"SELECT * FROM canonical.{table}"):
            expected = rerank_text_v1(kind, row)
            got = sql.get(row["object_id"])
            if expected is None:
                assert got is None
            else:
                assert got["text"] == expected and got["text_sha256"] == sha256_text(expected)
                n += 1
    assert n > 20


def test_build_refuses_staging_and_mismatch(canon, tmp_path):
    from vkm_corpus.duckdb.build import FingerprintMismatch, build_duckdb, duckdb_status
    from vkm_corpus.parquet.layout import RootError

    with pytest.raises(RootError):
        build_duckdb(canon.staging)
    st = duckdb_status(canon.layout)
    assert st["up_to_date"] and st["snapshot_id"] == canon.snapshot_id
    # rebuilding gives the same answers
    before = q(duckdb.connect(str(canon.duckdb_path), read_only=True), "SELECT * FROM corpus_counts")
    build_duckdb(canon.layout)
    after = q(duckdb.connect(str(canon.duckdb_path), read_only=True), "SELECT * FROM corpus_counts")
    assert before == after
    # a manifest whose fingerprint does not match the files: the DuckDB file is not replaced
    import json

    mpath = canon.layout.path(canon.layout.snapshot_manifest(canon.snapshot_id))
    original = mpath.read_bytes()
    m = json.loads(original)
    m["datasets"]["pages"]["table_fingerprint"] = "0" * 64
    mpath.write_text(json.dumps(m), encoding="utf-8")
    stamp = canon.duckdb_path.stat().st_mtime_ns
    try:
        with pytest.raises(FingerprintMismatch):
            build_duckdb(canon.layout)
        assert canon.duckdb_path.stat().st_mtime_ns == stamp
    finally:
        mpath.write_bytes(original)
