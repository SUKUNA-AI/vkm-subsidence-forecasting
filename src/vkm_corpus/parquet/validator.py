"""Canon validator (design D §11 + review H): a snapshot candidate is checked before ``CURRENT`` may move.

Groups: A files, B keys/IDs, C references, D provenance, E science, F completeness, G hygiene, T §50 trace.
Each check yields ``{check_id, group, status (PASS/FAIL/WARN/SKIP), blocking, violations, examples}``; the report is
PASS when no blocking check fails. Cross-table checks run in an in-memory DuckDB built from the manifest with the same
SQL files as the query layer; ID recomputation, page text and hashes run in Python.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from vkm_corpus import ids
from vkm_corpus.contracts import arrow as ca
from vkm_corpus.contracts.datasets import (
    DATASETS,
    DOCUMENT_DATASETS,
    PAGE_OBJECT_DATASETS,
    STORED_DATASETS,
)
from vkm_corpus.contracts.site_scope import SITE_SCOPE_TABLE
from vkm_corpus.contracts.text_rules import page_text_v1
from vkm_corpus.contracts.vocab import (
    CRS_REQUIRED_SPACES,
    FORBIDDEN_STATUS_VALUES,
    QUALITY_FLAG_SCOPE,
    CheckStatus,
)
from vkm_corpus.ids import grammar
from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.parquet.layout import CanonLayout

MAX_EXAMPLES = 20
RESERVED_L2_COLUMNS = frozenset({"epistemic_status", "evidence_type", "scope", "scale", "reviewer", "reviewed_at",
                                 "event_time", "measurement_time", "world_id", "representation_id", "solver_run_id",
                                 "seed"})
FORBIDDEN_COLUMN_NAMES = frozenset({"quote", "verbatim_quote", "ocr_text", "page_text", "full_text"})
ABS_PATH = re.compile(r"(?:^|[\s'\"(=])(?:[A-Za-z]:[\\/]|/home/|/mnt/|/Users/|/root/|/srv/|/tmp/)")


@dataclass
class Check:
    check_id: str
    group: str
    description: str
    blocking: bool = True
    status: str = CheckStatus.PASS
    violations: int = 0
    examples: list[Any] = field(default_factory=list)
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        out = {"check_id": self.check_id, "group": self.group, "description": self.description,
               "blocking": self.blocking, "status": str(self.status), "violations": self.violations,
               "examples": self.examples[:MAX_EXAMPLES]}
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class ValidationOptions:
    deep: bool = False                    # F10: hash every stored blob
    acceptance: bool = False              # F02 blocking; F01 compares with expected_sources
    expected_sources: int | None = None   # e.g. 251 for the real register
    parent_manifest: dict[str, Any] | None = None   # for B07 (same object_id ⇒ same raw_content_sha256)
    trace_sample: int = 20


class Validator:
    def __init__(self, layout: CanonLayout, manifest: dict[str, Any], options: ValidationOptions | None = None):
        self.layout = layout
        self.manifest = manifest
        self.opt = options or ValidationOptions()
        self.checks: list[Check] = []
        self.con = None

    # ------------------------------------------------------------ helpers
    def sql(self, query: str, params: list | None = None) -> list[tuple]:
        return self.con.execute(query, params or []).fetchall()

    def add(self, check_id: str, group: str, description: str, violations: list[Any] | int, *,
            blocking: bool = True, note: str | None = None, skipped: bool = False) -> Check:
        if isinstance(violations, int):
            n, examples = violations, []
        else:
            n, examples = len(violations), [list(v) if isinstance(v, tuple) else v for v in violations]
        status = CheckStatus.SKIP if skipped else (CheckStatus.PASS if n == 0 else
                                                     (CheckStatus.FAIL if blocking else CheckStatus.WARN))
        c = Check(check_id, group, description, blocking, status, n, examples[:MAX_EXAMPLES], note)
        self.checks.append(c)
        return c

    def count_sql(self, check_id: str, group: str, description: str, query: str, *, blocking: bool = True) -> Check:
        rows = self.sql(query)
        return self.add(check_id, group, description, rows, blocking=blocking)

    # ------------------------------------------------------------ run
    def run(self) -> dict[str, Any]:
        from vkm_corpus.duckdb.build import apply_sql, attach_manifest, connect

        self.check_files()
        counts: dict[str, Any] = {}
        if any(c.status == CheckStatus.FAIL for c in self.checks if c.check_id in ("A01", "A02")):
            self.add("Z00", "files", "content checks skipped: files of the manifest are missing, altered or unknown",
                     1)
        else:
            self.con = connect()
            try:
                attach_manifest(self.con, self.layout, self.manifest)
                apply_sql(self.con)
                for fn in (self.check_keys, self.check_references, self.check_provenance, self.check_science,
                           self.check_completeness, self.check_hygiene, self.check_trace):
                    fn()
                cur = self.con.execute("SELECT * FROM corpus_counts")
                counts = dict(zip([d[0] for d in cur.description], cur.fetchone()))
            finally:
                self.con.close()
                self.con = None
        blocking = [c for c in self.checks if c.status == CheckStatus.FAIL]
        warnings = [c for c in self.checks if c.status == CheckStatus.WARN]
        return {"validator_version": "1", "status": "FAIL" if blocking else "PASS",
                "blocking_failures": len(blocking), "warnings": len(warnings),
                "options": {"deep": self.opt.deep, "acceptance": self.opt.acceptance,
                            "expected_sources": self.opt.expected_sources},
                "counts": {k: (int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                           for k, v in counts.items()},
                "checks": [c.to_json() for c in self.checks]}

    # ------------------------------------------------------------ A: files
    def check_files(self) -> None:
        from vkm_corpus.parquet.writer import read_kv

        bad_files, bad_kv, bad_version, forbidden_cols, unknown_cols = [], [], [], [], []
        for name, d in sorted(self.manifest.get("datasets", {}).items()):
            spec = DATASETS.get(name)
            if spec is None or not spec.stored:
                bad_version.append([name, "unknown dataset"])
                continue
            fp = ca.schema_fingerprint(name)
            for f in d.get("files", []):
                p = self.layout.path(f["path"])
                if not p.is_file():
                    bad_files.append([f["path"], "missing"])
                    continue
                if p.stat().st_size != f["bytes"] or sha256_of(p) != f["sha256"]:
                    bad_files.append([f["path"], "sha256/size mismatch"])
                    continue
                try:
                    kv = read_kv(p)
                except Exception as exc:  # noqa: BLE001 - an unreadable footer is a finding, not a crash
                    bad_files.append([f["path"], f"unreadable: {type(exc).__name__}"])
                    continue
                if kv.get("vkm.dataset") != name:
                    bad_kv.append([f["path"], kv.get("vkm.dataset")])
                if kv.get("vkm.schema_version") != spec.version or kv.get("vkm.schema_fingerprint") != fp:
                    bad_version.append([f["path"], kv.get("vkm.schema_version")])
                import pyarrow.parquet as pq

                names = set(pq.read_schema(p).names)
                if names & FORBIDDEN_COLUMN_NAMES:
                    forbidden_cols.append([f["path"], sorted(names & FORBIDDEN_COLUMN_NAMES)])
                if names - set(spec.fields):
                    unknown_cols.append([f["path"], sorted(names - set(spec.fields))])
        self.add("A01", "files", "every manifest file exists with its sha256 and size", bad_files)
        self.add("A02", "files", "Parquet key-value metadata: dataset, schema version and fingerprint known to "
                 "this code", bad_kv + bad_version + unknown_cols)
        self.add("G01", "hygiene", "no forbidden column names (quote, ocr_text, page_text, ...) in any file",
                 forbidden_cols)
        # A03: head commits list every document dataset, exactly one file per (dataset, committed source)
        heads = self.manifest.get("head_commits", {})
        problems = []
        for key, marker in sorted(heads.items()):
            if key == "REGISTRY":
                continue
            missing = [n for n in DOCUMENT_DATASETS if n not in marker.get("datasets", {})]
            if missing:
                problems.append([key, "missing datasets", missing])
        per_source: dict[tuple[str, str], int] = {}
        for name in DOCUMENT_DATASETS:
            for f in self.manifest.get("datasets", {}).get(name, {}).get("files", []):
                m = re.search(r"source_id=(VKM-SRC-[0-9]{3})/", f["path"])
                if m:
                    per_source[(name, m.group(1))] = per_source.get((name, m.group(1)), 0) + 1
        problems += [[n, s, c] for (n, s), c in per_source.items() if c != 1]
        self.add("A03", "files", "one head file per (document dataset, committed source); markers list all datasets",
                 problems)
        # fingerprints of the manifest vs the files (combined digests)
        fp_bad = []
        for name, d in self.manifest.get("datasets", {}).items():
            acc = ca.EMPTY_DIGEST
            for f in d.get("files", []):
                acc = acc + ca.RowDigest.parse(f["rows"], f["digest"])
            if d.get("table_fingerprint") and ca.fingerprint_of(name, acc) != d["table_fingerprint"]:
                fp_bad.append(name)
        self.add("A05", "files", "dataset fingerprints equal the combination of file digests", fp_bad)

    # ------------------------------------------------------------ B: keys
    def check_keys(self) -> None:
        dup = []
        for name in STORED_DATASETS:
            if name == "artifacts":
                continue
            pk = ", ".join(DATASETS[name].primary_key)
            rows = self.sql(f'SELECT {pk}, count(*) FROM canonical."{name}" GROUP BY ALL HAVING count(*) > 1 LIMIT 20')
            dup += [[name, *r] for r in rows]
        self.add("B01", "keys", "primary keys are unique in every dataset", dup)
        self.count_sql("B01a", "keys", "one description per artifact id (sha, size, kind, media type)", """
            SELECT artifact_id FROM canonical.artifacts GROUP BY artifact_id
            HAVING count(DISTINCT coalesce(size_bytes, -1)) > 1 OR count(DISTINCT media_type) > 1 LIMIT 20""")
        patterns = {"sources": [("source_id", "source"), ("object_id", "source")],
                    "works": [("work_id", "work"), ("anchor_source_id", "source")],
                    "authors": [("author_id", "author")], "venues": [("venue_id", "venue")],
                    "documents": [("object_id", "document"), ("source_id", "source")],
                    "pages": [("page_id", "page"), ("object_id", "page")],
                    "source_work_links": [("object_id", "link")], "work_relations": [("object_id", "link")],
                    "source_relations": [("object_id", "link")], "work_authors": [("object_id", "link")],
                    "processing_runs": [("processing_run_id", "run")],
                    "processing_steps": [("step_id", "step"), ("processing_run_id", "run")],
                    "errors": [("error_id", "error"), ("processing_run_id", "run")],
                    "artifacts": [("artifact_id", "artifact"), ("created_by_run_id", "run")]}
        for name in PAGE_OBJECT_DATASETS:
            patterns[name] = [("object_id", "object"), ("source_id", "source")]
        bad = []
        for name, cols in patterns.items():
            for col, kind in cols:
                pat = grammar.PATTERNS[kind].replace("'", "''")
                rows = self.sql(f"SELECT {col} FROM canonical.\"{name}\" WHERE {col} IS NOT NULL AND NOT "
                                f"regexp_full_match({col}, '{pat}') LIMIT 5")
                bad += [[name, col, r[0]] for r in rows]
        self.add("B02", "keys", "ID columns match the grammar", bad)
        self.count_sql("B03", "keys", "page ids agree with page_kind/page_index; object ids start with their page id "
                       "(DOCX document-scoped objects excepted)", """
            SELECT page_id FROM canonical.pages
             WHERE object_id <> page_id OR substr(page_id, 13, 1) <> CASE page_kind WHEN 'DOCX_RENDERED_PAGE' THEN 'r'
                   WHEN 'EPUB_SPINE_ITEM' THEN 's' ELSE 'p' END
                OR TRY_CAST(substr(page_id, 14) AS INTEGER) <> page_index
            UNION ALL
            SELECT object_id FROM all_objects WHERE object_kind NOT IN ('PAGE', 'DOCUMENT')
               AND NOT (starts_with(object_id, page_id || ':')
                        OR (region_origin = 'DOCX_ELEMENT' AND starts_with(object_id, source_id || ':doc:')))
            LIMIT 20""")
        self.check_object_ids()
        self.check_stable_raw_content()

    def check_object_ids(self) -> None:
        """B04: recompute object ids from (scope, kind, origin, region_origin, anchor, producer_key)."""
        bad, skipped = [], 0
        for name in PAGE_OBJECT_DATASETS:
            cols = ["object_id", "object_kind", "source_id", "page_id", "origin", "region_origin", "bbox_space",
                    "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1", "docx_paragraph_path", "extractor_id",
                    "extraction_generation", "raw_config_hash", "models", "quality_flags"]
            extra = ", reading_order, text" if name == "blocks" else ", NULL AS reading_order, NULL AS text"
            for r in self.con.execute(f'SELECT {", ".join(cols)}{extra} FROM canonical."{name}"').fetchall():
                row = dict(zip(cols + ["reading_order", "text"], r))
                if row["region_origin"] == "DOCX_ELEMENT":
                    scope, anchor = ids.document_id(row["source_id"]), ids.xml_anchor(row["docx_paragraph_path"])
                elif row["bbox_space"] == "PAGE_PT_TL":
                    scope = row["page_id"]
                    anchor = ids.bbox_anchor(row["bbox_x0"], row["bbox_y0"], row["bbox_x1"], row["bbox_y1"])
                elif row["reading_order"] is not None:
                    scope, anchor = row["page_id"], ids.ordinal_anchor(row["reading_order"], row["text"] or "")
                else:
                    skipped += 1
                    continue
                pk = ids.producer_key(row["extractor_id"], row["extraction_generation"], row["raw_config_hash"],
                                      row["models"] or [])
                ok = False
                dups = 8 if "DUPLICATE_DETECTION_DISAMBIGUATED" in (row["quality_flags"] or []) else 0
                for dup in range(0, dups + 1):
                    if ids.object_id(scope, row["object_kind"], row["origin"], row["region_origin"], anchor, pk,
                                     dup) == row["object_id"]:
                        ok = True
                        break
                if not ok:
                    bad.append([name, row["object_id"]])
        self.add("B04", "keys", "object ids recompute from region anchor and producer key (H-14)", bad,
                 note=f"{skipped} objects without a recomputable anchor" if skipped else None)

    def check_stable_raw_content(self) -> None:
        """B07: an object id keeps its raw_content_sha256 across snapshots (else bump extraction_generation)."""
        parent = self.opt.parent_manifest
        if not parent:
            self.add("B07", "keys", "same object_id ⇒ same raw_content_sha256 as in the parent snapshot", 0,
                     skipped=True, note="no parent snapshot")
            return
        from vkm_corpus.duckdb.build import _lit

        bad = []
        for name in PAGE_OBJECT_DATASETS:
            files = parent.get("datasets", {}).get(name, {}).get("files", [])
            if not files:
                continue
            paths = ", ".join(_lit(self.layout.path(f["path"])) for f in files)
            rows = self.sql(f"""
                SELECT n.object_id FROM canonical."{name}" n
                JOIN read_parquet([{paths}], union_by_name = true) o USING (object_id)
                WHERE n.raw_content_sha256 IS DISTINCT FROM o.raw_content_sha256 LIMIT 20""")
            bad += [[name, r[0]] for r in rows]
        self.add("B07", "keys", "same object_id ⇒ same raw_content_sha256 as in the parent snapshot", bad)

    # ------------------------------------------------------------ C: references
    def check_references(self) -> None:
        self.count_sql("C01", "references", "pages and documents reference registered sources", """
            SELECT source_id FROM canonical.pages WHERE source_id NOT IN (SELECT source_id FROM canonical.sources)
            UNION ALL
            SELECT source_id FROM canonical.documents WHERE source_id NOT IN (SELECT source_id FROM canonical.sources)
            LIMIT 20""")
        self.count_sql("C02", "references", "every page object lies on an existing page of its own source", """
            SELECT o.object_id FROM all_objects o LEFT JOIN canonical.pages p ON p.page_id = o.page_id
            WHERE o.object_kind NOT IN ('PAGE', 'DOCUMENT')
              AND ((o.page_id IS NOT NULL AND (p.page_id IS NULL OR p.source_id <> o.source_id))
                   OR (o.page_id IS NULL AND o.region_origin IS DISTINCT FROM 'DOCX_ELEMENT'))
            LIMIT 20""")
        self.count_sql("C03", "references", "caption blocks, continuations and list blocks exist in the same source", """
            WITH ob AS (SELECT object_id, source_id FROM all_objects)
            SELECT f.object_id FROM canonical.figures f LEFT JOIN canonical.blocks b ON b.object_id = f.caption_block_id
             WHERE f.caption_block_id IS NOT NULL AND (b.object_id IS NULL OR b.source_id <> f.source_id)
            UNION ALL
            SELECT t.object_id FROM canonical."tables" t LEFT JOIN canonical.blocks b ON b.object_id = t.caption_block_id
             WHERE t.caption_block_id IS NOT NULL AND (b.object_id IS NULL OR b.source_id <> t.source_id)
            UNION ALL
            SELECT t.object_id FROM canonical."tables" t LEFT JOIN canonical."tables" n
                ON n.object_id = t.continues_object_id
             WHERE t.continues_object_id IS NOT NULL AND (n.object_id IS NULL OR n.source_id <> t.source_id)
            UNION ALL
            SELECT e.object_id FROM canonical.bibliography_entries e LEFT JOIN canonical.pages p
                ON p.page_id = e.continues_on_page_id
             WHERE e.continues_on_page_id IS NOT NULL AND (p.page_id IS NULL OR p.source_id <> e.source_id)
            UNION ALL
            SELECT e.object_id FROM (SELECT object_id, source_id, unnest(list_block_ids) AS bid
                                     FROM canonical.bibliography_entries) e
             LEFT JOIN canonical.blocks b ON b.object_id = e.bid
             WHERE b.object_id IS NULL OR b.source_id <> e.source_id
            LIMIT 20""")
        self.count_sql("C04", "references", "every referenced artifact id is in the artifact index", """
            WITH refs AS (
              SELECT object_id, raw_artifact_id AS aid FROM all_objects
              UNION ALL SELECT object_id, unnest(raw_artifacts).artifact_id FROM all_objects
              UNION ALL SELECT object_id, native_raw_artifact_id FROM canonical.pages
              UNION ALL SELECT object_id, layout_raw_artifact_id FROM canonical.pages
              UNION ALL SELECT object_id, ocr_raw_artifact_id FROM canonical.pages
              UNION ALL SELECT object_id, render_artifact_id FROM canonical.pages
              UNION ALL SELECT object_id, preview_artifact_id FROM canonical.pages
              UNION ALL SELECT object_id, pagination_artifact_id FROM canonical.documents
              UNION ALL SELECT object_id, image_artifact_id FROM canonical.figures
              UNION ALL SELECT object_id, embedded_image_artifact_id FROM canonical.figures
              UNION ALL SELECT object_id, unnest(vector_artifacts).artifact_id FROM canonical.figures
              UNION ALL SELECT object_id, image_artifact_id FROM canonical."tables"
              UNION ALL SELECT object_id, image_artifact_id FROM canonical.formulas
              UNION ALL SELECT processing_run_id, config_artifact_id FROM canonical.processing_runs
              UNION ALL SELECT processing_run_id, log_artifact_id FROM canonical.processing_runs)
            SELECT refs.object_id, refs.aid FROM refs
            WHERE refs.aid IS NOT NULL AND refs.aid NOT IN (SELECT artifact_id FROM canonical.artifacts) LIMIT 20""")
        self.count_sql("C05", "references", "every row's processing_run_id has a START record", """
            WITH r AS (SELECT DISTINCT processing_run_id FROM canonical.processing_runs WHERE record_phase = 'START'),
            used AS (
              SELECT processing_run_id FROM all_objects
              UNION SELECT processing_run_id FROM canonical.sources UNION SELECT processing_run_id FROM canonical.works
              UNION SELECT processing_run_id FROM canonical.processing_steps
              UNION SELECT processing_run_id FROM canonical.errors
              UNION SELECT created_by_run_id FROM canonical.artifacts)
            SELECT processing_run_id FROM used WHERE processing_run_id NOT IN (SELECT processing_run_id FROM r)
            LIMIT 20""")
        self.count_sql("C05b", "references", "runs whose rows were committed have an END record (else: crash after "
                       "commit)", """
            SELECT processing_run_id FROM processing_runs WHERE NOT has_end
               AND processing_run_id IN (SELECT processing_run_id FROM all_objects) LIMIT 20""", blocking=False)
        self.count_sql("C06", "references", "foreign keys of links, relations, authors, venues and works", """
            SELECT object_id FROM canonical.source_work_links
             WHERE source_id NOT IN (SELECT source_id FROM canonical.sources)
                OR (work_id IS NOT NULL AND work_id NOT IN (SELECT work_id FROM canonical.works))
            UNION ALL SELECT object_id FROM canonical.work_relations
             WHERE from_work_id NOT IN (SELECT work_id FROM canonical.works)
                OR to_work_id NOT IN (SELECT work_id FROM canonical.works)
            UNION ALL SELECT object_id FROM canonical.source_relations
             WHERE from_source_id NOT IN (SELECT source_id FROM canonical.sources)
                OR to_source_id NOT IN (SELECT source_id FROM canonical.sources)
            UNION ALL SELECT object_id FROM canonical.work_authors
             WHERE work_id NOT IN (SELECT work_id FROM canonical.works)
                OR author_id NOT IN (SELECT author_id FROM canonical.authors)
            UNION ALL SELECT work_id FROM canonical.works
             WHERE (venue_id IS NOT NULL AND venue_id NOT IN (SELECT venue_id FROM canonical.venues))
                OR anchor_source_id NOT IN (SELECT source_id FROM canonical.sources)
                OR (merged_into_work_id IS NOT NULL AND merged_into_work_id NOT IN (SELECT work_id FROM canonical.works))
            LIMIT 20""")
        self.count_sql("C07", "references", "MERGED_INTO chains end in an ACTIVE work without cycles", """
            WITH RECURSIVE chain(start_id, work_id, status, merged_into_work_id, depth) AS (
              SELECT work_id, work_id, status, merged_into_work_id, 0 FROM canonical.works
               WHERE status = 'MERGED_INTO'
              UNION ALL
              SELECT c.start_id, w.work_id, w.status, w.merged_into_work_id, c.depth + 1
              FROM chain c JOIN canonical.works w ON w.work_id = c.merged_into_work_id
              WHERE c.status = 'MERGED_INTO' AND c.depth < 16)
            SELECT start_id FROM chain GROUP BY start_id HAVING NOT bool_or(status = 'ACTIVE') LIMIT 20""")
        self.count_sql("C08", "references", "steps and errors name registered sources", """
            SELECT step_id FROM canonical.processing_steps
             WHERE source_id IS NOT NULL AND source_id NOT IN (SELECT source_id FROM canonical.sources)
            UNION ALL SELECT error_id FROM canonical.errors
             WHERE source_id IS NOT NULL AND source_id NOT IN (SELECT source_id FROM canonical.sources)
            LIMIT 20""")
        self.count_sql("C08b", "references", "page ids of steps and errors exist in the canon", """
            SELECT step_id FROM canonical.processing_steps
             WHERE page_id IS NOT NULL AND page_id NOT IN (SELECT page_id FROM canonical.pages)
            UNION ALL SELECT error_id FROM canonical.errors
             WHERE page_id IS NOT NULL AND page_id NOT IN (SELECT page_id FROM canonical.pages)
            LIMIT 20""", blocking=False)

    # ------------------------------------------------------------ D: provenance
    def check_provenance(self) -> None:
        nulls = []
        for name in STORED_DATASETS:
            for f in ca.field_specs(DATASETS[name].model):
                if not f.nullable:
                    n = self.sql(f'SELECT count(*) FROM canonical."{name}" WHERE "{f.name}" IS NULL')[0][0]
                    if n:
                        nulls.append([name, f.name, n])
        self.add("D01", "provenance", "required fields are never NULL (also for files of older schema versions)",
                 nulls)
        self.count_sql("D02", "provenance", "OCR content names its model; region LAYOUT_MODEL names a LAYOUT model", """
            SELECT object_id FROM all_objects
             WHERE (origin = 'OCR' AND (model_id IS NULL OR model_revision IS NULL
                    OR NOT list_contains(list_transform(models, x -> x.role), 'RECOGNITION')))
                OR (region_origin = 'LAYOUT_MODEL' AND NOT list_contains(list_transform(models, x -> x.role), 'LAYOUT'))
            LIMIT 20""")
        self.count_sql("D02b", "provenance", "models of OCR rows are declared by their processing run", """
            SELECT DISTINCT o.processing_run_id FROM all_objects o JOIN processing_runs r USING (processing_run_id)
             WHERE o.origin = 'OCR' AND NOT list_contains(list_transform(r.models, x -> x.model_revision),
                                                          o.model_revision) LIMIT 20""", blocking=False)
        self.count_sql("D03", "provenance", "native, embedded-OCR and OCR content has a raw artifact", """
            SELECT object_id FROM all_objects
             WHERE object_kind NOT IN ('DOCUMENT', 'PAGE') AND origin IN ('NATIVE', 'EMBEDDED_OCR', 'OCR')
               AND raw_artifact_id IS NULL
            UNION ALL SELECT page_id FROM canonical.pages
             WHERE primary_text_origin IS NOT NULL AND raw_artifact_id IS NULL
            LIMIT 20""")
        self.count_sql("D04", "provenance", "source_sha256 of every row equals the register", """
            SELECT o.object_id FROM all_objects o JOIN canonical.sources s USING (source_id)
             WHERE o.source_sha256 <> s.source_sha256 LIMIT 20""")
        heads = self.manifest.get("head_commits", {})
        reg = {r[0]: r[1] for r in self.sql("SELECT source_id, source_sha256 FROM canonical.sources")}
        bad_markers = [[k, m.get("source_sha256")] for k, m in heads.items()
                       if k != "REGISTRY" and reg.get(k) != m.get("source_sha256")]
        self.add("D04b", "provenance", "commit markers carry the register sha256 of their source", bad_markers)
        self.add("D05", "provenance", "extraction_signature recomputed from step components", 0, skipped=True,
                 note="needs stage inputs; checked by the pipeline at commit time")
        self.count_sql("D06", "provenance", "created_at lies within the window of its run", """
            SELECT o.object_id FROM all_objects o JOIN processing_runs r USING (processing_run_id)
             WHERE o.created_at < r.started_at - INTERVAL 1 MINUTE
                OR (r.finished_at IS NOT NULL AND o.created_at > r.finished_at + INTERVAL 1 MINUTE) LIMIT 20""",
                       blocking=False)
        self.count_sql("D08", "provenance", "source_site_scope* of objects equal the scope of their source (H-18)", """
            SELECT o.object_id FROM all_objects o JOIN canonical.sources s USING (source_id)
             WHERE o.source_site_scope <> s.site_scope OR o.source_site_scope_raw <> s.site_scope_raw
                OR o.source_site_scope_mapping <> s.site_scope_mapping LIMIT 20""")

    # ------------------------------------------------------------ E: science
    def check_science(self) -> None:
        forb = ", ".join(f"'{v}'" for v in sorted(FORBIDDEN_STATUS_VALUES))
        bad = []
        for name in STORED_DATASETS:
            for f in ca.field_specs(DATASETS[name].model):
                if str(f.type) == "string" and ("status" in f.name or f.name.endswith("_basis")):
                    rows = self.sql(f'SELECT "{f.name}" FROM canonical."{name}" WHERE "{f.name}" IN ({forb}) LIMIT 3')
                    bad += [[name, f.name, r[0]] for r in rows]
        self.add("E01", "science", "no FACT / REVIEWED_MEASUREMENT / ACCEPTED_* / epistemic status in any status "
                 "column", bad)
        self.count_sql("E02", "science", "automatic objects are AUTO_EXTRACTED_UNREVIEWED; registry entities and "
                       "curated links NOT_APPLICABLE", """
            SELECT object_id FROM all_objects WHERE review_status <> 'AUTO_EXTRACTED_UNREVIEWED'
            UNION ALL SELECT object_id FROM canonical.works WHERE review_status <> 'NOT_APPLICABLE'
            UNION ALL SELECT object_id FROM canonical.authors WHERE review_status <> 'NOT_APPLICABLE'
            UNION ALL SELECT object_id FROM canonical.venues WHERE review_status <> 'NOT_APPLICABLE'
            UNION ALL SELECT object_id FROM canonical.source_work_links WHERE review_status <> 'NOT_APPLICABLE'
            UNION ALL SELECT object_id FROM canonical.work_relations WHERE review_status <> 'NOT_APPLICABLE'
            UNION ALL SELECT object_id FROM canonical.source_relations WHERE review_status <> 'NOT_APPLICABLE'
            UNION ALL SELECT object_id FROM canonical.work_authors WHERE review_status <> 'NOT_APPLICABLE'
            LIMIT 20""")
        self.count_sql("E03", "science", "review status of sources follows CP-06 (with basis)", """
            SELECT source_id, review_status, review_status_basis FROM (
              SELECT *, CAST(substr(source_id, 9) AS INTEGER) AS n FROM canonical.sources) s
            WHERE NOT CASE
              WHEN lifecycle_status <> 'ACTIVE' THEN review_status = 'NOT_APPLICABLE'
                   AND review_status_basis = 'LIFECYCLE'
              WHEN n <= 41 THEN review_status IN ('FULLY_REVIEWED', 'RELEVANT_SECTIONS_REVIEWED')
                   AND review_status_basis = 'PHASE1_COVERAGE_MASTER'
              WHEN n <= 195 THEN review_status = 'UNSEEN' AND review_status_basis = 'DEFAULT_UNSEEN'
              ELSE review_status = 'QUICK_LOOK_ONLY' AND review_status_basis = 'INTAKE_QUICK_LOOK_MARKER' END
            LIMIT 20""")
        self.count_sql("E04", "science", "a figure type needs a method and confidence ≥ threshold (else "
                       "UNKNOWN_FIGURE_TYPE)", """
            SELECT object_id FROM canonical.figures WHERE detected_figure_type <> 'UNKNOWN_FIGURE_TYPE'
               AND (figure_type_method = 'NONE' OR figure_type_confidence IS NULL OR figure_type_threshold IS NULL
                    OR figure_type_confidence < figure_type_threshold) LIMIT 20""")
        crs = ", ".join(f"'{s}'" for s in sorted(CRS_REQUIRED_SPACES))
        self.count_sql("E05", "science", "crs_status only (and always) for GEO/DRAWING_UNITS; objects in PAGE_PT_TL "
                       "or NONE; no invented EPSG", f"""
            SELECT artifact_id FROM canonical.artifacts
             WHERE (crs_status IS NOT NULL) <> (coalesce(coordinate_space, '') IN ({crs}))
            UNION ALL SELECT object_id FROM all_objects WHERE bbox_space NOT IN ('PAGE_PT_TL', 'NONE')
            LIMIT 20""")
        values = ", ".join(f"('{flag}', '{ds}')" for flag, dss in sorted(QUALITY_FLAG_SCOPE.items())
                           for ds in sorted(dss))
        bad = []
        for name in STORED_DATASETS:
            if "quality_flags" not in DATASETS[name].fields:
                continue
            rows = self.sql(f"""
                WITH scope(flag, ds) AS (VALUES {values})
                SELECT object_id, f FROM (SELECT object_id, unnest(quality_flags) AS f FROM canonical."{name}") q
                WHERE NOT EXISTS (SELECT 1 FROM scope WHERE scope.flag = q.f AND scope.ds = '{name}') LIMIT 5""")
            bad += [[name, *r] for r in rows]
        self.add("E06", "science", "quality flags are from the dictionary and applicable to the dataset", bad)
        bad_cols = [[n, sorted(set(DATASETS[n].fields) & RESERVED_L2_COLUMNS)] for n in STORED_DATASETS
                    if set(DATASETS[n].fields) & RESERVED_L2_COLUMNS]
        self.add("E07", "science", "no reserved L2 columns (epistemic_status, event_time, ...) in L1 schemas",
                 bad_cols)
        self.count_sql("E08", "science", "a work with several instance sources is grouped only by CURATED links "
                       "(CP-09)", """
            SELECT work_id FROM work_sources GROUP BY work_id
            HAVING count(DISTINCT source_id) > 1 AND count(*) FILTER (WHERE curation_status <> 'CURATED') > 0
            LIMIT 20""")
        self.count_sql("E09", "science", "entries on pages with foreign content never cite on behalf of the host; "
                       "CITES only from exact-id matches", """
            SELECT b.object_id FROM bibliography b JOIN foreign_content_pages f ON f.page_id = b.page_id
             WHERE b.citing_work_id IS NOT NULL
            UNION ALL SELECT entry_id FROM bibliography_links
             WHERE match_status NOT IN ('CANDIDATE', 'AUTO_EXACT_ID_MATCH', 'REJECTED')
            LIMIT 20""")
        bad = []
        for sid, raw, scope, mapping in self.sql(
                "SELECT source_id, site_scope_raw, site_scope, site_scope_mapping FROM canonical.sources"):
            m = SITE_SCOPE_TABLE.get(raw)
            if m is None or list(m.scopes) != list(scope) or m.mapping != mapping:
                bad.append([sid, raw])
        self.add("E10", "science", "source scope follows the versioned table (all raw values mapped; AMBIGUOUS ⇒ [])",
                 bad)
        self.count_sql("E11", "science", "no NATIVE text on raster scans: embedded OCR is EMBEDDED_OCR (H-02)", """
            SELECT page_id FROM canonical.pages WHERE page_class = 'RASTER_SCAN' AND primary_text_origin = 'NATIVE'
            UNION ALL SELECT b.object_id FROM canonical.blocks b JOIN canonical.pages p ON p.page_id = b.page_id
             WHERE p.page_class = 'RASTER_SCAN' AND b.origin = 'NATIVE'
            UNION ALL SELECT b.object_id FROM canonical.blocks b JOIN canonical.pages p ON p.page_id = b.page_id
             WHERE p.page_kind = 'DJVU_PAGE' AND b.origin = 'NATIVE'
            LIMIT 20""")
        self.count_sql("E13", "science", "a page with text carries the origin of its primary text layer; OCR pages "
                       "name the recognition model (DATA_CONTRACTS §4)", """
            SELECT page_id FROM canonical.pages
             WHERE primary_text_origin IN ('OCR', 'EMBEDDED_OCR') AND origin <> primary_text_origin
            UNION ALL SELECT page_id FROM canonical.pages
             WHERE primary_text_origin = 'OCR' AND (model_id IS NULL OR model_revision IS NULL)
            LIMIT 20""", blocking=False)   # v0: reported (WARN); becomes blocking once the corpus is re-assembled
        self.check_page_text()

    def check_page_text(self) -> None:
        """E12 (H-04): page text = page_text_v1(primary blocks); text_sha256 = sha256(normalized_text)."""
        blocks: dict[str, list[dict]] = {}
        for oid, pid, bt, ro, prim, nt in self.sql(
                "SELECT object_id, page_id, block_type, reading_order, is_primary_layer, normalized_text "
                "FROM canonical.blocks WHERE page_id IS NOT NULL"):
            blocks.setdefault(pid, []).append({"object_id": oid, "block_type": bt, "reading_order": ro,
                                               "is_primary_layer": prim, "normalized_text": nt})
        bad = []
        for pid, text, sha, rule in self.sql(
                "SELECT page_id, normalized_text, text_sha256, text_rule FROM canonical.pages"):
            if rule != "page_text_v1":
                continue
            if pid not in blocks and text is None:
                continue
            pt = page_text_v1(blocks.get(pid, []))
            if pt.normalized_text != text or pt.text_sha256 != sha:
                bad.append([pid])
        self.add("E12", "science", "pages.normalized_text/text_sha256 equal page_text_v1 of the primary blocks (H-04)",
                 bad)

    # ------------------------------------------------------------ F: completeness
    def check_completeness(self) -> None:
        n_sources = self.sql("SELECT count(*) FROM canonical.sources")[0][0]
        expected = self.opt.expected_sources
        viol = [] if expected is None or n_sources == expected else [[n_sources, expected]]
        if n_sources == 0:
            viol.append(["no sources"])
        self.add("F01", "completeness", "sources = register rows (251 for the real register)", viol)
        heads = self.manifest.get("source_heads", {})
        missing = [r[0] for r in self.sql("SELECT source_id FROM canonical.sources WHERE lifecycle_status = 'ACTIVE' "
                                          "AND file_status = 'PRESENT_VERIFIED' ORDER BY source_id")
                   if r[0] not in heads]
        self.add("F02", "completeness", "every present ACTIVE source has a head commit (else NOT_PROCESSED)",
                 missing, blocking=self.opt.acceptance)
        self.count_sql("F03", "completeness", "documents.page_count = pages rows, indexes contiguous 1..N", """
            SELECT d.source_id, d.page_count, pc.pages_total FROM canonical.documents d
            LEFT JOIN page_coverage pc USING (source_id)
            WHERE coalesce(pc.pages_total, 0) <> d.page_count OR NOT coalesce(pc.contiguous, d.page_count = 0)
            LIMIT 20""")
        self.count_sql("F04", "completeness", "a second page count disagrees without an explaining flag", """
            SELECT source_id FROM canonical.documents WHERE page_count_check IS NOT NULL
               AND page_count_check <> page_count AND NOT list_contains(quality_flags,
                   'PAGECOUNT_DIFFERS_FROM_REGISTER_HINT') LIMIT 20""", blocking=False)
        self.count_sql("F05", "completeness", "FAILED / UNSUPPORTED / PARTIAL pages have at least one error", """
            SELECT p.page_id FROM canonical.pages p
            WHERE p.page_status IN ('FAILED', 'UNSUPPORTED', 'PARTIAL')
              AND NOT EXISTS (SELECT 1 FROM canonical.errors e
                              WHERE e.page_id = p.page_id OR (e.source_id = p.source_id AND e.page_index = p.page_index))
            LIMIT 20""")
        self.count_sql("F06", "completeness", "an ACTIVE source without a verified file has a SOURCE_* error", """
            SELECT s.source_id FROM canonical.sources s
            WHERE s.lifecycle_status = 'ACTIVE' AND s.file_status <> 'PRESENT_VERIFIED'
              AND NOT EXISTS (SELECT 1 FROM canonical.errors e WHERE e.source_id = s.source_id
                              AND starts_with(e.code, 'SOURCE_')) LIMIT 20""")
        self.count_sql("F07", "completeness", "exactly one primary link per source; every ACTIVE work has an instance "
                       "source", """
            SELECT s.source_id FROM canonical.sources s
            LEFT JOIN (SELECT source_id, count(*) AS n FROM canonical.source_work_links
                       WHERE is_primary AND curation_status <> 'REJECTED' GROUP BY source_id) l USING (source_id)
            WHERE coalesce(l.n, 0) <> 1
            UNION ALL SELECT w.work_id FROM canonical.works w
            WHERE w.status = 'ACTIVE' AND w.work_id NOT IN (SELECT work_id FROM work_sources)
            LIMIT 20""")
        rows = self.sql("SELECT sources_total, rollup_closed FROM corpus_counts")
        viol = [] if rows and rows[0][1] and rows[0][0] == n_sources else [list(rows[0]) if rows else ["no counts"]]
        viol += self.sql("""SELECT source_id FROM canonical.sources WHERE (lifecycle_status <> 'ACTIVE')
                            <> (register_skip_status IS NOT NULL AND register_skip_reason IS NOT NULL) LIMIT 20""")
        self.add("F08", "completeness", "roll-up classes sum to the number of sources; skipped by register ⇔ "
                 "lifecycle not ACTIVE with a reason", viol)
        self.count_sql("F09", "completeness", "every FAILED step has an error", """
            SELECT st.step_id FROM canonical.processing_steps st WHERE st.status = 'FAILED'
              AND NOT EXISTS (SELECT 1 FROM canonical.errors e WHERE e.step_id = st.step_id
                              OR (e.processing_run_id = st.processing_run_id AND e.source_id IS NOT DISTINCT FROM
                                  st.source_id AND e.page_id IS NOT DISTINCT FROM st.page_id AND e.stage = st.stage))
            LIMIT 20""")
        missing = []
        from vkm_corpus.parquet.blobs import check_blob

        for aid, rel in self.sql("SELECT DISTINCT artifact_id, storage_relpath FROM canonical.artifacts "
                                 "WHERE materialization = 'STORED'"):
            reason = check_blob(self.layout, rel, aid, deep=self.opt.deep)
            if reason:
                missing.append([aid, reason])
        self.add("F10", "completeness", "stored artifact blobs exist" + (" and hash to their id" if self.opt.deep
                 else ""), missing)

    # ------------------------------------------------------------ G: hygiene
    def check_hygiene(self) -> None:
        self.count_sql("G02", "hygiene", "path columns are relative or logical (no drive letters, absolute paths, ..)", r"""
            SELECT canonical_path FROM canonical.sources
             WHERE regexp_matches(canonical_path, '^(/|[A-Za-z]:)') OR contains(canonical_path, '..')
                OR contains(canonical_path, '\')
            UNION ALL SELECT storage_relpath FROM canonical.artifacts
             WHERE storage_relpath IS NOT NULL AND (regexp_matches(storage_relpath, '^(/|[A-Za-z]:)')
                   OR contains(storage_relpath, '..'))
            UNION ALL SELECT log_ref FROM canonical.processing_steps
             WHERE log_ref IS NOT NULL AND regexp_matches(log_ref, '^(/|[A-Za-z]:)')
            UNION ALL SELECT input_ref FROM canonical.sources WHERE NOT regexp_matches(input_ref, '^[A-Z]+:')
            LIMIT 20""")
        bad = [[eid] for eid, msg in self.sql("SELECT error_id, message FROM canonical.errors") if ABS_PATH.search(msg)]
        self.add("G03", "hygiene", "error messages carry no absolute paths", bad, blocking=False)

    # ------------------------------------------------------------ T: §50
    def check_trace(self) -> None:
        required = ["object_kind", "object_id", "schema_version", "source_id", "origin", "review_status",
                    "processing_run_id", "run_kind", "pipeline_version", "extractor_id", "extractor_version",
                    "config_hash", "content_sha256", "created_at", "source_canonical_path", "register_sha256",
                    "record_role", "snapshot_id", "commit_id", "object_version"]
        bad = []
        kinds = [r[0] for r in self.sql("SELECT DISTINCT object_kind FROM all_objects ORDER BY 1")]
        for kind in kinds:
            sample = [r[0] for r in self.sql("SELECT object_id FROM all_objects WHERE object_kind = ? "
                                             "ORDER BY object_id LIMIT ?", [kind, self.opt.trace_sample])]
            for oid in sample:
                # via Arrow: fetching TIMESTAMPTZ through fetchone() would need pytz
                recs = self.con.execute("SELECT * FROM provenance_trace(?)", [oid]).to_arrow_table().to_pylist()
                if not recs:
                    bad.append([oid, "no trace"])
                    continue
                rec = recs[0]
                need = list(required)
                if kind not in ("DOCUMENT",) and rec.get("region_origin") != "DOCX_ELEMENT":
                    need.append("page_id")
                if rec.get("origin") == "OCR":
                    need += ["model_id", "model_revision"]
                if rec.get("origin") in ("NATIVE", "EMBEDDED_OCR", "OCR") and kind not in ("DOCUMENT", "PAGE"):
                    need += ["raw_artifact_id", "raw_artifact_relpath"]
                missing = [c for c in need if rec.get(c) is None]
                if missing or rec.get("source_sha256_matches_register") is not True:
                    bad.append([oid, missing])
        self.add("T01", "trace", "§50: provenance_trace answers the ten questions without NULL in required fields "
                 f"(sample {self.opt.trace_sample} per object kind)", bad)


def validate(layout: CanonLayout, manifest: dict[str, Any], options: ValidationOptions | None = None) -> dict:
    return Validator(layout, manifest, options).run()


def report_text(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"


CheckFn = Callable[[Validator], None]
