"""Read side of the parameter candidates (NAV §8): pure query functions for the API and the MCP tools.

``con`` is a DuckDB connection where ``parameter_candidates`` and ``parameter_summary`` are visible as tables or views
(:func:`attach_parquet` creates views over a derived ``<snapshot>/`` directory, :func:`register_tables` registers
Arrow tables; the serving store exposes the datasets under these names). A property or a material is matched through
the vocabulary: keys, labels (Russian and English), symbols, the extractor's own phrase patterns (all case forms) and
lemma keys (the morphology of the concept graph when pymorphy3 is installed, crude stems otherwise). A material query
also matches its kinds («каменная соль» → покровная, подстилающая, междупластовая) and a group name its members
(«соляные породы»). A site query «СКРУ» matches СКРУ-1/2/3, «ВКМ» every VKM mine, «ANALOGUE» every other deposit.

Everything returned is navigation (``AUTO_EXTRACTED_UNREVIEWED``): a candidate is not evidence and a summary is not a
recommended value.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any, Mapping

from vkm_corpus.navigation import parameters_vocab as V

DATASETS = ("parameter_candidates", "parameter_summary")
DEFAULT_TABLES: dict[str, str] = {name: name for name in DATASETS}
REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
NOTE = ("DERIVED navigation layer (AUTO_EXTRACTED_UNREVIEWED): parameter candidates parsed by rules from the corpus "
        "with their locators; not evidence — check the page before use; LAB ≠ MASSIF, an analogue deposit is not "
        "СКРУ-1, a normative value is not a measurement; UNKNOWN means the context did not say.")
SUMMARY_NOTE = ("navigation aid: counts and the SI range of auto-extracted candidates per property, material and scale "
                "hint — never a recommended value")
CANDIDATE_FIELDS = ("candidate_id", "property_key", "property_label", "symbol", "material", "material_raw",
                    "value_text", "value_min", "value_max", "qualifier", "unit_raw", "unit_si", "value_si_min",
                    "value_si_max", "scale_hint", "scale_cues", "site_hint", "source_site_scope", "source_class",
                    "method", "source_id", "page_id", "page_index", "section_id", "block_id", "table_id", "table_row",
                    "table_col", "formula_id", "char_start", "char_end", "confidence", "flags")
_SCALES = frozenset(V.SCALE_HINTS)


def attach_parquet(con: Any, directory: str, *, tables: Mapping[str, str] | None = None) -> None:
    """Views over ``<directory>/parameter_candidates.parquet`` and ``parameter_summary.parquet``."""
    names = {**DEFAULT_TABLES, **(tables or {})}
    for ds, view in names.items():
        path = os.path.join(directory, ds + ".parquet").replace("'", "''")   # DDL cannot take parameters
        con.execute(f"CREATE OR REPLACE TEMP VIEW {view} AS SELECT * FROM read_parquet('{path}')")


def register_tables(con: Any, tables: Mapping[str, Any]) -> None:
    for name in DATASETS:
        if name in tables:
            con.register(name, tables[name])


def _rows(con: Any, sql: str, params: list[Any] | tuple = ()) -> list[dict[str, Any]]:
    cur = con.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower().replace("ё", "е"))


# --------------------------------------------------------------------------------------------------- lemma keys
@lru_cache(maxsize=1)
def _morph() -> Any:
    try:
        from vkm_corpus.navigation.concepts import Morphology  # noqa: PLC0415 - heavy optional import

        return Morphology()
    except Exception:  # noqa: BLE001 - numpy/pyarrow/pymorphy3 missing: crude stems
        return None


def _crude(text: str) -> str:
    words = re.findall(r"[^\W\d_]{2,}", _norm(text))
    return " ".join(w if len(w) <= 4 else w[: max(4, len(w) - 2)] for w in words)


@lru_cache(maxsize=4096)
def lemma_key(text: str) -> str:
    """Lemma key of a phrase: the key spanning all its words (the concept graph's rule), or crude stems."""
    morph = _morph()
    if morph is not None:
        try:
            from vkm_corpus.navigation.concepts import phrase_keys  # noqa: PLC0415

            keys = phrase_keys(text, morph)
            if keys:
                return keys[0]
        except Exception:  # noqa: BLE001
            pass
    return _crude(text)


@lru_cache(maxsize=1)
def _property_labels() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in V.PROPERTIES:
        out[p.key] = [p.key, p.key.replace("_", " "), p.label_ru, p.label_en, *p.synonyms,
                      re.sub(r"\s*\(.*?\)", "", p.label_ru), re.sub(r"\s*\(.*?\)", "", p.label_en)]
    return out


def resolve_property(text: str | None) -> list[str]:
    """Property keys a query names: a key, a label, a symbol, the extractor's phrase patterns, lemma keys."""
    q = (text or "").strip()
    if not q:
        return []
    nq = _norm(q)
    if nq in V.PROPERTY_BY_KEY:
        return [nq]
    labels = _property_labels()
    exact = [k for k, ls in labels.items() if nq in {_norm(x) for x in ls}]
    if exact:
        return exact
    from vkm_corpus.navigation.parameters import find_mentions, lower_same_length, symbolish  # noqa: PLC0415

    found = list(dict.fromkeys(m.prop.key for m in find_mentions(q, lower_same_length(q))))
    if found:
        return found
    sym = symbolish(q, assigned=True)
    if sym:
        from vkm_corpus.navigation.formulas import symbol_key  # noqa: PLC0415

        key = symbol_key(sym, loose=True)
        by_sym = [p.key for p in V.PROPERTIES if any(symbol_key(s, loose=True) == key for s in p.symbols)]
        if by_sym:
            return by_sym
    lk = lemma_key(q)
    words = set(lk.split())
    out = []
    for k, ls in labels.items():
        for lab in ls[2:]:
            ll = lemma_key(lab)
            if ll == lk or (words and words <= set(ll.split())):
                out.append(k)
                break
    return out


def resolve_material(text: str | None) -> list[str]:
    """Normalised material labels a query names, with the kinds of a material and the members of a group."""
    q = (text or "").strip()
    if not q:
        return []
    nq = _norm(q)
    if nq in {"unknown", "неизвестно"}:
        return [V.UNKNOWN]
    labels = {m.label: [m.label, *m.synonyms] for m in V.MATERIALS}
    hit = [lab for lab, ls in labels.items() if nq in {_norm(x) for x in ls}]
    groups = [g for g in V.MATERIAL_GROUPS if _norm(g) == nq]
    if not hit:
        from vkm_corpus.navigation.parameters import find_materials, lower_same_length  # noqa: PLC0415

        hit = list(dict.fromkeys(t.label for t in find_materials(q, lower_same_length(q))))
    if not hit and not groups:
        lk = lemma_key(q)
        hit = [lab for lab, ls in labels.items() if any(lemma_key(x) == lk for x in ls)]
    out = list(hit)
    for lab in hit:                                   # kinds: «каменная соль» → «покровная каменная соль», …
        out += [m.label for m in V.MATERIALS if m.label != lab and m.label.endswith(" " + lab)]
    for g in groups:
        out += [m.label for m in V.MATERIALS if m.group == g]
    return list(dict.fromkeys(out))


def _site_labels(con: Any, table: str, site: str) -> list[str]:
    q = site.strip()
    present = [r["site_hint"] for r in _rows(con, f"SELECT DISTINCT site_hint FROM {table}")]
    nq = _norm(q).replace("–", "-").replace(" ", "")
    if nq in ("analogue", "аналог"):
        return [s for s in present if s.startswith("ANALOGUE:")]
    if nq == "вкм":
        return [s for s in present if s in V.VKM_FAMILY]
    out = [s for s in present if _norm(s).replace(" ", "") == nq]
    if out:
        return out + ([s for s in present if _norm(s).startswith(nq + "-")] if nq in ("скру", "бкпру") else [])
    return [s for s in present if nq and nq in _norm(s).replace(" ", "")]


def _clean(r: dict[str, Any]) -> dict[str, Any]:
    out = {k: r.get(k) for k in CANDIDATE_FIELDS if k in r}
    out["review_status"] = REVIEW_STATUS
    return out


def find_parameters(con: Any, property: str | None = None, material: str | None = None,  # noqa: A002
                    site: str | None = None, scale: str | None = None, source_id: str | None = None,
                    limit: int = 50, *, tables: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Parameter candidates by property («модуль деформации», «E», «ucs»), material («каменной соли», «соляные
    породы»), site («СКРУ-1», «ВКМ», «ANALOGUE»), scale hint (LAB/MASSIF/NORMATIVE/MODEL/UNKNOWN) and source;
    ordered by confidence. The answer says how the query was resolved; an unresolved filter returns nothing."""
    t = {**DEFAULT_TABLES, **(tables or {})}["parameter_candidates"]
    query: dict[str, Any] = {"property": property, "material": material, "site": site, "scale": scale,
                             "source_id": source_id}
    where: list[str] = []
    params: list[Any] = []
    unresolved: list[str] = []
    if property:
        keys = resolve_property(property)
        query["property_keys"] = keys
        if not keys:
            unresolved.append("property")
        where.append(f"property_key IN ({', '.join('?' for _ in keys) or 'NULL'})")
        params += keys
    if material:
        mats = resolve_material(material)
        query["materials"] = mats
        if not mats:
            unresolved.append("material")
        where.append(f"material IN ({', '.join('?' for _ in mats) or 'NULL'})")
        params += mats
    if site:
        sites = _site_labels(con, t, site)
        query["sites"] = sites
        if not sites:
            unresolved.append("site")
        where.append(f"site_hint IN ({', '.join('?' for _ in sites) or 'NULL'})")
        params += sites
    if scale:
        sc = scale.strip().upper()
        if sc not in _SCALES:
            unresolved.append("scale")
        where.append("scale_hint = ?")
        params.append(sc)
    if source_id:
        where.append("source_id = ?")
        params.append(source_id)
    cond = ("WHERE " + " AND ".join(where)) if where else ""
    total = _rows(con, f"SELECT count(*) AS n FROM {t} {cond}", params)[0]["n"]
    rows = _rows(con, f"SELECT * FROM {t} {cond} ORDER BY confidence DESC, source_id, page_index, candidate_id "
                      f"LIMIT {max(1, int(limit))}", params)
    return {"query": query, "unresolved": unresolved, "total": int(total), "candidates": [_clean(r) for r in rows],
            "note": NOTE}


def parameter_summary(con: Any, property: str, material: str | None = None,  # noqa: A002
                      *, tables: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Per material and scale hint: counts of candidates, sources, pages and the SI range of a property — a
    navigation aid, never a recommended value."""
    t = {**DEFAULT_TABLES, **(tables or {})}["parameter_summary"]
    keys = resolve_property(property)
    out: dict[str, Any] = {"query": {"property": property, "property_keys": keys, "material": material},
                           "rows": [], "note": SUMMARY_NOTE}
    if not keys:
        out["unresolved"] = ["property"]
        return out
    where = [f"property_key IN ({', '.join('?' for _ in keys)})"]
    params: list[Any] = list(keys)
    if material:
        mats = resolve_material(material)
        out["query"]["materials"] = mats
        if not mats:
            out["unresolved"] = ["material"]
            return out
        where.append(f"material IN ({', '.join('?' for _ in mats)})")
        params += mats
    out["rows"] = _rows(con, f"""
        SELECT property_key, property_label, material, scale_hint, unit_si, n_candidates, n_sources, n_pages,
               n_without_si, value_si_min, value_si_max, site_hints, methods, source_ids
        FROM {t} WHERE {' AND '.join(where)}
        ORDER BY property_key, n_candidates DESC, material, scale_hint""", params)
    return out
