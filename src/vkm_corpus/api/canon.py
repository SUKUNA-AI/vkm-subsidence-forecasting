"""Read-only access to the canonical snapshot through agent D's DuckDB file (task §25; H-07, H-48).

This module is the only place of the API that knows table, view and column names of the canonical layer.

* The file ``$VKM_DATA_ROOT/duckdb/vkm_corpus.duckdb`` of a CANONICAL root is opened ``read_only``; a STAGING root is
  refused (H-07). The connection is reopened when the file is replaced (the builder swaps it atomically).
* ``snapshot()`` names the snapshot the file was built from (``meta.snapshot``); ``status()`` compares it with the
  root's ``CURRENT`` (H-48) — the API never pretends a stale file is the current canon.
* Every read is a parametrised query over D's views and macros; no SQL comes from a caller.
"""
from __future__ import annotations

import base64
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

from vkm_corpus.api.errors import ApiFailure
from vkm_corpus.ids import grammar

# object kind → (view/table, key column)
KIND_TABLE: dict[str, tuple[str, str]] = {
    "SOURCE": ("sources", "source_id"),
    "WORK": ("works_all", "work_id"),
    "DOCUMENT": ("documents", "object_id"),
    "PAGE": ("pages", "page_id"),
    "BLOCK": ("blocks", "object_id"),
    "FIGURE": ("figures", "object_id"),
    "TABLE": ('"tables"', "object_id"),
    "FORMULA": ("formulas", "object_id"),
    "BIBLIOGRAPHY_ENTRY": ("bibliography", "object_id"),
    "AUTHOR": ("authors", "author_id"),
    "VENUE": ("venues", "venue_id"),
    "ARTIFACT": ("artifacts", "artifact_id"),
    "PROCESSING_RUN": ("processing_runs", "processing_run_id"),
}
OBJECT_KIND_BY_CODE = {"b": "BLOCK", "f": "FIGURE", "t": "TABLE", "m": "FORMULA", "c": "BIBLIOGRAPHY_ENTRY"}
QUERYABLE_KINDS = ("BLOCK", "FIGURE", "TABLE", "FORMULA", "BIBLIOGRAPHY_ENTRY")


def kind_of(object_id: str) -> str:
    """Object kind from the ID grammar (``INVALID_ID`` if the string is no VKM id)."""
    for kind, grammar_kind in (("SOURCE", "source"), ("WORK", "work"), ("PAGE", "page"), ("DOCUMENT", "document"),
                               ("AUTHOR", "author"), ("VENUE", "venue"), ("ARTIFACT", "artifact"),
                               ("PROCESSING_RUN", "run")):
        if grammar.matches(grammar_kind, object_id):
            return kind
    if grammar.matches("object", object_id):
        return OBJECT_KIND_BY_CODE[object_id.rsplit(":", 1)[1][0]]
    raise ApiFailure("INVALID_ID", f"{object_id!r} is not a VKM id",
                     hint="ids look like VKM-SRC-001, VKM-SRC-001:p0012, VKM-SRC-001:p0012:f1a2b3c4d5e6f, "
                          "VKM-WRK-001, sha256:<64 hex>")


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return value


@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: str | None
    manifest_sha256: str | None
    built_at: str | None
    duckdb_version: str | None


