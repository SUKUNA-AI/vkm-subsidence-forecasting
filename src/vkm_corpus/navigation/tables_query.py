"""Read side of the structured tables (NAV §10): pure query functions for the API and the MCP tools.

``con`` is a DuckDB connection where ``table_structure``, ``table_cells`` and ``table_columns`` are visible as
tables or views (:func:`attach_parquet` creates views over a derived ``<snapshot>/`` directory, :func:`register_tables`
registers Arrow tables; the serving store exposes the datasets under these names). A property or a material is
matched through the parameters vocabulary (:func:`vkm_corpus.navigation.parameters_query.resolve_property` /
``resolve_material``). Everything returned is navigation (``AUTO_EXTRACTED_UNREVIEWED``): a grid parsed by rules from
the canonical table, values as printed — not evidence.
"""
from __future__ import annotations

import os
import re
import base64
import hashlib
import json
from typing import Any, Mapping

from vkm_corpus.navigation.tables import NOTE, VALUE_ROWS

DATASETS = ("table_structure", "table_cells", "table_columns")
DEFAULT_TABLES: dict[str, str] = {name: name for name in DATASETS}
REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
TABLE_FIELDS = ("table_id", "nav_table_id", "source_id", "page_id", "page_index", "section_id", "table_label",
                "table_number", "caption", "n_rows", "n_cols", "n_header_rows", "header_method", "n_bands", "n_blocks",
                "blocks", "orientation", "parse_method", "confidence", "structure_ok", "covers_region", "quality_flags",
                "property_keys", "materials")
COLUMN_FIELDS = ("block", "col", "header_path", "header_text", "symbol", "unit_raw", "unit_canonical", "unit_source",
                 "multiplier_exp", "stat", "column_type", "role", "col_group", "property_key", "property_label",
                 "property_basis", "materials", "flags")
CELL_FIELDS = ("cell_id", "col", "row_span", "col_span", "text_clean", "value_type", "value_text", "value_min",
               "value_max", "value_pm", "qualifier", "unit_raw", "unit_canonical", "unit_source", "multiplier_exp",
               "row_property_key", "flags")


def attach_parquet(con: Any, directory: str, *, tables: Mapping[str, str] | None = None) -> None:
    """Views over ``<directory>/table_structure.parquet``, ``table_cells.parquet`` and ``table_columns.parquet``."""
    names = {**DEFAULT_TABLES, **(tables or {})}
    for ds, view in names.items():
        path = os.path.join(directory, ds + ".parquet").replace("'", "''")   # DDL cannot take parameters
        con.execute(f"CREATE OR REPLACE TEMP VIEW {view} AS SELECT * FROM read_parquet('{path}')")


def register_tables(con: Any, tables: Mapping[str, Any]) -> None:
    for name in DATASETS:
        if name in tables:
            con.register(name, tables[name])


def _names(tables: Mapping[str, str] | None) -> dict[str, str]:
    return {**DEFAULT_TABLES, **(tables or {})}


def _rows(con: Any, sql: str, params: list[Any] | tuple = ()) -> list[dict[str, Any]]:
    cur = con.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and v != []}


def _markdown(cells: list[dict[str, Any]], n_cols: int, header_rows: set[int], max_chars: int) -> tuple[str, bool]:
    """Pipe table of the cleaned texts (a merged cell written once, at its top-left corner)."""
    grid: dict[int, dict[int, str]] = {}
    for c in cells:
        grid.setdefault(c["row"], {})[c["col"]] = re.sub(r"\s*\|\s*", " / ", c.get("text_clean") or "")
    lines: list[str] = []
    done_sep = False
    for r in sorted(grid):
        lines.append("| " + " | ".join(grid[r].get(c, "") for c in range(n_cols)) + " |")
        if not done_sep and r in header_rows and (r + 1) not in header_rows:
            lines.append("|" + "---|" * n_cols)
            done_sep = True
    out, used = [], 0
    for ln in lines:
        if used + len(ln) + 1 > max_chars:
            return "\n".join(out), True
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out), False


