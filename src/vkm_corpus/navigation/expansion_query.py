"""Other wordings of a query from the concept graph and the term dictionary — stage G3 ``concepts`` of the hybrid
search (agent GS; ``benchmarks/graph_search_v1``).

The query's noun phrases and abbreviations are parsed with the morphology of the concept graph (``analyze_text``, the
same keys as N3 and the dictionary). Two wordings at most, each searched by BM25 only:

* ``equivalents`` — every matched phrase (longest first, non-overlapping) replaced by its best same-concept pair of the
  term dictionary: a SYNONYM or an ABBREVIATION (``междукамерный целик`` ↔ ``МКЦ``; the dictionary also carries the
  N3 abbreviations printed in parentheses), score ≥ ``min_score`` (0.8, the dictionary's trusted threshold); a pair
  that only splits or hyphenates the same letters («место рождения», «поверх-ность») is OCR noise and is skipped;
* ``narrower`` — the query plus the most frequent narrower term of its main concept (N3 ``CONTAINS``: «полная
  закладка выработанного пространства» is narrower than «закладка выработанного пространства»). The main concept is
  the query's longest phrase that is an N3 term, of two words at least (a one-word head has generic narrower terms;
  a shorter phrase is never tried instead — its narrower terms are siblings of the query's concept). The term must
  occur in ≥ ``narrower_min_df`` units and not be in the query already.

Translations are not here: the other-language wording is the ``translate`` flag (agent TR). Pure: a DuckDB connection
in (``terms``, ``term_edges``, ``term_translations``), plain dicts out (ids, lemmas, scores — no corpus text). The
result is DERIVED navigation (``AUTO_EXTRACTED_UNREVIEWED``): a query expansion, never evidence.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from vkm_corpus.navigation.concepts import Morphology, analyze_text

DEFAULT_TABLES: dict[str, str] = {"terms": "terms", "term_edges": "term_edges",
                                  "term_translations": "term_translations"}
RELATIONS = ("SYNONYM", "ABBREVIATION")
MIN_SCORE = 0.8                     # pairs trusted for query expansion (term dictionary, EXPANSION_MIN_SCORE)
NARROWER_MIN_DF = 5                 # a narrower term must occur in this many units (N3 df_units)
MAX_EXPANSIONS = 2
NOTE = ("DERIVED navigation layer (AUTO_EXTRACTED_UNREVIEWED): other wordings of the query from the term dictionary "
        "(synonyms, abbreviations) and the concept graph (a narrower term) — a query expansion, not evidence.")
_MORPH: Morphology | None = None


def _morph() -> Morphology:
    global _MORPH  # noqa: PLW0603 — one analyser per process (the dictionary load is slow)
    if _MORPH is None:
        _MORPH = Morphology()
    return _MORPH


def _rows(con: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _squash(text: str) -> str:
    """Letters and digits only, lower case, ё → е: two spellings of one word compare equal."""
    return "".join(ch for ch in (text or "").lower().replace("ё", "е") if ch.isalnum())


def _same_spelling(a: str, b: str) -> bool:
    """The same letters split, hyphenated or with another ending (≤ 2 letters): «место рождения» ~ «месторождение»,
    «поверх-ность» ~ «поверхность». A real synonym («деформации» / «деформирование») differs more."""
    sa, sb = _squash(a), _squash(b)
    if sa == sb:
        return True
    n = min(len(sa), len(sb))
    cp = 0
    while cp < n and sa[cp] == sb[cp]:
        cp += 1
    return n >= 6 and cp >= n - 2 and abs(len(sa) - len(sb)) <= 2


def _exists(con: Any, name: str) -> bool:
    try:
        con.execute(f"SELECT 1 FROM {name} LIMIT 0")
        return True
    except Exception:  # noqa: BLE001
        return False


def expand_query(con: Any, text: str, *, min_score: float = MIN_SCORE, narrower: bool = True,
                 narrower_min_df: int = NARROWER_MIN_DF, max_expansions: int = MAX_EXPANSIONS,
                 relations: Sequence[str] = RELATIONS, tables: Mapping[str, str] | None = None,
                 morph: Morphology | None = None) -> dict[str, Any]:
    """≤ ``max_expansions`` other wordings of ``text``: ``equivalents`` (synonyms / abbreviations of its phrases) and
    ``narrower`` (+ one narrower term). ``relations`` limits the equivalents to SYNONYM or ABBREVIATION pairs (the
    hybrid search's ``expand=terms`` asks for each alone). ``terms`` lists what was used; a build without a table
    skips its part."""
    relations = tuple(r for r in RELATIONS if r in set(relations))
    t = {**DEFAULT_TABLES, **(tables or {})}
    q = " ".join((text or "").split())
    out: dict[str, Any] = {"query": q, "expansions": [], "phrases": 0, "note": NOTE}
    if not q:
        return out
    morph = morph or _morph()
    res = analyze_text(q, morph, patterns=False)
    cands = [c for c in res.cands if c.kind in ("NP", "ABBR")]
    out["phrases"] = len(cands)
    if not cands:
        return out
    from vkm_corpus.navigation.concepts import _tokenize  # noqa: PLC0415 — the tokens analyze_text indexes

    toks, _scripts = _tokenize(q)
    qlang = "ru" if sum(1 for ch in q if "а" <= ch.lower() <= "я" or ch.lower() == "ё") >= \
        sum(1 for ch in q if "a" <= ch.lower() <= "z") else "en"

    def lang_of(c: Any) -> str:
        return c.lang if c.lang in ("ru", "en", "de") else qlang

    keyset = {(lang_of(c), c.key) for c in cands}
    keys = sorted(keyset)
    expansions: list[dict[str, Any]] = []
    # ---- equivalents: synonyms and abbreviations of the dictionary
    if relations and _exists(con, t["term_translations"]):
        ks = [f"{lg}\t{k}" for lg, k in keys]
        pairs = _rows(con, f"""
            SELECT pair_id, relation, lang_a, key_a, lemma_a, lang_b, key_b, lemma_b, in_terms_a, in_terms_b, score
            FROM {t['term_translations']}
            WHERE score >= ? AND relation IN (SELECT unnest(?::VARCHAR[]))
              AND (lang_a || chr(9) || key_a IN (SELECT unnest(?::VARCHAR[]))
                   OR lang_b || chr(9) || key_b IN (SELECT unnest(?::VARCHAR[])))
            ORDER BY score DESC, pair_id""", [float(min_score), list(relations), ks, ks])
        best: dict[tuple[str, str], dict[str, Any]] = {}
        for p in pairs:
            for side, other in (("a", "b"), ("b", "a")):
                k = (p[f"lang_{side}"], p[f"key_{side}"])
                if k not in keyset or p[f"key_{other}"] == p[f"key_{side}"]:
                    continue
                if _same_spelling(p[f"lemma_{other}"], p[f"lemma_{side}"]):
                    continue            # a split or hyphenated spelling («место рождения», «поверх-ность»): OCR noise
                e = {"equivalent": p[f"lemma_{other}"], "relation": p["relation"], "score": round(float(p["score"]), 4),
                     "in_terms": bool(p[f"in_terms_{other}"]), "pair_id": p["pair_id"]}
                cur = best.get(k)
                rank = (-e["score"], not e["in_terms"], len(e["equivalent"]), e["equivalent"])
                if cur is None or rank < (-cur["score"], not cur["in_terms"], len(cur["equivalent"]), cur["equivalent"]):
                    best[k] = e
        used: set[int] = set()
        chosen = []
        for c in sorted(cands, key=lambda c: (-(c.t1 - c.t0), -best.get((lang_of(c), c.key), {}).get("score", 0),
                                              c.t0)):
            e = best.get((lang_of(c), c.key))
            if e is None or any(i in used for i in range(c.t0, c.t1 + 1)):
                continue
            chosen.append((c, e))
            used.update(range(c.t0, c.t1 + 1))
        if chosen:
            start = {c.t0: (c, e) for c, e in chosen}
            words, i = [], 0
            while i < len(toks):
                if i in start:
                    c, e = start[i]
                    words.append(e["equivalent"])
                    i = c.t1 + 1
                    continue
                words.append(toks[i])
                i += 1
            wording = " ".join(w for w in words if w.strip())
            if wording.lower() != q.lower():
                expansions.append({"kind": "equivalents", "text": wording[:512],
                                   "terms": [{"span": c.surface, "key": c.key, **e}
                                             for c, e in sorted(chosen, key=lambda x: x[0].t0)]})
    # ---- narrower: the query + the most frequent narrower term of its longest phrase that has one
    if narrower and len(expansions) < max_expansions and _exists(con, t["terms"]) and _exists(con, t["term_edges"]):
        found = _rows(con, f"""SELECT term_id, lemma_key, language, df_units FROM {t['terms']}
                              WHERE language || chr(9) || lemma_key IN (SELECT unnest(?::VARCHAR[]))""",
                      [[f"{lg}\t{k}" for lg, k in keys]])
        term_of = {(r["language"], r["lemma_key"]): r for r in found}
        ids = [r["term_id"] for r in found]
        nar: dict[str, list[dict[str, Any]]] = {}
        if ids:
            for r in _rows(con, f"""
                    SELECT e.dst_term_id, x.term_id, x.lemma, x.lemma_key, x.df_units
                    FROM {t['term_edges']} e JOIN {t['terms']} x ON x.term_id = e.src_term_id
                    WHERE e.kind = 'CONTAINS' AND e.dst_term_id IN (SELECT unnest(?::VARCHAR[])) AND x.df_units >= ?
                    ORDER BY e.dst_term_id, x.df_units DESC, x.term_id""", [ids, int(narrower_min_df)]):
                nar.setdefault(r["dst_term_id"], []).append(r)
        qkeys = {c.key for c in cands}
        # the query's main concept: its longest phrase that is an N3 term (ties: more units), at least two words —
        # a one-word head («наблюдение») has generic narrower terms; no fallback to a shorter phrase (a sibling
        # concept of a part — «верхнекамское месторождение» for a query about another deposit — is not narrower)
        terms_in = sorted((c for c in cands if (lang_of(c), c.key) in term_of),
                          key=lambda c: (-len(c.key.split()), -term_of[(lang_of(c), c.key)]["df_units"], c.t0))
        main = terms_in[0] if terms_in and len(terms_in[0].key.split()) >= 2 else None
        if main is not None:
            term = term_of[(lang_of(main), main.key)]
            pick = next((x for x in nar.get(term["term_id"], ()) if x["lemma_key"] not in qkeys), None)
            if pick is not None:
                expansions.append({"kind": "narrower", "text": f"{q} {pick['lemma']}"[:512],
                                   "terms": [{"span": main.surface, "key": main.key, "narrower": pick["lemma"],
                                              "narrower_term_id": pick["term_id"], "df_units": int(pick["df_units"]),
                                              "term_id": term["term_id"]}]})
    out["expansions"] = expansions[:max(0, int(max_expansions))]
    return out


__all__ = ["expand_query", "NOTE", "MIN_SCORE", "NARROWER_MIN_DF", "MAX_EXPANSIONS"]
