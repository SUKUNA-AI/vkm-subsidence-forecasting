"""Primary-layer rule of tables and formulas (``is_primary_layer``: tables 0.1.3, formulas 0.1.2).

A page may carry objects of two recognition layers (an imported primary layer, ``PADDLEOCR_VL`` on CHOICE_V1 pages, and
the page's previous layer kept as secondary). Blocks have a non-null ``is_primary_layer`` since 0.1.0; tables and
formulas got a nullable one:

* ``TRUE`` — object of the page's primary layer;
* ``FALSE`` — object of a kept secondary layer (still in the canon, still readable by id; not taken wholesale);
* ``NULL`` — no layer choice recorded (every historical row, every page without an imported layer) → primary.

Readers that take tables or formulas wholesale (NAV, retrieval units, search documents, graph rows, topic maps) skip
``FALSE`` only: ``PRIMARY_SQL`` / ``is_primary``. A projection built before the column existed (or a test fixture with a
narrower table) has no such column: ``primary_clause`` then filters nothing.
"""
from __future__ import annotations

from typing import Any, Mapping

PRIMARY_SQL = "is_primary_layer IS NOT FALSE"


def is_primary(row: Mapping[str, Any] | Any) -> bool:
    """Row-level form of the rule (mapping or object with ``is_primary_layer``; a missing key counts as NULL)."""
    value = row.get("is_primary_layer") if isinstance(row, Mapping) else getattr(row, "is_primary_layer", None)
    return value is not False


def has_column(con: Any, table: str, column: str = "is_primary_layer", schema: str = "canonical") -> bool:
    try:
        rows = con.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = ? AND table_name = ? "
                           "AND column_name = ?", [schema, table, column]).fetchall()
    except Exception:  # noqa: BLE001 - a connection without information_schema: no filter
        return False
    return bool(rows)


def primary_clause(con: Any, table: str, alias: str | None = None, schema: str = "canonical") -> str:
    """SQL predicate selecting primary-layer rows of ``schema.table`` (``TRUE`` when the column is absent)."""
    if not has_column(con, table, schema=schema):
        return "TRUE"
    return f"{alias + '.' if alias else ''}{PRIMARY_SQL}"