def get_table_structured(con: Any, table_id: str, *, max_rows: int = 200, max_chars: int = 8000,
                         cursor: str | None = None,
                         tables: Mapping[str, str] | None = None) -> dict[str, Any]:
    """A structured table by its canonical id (``<page_id>:t…``) or NAV id (``TBL-…``): structure (number, caption,
    size, header rows, bands and blocks, orientation, confidence, flags), columns (header path, unit and its source,
    role, type, property), rows (role, block, cells with the parsed value, unit and flags) up to ``max_rows`` and a
    Markdown rendering of the cleaned texts (≤ ``max_chars``)."""
    t = _names(tables)
    found = _rows(con, f"SELECT * FROM {t['table_structure']} WHERE table_id = ? OR nav_table_id = ?",
                  [table_id, table_id])
    out: dict[str, Any] = {"query": table_id, "found": bool(found), "review_status": REVIEW_STATUS, "note": NOTE}
    if not found:
        return out
    if len(found) != 1:
        raise ValueError("ambiguous table identity in navigation snapshot")
    s = found[0]
    tid = s["table_id"]
    raw_columns = _rows(con, f"SELECT * FROM {t['table_columns']} WHERE table_id = ? ORDER BY block, col", [tid])
    # Fingerprint the full record, including raw cell text. A repack or same-snapshot
    # correction cannot silently continue an older traversal. Stream the hash so a
    # large table is not materialised just to compute its identity.
    digest = hashlib.sha256()
    def hash_row(row: Any) -> None:
        digest.update(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str,
                                 separators=(",", ":")).encode("utf-8") + b"\n")
    snapshot = None
    if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='nav_meta'").fetchone()[0]:
        meta = json.loads(con.execute("SELECT meta_json FROM nav_meta").fetchone()[0])
        snapshot = meta.get("snapshot_id")
    hash_row({"snapshot_id": snapshot, "structure": s, "columns": raw_columns})
    stream = con.execute(f'SELECT * FROM {t["table_cells"]} WHERE table_id=? ORDER BY "row", col, cell_id', [tid])
    field_names = [d[0] for d in stream.description]
    total_cells = 0
    while batch := stream.fetchmany(1024):
        for row in batch:
            hash_row(dict(zip(field_names, row)))
        total_cells += len(batch)
    version = digest.hexdigest()
    after = -1
    if cursor:
        try:
            if len(cursor) > 2048:
                raise ValueError("oversized cursor")
            token = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
            if (set(token) != {"v", "table", "version", "after"} or type(token["v"]) is not int or token["v"] != 1
                    or token["table"] != tid or type(token["after"]) is not int
                    or not 0 <= token["after"] <= 2_147_483_647):
                raise ValueError("invalid table cursor")
            if token["version"] != version:
                raise ValueError("table cursor is stale: snapshot or table content changed; restart traversal")
            after = token["after"]
        except (TypeError, KeyError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid or stale table cursor: {exc}") from exc
    columns = [_compact({k: c.get(k) for k in COLUMN_FIELDS}) for c in raw_columns
        if c.get("role") != "EMPTY" or c.get("header_text")]
    limit = max(1, int(max_rows))
    row_ids = [r[0] for r in con.execute(f'SELECT DISTINCT "row" FROM {t["table_cells"]} '
                                       'WHERE table_id=? AND "row">? ORDER BY "row" LIMIT ?',
                                       [tid, after, limit + 1]).fetchall()]
    more = len(row_ids) > limit
    last = row_ids[min(len(row_ids), limit) - 1] if row_ids else after
    cells = _rows(con, f'SELECT * FROM {t["table_cells"]} WHERE table_id=? AND "row">? AND "row"<=? '
                      'ORDER BY "row", col, cell_id', [tid, after, last])
    rows: dict[int, dict[str, Any]] = {}
    for c in cells:
        r = rows.setdefault(c["row"], {"row": c["row"], "role": c["row_role"], "block": c["block"],
                                       "band": c["band"], "label": c.get("row_label"), "cells": []})
        if r["label"] is None and c.get("row_label"):
            r["label"] = c["row_label"]
        cell = _compact({k: c.get(k) for k in CELL_FIELDS})
        if c.get("text") and c.get("text") != c.get("text_clean"):
            cell["text_printed"] = c["text"]
        r["cells"].append(cell)
    header_rows = {c["row"] for c in cells if c.get("is_header")}
    md, cut = _markdown(cells, int(s.get("n_cols") or 0), header_rows, max(200, int(max_chars)))
    out.update({"table": _compact({k: s.get(k) for k in TABLE_FIELDS}), "columns": columns,
                "rows": [rows[r] for r in sorted(rows)], "markdown": md,
                "truncated": {"rows": more, "markdown": cut},
                "pagination": {"snapshot_id": snapshot, "table_version": version, "total_cells": total_cells,
                               "has_more": more, "next_cursor": base64.urlsafe_b64encode(json.dumps(
                                   {"v": 1, "table": tid, "version": version, "after": last},
                                   separators=(",", ":")).encode()).decode().rstrip("=") if more else None}})
    return out


def find_tables(con: Any, property: str | None = None, material: str | None = None,  # noqa: A002
                source_id: str | None = None, text: str | None = None, limit: int = 20, *,
                tables: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Structured tables by the property a column header, a row label or the caption names («модуль деформации»,
    «σсж», «ucs»), by a material named in headers, row labels or the caption («каменная соль», «соляные породы»), by
    source and by words of the caption, headers or row labels; every given filter must match. Ordered by how strongly
    the property is named (columns, then rows, then the caption), then by confidence. The answer says how the query
    was resolved; an unresolved filter returns nothing."""
    from vkm_corpus.navigation.parameters_query import resolve_material, resolve_property  # noqa: PLC0415

    t = _names(tables)
    query: dict[str, Any] = {"property": property, "material": material, "source_id": source_id, "text": text}
    unresolved: list[str] = []
    sets: list[set[str]] = []
    score: dict[str, float] = {}
    matched: dict[str, list[dict[str, Any]]] = {}
    if property:
        keys = resolve_property(property)
        query["property_keys"] = keys
        hit: set[str] = set()
        if not keys:
            unresolved.append("property")
        else:
            ph = ", ".join("?" for _ in keys)
            for c in _rows(con, f"""SELECT table_id, block, col, header_text, unit_raw, unit_source, property_key
                                    FROM {t['table_columns']} WHERE property_key IN ({ph}) AND role = 'VALUE'
                                    ORDER BY table_id, block, col""", keys):
                hit.add(c["table_id"])
                matched.setdefault(c["table_id"], []).append(_compact({k: c[k] for k in c if k != "table_id"}))
                score[c["table_id"]] = score.get(c["table_id"], 0.0) + (2.0 if len(matched[c["table_id"]]) <= 3
                                                                       else 0.0)
            roles = ", ".join(f"'{r}'" for r in sorted(VALUE_ROWS))
            for r in _rows(con, f"""SELECT table_id, count(DISTINCT "row") AS n FROM {t['table_cells']}
                                    WHERE row_property_key IN ({ph}) AND row_role IN ({roles}) GROUP BY table_id""",
                           keys):
                hit.add(r["table_id"])
                score[r["table_id"]] = score.get(r["table_id"], 0.0) + 1.5
            for r in _rows(con, f"SELECT table_id FROM {t['table_structure']} WHERE list_has_any(property_keys, "
                                f"[{ph}]::VARCHAR[])", keys):
                hit.add(r["table_id"])
                score[r["table_id"]] = score.get(r["table_id"], 0.0) + 0.5
        sets.append(hit)
    if material:
        mats = resolve_material(material)
        query["materials"] = mats
        if not mats:
            unresolved.append("material")
            sets.append(set())
        else:
            ph = ", ".join("?" for _ in mats)
            got = {r["table_id"] for r in _rows(con, f"SELECT table_id FROM {t['table_structure']} WHERE "
                                                     f"list_has_any(materials, [{ph}]::VARCHAR[])", mats)}
            for tid in got:
                score[tid] = score.get(tid, 0.0) + 1.0
            sets.append(got)
    if text:
        words = [w for w in re.findall(r"[^\W_]{3,}", text.lower())][:6]
        query["words"] = words
        for w in words:
            like = f"%{w}%"
            got = {r["table_id"] for r in _rows(con, f"""
                SELECT table_id FROM {t['table_structure']} WHERE lower(caption) LIKE ? OR lower(table_label) LIKE ?
                UNION SELECT table_id FROM {t['table_cells']}
                WHERE (is_header AND lower(text_clean) LIKE ?) OR lower(row_label) LIKE ?""",
                [like, like, like, like])}
            for tid in got:
                score[tid] = score.get(tid, 0.0) + 0.5
            sets.append(got)
    where, params = [], []
    if source_id:
        where.append("source_id = ?")
        params.append(source_id)
    ids: set[str] | None = None
    for s in sets:
        ids = set(s) if ids is None else ids & s
    if ids is not None:
        if not ids:
            return {"query": query, "unresolved": unresolved, "total": 0, "tables": [], "note": NOTE}
        where.append(f"table_id IN ({', '.join('?' for _ in ids)})")
        params += sorted(ids)
    cond = ("WHERE " + " AND ".join(where)) if where else ""
    rows = _rows(con, f"""SELECT table_id, nav_table_id, source_id, page_id, page_index, section_id, table_number,
                                 caption, n_rows, n_cols, orientation, confidence, quality_flags, property_keys,
                                 materials FROM {t['table_structure']} {cond}""", params)
    for r in rows:
        r["score"] = round(score.get(r["table_id"], 0.0), 2)
        if r.get("caption") and len(r["caption"]) > 200:
            r["caption"] = r["caption"][:200] + "…"
        if r["table_id"] in matched:
            r["matched_columns"] = matched[r["table_id"]][:6]
    rows.sort(key=lambda r: (-r["score"], -(r["confidence"] or 0), r["source_id"] or "", r["page_index"] or 0,
                             r["table_id"]))
    return {"query": query, "unresolved": unresolved, "total": len(rows),
            "tables": [_compact(r) for r in rows[:max(1, int(limit))]], "note": NOTE}
