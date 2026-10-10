"""Read-only, metadata-only adoption of one existing canonical source.

This is a legacy baseline, not detector recall, original-byte verification or
scientific admission. Missing document units remain in the denominator. Invalid
identities fail closed instead of being recast as native/document-level objects.
"""
from __future__ import annotations

from collections.abc import Iterator

from vkm_corpus.contracts.vocab import OBJECT_KIND_CODE, PAGE_KIND_UNIT
from vkm_corpus.ids import grammar
from vkm_evidence.contracts import record_hash
from vkm_evidence.coverage import CoverageLedger


# CanonStore's actual key is object_id, including blocks (not block_id).
# Bibliography is an extracted object too, but not a scientific claim.
OBJECT_TABLES = (("TEXT", "BLOCK", "blocks"), ("TABLE", "TABLE", '"tables"'),
                 ("FORMULA", "FORMULA", "formulas"), ("FIGURE", "FIGURE", "figures"),
                 ("OTHER", "BIBLIOGRAPHY_ENTRY", "bibliography"))
_GOOD_PAGE = {"NATIVE_OK", "EMBEDDED_TEXT_OK", "OCR_OK"}


class _SnapshotRead:
    """Reject a replacement between metadata queries, including same-ID rebuilds."""

    def __init__(self, canon):
        self.canon = canon
        self.snapshot = canon.snapshot()
        if not self.snapshot.snapshot_id or not grammar.matches("sha256", self.snapshot.manifest_sha256):
            raise ValueError("canonical snapshot identity unavailable")

    def check(self):
        if self.canon.snapshot() != self.snapshot:
            raise ValueError("canonical snapshot changed during inventory; restart the source partition")

    def query(self, sql, params=()):
        self.check()
        rows = self.canon.query(sql, params)
        self.check()
        return rows

    def count(self, sql, params=()):
        return self.query(sql, params)[0]["n"]


def _hash(value, label):
    if not grammar.matches("sha256", value):
        raise ValueError(f"invalid {label} hash")
    return value


def _identity(row, source_id, source_hash):
    if row["source_id"] != source_id or row["source_sha256"] != source_hash:
        raise ValueError("mixed original source identities")


def _rows(read, table, columns, source_id, batch_rows, key) -> Iterator[dict]:
    # Bounded fetches, source predicate on every data read. OFFSET is intentional:
    # a keyset cursor would skip repeated primary keys at a batch boundary.
    offset = 0
    while True:
        batch = read.query(f"SELECT {columns} FROM {table} WHERE source_id=? "
                           f"ORDER BY {key} LIMIT ? OFFSET ?", [source_id, batch_rows, offset])
        yield from batch
        if len(batch) < batch_rows:
            break
        offset += len(batch)


