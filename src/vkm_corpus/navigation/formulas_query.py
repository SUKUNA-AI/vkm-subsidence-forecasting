"""Read side of the formula layer (NAV §3, §5): pure query functions for the MCP tools ``get_formula_context`` and
``find_formulas``.

``con`` is a DuckDB connection where the four datasets are visible as tables or views named ``formula_context``,
``formula_symbols``, ``formula_refs``, ``formula_parameters`` (``attach_parquet`` creates views over a derived
``<snapshot>/`` directory; ``register_tables`` registers Arrow tables). When the canonical schema is present too,
texts of blocks and the LaTeX of formulas are added. Everything returned is navigation (AUTO_EXTRACTED_UNREVIEWED).
"""
from __future__ import annotations

import os
import re
from typing import Any

from vkm_corpus.navigation.formulas import REVIEW_STATUS, _stem, plain_symbol, symbol_key

DATASETS = ("formula_context", "formula_symbols", "formula_refs", "formula_parameters")


def attach_parquet(con: Any, directory: str) -> None:
    """Views ``formula_*`` over ``<directory>/<dataset>.parquet``."""
    for name in DATASETS:
        path = os.path.join(directory, name + ".parquet").replace("'", "''")
        con.execute(f"CREATE OR REPLACE TEMP VIEW {name} AS SELECT * FROM read_parquet('{path}')")


def register_tables(con: Any, tables: dict[str, Any]) -> None:
    for name in DATASETS:
        if name in tables:
            con.register(name, tables[name])


def _rows(con: Any, sql: str, params: list[Any] | tuple = ()) -> list[dict[str, Any]]:
    cur = con.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _has_canon(con: Any) -> bool:
    try:
        con.execute("SELECT 1 FROM canonical.blocks LIMIT 0")
        con.execute("SELECT 1 FROM canonical.formulas LIMIT 0")
        return True
    except Exception:  # noqa: BLE001 - the canonical schema is optional
        return False


def _block_texts(con: Any, ids: list[str]) -> dict[str, dict[str, Any]]:
    ids = [i for i in ids if i]
    if not ids or not _has_canon(con):
        return {}
    ph = ", ".join("?" for _ in ids)
    rows = _rows(con, f"SELECT object_id AS block_id, page_id, block_type, text FROM canonical.blocks "
                      f"WHERE object_id IN ({ph})", ids)
    return {r["block_id"]: r for r in rows}


