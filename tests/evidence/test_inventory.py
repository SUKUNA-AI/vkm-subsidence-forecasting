"""Synthetic metadata partitions; never opens a production root or raw source."""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from vkm_corpus.api.canon import CanonStore, KIND_TABLE, SnapshotInfo
from vkm_corpus.contracts.datasets import DATASETS
from vkm_evidence.inventory import OBJECT_TABLES, inventory_snapshot, inventory_source, inventory_summary

SID = "VKM-SRC-001"
SHA = "a" * 64
OTHER = "b" * 64
SNAPSHOT = SnapshotInfo("snap-test", "c" * 64, "2026-10-01T00:00:00Z", "synthetic")
PAGE = SID + ":p0001"
OBJECT = PAGE + ":b123456abcdef"

SCHEMAS = {
    "sources": "source_id TEXT, source_sha256 TEXT, lifecycle_status TEXT",
    "documents": "object_id TEXT, source_id TEXT, source_sha256 TEXT, page_count INTEGER, page_unit TEXT, "
                 "format_detected TEXT, processing_status TEXT",
    "pages": "object_id TEXT, source_id TEXT, source_sha256 TEXT, page_id TEXT, page_index INTEGER, "
             "page_kind TEXT, page_status TEXT",
}
OBJECT_COLUMNS = ("object_id TEXT, object_kind TEXT, source_id TEXT, source_sha256 TEXT, page_id TEXT, "
                  "config_hash TEXT, content_sha256 TEXT, region_origin TEXT, docx_paragraph_path TEXT, raw_locator TEXT")
for _, _, table in OBJECT_TABLES:
    SCHEMAS[table] = OBJECT_COLUMNS.removesuffix(", raw_locator TEXT") if table == "bibliography" else OBJECT_COLUMNS


