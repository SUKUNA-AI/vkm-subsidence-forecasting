"""Copies of one work in the NAV lists and summaries (agent Б, 13.10): the parts count files (sources); a work
registered as two files (VKM-WRK-013 = VKM-SRC-025 + VKM-SRC-202) is one publication, and its copies are not
independent evidence.

Pure functions over the answers of ``parameters_query`` and ``tables_query`` and a map ``work_of`` (source → the work
it is an instance of, the canon's primary ``work_sources`` link; the API passes it) — no NAV rebuild:

* :func:`summary_by_work` — every row of ``parameter_summary`` gains ``n_works`` (distinct works of its
  ``source_ids``; a source without a known work counts as its own), ``work_ids`` and ``copies`` (work → its sources
  in the row, only works with two or more); ``n_candidates`` / ``n_pages`` still count every file;
* :func:`merge_copies` — a list (``find_parameters`` candidates, ``find_tables`` tables) keeps one position per object
  of a work: an item from another source of the same work with the same copy key joins the first one as ``copies``
  (its id, source and page). Keys: :func:`parameter_keys` — the same property, material, scale hint, unit and value
  (SI range, else the printed value) on the same page index; :func:`table_keys` — the same printed table number on the
  same page index, or the same NAV ``object_dup_clusters`` group. Copies whose pages are shifted merge only through
  the NAV group (conservative: an unaligned candidate stays a position of its own).

Navigation only (``AUTO_EXTRACTED_UNREVIEWED``): a merged position says «the same printed object in two files of one
work», not that a value was checked.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Hashable, Iterable, Mapping

NOTE = ("copies of one work merged: one position per printed object of a work (copies = the same object in another "
        "file of the work); n_works counts works, n_sources files — copies are not independent evidence")


def _works(source_ids: Iterable[str], work_of: Mapping[str, str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for s in source_ids:
        if s:
            out.setdefault(work_of.get(s) or s, []).append(s)
    return out


def summary_by_work(rows: list[dict[str, Any]], work_of: Mapping[str, str]) -> list[dict[str, Any]]:
    """``parameter_summary`` rows + ``n_works``, ``work_ids``, ``copies`` (see the module note)."""
    out = []
    for r in rows:
        works = _works(r.get("source_ids") or [], work_of)
        out.append({**r, "n_works": len(works),
                    "work_ids": sorted(w for w in works if not w.startswith("VKM-SRC-")),
                    "copies": {w: sorted(s) for w, s in sorted(works.items()) if len(s) > 1}})
    return out


def _num(v: Any) -> Any:
    return None if v is None else float(f"{float(v):.6g}")


def _norm_value(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "").lower().replace(",", ".").replace("ё", "е"))


def parameter_keys(c: Mapping[str, Any], work: str) -> list[Hashable]:
    value = ((_num(c.get("value_si_min")), _num(c.get("value_si_max")))
             if c.get("value_si_min") is not None or c.get("value_si_max") is not None
             else _norm_value(c.get("value_text")))
    if c.get("page_index") is None or value in ("", (None, None)):
        return []
    return [(work, c.get("property_key"), c.get("material"), c.get("scale_hint"), c.get("unit_si") or
             c.get("unit_raw"), value, int(c["page_index"]))]


def table_keys(t: Mapping[str, Any], work: str, cluster_of: Mapping[str, str] | None = None) -> list[Hashable]:
    keys: list[Hashable] = []
    number = re.sub(r"\s+", "", str(t.get("table_number") or "").lower())
    if number and t.get("page_index") is not None:
        keys.append((work, "number", number, int(t["page_index"])))
    cluster = (cluster_of or {}).get(str(t.get("table_id")))
    if cluster:
        keys.append((work, "nav_group", cluster))
    return keys


def merge_copies(items: list[dict[str, Any]], work_of: Mapping[str, str],
                 keys_of: Callable[[Mapping[str, Any], str], list[Hashable]], *, id_field: str,
                 limit: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One position per object of a work, in the order of ``items`` (see the module note). Items gain ``work_id``;
    a kept item that has copies gains ``copies`` = [{id_field, source_id, page_id, page_index}]. Two items of one
    source never merge. → (kept items, cut to ``limit``; {merged, n_works, n_sources} over the kept ones)."""
    kept: list[dict[str, Any]] = []
    rep_of: dict[Hashable, int] = {}
    sources: list[set[str]] = []
    for raw in items:
        it = dict(raw)
        src = str(it.get("source_id") or "")
        work = work_of.get(src)
        if work:
            it["work_id"] = work
        keys = keys_of(it, work) if work else []
        rep = next((rep_of[k] for k in keys if k in rep_of and src not in sources[rep_of[k]]), None)
        if rep is not None:
            kept[rep].setdefault("copies", []).append({id_field: it.get(id_field), "source_id": src or None,
                                                       "page_id": it.get("page_id"),
                                                       "page_index": it.get("page_index")})
            sources[rep].add(src)
            for k in keys:
                rep_of.setdefault(k, rep)
            continue
        for k in keys:
            rep_of.setdefault(k, len(kept))
        kept.append(it)
        sources.append({src})
    if limit is not None:
        kept = kept[:max(0, int(limit))]
    merged = sum(len(it.get("copies") or []) for it in kept)
    stats = {"merged": merged, "n_works": len(_works((str(it.get("source_id") or "") for it in kept), work_of)),
             "n_sources": len({str(it.get("source_id") or "") for it in kept} |
                              {str(c.get("source_id") or "") for it in kept for c in it.get("copies") or []}),
             "note": NOTE}
    return kept, stats