def get_formula_context(con: Any, formula_id: str) -> dict[str, Any] | None:
    """Number, section, text before, «где…» block(s) with the symbols and their definitions, references to the
    formula and from its text, parameter candidates — one formula."""
    ctx = _rows(con, "SELECT * FROM formula_context WHERE formula_id = ?", [formula_id])
    if not ctx:
        return None
    c = ctx[0]
    out: dict[str, Any] = {
        "formula_id": formula_id, "source_id": c["source_id"], "page_id": c["page_id"], "kind": c["kind"],
        "equation_number": c["equation_number"], "equation_number_raw": c["equation_number_raw"],
        "number_method": c["number_method"], "section_id": c["section_id"], "rule_version": c["rule_version"],
        "review_status": REVIEW_STATUS,
    }
    if _has_canon(con):
        f = _rows(con, "SELECT normalized_latex, raw_output FROM canonical.formulas WHERE object_id = ?", [formula_id])
        out["latex"] = (f[0]["normalized_latex"] or f[0]["raw_output"]) if f else None
    where_ids = list(c["where_block_ids"] or [])
    texts = _block_texts(con, [c["intro_block_id"], c["next_block_id"], c["host_block_id"], *where_ids])

    def blk(bid: str | None) -> dict[str, Any] | None:
        if not bid:
            return None
        t = texts.get(bid, {})
        return {"block_id": bid, "page_id": t.get("page_id"), "text": t.get("text")}

    out["intro"] = blk(c["intro_block_id"])
    out["where"] = [blk(b) for b in where_ids]
    out["next"] = blk(c["next_block_id"]) if c["next_block_id"] not in where_ids else None
    out["host"] = blk(c["host_block_id"])
    out["symbols"] = _rows(con, """
        SELECT symbol, role, n_occurrences, in_formula, definition, unit, definition_block_id, definition_symbol_raw,
               match_method
        FROM formula_symbols WHERE formula_id = ?
        ORDER BY in_formula DESC, CASE role WHEN 'LHS' THEN 0 WHEN 'RHS' THEN 1 ELSE 2 END, symbol""", [formula_id])
    out["referenced_by"] = _rows(con, """
        SELECT ref_id, block_id, page_id, number_text, ref_type, cue, resolution, citing_formula_id
        FROM formula_refs WHERE formula_id = ? ORDER BY page_id, char_start""", [formula_id])
    own = [b for b in (c["intro_block_id"], c["next_block_id"], *where_ids) if b]
    refs_out: list[dict[str, Any]] = []
    if own:
        ph = ", ".join("?" for _ in own)
        refs_out = _rows(con, f"""
            SELECT r.ref_id, r.block_id, r.formula_id, r.number_text, r.ref_type, r.cue, x.equation_number,
                   x.page_id AS formula_page_id
            FROM formula_refs r LEFT JOIN formula_context x ON x.formula_id = r.formula_id
            WHERE r.block_id IN ({ph}) AND r.formula_id <> ?
            ORDER BY r.block_id, r.char_start""", [*own, formula_id])
    out["refers_to"] = refs_out
    out["parameters"] = _rows(con, """
        SELECT symbol, value_text, value, value_min, value_max, unit, block_id, in_formula, context_kind
        FROM formula_parameters WHERE formula_id = ? ORDER BY context_kind, symbol""", [formula_id])
    return out


def _concept_stems(concept: str) -> list[str]:
    words = re.findall(r"[^\W\d_]{2,}", concept.lower().replace("ё", "е"))
    return [_stem(w) for w in words]


def find_formulas(con: Any, *, concept: str | None = None, symbol: str | None = None, source_id: str | None = None,
                  limit: int = 50) -> list[dict[str, Any]]:
    """Formulas by a concept (words of symbol definitions: «скорость ползучести» — across books) or by a symbol
    inside one source (``\\sigma_1``, ``σ1``). A bare symbol is never matched across sources."""
    if not concept and not symbol:
        raise ValueError("find_formulas needs concept= or symbol=")
    if symbol and not source_id:
        raise ValueError("symbol search is scoped to one source (symbols are not global): pass source_id")
    where: list[str] = []
    params: list[Any] = []
    if source_id:
        where.append("s.source_id = ?")
        params.append(source_id)
    stems: list[str] = []
    if concept:
        stems = _concept_stems(concept)
        if not stems:
            return []
        where.append("s.definition_key IS NOT NULL")
        for st in stems:
            where.append("s.definition_key LIKE ?")
            params.append(f"%{st}%")
    if symbol:
        norm = plain_symbol(symbol) or symbol
        where.append("(s.symbol = ? OR s.symbol_key = ?)")
        params.extend([norm, symbol_key(norm)])
    sql = f"""
        SELECT s.formula_id, s.source_id, c.page_id, c.equation_number, c.kind, s.symbol, s.role, s.in_formula,
               s.definition, s.unit, s.definition_key
        FROM formula_symbols s JOIN formula_context c ON c.formula_id = s.formula_id
        WHERE {' AND '.join(where)}
        ORDER BY s.source_id, c.page_index, s.formula_id, s.symbol"""
    rows = _rows(con, sql, params)
    if stems:                                   # every query stem starts a word of the definition
        rows = [r for r in rows if all(any(w.startswith(st) for w in (r["definition_key"] or "").split())
                                       for st in stems)]
    out = []
    for r in rows[: max(1, int(limit))]:
        r = dict(r)
        r.pop("definition_key", None)
        r["match"] = "concept" if concept else "symbol"
        r["review_status"] = REVIEW_STATUS
        out.append(r)
    return out
