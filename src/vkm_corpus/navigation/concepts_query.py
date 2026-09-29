"""Pure query functions over the concept graph (NAV §4, §5) for the MCP tool ``explore_concept`` and the API.

The functions read the ``terms``, ``term_mentions`` and ``term_edges`` datasets through a DuckDB connection (views or
tables; names are configurable with ``tables=``) and never write. A query phrase is lemmatised with the same
morphology as the builder (``concepts.phrase_keys``), so «ползучести соли» finds «ползучесть соли». Answers are
compact dicts of IDs and counts (no corpus text unless ``snippets=True`` and a ``blocks`` table is available):
neighbours are navigation hints — «discussed together in N units of M sources» — not physical claims.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from vkm_corpus.navigation.concepts import Morphology, phrase_keys

DEFAULT_TABLES: dict[str, str] = {"terms": "terms", "term_mentions": "term_mentions", "term_edges": "term_edges"}
NOTE = ("DERIVED navigation layer (AUTO_EXTRACTED_UNREVIEWED): CO_OCCURS means the terms are discussed in the same "
        "units (sections or heading groups) of several sources; it is not a physical or causal claim.")
_TERM_ID = re.compile(r"^TRM-[0-9a-f]{16}$")
_MORPH: Morphology | None = None


def _morph() -> Morphology:
    global _MORPH  # noqa: PLW0603 — one analyser per process (dictionary load is slow)
    if _MORPH is None:
        _MORPH = Morphology()
    return _MORPH


def attach(con: Any, source: str | Path | Mapping[str, Any], *, tables: Mapping[str, str] | None = None) -> None:
    """Register the three datasets as views: from a directory of ``<name>.parquet`` files or from Arrow tables."""
    names = {**DEFAULT_TABLES, **(tables or {})}
    for ds, view in names.items():
        if isinstance(source, Mapping):
            con.register(f"_nav_{ds}", source[ds])
            con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM _nav_{ds}")
        else:
            path = str(Path(source) / f"{ds}.parquet").replace("'", "''")   # DDL cannot take parameters
            con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{path}')")


def _rows(con: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def find_terms(con: Any, query: str, *, limit: int = 10, tables: Mapping[str, str] | None = None,
               morph: Morphology | None = None) -> list[dict[str, Any]]:
    """Terms matching a phrase: exact lemma key (the whole phrase first), SAME_AS equivalents, then containment."""
    t = {**DEFAULT_TABLES, **(tables or {})}
    q = (query or "").strip()
    if not q:
        return []
    cols = "term_id, lemma, lemma_key, language, kind, df_units, df_sources, tf, seed, community, surface_forms"
    if _TERM_ID.match(q):
        return _rows(con, f"SELECT {cols}, 'term_id' AS match FROM {t['terms']} WHERE term_id = ?", [q])
    keys = phrase_keys(q, morph or _morph())
    if not keys:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(rows: list[dict[str, Any]]) -> None:
        for r in rows:
            if r["term_id"] not in seen and len(out) < limit:
                seen.add(r["term_id"])
                out.append(r)

    exact = _rows(con, f"SELECT {cols}, 'lemma_key' AS match FROM {t['terms']} WHERE lemma_key IN "
                       f"(SELECT unnest(?::VARCHAR[]))", [keys])
    rank = {k: i for i, k in enumerate(keys)}
    add(sorted(exact, key=lambda r: (rank.get(r["lemma_key"], 99), -r["df_units"])))
    if keys[0] not in {r["lemma_key"] for r in exact}:
        # the whole phrase is not a term: its SAME_AS equivalent (e.g. «creep» printed next to «ползучесть»)
        add(_rows(con, f"""
            SELECT {', '.join('x.' + c.strip() for c in cols.split(','))}, 'same_as' AS match
            FROM {t['term_edges']} e JOIN {t['terms']} x ON x.term_id = e.src_term_id
            WHERE e.kind = 'SAME_AS' AND e.dst_term_id IS NULL AND split_part(e.dst_ref, ':', 2) = ?
            ORDER BY e.weight DESC, x.df_units DESC""", [keys[0]]))
        toks = keys[0].split()
        add(_rows(con, f"""
            SELECT {cols}, 'contains' AS match FROM {t['terms']}
            WHERE list_has_all(string_split(lemma_key, ' '), ?::VARCHAR[])
            ORDER BY len(string_split(lemma_key, ' ')), df_units DESC, term_id LIMIT ?""", [toks, limit]))
    return out


def _nested(a: str, b: str) -> bool:
    return f" {a} " in f" {b} " or f" {b} " in f" {a} "


def neighbours(con: Any, term_id: str, *, limit: int = 20, tables: Mapping[str, str] | None = None,
               diversify: bool = True) -> list[dict[str, Any]]:
    """``CO_OCCURS`` neighbours ranked by support-weighted NPMI, ``score = npmi · n/(n + 2)`` over shared units
    (the builder keeps its top-k by the same score). ``diversify`` skips a neighbour that lexically contains, or is
    contained in, a better-ranked one («synthetic aperture» after «synthetic aperture radar»)."""
    t = {**DEFAULT_TABLES, **(tables or {})}
    lim = max(1, int(limit))
    rows = _rows(con, f"""
        SELECT CASE WHEN e.src_term_id = $t THEN e.dst_term_id ELSE e.src_term_id END AS term_id, x.lemma,
               x.lemma_key, e.weight AS npmi, round(e.weight * e.n_units / (e.n_units + 2.0), 6) AS score,
               e.n_units, e.n_sources, e.examples AS example_page_ids
        FROM {t['term_edges']} e
        JOIN {t['terms']} x ON x.term_id = CASE WHEN e.src_term_id = $t THEN e.dst_term_id ELSE e.src_term_id END
        WHERE e.kind = 'CO_OCCURS' AND (e.src_term_id = $t OR e.dst_term_id = $t)
        ORDER BY score DESC, e.n_units DESC, term_id LIMIT $lim""", {"t": term_id, "lim": lim * 4 if diversify else lim})
    out: list[dict[str, Any]] = []
    for r in rows:
        if diversify and any(_nested(r["lemma_key"], o["lemma_key"]) or r["lemma"] == o["lemma"] for o in out):
            continue
        out.append(r)
        if len(out) >= lim:
            break
    for r in out:
        r.pop("lemma_key", None)
    return out


def explore_concept(con: Any, term: str, *, limit: int = 20, tables: Mapping[str, str] | None = None,
                    morph: Morphology | None = None, snippets: bool = False, blocks_table: str = "blocks",
                    min_df_focus: int = 10, diversify: bool = True) -> dict[str, Any]:
    """A concept with its definitions, strongest neighbours (counts and example pages), top units and sources.

    ``term`` is a phrase in any inflected form or a ``TRM-`` id. Neighbours are ``CO_OCCURS`` edges ranked by NPMI
    (ties: shared units); ``broader``/``narrower`` come from lexical containment, ``same_as`` from translations and
    abbreviations printed in parentheses. When the exact phrase is rare (``df_units < min_df_focus``, e.g.
    «ползучесть соли») the neighbours are those of its head term («ползучесть»), named in ``focus``; the rare
    phrase's own neighbours are kept in ``neighbours_exact``. With ``snippets=True`` definitions carry the first
    300 characters of the block from ``blocks_table`` (a canonical DuckDB), otherwise only block/page ids.
    """
    t = {**DEFAULT_TABLES, **(tables or {})}
    found = find_terms(con, term, limit=6, tables=t, morph=morph)
    if not found:
        return {"query": term, "match": None, "alternatives": [], "note": NOTE}
    m = found[0]
    tid = m["term_id"]
    lim = max(1, int(limit))
    focus = m
    if m["df_units"] < min_df_focus and m["kind"] == "NP" and " " in m["lemma_key"]:
        heads = _rows(con, f"""
            SELECT x.term_id, x.lemma, x.lemma_key, x.df_units, x.n_words FROM {t['term_edges']} e
            JOIN {t['terms']} x ON x.term_id = e.dst_term_id WHERE e.kind = 'CONTAINS' AND e.src_term_id = ?""", [tid])
        key = m["lemma_key"]
        heads = [h for h in heads if h["df_units"] >= 3 * max(1, m["df_units"]) and (
            key.startswith(h["lemma_key"] + " ") if m["language"] == "ru" else key.endswith(" " + h["lemma_key"]))]
        if heads:
            focus = max(heads, key=lambda h: (h["n_words"], h["df_units"]))
    near = neighbours(con, focus["term_id"], limit=lim, tables=t, diversify=diversify)
    near_exact = neighbours(con, tid, limit=5, tables=t, diversify=diversify) if focus is not m else None
    definitions = _rows(con, f"""
        SELECT e.dst_ref AS block_id, e.examples[1] AS page_id, split_part(e.dst_ref, ':', 1) AS source_id, e.rule
        FROM {t['term_edges']} e WHERE e.kind = 'DEFINED_AS' AND e.src_term_id = ?
        ORDER BY e.rule, e.dst_ref LIMIT 5""", [tid])
    if snippets and definitions:
        try:
            texts = dict(con.execute(f"SELECT object_id, left(normalized_text, 300) FROM {blocks_table} "
                                     "WHERE object_id IN (SELECT unnest(?::VARCHAR[]))",
                                     [[d["block_id"] for d in definitions]]).fetchall())
            for d in definitions:
                d["text"] = texts.get(d["block_id"])
        except Exception:  # noqa: BLE001 — no canonical blocks behind this connection
            pass
    same_as = _rows(con, f"""
        SELECT CASE WHEN e.src_term_id = $t THEN e.dst_term_id ELSE e.src_term_id END AS term_id,
               coalesce(x.lemma, e.dst_ref) AS lemma, e.weight AS count, e.n_sources, e.rule
        FROM {t['term_edges']} e
        LEFT JOIN {t['terms']} x ON x.term_id = CASE WHEN e.src_term_id = $t THEN e.dst_term_id ELSE e.src_term_id END
        WHERE e.kind = 'SAME_AS' AND (e.src_term_id = $t OR e.dst_term_id = $t)
        ORDER BY e.weight DESC, lemma LIMIT 10""", {"t": tid})
    broader = _rows(con, f"""
        SELECT x.term_id, x.lemma, x.df_units FROM {t['term_edges']} e JOIN {t['terms']} x ON x.term_id = e.dst_term_id
        WHERE e.kind = 'CONTAINS' AND e.src_term_id = ? ORDER BY x.n_words DESC, x.df_units DESC LIMIT 5""", [tid])
    narrower = _rows(con, f"""
        SELECT x.term_id, x.lemma, x.df_units FROM {t['term_edges']} e JOIN {t['terms']} x ON x.term_id = e.src_term_id
        WHERE e.kind = 'CONTAINS' AND e.dst_term_id = ? ORDER BY x.df_units DESC, x.term_id LIMIT 10""", [tid])
    top_units = _rows(con, f"""
        SELECT unit_id, unit_kind, section_id, source_id, tf, round(tfidf, 3) AS tfidf, page_ids[1:3] AS page_ids
        FROM {t['term_mentions']} WHERE term_id = ? ORDER BY tfidf DESC, tf DESC, unit_id LIMIT ?""",
                      [tid, max(1, lim // 2)])
    top_sources = _rows(con, f"""
        SELECT source_id, count(*) AS n_units, sum(tf) AS tf FROM {t['term_mentions']} WHERE term_id = ?
        GROUP BY source_id ORDER BY tf DESC, source_id LIMIT 10""", [tid])
    out = {
        "query": term,
        "match": {k: m[k] for k in ("term_id", "lemma", "language", "kind", "df_units", "df_sources", "tf", "seed",
                                    "community", "surface_forms", "match")},
        "alternatives": [{k: r[k] for k in ("term_id", "lemma", "df_units", "match")} for r in found[1:]],
        "focus": None if focus is m else {"term_id": focus["term_id"], "lemma": focus["lemma"],
                                          "df_units": focus["df_units"], "reason": f"df_units < {min_df_focus}"},
        "definitions": definitions,
        "same_as": same_as,
        "broader": broader,
        "narrower": narrower,
        "neighbours": near,
        "top_units": top_units,
        "top_sources": top_sources,
        "note": NOTE,
    }
    if near_exact is not None:
        out["neighbours_exact"] = near_exact
    return out
