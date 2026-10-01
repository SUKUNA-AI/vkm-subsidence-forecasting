"""Read functions over the sections datasets — the future MCP tools ``get_outline`` / ``get_section`` /
``section_of_page`` (pure: a DuckDB connection in, plain dicts out; no writes).

The connection must expose ``nav_sections`` and ``nav_section_pages`` (``attach`` creates temporary views over the
Parquet files of ``vkm-corpus nav build``; ``nav build`` itself registers the Arrow tables under the same names) and,
for the counts of a section, the canonical tables (``canonical.pages`` + ``figures`` / ``tables`` / ``formulas`` /
``bibliography_entries``). Sections are navigation (DERIVED, ``AUTO_EXTRACTED_UNREVIEWED``), not evidence.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

SECTION_FIELDS = ("section_id", "source_id", "work_id", "parent_section_id", "level", "ordinal", "numbering", "title",
                  "title_path", "page_start_id", "page_end_id", "page_start_index", "page_end_index", "method",
                  "confidence", "heading_block_id", "rule_version")
COUNTED = (("figures", "figures"), ("tables", "tables"), ("formulas", "formulas"),
           ("bibliography_entries", "bibliography_entries"))
MAX_PAGE_IDS = 300


def _lit(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def attach(con, nav_dir: str | Path) -> None:
    """Temporary views ``nav_sections`` / ``nav_section_pages`` over ``<nav_dir>/*.parquet`` (read-only safe)."""
    d = Path(nav_dir)
    for name in ("sections", "section_pages"):
        con.execute(f"CREATE OR REPLACE TEMP VIEW nav_{name} AS SELECT * FROM read_parquet({_lit(d / (name + '.parquet'))})")


def _dicts(cur) -> list[dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _section(con, section_id: str) -> dict[str, Any] | None:
    rows = _dicts(con.execute(f"SELECT {', '.join(SECTION_FIELDS)} FROM nav_sections WHERE section_id = ?",
                              [section_id]))
    return rows[0] if rows else None


def _brief(r: dict[str, Any]) -> dict[str, Any]:
    return {**{k: r[k] for k in ("section_id", "level", "numbering", "title", "page_start_index", "page_end_index",
                                 "page_start_id", "page_end_id", "work_id")},
            "work_attribution_status": _work_status(r)}


def _work_status(row):
    from vkm_corpus.navigation.ids import RULE_VERSIONS

    if row.get("rule_version") != RULE_VERSIONS["sections"]:
        return "LEGACY_ATTRIBUTION_UNVERIFIED"
    return "NAV_RANGE_ASSIGNED_UNREVIEWED" if row.get("work_id") else "MIXED_OR_UNKNOWN"


def _covers_canonical_source(con, source_id, rows):
    """Sections that start after front matter cannot identify the whole file."""
    try:
        first, last, count = con.execute(
            "SELECT min(page_index), max(page_index), count(DISTINCT page_index) FROM canonical.pages "
            "WHERE source_id = ?", [source_id]).fetchone()
    except Exception:
        return False
    if first != 1 or not last or count != last:
        return False
    next_page = 1
    for row in sorted(rows, key=lambda row: (row["page_start_index"], row["page_end_index"])):
        if row["page_start_index"] > next_page or row["page_end_index"] > last:
            return False
        next_page = max(next_page, row["page_end_index"] + 1)
    return next_page == last + 1


def get_outline(con, source_id: str, *, max_level: int | None = None) -> dict[str, Any]:
    """Tree of the sections of a source: nested ``children``, page ranges (ids and physical indexes), method,
    confidence. ``max_level`` cuts deeper levels (their count stays in ``n_sections``)."""
    rows = _dicts(con.execute(f"SELECT {', '.join(SECTION_FIELDS)} FROM nav_sections WHERE source_id = ? "
                              "ORDER BY ordinal", [source_id]))
    nodes: dict[str, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    for r in rows:
        if max_level is not None and r["level"] > max_level:
            continue
        node = {**_brief(r), "method": r["method"], "confidence": r["confidence"],
                "heading_block_id": r["heading_block_id"], "children": []}
        nodes[r["section_id"]] = node
        parent = nodes.get(r["parent_section_id"]) if r["parent_section_id"] else None
        (parent["children"] if parent is not None else roots).append(node)
    methods = sorted({r["method"] for r in rows})
    covers_source = _covers_canonical_source(con, source_id, rows)
    work_ids = {row["work_id"] for row in rows}
    work = next(iter(work_ids)) if covers_source and len(work_ids) == 1 and None not in work_ids else None
    return {"source_id": source_id, "n_sections": len(rows), "methods": methods,
            "work_attribution_scope": "CANONICAL_SOURCE_PAGES" if covers_source else "OUTLINED_SECTIONS",
            "work_id": work, "work_attribution_status": "NAV_RANGE_ASSIGNED_UNREVIEWED" if work and
            all(_work_status(row) == "NAV_RANGE_ASSIGNED_UNREVIEWED" for row in rows) else
            ("LEGACY_ATTRIBUTION_UNVERIFIED" if any(_work_status(row) == "LEGACY_ATTRIBUTION_UNVERIFIED" for row in rows)
             else ("MIXED_OR_UNKNOWN" if covers_source else "PARTIAL_SECTION_COVERAGE")),
            "rule_version": rows[0]["rule_version"] if rows else None,
            "sections": roots}


def _path(con, row: dict[str, Any]) -> list[dict[str, Any]]:
    chain = [row]
    seen = {row["section_id"]}
    while chain[-1]["parent_section_id"]:
        parent = _section(con, chain[-1]["parent_section_id"])
        if parent is None or parent["section_id"] in seen:
            break
        seen.add(parent["section_id"])
        chain.append(parent)
    return [{k: r[k] for k in ("section_id", "level", "numbering", "title")} for r in reversed(chain)]


def _counts(con, source_id: str, first: int, last: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, table in COUNTED:
        try:
            out[key] = con.execute(f'SELECT count(*) FROM canonical."{table}" o JOIN canonical.pages p '
                                   "ON p.page_id = o.page_id WHERE p.source_id = ? AND p.page_index BETWEEN ? AND ?",
                                   [source_id, first, last]).fetchone()[0]
        except Exception:        # a canon without that table (tests, partial copies): count unknown
            out[key] = None
    return out


def get_section(con, section_id: str) -> dict[str, Any] | None:
    """A section with its path (root → section), parent, children, pages and counts of figures, tables, formulas and
    bibliography entries on its pages (the whole range, children included)."""
    row = _section(con, section_id)
    if row is None:
        return None
    children = _dicts(con.execute(f"SELECT {', '.join(SECTION_FIELDS)} FROM nav_sections "
                                  "WHERE parent_section_id = ? ORDER BY ordinal", [section_id]))
    parent = _section(con, row["parent_section_id"]) if row["parent_section_id"] else None
    first, last = row["page_start_index"], row["page_end_index"]
    try:
        pages = [r[0] for r in con.execute("SELECT page_id FROM canonical.pages WHERE source_id = ? AND page_index "
                                           "BETWEEN ? AND ? ORDER BY page_index", [row["source_id"], first, last]
                                           ).fetchall()]
    except Exception:
        pages = []
    return {**row, "work_attribution_status": _work_status(row), "path": _path(con, row), "parent": _brief(parent) if parent else None,
            "children": [_brief(c) for c in children],
            "pages": {"first_id": row["page_start_id"], "last_id": row["page_end_id"], "first_index": first,
                      "last_index": last, "n_pages": last - first + 1, "page_ids": pages[:MAX_PAGE_IDS],
                      "page_ids_truncated": len(pages) > MAX_PAGE_IDS},
            "counts": _counts(con, row["source_id"], first, last)}


def section_of_page(con, page_id: str) -> dict[str, Any] | None:
    """The deepest section(s) covering a page (two on a boundary page shared by siblings) with the path of the first;
    None when the page lies outside every section (e.g. front matter before the first outline entry)."""
    rows = _dicts(con.execute("SELECT sp.section_id FROM nav_section_pages sp JOIN nav_sections s USING (section_id) "
                              "WHERE sp.page_id = ? ORDER BY s.ordinal", [page_id]))
    if not rows:
        return None
    sections = [_section(con, r["section_id"]) for r in rows]
    return {"page_id": page_id, "sections": [{**_brief(s), "title_path": s["title_path"], "method": s["method"],
                                              "confidence": s["confidence"]} for s in sections if s],
            "path": _path(con, sections[0]) if sections[0] else []}