def _inventory(canon, campaign_sha256, source_id, *, batch_rows, max_units, max_objects):
    if not grammar.matches("source", source_id):
        raise ValueError("invalid source identity")
    _hash(campaign_sha256, "campaign")
    for value in (batch_rows, max_units, max_objects):
        if type(value) is not int or value < 1:
            raise ValueError("inventory limits must be positive integers")
    if batch_rows > 10_000:
        raise ValueError("batch_rows exceeds bounded query limit")
    read = _SnapshotRead(canon)
    sources = read.query("SELECT source_id, source_sha256 FROM sources WHERE source_id=? LIMIT 2", [source_id])
    if len(sources) != 1:
        raise ValueError("source must have exactly one registry identity")
    source_hash = _hash(sources[0]["source_sha256"], "source")
    documents = read.query("SELECT object_id, source_id, source_sha256, page_count, page_unit, "
                           "format_detected, processing_status FROM documents WHERE source_id=? LIMIT 2", [source_id])
    if len(documents) != 1:
        raise ValueError("source must have exactly one canonical document denominator")
    document = documents[0]
    _identity(document, source_id, source_hash)
    if document["object_id"] != source_id + ":doc":
        raise ValueError("document identity does not match source")
    page_count = document["page_count"]
    if type(page_count) is not int or page_count < 0:
        raise ValueError("invalid document unit denominator")
    if page_count > max_units:
        raise ValueError("document unit denominator exceeds inventory limit")
    if page_count > 9999:
        raise ValueError("document denominator exceeds canonical page ID grammar")
    if document["page_unit"] not in {"p", "r", "s"}:
        raise ValueError("unknown document page unit")
    units, inspected, candidates, attempts = [], [], [], []
    pages, page_ids = {}, {}
    columns = "object_id, source_id, source_sha256, page_id, page_index, page_kind, page_status"
    for page in _rows(read, "pages", columns, source_id, batch_rows, "page_index, page_id"):
        _identity(page, source_id, source_hash)
        index, pid = page["page_index"], page["page_id"]
        if type(index) is not int or not 1 <= index <= page_count:
            raise ValueError("canonical page lies outside document denominator")
        if index in pages or pid in page_ids:
            raise ValueError("duplicate canonical page identity")
        if (not grammar.matches("page", pid) or page["object_id"] != pid
                or pid != f"{source_id}:{document['page_unit']}{index:04d}"
                or PAGE_KIND_UNIT.get(page["page_kind"]) != document["page_unit"]):
            raise ValueError("page identity/unit does not match document denominator")
        pages[index], page_ids[pid] = page, page
    for index in range(1, page_count + 1):
        uid = f"{source_id}:{document['page_unit']}{index:04d}"
        units.append({"unit_id": uid, "source_id": source_id, "source_sha256": source_hash,
                      "unit_kind": "PAGE", "locator": f"document-unit:{document['page_unit']}:{index}"})
        if index in pages:
            inspected.append(uid)
    seen, native_unit, native_seen = set(), source_id + ":native-document", False
    for kind, object_kind, table in OBJECT_TABLES:
        # Nullable raw_locator was appended to some schemas in 0.1.1. An old
        # served snapshot is still inventoryable; its locator remains ID/path.
        has_raw = bool(read.query("SELECT column_name FROM information_schema.columns "
                                  "WHERE table_name=? AND column_name='raw_locator'", [table.strip('"')]))
        columns = ("object_id, object_kind, source_id, source_sha256, page_id, config_hash, content_sha256, "
                   "region_origin, docx_paragraph_path" + (", raw_locator" if has_raw else ""))
        for row in _rows(read, table, columns, source_id, batch_rows, "object_id"):
            _identity(row, source_id, source_hash)
            oid, pid = row["object_id"], row["page_id"]
            if oid in seen:
                raise ValueError("duplicate canonical object identity")
            seen.add(oid)
            if len(seen) > max_objects:
                raise ValueError("source object count exceeds inventory limit")
            if (not grammar.matches("object", oid) or row["object_kind"] != object_kind
                    or oid.split(":")[0] != source_id or oid.rsplit(":", 1)[1][0] != OBJECT_KIND_CODE[object_kind]):
                raise ValueError("object identity/kind does not match canonical table")
            if pid is not None and pid not in page_ids:
                raise ValueError("object references missing or foreign page")
            native = row["region_origin"] == "DOCX_ELEMENT"
            if native:
                if (document["format_detected"] != "DOCX" or oid.split(":")[1] != "doc"
                        or not row["docx_paragraph_path"]):
                    raise ValueError("invalid native document object identity")
                uid = native_unit
                if not native_seen:
                    if len(units) >= max_units:
                        raise ValueError("native document unit exceeds inventory limit")
                    units.append({"unit_id": uid, "source_id": source_id, "source_sha256": source_hash,
                                  "unit_kind": "NATIVE_OBJECT", "locator": "native-document"})
                    inspected.append(uid)
                    native_seen = True
            else:
                if pid is None or not oid.startswith(pid + ":"):
                    raise ValueError("page-scoped object lacks its exact page identity")
                uid = pid
            content_hash = _hash(row["content_sha256"], "object content")
            _hash(row["config_hash"], "extraction config")
            attempt_id = "legacy-" + record_hash({"snapshot": read.snapshot.snapshot_id,
                "manifest": read.snapshot.manifest_sha256, "id": oid, "content": content_hash})[:32]
            attempts.append({"attempt_id": attempt_id, "unit_id": uid, "candidate_id": oid,
                "stage": "LEGACY_CANONICAL_ADOPTION", "input_sha256": source_hash,
                "config_sha256": row["config_hash"], "code_revision": "LEGACY_NOT_ATTESTED",
                "state": "SUCCEEDED", "outputs": (content_hash,), "object_outputs": {oid: content_hash}})
            # Object rows do not have processing_status. Use the real document
            # and page metadata rather than treating a nonexistent column as OK.
            partial = (document["processing_status"] != "COMPLETE"
                       or (pid is not None and page_ids[pid]["page_status"] not in _GOOD_PAGE))
            candidates.append({"candidate_id": oid, "unit_id": uid, "kind": kind,
                "locator": row.get("raw_locator") or row["docx_paragraph_path"] or oid,
                "state": "NEEDS_REVIEW" if partial else "EXTRACTED",
                "reason": "LEGACY_PARENT_NOT_COMPLETE" if partial else None,
                "attempts": (attempt_id,), "output_objects": (oid,)})
    read.check()
    ledger = CoverageLedger(campaign_sha256=campaign_sha256, units=tuple(units), inspected_units=tuple(inspected),
                            candidates=tuple(candidates), attempts=tuple(attempts), detection_assessment="NOT_RUN")
    return read.snapshot, source_hash, ledger


