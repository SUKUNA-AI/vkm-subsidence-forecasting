"""Read functions over the object-duplicates datasets (NAV §9, part ``object_duplicates``) — the future MCP tools
``copies_of_object`` / ``shared_formulas`` (pure: a DuckDB connection in, plain dicts out; no writes).

The connection must expose ``nav_object_dup_clusters``, ``nav_object_dup_members`` and ``nav_formula_keys``
(``attach`` creates temporary views over the Parquet files of ``vkm-corpus nav build``; ``nav build`` registers the
Arrow tables under the same names; ``NavStore`` serves them from ``nav.duckdb``). The layer is DERIVED navigation
(``AUTO_EXTRACTED_UNREVIEWED``): «primary» is the earliest source by publication year — a hint for ordering copies,
not a claim of authorship or priority; a formula group says where the same written form occurs, not that a law holds.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from vkm_corpus.navigation.object_duplicates import NOTE, canonical_formula

DATASETS = ("object_dup_clusters", "object_dup_members", "formula_keys")
MEMBER_FIELDS = ("object_id", "object_type", "source_id", "work_id", "year", "page_id", "page_index", "label", "match",
                 "similarity", "image_distance", "dhash_distance", "transform", "caption_similarity",
                 "cell_containment", "number_containment", "header_similarity", "equation_number", "section_id",
                 "context", "is_primary", "is_reference")
CLUSTER_FIELDS = ("cluster_id", "object_type", "kind", "match_basis", "n_members", "n_sources", "n_works", "n_groups",
                  "primary_source_id", "primary_object_id", "primary_work_id", "primary_year", "primary_rule",
                  "reference_source_id", "reference_object_id", "label", "rule_version")
KEY_FIELDS = ("formula_id", "source_id", "work_id", "year", "page_id", "page_index", "formula_kind", "equation_number",
              "section_id", "key_hash", "shape_hash", "trivial", "distinctive")


def _lit(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def attach(con: Any, nav_dir: str | Path) -> None:
    """Temporary views ``nav_<dataset>`` over ``<nav_dir>/<dataset>.parquet`` (read-only safe)."""
    d = Path(nav_dir)
    for name in DATASETS + ("figure_hashes",):
        if (d / (name + ".parquet")).is_file():
            con.execute(f"CREATE OR REPLACE TEMP VIEW nav_{name} AS SELECT * FROM read_parquet({_lit(d / (name + '.parquet'))})")


def _dicts(con: Any, sql: str, params: list[Any] | dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, params or [])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _order(m: dict[str, Any]) -> tuple:
    return (not m["is_primary"], not m["is_reference"], m["year"] is None, m["year"] or 0, m["source_id"],
            m["page_index"] if m["page_index"] is not None else 0, m["object_id"])


def copies_of_object(con: Any, object_id: str, *, limit: int = 50) -> dict[str, Any]:
    """Where else a figure, table or formula (by its object id) appears.

    For every cluster of the object: type, kind, primary source (with the rule that chose it), the object itself
    (``this``) and the other members (``copies``: primary first, then by year, source, page) with their evidence
    against the reference object (image distance, caption similarity, cell containment, formula match)."""
    fields = ", ".join(("cluster_id",) + MEMBER_FIELDS)
    own = _dicts(con, f"SELECT {fields} FROM nav_object_dup_members WHERE object_id = ? ORDER BY cluster_id",
                 [object_id])
    out: dict[str, Any] = {"query": object_id, "object_type": own[0]["object_type"] if own else None,
                           "clusters": [], "note": NOTE}
    if not own:
        return out
    cids = sorted({m["cluster_id"] for m in own})
    ph = ", ".join("?" for _ in cids)
    clusters = {c["cluster_id"]: c for c in _dicts(
        con, f"SELECT {', '.join(CLUSTER_FIELDS)} FROM nav_object_dup_clusters WHERE cluster_id IN ({ph})", cids)}
    members = _dicts(con, f"SELECT {fields} FROM nav_object_dup_members WHERE cluster_id IN ({ph})", cids)
    by_cluster: dict[str, list[dict[str, Any]]] = {}
    for m in members:
        by_cluster.setdefault(m.pop("cluster_id"), []).append(m)
    lim = max(1, int(limit))
    for cid in cids:
        ms = sorted(by_cluster.get(cid, []), key=_order)
        this = [m for m in ms if m["object_id"] == object_id]
        copies = [m for m in ms if m["object_id"] != object_id]
        c = dict(clusters.get(cid, {"cluster_id": cid}))
        c["this"] = this
        c["copies"] = copies[:lim]
        c["copies_truncated"] = len(copies) > lim
        c["this_is_primary"] = any(m["is_primary"] for m in this)
        out["clusters"].append(c)
    out["clusters"].sort(key=lambda c: (-(c.get("n_sources") or 0), c["cluster_id"]))
    return out


def _has_view(con: Any, name: str) -> bool:
    try:
        con.execute(f"SELECT 1 FROM {name} LIMIT 0")
        return True
    except Exception:  # noqa: BLE001 — the dataset is not part of this build
        return False


def shared_formulas(con: Any, ref: str, *, renamed: bool = True, limit: int = 50) -> dict[str, Any]:
    """Where the same formula is written: ``ref`` is a formula id or a LaTeX string.

    ``exact`` — formulas with the same canonical form (``latex_key``); ``renamed`` — distinctive formulas of the same
    structure in another notation (``shape_key``; only when the query is distinctive and ``renamed``). Both are
    grouped by work (earliest year first) with sources, pages, printed equation numbers and sections; ``clusters``
    names the object-duplicate groups of these formulas. Trivial forms (no relation, notation, a single symbol) are
    looked up too but never grouped: ``trivial`` says so."""
    fields = ", ".join(KEY_FIELDS)
    rows = _dicts(con, f"SELECT {fields}, latex_key FROM nav_formula_keys WHERE formula_id = ?", [ref])
    if rows:
        q = rows[0]
        match, key, shape = "formula_id", q["key_hash"], q["shape_hash"]
        latex_key, trivial, distinctive = q["latex_key"], q["trivial"], q["distinctive"]
    else:
        c = canonical_formula(ref)
        match, key, shape = "latex", c["key_hash"], c["shape_hash"]
        latex_key, trivial, distinctive = c["latex_key"], c["trivial"], c["distinctive"]
    out: dict[str, Any] = {"query": ref, "match": match, "latex_key": latex_key, "trivial": bool(trivial),
                           "distinctive": bool(distinctive), "exact": [], "renamed": [], "clusters": [],
                           "n_exact": 0, "n_renamed": 0, "note": NOTE}
    if not latex_key:
        return out
    exact = _dicts(con, f"SELECT {fields} FROM nav_formula_keys WHERE key_hash = ?", [key])
    ren: list[dict[str, Any]] = []
    if renamed and distinctive:
        ren = _dicts(con, f"SELECT {fields} FROM nav_formula_keys WHERE shape_hash = ? AND key_hash <> ?",
                     [shape, key])
    lim = max(1, int(limit))
    for name, found in (("exact", exact), ("renamed", ren)):
        out["n_" + name] = len(found)
        by_work: dict[str, dict[str, Any]] = {}
        for r in sorted(found, key=lambda r: (r["year"] is None, r["year"] or 0, r["work_id"] or "",
                                              r["source_id"], r["page_index"] or 0, r["formula_id"])):
            w = r["work_id"] or f"(source){r['source_id']}"
            g = by_work.setdefault(w, {"work_id": r["work_id"], "year": r["year"], "sources": [], "formulas": []})
            if r["source_id"] not in g["sources"]:
                g["sources"].append(r["source_id"])
            g["formulas"].append({k: r[k] for k in ("formula_id", "source_id", "page_id", "page_index",
                                                    "formula_kind", "equation_number", "section_id")}
                                 | {"this": r["formula_id"] == ref})
        works = list(by_work.values())
        out[name] = works[:lim]
        out[name + "_truncated"] = len(works) > lim
    ids = [r["formula_id"] for r in exact + ren]
    if ids and _has_view(con, "nav_object_dup_members"):
        ph = ", ".join("?" for _ in ids)
        out["clusters"] = _dicts(con, f"""
            SELECT DISTINCT c.cluster_id, c.kind, c.match_basis, c.n_members, c.n_sources, c.n_works,
                   c.primary_source_id, c.primary_object_id, c.primary_year, c.primary_rule
            FROM nav_object_dup_clusters c JOIN nav_object_dup_members m ON m.cluster_id = c.cluster_id
            WHERE m.object_id IN ({ph}) ORDER BY c.n_works DESC, c.cluster_id""", ids)[:lim]
    out["n_works"] = len({r["work_id"] or r["source_id"] for r in exact + ren})
    return out
