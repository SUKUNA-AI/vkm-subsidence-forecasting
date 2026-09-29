"""Read functions over the duplicates datasets (NAV §9) — the future MCP tools ``copies_of`` / ``source_overlap``
(pure: a DuckDB connection in, plain dicts out; no writes, no corpus text).

The connection must expose ``nav_dup_clusters``, ``nav_dup_members`` and ``nav_source_overlap`` (``attach`` creates
temporary views over the Parquet files of ``vkm-corpus nav build``; ``nav build`` registers the Arrow tables under the
same names; ``NavStore`` serves them from ``nav.duckdb``). The layer is DERIVED navigation
(``AUTO_EXTRACTED_UNREVIEWED``): «primary» is the earliest source by publication year — a hint for ordering copies,
not a claim of authorship or priority.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from vkm_corpus.navigation.duplicates import NOTE

DATASETS = ("dup_clusters", "dup_members", "source_overlap")
MEMBER_FIELDS = ("unit_id", "source_id", "work_id", "year", "page_id", "page_index", "similarity", "containment",
                 "cosine", "is_primary", "is_reference")
CLUSTER_FIELDS = ("cluster_id", "kind", "n_members", "n_sources", "n_works", "primary_source_id", "primary_work_id",
                  "primary_year", "primary_rule", "reference_source_id", "rule_version")


def _lit(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def attach(con: Any, nav_dir: str | Path) -> None:
    """Temporary views ``nav_<dataset>`` over ``<nav_dir>/<dataset>.parquet`` (read-only safe)."""
    d = Path(nav_dir)
    for name in DATASETS:
        path = _lit(d / (name + ".parquet"))
        con.execute(f"CREATE OR REPLACE TEMP VIEW nav_{name} AS SELECT * FROM read_parquet({path})")


def _dicts(con: Any, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, params or [])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _resolve(con: Any, ref: str) -> tuple[str | None, list[dict[str, Any]]]:
    """Members matching ``ref`` as a unit id, else a page id, else a block id."""
    fields = ", ".join(("cluster_id",) + MEMBER_FIELDS)
    for match, where in (("unit", "unit_id = ?"), ("page", "page_id = ?"), ("block", "list_contains(block_ids, ?)")):
        rows = _dicts(con, f"SELECT {fields} FROM nav_dup_members WHERE {where} ORDER BY unit_id", [ref])
        if rows:
            return match, rows
    return None, []


def _order(m: dict[str, Any]) -> tuple:
    return (not m["is_primary"], not m["is_reference"], m["year"] is None, m["year"] or 0, m["source_id"],
            m["page_index"] if m["page_index"] is not None else 0, m["unit_id"])


def copies_of(con: Any, ref: str, *, limit: int = 50) -> dict[str, Any]:
    """Where else the text of a unit (``u1-…``), a page or a block appears.

    For every cluster of the matched units: kind, primary source (with the rule that chose it), the matched units
    (``this``) and the other members (``copies``: primary first, then by year, source, page). ``pages`` aggregates the
    other pages over all clusters (how many clusters each shares with the query, whether it holds a primary).
    """
    match, own = _resolve(con, ref)
    out: dict[str, Any] = {"query": ref, "match": match, "clusters": [], "pages": [], "note": NOTE}
    if not own:
        return out
    own_ids = {m["unit_id"] for m in own}
    cids = sorted({m["cluster_id"] for m in own})
    ph = ", ".join("?" for _ in cids)
    clusters = {c["cluster_id"]: c for c in _dicts(
        con, f"SELECT {', '.join(CLUSTER_FIELDS)} FROM nav_dup_clusters WHERE cluster_id IN ({ph})", cids)}
    members = _dicts(con, f"SELECT cluster_id, {', '.join(MEMBER_FIELDS)} FROM nav_dup_members "
                          f"WHERE cluster_id IN ({ph})", cids)
    by_cluster: dict[str, list[dict[str, Any]]] = {}
    for m in members:
        by_cluster.setdefault(m.pop("cluster_id"), []).append(m)
    pages: dict[str, dict[str, Any]] = {}
    lim = max(1, int(limit))
    for cid in cids:
        ms = sorted(by_cluster.get(cid, []), key=_order)
        this = [m for m in ms if m["unit_id"] in own_ids]
        copies = [m for m in ms if m["unit_id"] not in own_ids]
        c = dict(clusters.get(cid, {"cluster_id": cid}))
        c["this"] = this
        c["copies"] = copies[:lim]
        c["copies_truncated"] = len(copies) > lim
        c["this_is_primary"] = any(m["is_primary"] for m in this)
        out["clusters"].append(c)
        own_pages = {m["page_id"] for m in this}
        for m in copies:
            if m["page_id"] in own_pages:
                continue
            pg = pages.setdefault(m["page_id"], {"page_id": m["page_id"], "source_id": m["source_id"],
                                                 "year": m["year"], "page_index": m["page_index"], "n_clusters": 0,
                                                 "has_primary": False, "kinds": set()})
            pg["n_clusters"] += 1
            pg["has_primary"] = pg["has_primary"] or bool(m["is_primary"])
            pg["kinds"].add(c.get("kind"))
    out["clusters"].sort(key=lambda c: (-(c.get("n_sources") or 0), c["cluster_id"]))
    ordered = sorted(pages.values(), key=lambda p: (not p["has_primary"], -p["n_clusters"], p["year"] is None,
                                                   p["year"] or 0, p["source_id"], p["page_index"] or 0))
    out["pages"] = [{**p, "kinds": sorted(k for k in p["kinds"] if k)} for p in ordered[:lim]]
    return out


def source_overlap(con: Any, source_id: str, *, min_shared: int = 1, include_boilerplate: bool = True,
                   limit: int = 50) -> dict[str, Any]:
    """Sources that share passages with ``source_id``: shared clusters (all / without template text), matched units
    and their shares on both sides, work identity, years and the dominant relation (kind). Oriented: ``this_*`` is
    ``source_id``, ``other_*`` the partner; ordered by shared passages."""
    rows = _dicts(con, """
        SELECT CASE WHEN source_a = $s THEN source_b ELSE source_a END AS other_source_id,
               CASE WHEN source_a = $s THEN work_b ELSE work_a END AS other_work_id,
               same_work, n_shared_units, n_shared_content, relation,
               CASE WHEN source_a = $s THEN n_units_a ELSE n_units_b END AS units_this,
               CASE WHEN source_a = $s THEN n_units_b ELSE n_units_a END AS units_other,
               CASE WHEN source_a = $s THEN share_of_a ELSE share_of_b END AS share_of_this,
               CASE WHEN source_a = $s THEN share_of_b ELSE share_of_a END AS share_of_other,
               CASE WHEN source_a = $s THEN year_a ELSE year_b END AS year_this,
               CASE WHEN source_a = $s THEN year_b ELSE year_a END AS year_other
        FROM nav_source_overlap
        WHERE (source_a = $s OR source_b = $s) AND n_shared_units >= $m
        ORDER BY n_shared_content DESC, n_shared_units DESC, other_source_id""",
                 {"s": source_id, "m": max(1, int(min_shared))})
    if not include_boilerplate:
        rows = [r for r in rows if r["relation"] != "BOILERPLATE"]
    for r in rows:
        yt, yo = r["year_this"], r["year_other"]
        r["earlier"] = None if yt is None or yo is None or yt == yo else ("this" if yt < yo else "other")
    lim = max(1, int(limit))
    return {"source_id": source_id, "n_overlaps": len(rows), "overlaps": rows[:lim],
            "overlaps_truncated": len(rows) > lim, "note": NOTE}