class SyntheticCanon:
    """SQLite tests the pure SQL contract; DuckDB variant uses actual CanonStore."""

    def __init__(self, backend):
        self.backend = backend
        self.calls = []
        self.replacement = None
        self.replace_at = None
        if backend == "duckdb":
            duckdb = pytest.importorskip("duckdb", reason="real CanonStore runtime requires installed DuckDB")
            self.con = duckdb.connect(":memory:")
            self.con.execute("CREATE SCHEMA meta")
            self.con.execute("CREATE TABLE meta.snapshot AS SELECT 'snap-test' snapshot_id, ? manifest_sha256, "
                             "TIMESTAMPTZ '2026-10-01 00:00:00+00' built_at, 'synthetic' duckdb_version", [SNAPSHOT.manifest_sha256])
            self.con.execute("CREATE TABLE meta.commits(commit_key TEXT, commit_id TEXT)")
            self.canon = CanonStore(connection=self.con)
        else:
            self.con = sqlite3.connect(":memory:")
            self.con.row_factory = sqlite3.Row
            self.con.execute("ATTACH DATABASE ':memory:' AS information_schema")
            self.con.execute("CREATE TABLE information_schema.columns(table_name TEXT, column_name TEXT)")
        for table, columns in SCHEMAS.items():
            # Verify fixture metadata against actual schema, not an imagined
            # block_id or processing_status that object rows do not contain.
            dataset = "bibliography_entries" if table == "bibliography" else table.strip('"')
            fixture_columns = [c.strip().split()[0] for c in columns.split(",")]
            assert set(fixture_columns) <= set(DATASETS[dataset].fields)
            self.con.execute(f"CREATE TABLE {table}({columns})")
            if backend == "sqlite":
                self.con.executemany("INSERT INTO information_schema.columns VALUES (?,?)",
                                     [(table.strip('"'), c) for c in fixture_columns])

    def insert(self, table, **row):
        self.con.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))

    def snapshot(self):
        return self.replacement or (self.canon.snapshot() if self.backend == "duckdb" else SNAPSHOT)

    def query(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        if self.replace_at == len(self.calls):
            self.replacement = replace(self.snapshot(), manifest_sha256=OTHER)
        if self.backend == "duckdb":
            return self.canon.query(sql, params)
        return [dict(r) for r in self.con.execute(sql, params).fetchall()]

    def close(self):
        self.con.close()


@pytest.fixture(params=["sqlite", "duckdb"])
def canon(request):
    c = SyntheticCanon(request.param)
    c.insert("sources", source_id=SID, source_sha256=SHA, lifecycle_status="ACTIVE")
    c.insert("documents", object_id=SID + ":doc", source_id=SID, source_sha256=SHA, page_count=1,
             page_unit="p", format_detected="PDF", processing_status="COMPLETE")
    c.insert("pages", object_id=PAGE, source_id=SID, source_sha256=SHA, page_id=PAGE, page_index=1,
             page_kind="PDF_PAGE", page_status="NATIVE_OK")
    c.insert("blocks", object_id=OBJECT, object_kind="BLOCK", source_id=SID, source_sha256=SHA,
             page_id=PAGE, config_hash=SHA, content_sha256=OTHER, region_origin="NATIVE_TEXT", raw_locator="block:1")
    yield c
    c.close()


def test_actual_schema_primary_keys():
    for _, kind, table in OBJECT_TABLES:
        assert KIND_TABLE[kind] == (table, "object_id")
        spec = DATASETS["bibliography_entries" if table == "bibliography" else table.strip('"')]
        assert spec.primary_key == ("object_id",)
        assert "processing_status" not in spec.fields


def test_inventory_is_bound_deterministic_metadata_only_and_source_scoped(canon):
    result = inventory_source(canon, SHA, SID, batch_rows=1)
    assert result == inventory_source(canon, SHA, SID, batch_rows=1)
    assert result["snapshot_id"] == "snap-test" and result["manifest_sha256"] == SNAPSHOT.manifest_sha256
    assert result["source_sha256"] == SHA and not result["original_bytes_verified"]
    assert result["report"]["status"] == "ACCOUNTED"
    assert result["report"]["expected_units"] == 1
    assert result["report"]["detector_recall"] == result["report"]["scientific_admission"] == "NOT_ESTABLISHED"
    assert result["ledger"]["candidates"][0]["locator"] == "block:1"
    assert result["ledger"]["attempts"][0]["object_outputs"] == {OBJECT: OTHER}
    for sql, params in canon.calls:
        assert "SELECT *" not in sql
        assert not any(word in sql.split() for word in ("text", "cells", "raw_output", "normalized_text"))
        if "information_schema" not in sql:
            assert "source_id=?" in sql and params[0] == SID and "LIMIT" in sql


def test_missing_page_stays_in_denominator(canon):
    canon.con.execute("UPDATE documents SET page_count=2")
    ledger = inventory_snapshot(canon, SHA, SID)
    assert len(ledger.units) == 2 and len(ledger.inspected_units) == 1
    assert ledger.units[1].unit_id == SID + ":p0002"
    assert ledger.report()["status"] == "INCOMPLETE"


def test_missing_page_with_existing_object_is_not_native(canon):
    canon.con.execute("DELETE FROM pages")
    with pytest.raises(ValueError, match="missing or foreign page"):
        inventory_snapshot(canon, SHA, SID)


def test_bibliography_is_counted_without_becoming_a_scientific_claim(canon):
    canon.insert("bibliography", object_id=PAGE + ":c123456abcdef", object_kind="BIBLIOGRAPHY_ENTRY",
                 source_id=SID, source_sha256=SHA, page_id=PAGE, config_hash=SHA, content_sha256=OTHER,
                 region_origin="NATIVE_TEXT")
    ledger = inventory_snapshot(canon, SHA, SID)
    assert len(ledger.candidates) == 2 and ledger.candidates[-1].kind == "OTHER"
    assert ledger.report()["scientific_admission"] == "NOT_ESTABLISHED"


@pytest.mark.parametrize("mutation,error", [
    ("DELETE FROM documents", "exactly one canonical document"),
    ("INSERT INTO documents SELECT * FROM documents", "exactly one canonical document"),
    ("DELETE FROM sources", "exactly one registry"),
    ("INSERT INTO sources SELECT * FROM sources", "exactly one registry"),
    ("UPDATE documents SET object_id='VKM-SRC-002:doc'", "document identity"),
    ("UPDATE documents SET page_count=-1", "denominator"),
    ("UPDATE pages SET page_index=2", "outside document denominator"),
    ("INSERT INTO pages SELECT * FROM pages", "duplicate canonical page"),
    ("UPDATE pages SET page_kind='EPUB_SPINE_ITEM'", "identity/unit"),
    ("UPDATE pages SET object_id='VKM-SRC-002:p0001'", "identity/unit"),
    ("UPDATE pages SET page_id='VKM-SRC-002:p0001'", "identity/unit"),
    ("UPDATE documents SET page_unit='UNKNOWN'", "unknown document page unit"),
    ("INSERT INTO blocks SELECT * FROM blocks", "duplicate canonical object"),
    ("UPDATE blocks SET page_id='VKM-SRC-001:p0002'", "missing or foreign page"),
    ("UPDATE blocks SET page_id=NULL", "exact page identity"),
    ("UPDATE blocks SET object_kind='TABLE'", "identity/kind"),
    ("UPDATE blocks SET object_id='VKM-SRC-001:doc:b123456abcdef'", "exact page identity"),
    ("UPDATE blocks SET content_sha256=NULL", "object content hash"),
    ("UPDATE blocks SET config_hash='invalid'", "extraction config hash"),
])
def test_invalid_partition_fails_closed(canon, mutation, error):
    canon.con.execute(mutation)
    with pytest.raises(ValueError, match=error):
        inventory_snapshot(canon, SHA, SID, batch_rows=1)


@pytest.mark.parametrize("table", ["documents", "pages", "blocks"])
def test_source_hash_mismatch_is_never_adopted(canon, table):
    canon.con.execute(f"UPDATE {table} SET source_sha256=?", [OTHER])
    with pytest.raises(ValueError, match="mixed original source"):
        inventory_snapshot(canon, SHA, SID)


@pytest.mark.parametrize("table,column,status", [("documents", "processing_status", "PARTIAL"),
                                               ("pages", "page_status", "OCR_REQUIRED")])
def test_existing_object_does_not_mask_partial_parent(canon, table, column, status):
    canon.con.execute(f"UPDATE {table} SET {column}=?", [status])
    ledger = inventory_snapshot(canon, SHA, SID)
    assert ledger.candidates[0].state == "NEEDS_REVIEW"
    assert ledger.candidates[0].reason == "LEGACY_PARENT_NOT_COMPLETE"


def test_real_docx_document_scope_is_preserved(canon):
    canon.con.execute("DELETE FROM pages")
    canon.con.execute("UPDATE documents SET page_count=0, page_unit='r', format_detected='DOCX'")
    canon.con.execute("UPDATE blocks SET object_id=?, page_id=NULL, region_origin='DOCX_ELEMENT', "
                      "docx_paragraph_path='body/p[1]'", [SID + ":doc:b123456abcdef"])
    ledger = inventory_snapshot(canon, SHA, SID)
    assert len(ledger.units) == 1 and ledger.units[0].unit_kind == "NATIVE_OBJECT"
    assert ledger.candidates[0].unit_id == SID + ":native-document"
    canon.con.execute("UPDATE blocks SET page_id='VKM-SRC-001:r0001'")
    with pytest.raises(ValueError, match="missing or foreign page"):
        inventory_snapshot(canon, SHA, SID)


def test_native_scope_cannot_be_used_to_hide_broken_page_reference(canon):
    canon.con.execute("UPDATE blocks SET object_id=?, page_id=NULL, region_origin='DOCX_ELEMENT', "
                      "docx_paragraph_path='body/p[1]'", [SID + ":doc:b123456abcdef"])
    with pytest.raises(ValueError, match="invalid native document"):
        inventory_snapshot(canon, SHA, SID)


def test_old_nullable_locator_schema_is_inventoryable(canon):
    canon.con.execute("ALTER TABLE blocks DROP COLUMN raw_locator")
    if canon.backend == "sqlite":
        canon.con.execute("DELETE FROM information_schema.columns WHERE table_name='blocks' AND column_name='raw_locator'")
    assert inventory_snapshot(canon, SHA, SID).candidates[0].locator == OBJECT


@pytest.mark.parametrize("entrypoint", [inventory_summary, lambda c: inventory_source(c, SHA, SID)])
def test_snapshot_replacement_including_same_id_rebuild_fails(canon, entrypoint):
    canon.replace_at = 2
    with pytest.raises(ValueError, match="snapshot changed"):
        entrypoint(canon)


def test_limits_fail_explicitly(canon):
    with pytest.raises(ValueError, match="positive integers"):
        inventory_snapshot(canon, SHA, SID, max_objects=0)
    canon.con.execute("UPDATE documents SET page_count=2")
    with pytest.raises(ValueError, match="denominator exceeds"):
        inventory_snapshot(canon, SHA, SID, max_units=1)
    canon.con.execute("INSERT INTO blocks SELECT REPLACE(object_id,'abcdef','abcdee'), object_kind, source_id, "
                      "source_sha256, page_id, config_hash, content_sha256, region_origin, docx_paragraph_path, "
                      "raw_locator FROM blocks")
    with pytest.raises(ValueError, match="object count exceeds"):
        inventory_snapshot(canon, SHA, SID, max_objects=1, batch_rows=1)


def test_other_source_partition_is_not_adopted(canon):
    canon.con.execute("INSERT INTO pages SELECT object_id,'VKM-SRC-002',source_sha256,page_id,page_index,"
                      "page_kind,page_status FROM pages")
    assert inventory_snapshot(canon, SHA, SID).report()["expected_units"] == 1


def test_summary_counts_missing_sources_not_row_subtraction(canon):
    canon.insert("sources", source_id="VKM-SRC-002", source_sha256=OTHER, lifecycle_status="ACTIVE")
    canon.insert("sources", source_id="VKM-SRC-013", source_sha256=OTHER, lifecycle_status="ABSENT_BY_REGISTER")
    canon.con.execute("INSERT INTO documents SELECT * FROM documents")
    result = inventory_summary(canon)
    assert result["missing_document_denominators"] == 2
    assert result["missing_active_document_denominators"] == 1
    assert result["excluded_without_document"] == 1
    assert result["duplicate_identities"] == {"sources": 0, "documents": 1}
    assert result["status"] == "INTEGRITY_FAILURE" and not result["document_denominator_valid"]
    assert result["source_partition_validation"] == "NOT_RUN"
    assert not result["original_bytes_verified"]
    assert all("SELECT *" not in sql for sql, _ in canon.calls)


def test_summary_reports_orphan_and_mixed_denominators(canon):
    canon.con.execute("UPDATE documents SET source_sha256=?", [OTHER])
    canon.con.execute("INSERT INTO documents SELECT object_id,'VKM-SRC-002',source_sha256,page_count,"
                      "page_unit,format_detected,processing_status FROM documents")
    result = inventory_summary(canon)
    assert result["orphan_documents"] == result["document_source_hash_mismatches"] == 1


def test_summary_does_not_claim_active_gap_is_an_exclusion(canon):
    canon.insert("sources", source_id="VKM-SRC-002", source_sha256=OTHER, lifecycle_status="ACTIVE")
    result = inventory_summary(canon)
    assert result["status"] == "INCOMPLETE" and result["excluded_without_document"] == 0
