"""Read the published canon for the lab: rows of pages/blocks/figures/tables/formulas/bibliography and source metadata.

Two entry points, both read-only:

* ``CanonReader.from_duckdb(path)`` — the materialised ``duckdb/vkm_corpus.duckdb`` of a CANONICAL root (or its copy);
* ``CanonReader.from_layout(layout)`` — in-memory DuckDB over the ``CURRENT`` snapshot of a CANONICAL root through
  agent D's loader (``vkm_corpus.duckdb.build.open_snapshot``), used by tests on ``synthetic_canon``.

Nothing is written to the canon. The text of every object is the canonical ``normalized_text`` (rule
``normalize_text_v1``); raw model outputs are never read.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from vkm_corpus.retrieval_lab.units import SourceMeta

PAGE_COLS = "page_id, source_id, page_index, page_kind, printed_page_labels, normalized_text, char_count"
BLOCK_COLS = ("object_id, source_id, page_id, block_type, reading_order, is_primary_layer, normalized_text, language, "
              "bbox_x0, bbox_y0, bbox_x1, bbox_y1")
FIGURE_COLS = ("object_id, source_id, page_id, figure_label, caption, caption_normalized, caption_block_id, "
               "detected_figure_type, image_artifact_id, bbox_x0, bbox_y0, bbox_x1, bbox_y1")
TABLE_COLS = ("object_id, source_id, page_id, table_label, caption, caption_normalized, caption_block_id, "
              "normalized_text, image_artifact_id, bbox_x0, bbox_y0, bbox_x1, bbox_y1")
FORMULA_COLS = ("object_id, source_id, page_id, equation_label, normalized_latex, raw_output, raw_format, "
                "image_artifact_id, bbox_x0, bbox_y0, bbox_x1, bbox_y1")
BIB_COLS = "object_id, source_id, page_id, normalized_text, language"


def _in_list(column: str, values: Iterable[str] | None) -> tuple[str, list[str]]:
    vals = sorted(set(values or ()))
    if not vals:
        return "", []
    return f" WHERE {column} IN ({', '.join('?' for _ in vals)})", vals


@dataclass
class CanonReader:
    con: Any
    origin: str

    @classmethod
    def from_duckdb(cls, path: str | Path) -> "CanonReader":
        import duckdb

        return cls(duckdb.connect(str(path), read_only=True), f"duckdb:{Path(path).name}")

    @classmethod
    def from_layout(cls, layout: Any, snapshot_id: str | None = None) -> "CanonReader":
        from vkm_corpus.duckdb.build import open_snapshot

        return cls(open_snapshot(layout, snapshot_id=snapshot_id), "snapshot")

    def close(self) -> None:
        self.con.close()

    def _rows(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        cur = self.con.execute(sql, params or [])
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    def snapshot(self) -> dict[str, Any]:
        try:
            rows = self._rows("SELECT snapshot_id, manifest_sha256, duckdb_version, pipeline_version FROM meta.snapshot")
            return rows[0] if rows else {}
        except Exception:  # noqa: BLE001 - an attached snapshot without meta
            return {}

    def source_ids(self) -> list[str]:
        return [r["source_id"] for r in self._rows("SELECT DISTINCT source_id FROM canonical.pages ORDER BY 1")]

    def pages(self, sources: Iterable[str] | None = None) -> list[dict[str, Any]]:
        where, vals = _in_list("source_id", sources)
        return self._rows(f"SELECT {PAGE_COLS} FROM canonical.pages{where} ORDER BY source_id, page_index", vals)

    def blocks(self, sources: Iterable[str] | None = None) -> list[dict[str, Any]]:
        where, vals = _in_list("source_id", sources)
        return self._rows(f"SELECT {BLOCK_COLS} FROM canonical.blocks{where} ORDER BY page_id, reading_order, "
                          "object_id", vals)

    def figures(self, sources: Iterable[str] | None = None) -> list[dict[str, Any]]:
        where, vals = _in_list("source_id", sources)
        return self._rows(f"SELECT {FIGURE_COLS} FROM canonical.figures{where} ORDER BY object_id", vals)

    def _primary(self, table: str, where: str) -> str:
        """Secondary-layer tables/formulas (``is_primary_layer`` FALSE) are no retrieval units
        (``contracts.primary_layer``)."""
        from vkm_corpus.contracts.primary_layer import primary_clause

        clause = primary_clause(self.con, table)
        return where if clause == "TRUE" else (f"{where} AND {clause}" if where else f" WHERE {clause}")

    def tables(self, sources: Iterable[str] | None = None) -> list[dict[str, Any]]:
        where, vals = _in_list("source_id", sources)
        where = self._primary("tables", where)
        return self._rows(f'SELECT {TABLE_COLS} FROM canonical."tables"{where} ORDER BY object_id', vals)

    def formulas(self, sources: Iterable[str] | None = None) -> list[dict[str, Any]]:
        where, vals = _in_list("source_id", sources)
        where = self._primary("formulas", where)
        return self._rows(f"SELECT {FORMULA_COLS} FROM canonical.formulas{where} ORDER BY object_id", vals)

    def bibliography(self, sources: Iterable[str] | None = None) -> list[dict[str, Any]]:
        where, vals = _in_list("source_id", sources)
        return self._rows(f"SELECT {BIB_COLS} FROM canonical.bibliography_entries{where} ORDER BY object_id", vals)

    def source_meta(self) -> dict[str, SourceMeta]:
        """Work metadata of each source via the INSTANCE_OF rule of D (``work_sources``)."""
        rows = self._rows("""
            SELECT s.source_id, s.source_class_raw, s.site_scope_raw, ws.work_id, w.title, w.authors_display,
                   w.publication_year, w.venue_display
            FROM canonical.sources s
            LEFT JOIN work_sources ws ON ws.source_id = s.source_id
            LEFT JOIN canonical.works w ON w.work_id = ws.work_id
            ORDER BY s.source_id, ws.work_id""")
        out: dict[str, SourceMeta] = {}
        for r in rows:
            if r["source_id"] in out:
                continue
            out[r["source_id"]] = SourceMeta(r["source_id"], r["work_id"], r["title"], r["authors_display"],
                                             r["publication_year"], r["venue_display"], r["source_class_raw"],
                                             r["site_scope_raw"])
        return out

    def page_labels(self) -> dict[str, str]:
        return {r["page_id"]: (r["printed_page_labels"] or [None])[0]
                for r in self._rows("SELECT page_id, printed_page_labels FROM canonical.pages")
                if r["printed_page_labels"]}

    def duplicate_pages(self) -> dict[str, str]:
        """page_id → dup_group_id of the duplicate-page candidates (rule duplicate_pages_v1)."""
        try:
            rows = self._rows("SELECT * FROM duplicate_page_candidates")
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, str] = {}
        for r in rows:
            gid = r.get("dup_group_id")
            for key in ("page_id", "page_id_a", "page_id_b", "other_page_id"):
                if r.get(key) and gid:
                    out[r[key]] = gid
        return out

    def load_all(self, sources: Iterable[str] | None = None) -> dict[str, list[dict[str, Any]]]:
        src = list(sources) if sources is not None else None
        return {"pages": self.pages(src), "blocks": self.blocks(src), "figures": self.figures(src),
                "tables": self.tables(src), "formulas": self.formulas(src), "bibliography": self.bibliography(src)}