def inventory_snapshot(canon, campaign_sha256: str, source_id: str, *, batch_rows=500,
                       max_units=100_000, max_objects=250_000) -> CoverageLedger:
    """Inventory one source, retaining at most the configured metadata partition.

    No content, raw artifacts or originals are read. Limits fail explicitly; a
    caller must never silently skip the oversized partition and claim coverage.
    """
    return _inventory(canon, campaign_sha256, source_id, batch_rows=batch_rows,
                      max_units=max_units, max_objects=max_objects)[2]


def inventory_source(canon, campaign_sha256: str, source_id: str, *, batch_rows=500,
                     max_units=100_000, max_objects=250_000) -> dict:
    """CLI wire envelope, including immutable snapshot and source identities."""
    snapshot, source_hash, ledger = _inventory(canon, campaign_sha256, source_id, batch_rows=batch_rows,
                                             max_units=max_units, max_objects=max_objects)
    report = ledger.report()
    return {"schema": "vkm-legacy-source-inventory/1", "snapshot_id": snapshot.snapshot_id,
            "manifest_sha256": snapshot.manifest_sha256, "source_id": source_id, "source_sha256": source_hash,
            "ledger": ledger.model_dump(mode="json"), "report": report, "status": report["status"],
            "original_bytes_verified": False, "baseline": "EXISTING_CANONICAL_METADATA_ONLY"}


def inventory_summary(canon) -> dict:
    """Aggregate metadata only; unit/object integrity is checked source by source.

    Skipped/retired registry entries legitimately have no document. Report them
    separately; never subtract row counts (duplicates can hide missing sources).
    """
    read = _SnapshotRead(canon)
    counts = {table.strip('"'): read.count(f"SELECT count(*) AS n FROM {table}")
              for table in ("sources", "documents", "pages", "blocks", '"tables"', "formulas", "figures", "bibliography")}
    counts["expected_document_units"] = read.count("SELECT coalesce(sum(page_count), 0) AS n FROM documents")
    missing = "NOT EXISTS (SELECT 1 FROM documents d WHERE d.source_id=s.source_id)"
    missing_all = read.count(f"SELECT count(DISTINCT source_id) AS n FROM sources s WHERE {missing}")
    missing_active = read.count(f"SELECT count(DISTINCT source_id) AS n FROM sources s "
                               f"WHERE {missing} AND lifecycle_status='ACTIVE'")
    missing_excluded = read.count(f"SELECT count(DISTINCT source_id) AS n FROM sources s "
                                 f"WHERE {missing} AND lifecycle_status IN ('ABSENT_BY_REGISTER','RETIRED')")
    duplicates = {table: read.count(f"SELECT count(*) AS n FROM (SELECT {key} FROM {table} "
                                    f"GROUP BY {key} HAVING count(*)>1) duplicates")
                  for table, key in (("sources", "source_id"), ("documents", "source_id"))}
    orphan_docs = read.count("SELECT count(*) AS n FROM documents d WHERE NOT EXISTS "
                             "(SELECT 1 FROM sources s WHERE s.source_id=d.source_id)")
    invalid_counts = read.count("SELECT count(*) AS n FROM documents WHERE page_count IS NULL OR page_count<0")
    mismatches = read.count("SELECT count(*) AS n FROM documents d JOIN sources s ON s.source_id=d.source_id "
                            "WHERE d.source_sha256 IS NULL OR s.source_sha256 IS NULL "
                            "OR d.source_sha256<>s.source_sha256")
    read.check()
    integrity_failure = any(duplicates.values()) or orphan_docs or invalid_counts or mismatches
    status = "INTEGRITY_FAILURE" if integrity_failure else ("INCOMPLETE" if missing_all > missing_excluded
                                                          else "METADATA_INVENTORIED")
    return {"schema": "vkm-legacy-inventory/1", "snapshot_id": read.snapshot.snapshot_id,
            "manifest_sha256": read.snapshot.manifest_sha256, "counts": counts,
            "detector_recall": "NOT_ESTABLISHED", "scientific_admission": "NOT_ESTABLISHED",
            "missing_document_denominators": missing_all, "missing_active_document_denominators": missing_active,
            "excluded_without_document": missing_excluded, "duplicate_identities": duplicates,
            "orphan_documents": orphan_docs, "invalid_document_unit_counts": invalid_counts,
            "document_source_hash_mismatches": mismatches, "source_partition_validation": "NOT_RUN",
            "document_denominator_valid": not bool(integrity_failure),
            "original_bytes_verified": False, "status": status}