class CanonStore:
    """DuckDB connection over one canonical snapshot (thread-safe: one cursor per query)."""

    def __init__(self, duckdb_path: Path | None = None, *, connection: Any = None,
                 current_snapshot: Callable[[], str | None] = lambda: None, root_kind: str | None = "CANONICAL"):
        if duckdb_path is None and connection is None:
            raise ValueError("a DuckDB file or an open connection is required")
        self.path = duckdb_path
        self._con = connection
        self._stamp: tuple[int, int, int] | None = None
        self._lock = threading.Lock()
        self._current = current_snapshot
        self.root_kind = root_kind
        self._snapshot: SnapshotInfo | None = None
        self._commits: dict[str, str] = {}
        if connection is not None:
            self._load_meta(connection)

    @classmethod
    def from_data_root(cls, data_root: Path) -> "CanonStore":
        """Canon of a CANONICAL data root; a STAGING root or a missing marker is refused (H-07)."""
        from vkm_corpus.contracts.vocab import RootKind
        from vkm_corpus.parquet.layout import CanonLayout, RootError
        from vkm_corpus.parquet.reader import current_snapshot_id

        layout = CanonLayout(Path(data_root))
        try:
            layout.require(RootKind.CANONICAL)
        except (RootError, ValueError, OSError) as exc:
            raise ApiFailure("SNAPSHOT_UNAVAILABLE", f"the data root is not a CANONICAL root: {exc}",
                             stage="canonical_lookup", tool="duckdb") from exc
        return cls(layout.duckdb_file, current_snapshot=lambda: current_snapshot_id(layout))

    # -------------------------------------------------------------------------------------------- connection
    def _file_stamp(self) -> tuple[int, int, int]:
        st = os.stat(self.path)  # type: ignore[arg-type]
        return (st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0))

    def _load_meta(self, con: Any) -> None:
        cur = con.cursor()
        try:
            cur.execute("SET TimeZone = 'UTC'")
            row = cur.execute("SELECT snapshot_id, manifest_sha256, strftime(timezone('UTC', built_at), "
                              "'%Y-%m-%dT%H:%M:%S.%fZ'), duckdb_version FROM meta.snapshot").fetchone()
            commits = cur.execute("SELECT commit_key, commit_id FROM meta.commits").fetchall()
        finally:
            cur.close()
        self._snapshot = SnapshotInfo(*row) if row else SnapshotInfo(None, None, None, None)
        self._commits = {k: v for k, v in commits}

    def _connection(self) -> Any:
        if self.path is None:
            return self._con
        try:
            stamp = self._file_stamp()
        except OSError as exc:
            raise ApiFailure("SNAPSHOT_UNAVAILABLE", "the canonical DuckDB file is missing (rebuild it from the "
                                                     "snapshot)", stage="canonical_lookup", tool="duckdb") from exc
        if self._con is None or stamp != self._stamp:
            import duckdb

            old, self._con = self._con, None
            try:
                con = duckdb.connect(str(self.path), read_only=True)
                # spills go to the (tmpfs) temp dir, never next to the read-only database file
                con.execute("SET temp_directory = ?", [str(Path(tempfile.gettempdir()) / "vkm_duckdb_spill")])
                self._load_meta(con)
            except Exception as exc:  # noqa: BLE001 - any open/meta failure means no usable canon
                raise ApiFailure("SNAPSHOT_UNAVAILABLE", f"cannot open the canonical DuckDB file: "
                                                         f"{type(exc).__name__}", stage="canonical_lookup",
                                 tool="duckdb") from exc
            self._con, self._stamp = con, stamp
            if old is not None:
                try:
                    old.close()
                except Exception:  # noqa: BLE001
                    pass
        return self._con

    def query(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._connection().cursor()
        try:
            cur.execute("SET TimeZone = 'UTC'")        # per cursor (each cursor is its own session); needs pytz
            cur.execute(sql, list(params))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except ApiFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - a query error is a canon problem, never silently empty
            raise ApiFailure("INTERNAL", f"canonical query failed: {type(exc).__name__}", stage="canonical_lookup",
                             tool="duckdb") from exc
        finally:
            cur.close()

    def one(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -------------------------------------------------------------------------------------------- snapshot
    def snapshot(self) -> SnapshotInfo:
        with self._lock:
            self._connection()
        assert self._snapshot is not None
        return self._snapshot

    def snapshot_id(self) -> str | None:
        return self.snapshot().snapshot_id

    def commit_of(self, key: str | None) -> str | None:
        self.snapshot()
        return self._commits.get(key or "")

    def status(self) -> dict[str, Any]:
        snap = self.snapshot()
        current = self._current()
        return {"snapshot_id": snap.snapshot_id, "manifest_sha256": snap.manifest_sha256, "built_at": snap.built_at,
                "duckdb_version": snap.duckdb_version, "current_snapshot_id": current,
                "up_to_date": current is None or current == snap.snapshot_id, "root_kind": self.root_kind}

    # -------------------------------------------------------------------------------------------- lookups
    def row(self, kind: str, object_id: str) -> dict[str, Any] | None:
        table, key = KIND_TABLE[kind]
        return self.one(f"SELECT * FROM {table} WHERE {key} = ?", [object_id])

    def rows(self, kind: str, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = sorted(set(ids))
        if not ids:
            return {}
        table, key = KIND_TABLE[kind]
        found = self.query(f"SELECT * FROM {table} WHERE {key} IN (SELECT unnest(?::VARCHAR[]))", [ids])
        return {r[key]: r for r in found}

    def hydrate(self, ids: Iterable[str]) -> dict[str, tuple[str, dict[str, Any]]]:
        """id → (kind, canonical row) for every id present in the snapshot (absent ids are simply missing)."""
        by_kind: dict[str, list[str]] = {}
        for oid in ids:
            try:
                by_kind.setdefault(kind_of(oid), []).append(oid)
            except ApiFailure:
                continue
        out: dict[str, tuple[str, dict[str, Any]]] = {}
        for kind, group in by_kind.items():
            for oid, row in self.rows(kind, group).items():
                out[oid] = (kind, row)
        return out

    def page_for(self, page_id: str | None) -> dict[str, Any] | None:
        if not page_id:
            return None
        return self.one("SELECT page_id, page_index, printed_page_raw, width_pt, height_pt, page_status "
                        "FROM pages WHERE page_id = ?", [page_id])

    def objects_on_page(self, page_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM objects_on_page(?)", [page_id])

    def blocks_of_page(self, page_id: str, primary_only: bool = True, limit: int = 500) -> list[dict[str, Any]]:
        extra = " AND is_primary_layer" if primary_only else ""
        return self.query("SELECT object_id, block_type, reading_order, text_layer, origin, is_primary_layer, "
                          "char_count, bbox_x0, bbox_y0, bbox_x1, bbox_y1, bbox_space, normalized_text FROM blocks "
                          f"WHERE page_id = ?{extra} ORDER BY text_layer, reading_order, object_id LIMIT ?",
                          [page_id, limit])

    def pages_of_source(self, source_id: str, from_index: int, to_index: int, limit: int,
                        offset: int) -> list[dict[str, Any]]:
        return self.query("SELECT page_id, page_index, page_kind, page_status, primary_text_origin, char_count, "
                          "printed_page_raw, quality_flags FROM pages WHERE source_id = ? AND page_index BETWEEN ? "
                          "AND ? ORDER BY page_index LIMIT ? OFFSET ?",
                          [source_id, from_index, to_index, limit, offset])

    def source_summary(self, source_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM source_status_summary WHERE source_id = ?", [source_id])

    def source_links(self, source_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT object_id, work_id, link_type, is_primary, page_start, page_end, part_label, "
                          "printed_range, curation_status, basis FROM source_work_links WHERE source_id = ? AND "
                          "curation_status <> 'REJECTED' ORDER BY is_primary DESC, link_type, object_id",
                          [source_id])

    def resolve_work(self, work_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM resolve_work(?)", [work_id])

    def work_sources(self, work_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT source_id, link_type, is_primary, page_start, page_end, part_label, "
                          "canonical_row_id FROM work_sources WHERE work_id = ? ORDER BY source_id", [work_id])

    def work_copy_counts(self, work_ids: Iterable[str]) -> dict[str, int]:
        ids = sorted({w for w in work_ids if w})
        if not ids:
            return {}
        rows = self.query("SELECT work_id, work_copy_count FROM work_copy_counts WHERE work_id IN "
                          "(SELECT unnest(?::VARCHAR[]))", [ids])
        return {r["work_id"]: int(r["work_copy_count"]) for r in rows}

    def primary_work_of_sources(self, source_ids: Iterable[str]) -> dict[str, str]:
        ids = sorted({s for s in source_ids if s})
        if not ids:
            return {}
        rows = self.query("SELECT source_id, work_id FROM work_sources WHERE is_primary AND source_id IN "
                          "(SELECT unnest(?::VARCHAR[]))", [ids])
        return {r["source_id"]: r["work_id"] for r in rows}

    def foreign_content_pages(self) -> dict[str, dict[str, Any]]:
        """page → works whose content the page carries (D's view ``foreign_content_pages``, H-16); a link without a
        work id (an unidentified foreign text) sets ``unidentified_work``."""
        rows = self.query("SELECT page_id, list(DISTINCT foreign_work_id ORDER BY foreign_work_id) FILTER "
                          "(WHERE foreign_work_id IS NOT NULL) AS works, bool_or(foreign_work_id IS NULL) AS "
                          "unidentified FROM foreign_content_pages GROUP BY page_id")
        return {r["page_id"]: {"work_ids": list(r["works"] or []), "unidentified_work": bool(r["unidentified"])}
                for r in rows}

    def work_authors(self, work_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT wa.ordinal, wa.role, wa.name_as_listed, wa.author_id, a.name_display, a.script, "
                          "a.identity_status FROM work_authors wa LEFT JOIN authors a ON a.author_id = wa.author_id "
                          "WHERE wa.work_id = ? ORDER BY wa.ordinal", [work_id])

    def rerank_texts(self, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = sorted(set(ids))
        if not ids:
            return {}
        rows = self.query("SELECT object_id, object_kind, source_id, page_id, is_primary_layer, text, text_sha256, "
                          "rule FROM rerank_text WHERE object_id IN (SELECT unnest(?::VARCHAR[]))", [ids])
        return {r["object_id"]: r for r in rows}

    def provenance(self, object_id: str, kind: str) -> dict[str, Any] | None:
        macro = "registry_trace" if kind in ("SOURCE", "WORK", "AUTHOR", "VENUE") else "provenance_trace"
        return self.one(f"SELECT * FROM {macro}(?)", [object_id])

    def run(self, run_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM processing_runs WHERE processing_run_id = ?", [run_id])

    def run_models(self, run_id: str | None) -> list[dict[str, Any]]:
        if not run_id:
            return []
        row = self.one("SELECT models FROM processing_runs WHERE processing_run_id = ?", [run_id])
        return list(row["models"] or []) if row else []

    def processing_of_source(self, source_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM processing_status WHERE source_id = ? ORDER BY page_index NULLS FIRST, "
                          "stage LIMIT ?", [source_id, limit])

    def processing_of_page(self, page_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM processing_status WHERE page_id = ? ORDER BY stage", [page_id])

    def errors(self, *, source_id: str | None = None, page_id: str | None = None, run_id: str | None = None,
               limit: int = 100) -> list[dict[str, Any]]:
        column, value = ("page_id", page_id) if page_id else ("source_id", source_id) if source_id else \
            ("processing_run_id", run_id)
        return self.query(f"SELECT error_id, processing_run_id, step_id, source_id, page_id, stage, code, tool, "
                          f"message, retryable, severity, log_ref, created_at FROM errors WHERE {column} = ? "
                          f"ORDER BY created_at, error_id LIMIT ?", [value, limit])

    def run_step_counts(self, run_id: str) -> dict[str, int]:
        rows = self.query("SELECT outcome, count(*) AS n FROM processing_steps WHERE processing_run_id = ? "
                          "GROUP BY outcome ORDER BY outcome", [run_id])
        return {r["outcome"]: int(r["n"]) for r in rows}

    def corpus_counts(self) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM corpus_counts")
        return jsonable(row) if row else None

    def citations_out(self, work_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM cites WHERE citing_work_id = ? ORDER BY cited_work_id", [work_id])

    def citations_in(self, work_id: str) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM cites WHERE cited_work_id = ? ORDER BY citing_work_id", [work_id])

    def entries_of_work(self, work_id: str, limit: int) -> list[dict[str, Any]]:
        return self.query("SELECT object_id, page_id, source_id, entry_label, ordinal_in_list, parsed_title, "
                          "parsed_year, parsed_doi, citing_work_resolution, citing_work_is_container, review_status "
                          "FROM bibliography WHERE citing_work_id = ? ORDER BY source_id, page_id, ordinal_in_list, "
                          "object_id LIMIT ?", [work_id, limit])

    def entry_links(self, entry_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        ids = sorted(set(entry_ids))
        if not ids:
            return {}
        rows = self.query("SELECT entry_id, cited_work_id, match_method, match_score, match_status, matched_fields "
                          "FROM bibliography_links WHERE entry_id IN (SELECT unnest(?::VARCHAR[])) "
                          "ORDER BY entry_id, match_score DESC, cited_work_id", [ids])
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["entry_id"], []).append(r)
        return out

    def query_objects(self, kind: str, *, source_ids: list[str], work_ids: list[str], page_from: int | None,
                      page_to: int | None, figure_types: list[str], review_status: list[str],
                      quality_flags_any: list[str], quality_flags_none: list[str], has_image: bool | None,
                      source_scope: list[str], source_scope_raw: list[str], caption_query: str | None,
                      origin: list[str], limit: int, offset: int) -> list[dict[str, Any]]:
        """Structured lookup of document objects (typed filters only; ordered by object id)."""
        table, _key = KIND_TABLE[kind]
        where, params = ["1 = 1"], []
        if source_ids:
            where.append("o.source_id IN (SELECT unnest(?::VARCHAR[]))")
            params.append(source_ids)
        if work_ids:
            where.append("o.source_id IN (SELECT source_id FROM work_sources WHERE work_id IN "
                         "(SELECT unnest(?::VARCHAR[])))")
            params.append(work_ids)
        if page_from is not None or page_to is not None:
            where.append("p.page_index BETWEEN ? AND ?")
            params += [page_from or 1, page_to or 9999]
        if figure_types and kind == "FIGURE":
            where.append("o.detected_figure_type IN (SELECT unnest(?::VARCHAR[]))")
            params.append(figure_types)
        if review_status:
            where.append("o.review_status IN (SELECT unnest(?::VARCHAR[]))")
            params.append(review_status)
        if origin:
            where.append("o.origin IN (SELECT unnest(?::VARCHAR[]))")
            params.append(origin)
        if quality_flags_any:
            where.append("len(list_intersect(o.quality_flags, ?::VARCHAR[])) > 0")
            params.append(quality_flags_any)
        if quality_flags_none:
            where.append("len(list_intersect(o.quality_flags, ?::VARCHAR[])) = 0")
            params.append(quality_flags_none)
        if has_image is not None and kind in ("FIGURE", "TABLE", "FORMULA"):
            where.append("(o.image_artifact_id IS NOT NULL) = ?")
            params.append(has_image)
        if source_scope:
            where.append("len(list_intersect(o.source_site_scope, ?::VARCHAR[])) > 0")
            params.append(source_scope)
        if source_scope_raw:
            where.append("o.source_site_scope_raw IN (SELECT unnest(?::VARCHAR[]))")
            params.append(source_scope_raw)
        if caption_query:
            text_col = {"FIGURE": "caption_normalized", "TABLE": "caption_normalized", "FORMULA": "normalized_latex",
                        "BLOCK": "normalized_text", "BIBLIOGRAPHY_ENTRY": "normalized_text"}[kind]
            where.append(f"coalesce(o.{text_col}, '') ILIKE ? ESCAPE '\\'")
            params.append("%" + caption_query.replace("%", r"\%").replace("_", r"\_") + "%")
        params += [limit, offset]
        return self.query(f"SELECT o.* FROM {table} o LEFT JOIN pages p ON p.page_id = o.page_id "
                          f"WHERE {' AND '.join(where)} ORDER BY o.object_id LIMIT ? OFFSET ?", params)
