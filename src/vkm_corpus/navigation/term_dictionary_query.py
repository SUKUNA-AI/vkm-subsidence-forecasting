"""Query functions of the term dictionary (NAV ``term_translations``) for the API, MCP and query expansion.

* :func:`translate_term` — equivalents of a term in the other languages (RU ↔ EN, DE): direct pairs, pairs of its
  synonyms and abbreviations (``via``), the abbreviations of the equivalents; a phrase without a pair of its own is
  translated part by part (``composed``);
* :func:`synonyms` — same-language synonyms and abbreviations (two hops at most);
* :func:`translate_query` — the other-language wording of a whole query (its terms translated, non-overlapping,
  in query order) for query expansion: the ``translation`` formulation of ``reconstruct_topic`` and the optional
  ``translate`` flag of the hybrid search.

A phrase is lemmatised with the morphology of the concept graph (``concepts.phrase_keys``) and matched by lemma key
(and by printed form, as the builder does). Answers are ids, lemmas, scores, methods and example page ids — no corpus
text. The dictionary is DERIVED navigation: a pair says «printed/used as equivalents», not a reviewed fact; seed rows
are ``REVIEWED_BY_AGENT`` (the agent's list), never user-reviewed.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from vkm_corpus.navigation.concepts import (PREPOSITIONS_RU, STOP_EN, STOP_WORDS_RU, Morphology, analyze_text,
                                            phrase_keys)

DEFAULT_TABLES: dict[str, str] = {"term_translations": "term_translations", "terms": "terms"}
NOTE = ("DERIVED navigation layer (AUTO_EXTRACTED_UNREVIEWED; CURATED_SEED rows are REVIEWED_BY_AGENT, not by the "
        "user): a pair means the corpus prints or uses the terms as equivalents (method and example pages given), "
        "not a reviewed fact.")
_TERM_ID = re.compile(r"^TRM-[0-9a-f]{16}$")
_CYR = re.compile(r"[Ѐ-ӿ]")
_LAT = re.compile(r"[A-Za-zÀ-ɏ]")
_MORPH: Morphology | None = None
_STOP = STOP_WORDS_RU | PREPOSITIONS_RU | STOP_EN
EXPANSION_MIN_SCORE = 0.8           # pairs trusted for query expansion (MODEL_CHOICE: estimated precision, receipt)
EXPANSION_MIN_COVERAGE = 0.5        # share of the query's content words a translation must cover


def _morph() -> Morphology:
    global _MORPH  # noqa: PLW0603 — one analyser per process
    if _MORPH is None:
        _MORPH = Morphology()
    return _MORPH


def _rows(con: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _lang(text: str) -> str:
    nc, nl = len(_CYR.findall(text)), len(_LAT.findall(text))
    if nc >= nl and nc:
        return "ru"
    return "de" if re.search("[äöüßÄÖÜ]", text) else "en"


def _t(tables: Mapping[str, str] | None) -> dict[str, str]:
    return {**DEFAULT_TABLES, **(tables or {})}


def _keys_of(con: Any, text: str, t: dict[str, str], morph: Morphology) -> list[tuple[str, str]]:
    """(lang, key) candidates of the whole phrase: the printed form of an N3 term, then its lemma key (its parts are
    translated by :func:`_composed`, never matched in its place)."""
    q = " ".join((text or "").strip().split())
    if not q:
        return []
    lang = _lang(q)
    out: list[tuple[str, str]] = []
    surface = q.lower().replace("ё", "е")
    try:
        for r in _rows(con, f"""SELECT language, lemma_key FROM {t['terms']}
                                WHERE (lower(lemma) = ? OR list_contains(surface_forms, ?)) ORDER BY df_units DESC
                                LIMIT 3""", [surface, surface]):
            out.append((r["language"], r["lemma_key"]))
    except Exception:  # noqa: BLE001 - a build without the concept tables
        pass
    for k in phrase_keys(q, morph)[:1]:
        if (lang, k) not in out:
            out.append((lang, k))
    return out


_PAIR_COLS = ("pair_id, relation, term_id_a, term_id_b, lemma_a, lemma_b, key_a, key_b, lang_a, lang_b, in_terms_a, "
              "in_terms_b, methods, n_sources, score, status, cosine, evidence")


def _pairs_of(con: Any, keys: list[tuple[str, str]], t: dict[str, str], min_score: float) -> list[dict[str, Any]]:
    if not keys:
        return []
    ks = [f"{lg}\t{k}" for lg, k in keys]
    rows = _rows(con, f"""SELECT {_PAIR_COLS} FROM {t['term_translations']}
        WHERE score >= ? AND (lang_a || chr(9) || key_a IN (SELECT unnest(?::VARCHAR[]))
                              OR lang_b || chr(9) || key_b IN (SELECT unnest(?::VARCHAR[])))
        ORDER BY score DESC, pair_id""", [float(min_score), ks, ks])
    for r in rows:
        ev = r.pop("evidence", None) or []
        r["example_pages"] = list(dict.fromkeys(e.get("page_id") for e in ev if e.get("page_id")))[:3]
    return rows


def _other(p: dict[str, Any], lang: str, key: str) -> dict[str, Any]:
    """The side of a pair that is not (lang, key)."""
    side = "b" if (p["lang_a"], p["key_a"]) == (lang, key) else "a"
    return {"term_id": p[f"term_id_{side}"], "lemma": p[f"lemma_{side}"], "key": p[f"key_{side}"],
            "language": p[f"lang_{side}"], "in_terms": p[f"in_terms_{side}"]}


def _entry(p: dict[str, Any], other: dict[str, Any], via: str | None = None) -> dict[str, Any]:
    e = {"term_id": other["term_id"], "lemma": other["lemma"], "key": other["key"], "language": other["language"],
         "in_terms": other["in_terms"], "relation": p["relation"], "score": round(float(p["score"]), 4),
         "methods": list(p["methods"]), "status": p["status"], "n_sources": p["n_sources"],
         "example_page_ids": [x for x in (p.get("example_pages") or []) if x], "pair_id": p["pair_id"]}
    if via:
        e["via"] = via
    return e


def _rank(e: dict[str, Any]) -> tuple:
    return (-e["score"], "via" in e, not e["in_terms"], -(e["n_sources"] or 0), len(e["lemma"]), e["lemma"])


def _resolve(con: Any, term: str, t: dict[str, str], morph: Morphology, min_score: float
             ) -> tuple[tuple[str, str, str] | None, list[dict[str, Any]]]:
    """The matched (lang, key, lemma) and its pairs: a TRM- id, else the first phrase key that has pairs."""
    q = (term or "").strip()
    if _TERM_ID.match(q):
        rows = _rows(con, f"""SELECT lang_a AS lang, key_a AS key, lemma_a AS lemma FROM {t['term_translations']}
                              WHERE term_id_a = ? UNION SELECT lang_b, key_b, lemma_b FROM {t['term_translations']}
                              WHERE term_id_b = ? LIMIT 1""", [q, q])
        if not rows:
            return None, []
        keys = [(rows[0]["lang"], rows[0]["key"])]
    else:
        keys = _keys_of(con, q, t, morph)
    for lang, key in keys:
        pairs = _pairs_of(con, [(lang, key)], t, min_score)
        if pairs:
            p = pairs[0]
            lemma = p["lemma_a"] if (p["lang_a"], p["key_a"]) == (lang, key) else p["lemma_b"]
            return (lang, key, lemma), pairs
    return None, []


def translate_term(con: Any, term: str, *, target: str | None = None, limit: int = 10, min_score: float = 0.0,
                   tables: Mapping[str, str] | None = None, morph: Morphology | None = None) -> dict[str, Any]:
    """Equivalents of ``term`` (a phrase in any inflected form, an abbreviation or a ``TRM-`` id) in the other
    languages (``target``: ru | en | de, default all), best first: direct pairs, then pairs of its synonyms and
    abbreviations (``via``). ``synonyms`` / ``abbreviations`` list the same-language group; ``composed`` translates
    the parts of a phrase that has no pair of its own."""
    t = _t(tables)
    morph = morph or _morph()
    lim = max(1, int(limit))
    match, pairs = _resolve(con, term, t, morph, min_score)
    out: dict[str, Any] = {"query": term, "target": target, "match": None, "translations": [], "synonyms": [],
                           "abbreviations": [], "composed": [], "note": NOTE}
    if match is None:
        out["composed"] = _composed(con, term, t, morph, target, min_score)
        return out
    lang, key, lemma = match
    out["match"] = {"lemma": lemma, "language": lang, "key": key,
                    "term_id": next((p[f"term_id_{s}"] for p in pairs for s in "ab"
                                     if (p[f"lang_{s}"], p[f"key_{s}"]) == (lang, key)), None)}
    translations: dict[tuple[str, str], dict[str, Any]] = {}
    group: dict[tuple[str, str], dict[str, Any]] = {}
    for p in pairs:
        o = _other(p, lang, key)
        if p["relation"] == "TRANSLATION":
            translations.setdefault((o["language"], o["key"]), _entry(p, o))
        else:
            group.setdefault((o["language"], o["key"]), _entry(p, o))
    if group:                                                   # translations of the synonyms / abbreviations
        for p in _pairs_of(con, list(group), t, min_score):
            if p["relation"] != "TRANSLATION":
                continue
            for (glang, gkey), g in group.items():
                if (p["lang_a"], p["key_a"]) == (glang, gkey) or (p["lang_b"], p["key_b"]) == (glang, gkey):
                    o = _other(p, glang, gkey)
                    if o["language"] != lang and (o["language"], o["key"]) not in translations:
                        e = _entry(p, o, via=g["lemma"])
                        e["score"] = round(min(e["score"], g["score"]), 4)
                        translations[(o["language"], o["key"])] = e
    trans = [e for e in translations.values() if e["language"] != lang and (target is None or e["language"] == target)]
    trans.sort(key=_rank)
    same = [e for e in group.values() if e["language"] == lang or e["relation"] == "ABBREVIATION"]
    out["synonyms"] = sorted((e for e in same if e["relation"] == "SYNONYM"), key=_rank)[:lim]
    out["abbreviations"] = sorted((e for e in same if e["relation"] == "ABBREVIATION"), key=_rank)[:lim]
    if trans:                                                  # abbreviations of the best equivalents («InSAR»)
        best = [(e["language"], _key_of(e)) for e in trans[:3]]
        for p in _pairs_of(con, best, t, max(min_score, 0.0)):
            if p["relation"] == "ABBREVIATION":
                for e in trans[:3]:
                    if (p["lang_a"], p["key_a"]) == (e["language"], _key_of(e)):
                        e.setdefault("abbreviations", []).append(p["lemma_b"])
    out["translations"] = trans[:lim]
    if not trans:
        out["composed"] = _composed(con, term, t, morph, target, min_score)
    return out


def _key_of(e: dict[str, Any]) -> str:
    return e.get("key") or ""


def _composed(con: Any, text: str, t: dict[str, str], morph: Morphology, target: str | None,
              min_score: float) -> list[dict[str, Any]]:
    res = translate_query(con, text, target=target, min_score=min_score, tables=t, morph=morph, min_coverage=0.0)
    return [{"span": x["span"], "translation": x["translation"], "language": x["target_language"],
             "score": x["score"], "methods": x["methods"]} for x in res.get("terms") or []]


def synonyms(con: Any, term: str, *, limit: int = 20, min_score: float = 0.0, tables: Mapping[str, str] | None = None,
             morph: Morphology | None = None) -> dict[str, Any]:
    """Same-language synonyms and abbreviations of ``term`` (direct pairs, then their own synonyms: two hops, the
    weaker score of the path)."""
    t = _t(tables)
    morph = morph or _morph()
    match, pairs = _resolve(con, term, t, morph, min_score)
    out: dict[str, Any] = {"query": term, "match": None, "synonyms": [], "abbreviations": [], "note": NOTE}
    if match is None:
        return out
    lang, key, lemma = match
    out["match"] = {"lemma": lemma, "language": lang, "key": key}
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for p in pairs:
        if p["relation"] in ("SYNONYM", "ABBREVIATION"):
            o = _other(p, lang, key)
            found.setdefault((o["language"], o["key"]), _entry(p, o))
    hop = [k for k, e in found.items() if e["relation"] == "SYNONYM"]
    for p in _pairs_of(con, hop, t, min_score):
        if p["relation"] not in ("SYNONYM", "ABBREVIATION"):
            continue
        for k in hop:
            if (p["lang_a"], p["key_a"]) == k or (p["lang_b"], p["key_b"]) == k:
                o = _other(p, *k)
                if (o["language"], o["key"]) != (lang, key) and (o["language"], o["key"]) not in found:
                    e = _entry(p, o, via=found[k]["lemma"])
                    e["score"] = round(min(e["score"], found[k]["score"]), 4)
                    found[(o["language"], o["key"])] = e
    lim = max(1, int(limit))
    out["synonyms"] = sorted((e for e in found.values() if e["relation"] == "SYNONYM"), key=_rank)[:lim]
    out["abbreviations"] = sorted((e for e in found.values() if e["relation"] == "ABBREVIATION"), key=_rank)[:lim]
    return out


def translate_query(con: Any, text: str, *, target: str | None = None, min_score: float = EXPANSION_MIN_SCORE,
                    min_coverage: float = EXPANSION_MIN_COVERAGE, max_terms: int = 8,
                    tables: Mapping[str, str] | None = None, morph: Morphology | None = None) -> dict[str, Any]:
    """The query in the other language: its terms (longest first, non-overlapping) replaced by their best
    translation (``min_score`` or more), in query order; Latin tokens of a Russian query (InSAR, K0) are kept.
    ``translation`` is None when the translated terms cover less than ``min_coverage`` of the content words."""
    t = _t(tables)
    morph = morph or _morph()
    q = " ".join((text or "").split())
    src = _lang(q) if q else "ru"
    tgt = target or ("en" if src == "ru" else "ru")
    out: dict[str, Any] = {"text": q, "source_language": src, "target_language": tgt, "translation": None,
                           "coverage": 0.0, "terms": [], "note": NOTE}
    if not q:
        return out
    res = analyze_text(q, morph, patterns=False)
    cands = [c for c in res.cands if c.kind in ("NP", "ABBR")]
    keys = sorted({(c.lang if c.lang in ("ru", "en", "de") else src, c.key) for c in cands})
    pairs = _pairs_of(con, keys, t, min_score) if keys else []
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for p in pairs:
        if p["relation"] != "TRANSLATION":
            continue
        for side, other in (("a", "b"), ("b", "a")):
            k = (p[f"lang_{side}"], p[f"key_{side}"])
            if k in keys and p[f"lang_{other}"] == tgt:
                e = {"translation": p[f"lemma_{other}"], "score": float(p["score"]), "methods": list(p["methods"]),
                     "in_terms": p[f"in_terms_{other}"], "pair_id": p["pair_id"]}
                cur = best.get(k)
                if cur is None or (-e["score"], not e["in_terms"], e["translation"]) < \
                        (-cur["score"], not cur["in_terms"], cur["translation"]):
                    best[k] = e
    toks, scripts = _analyze_tokens(q)
    content = [i for i, s in enumerate(scripts) if s in ("cyr", "lat") and len(toks[i]) > 2
               and toks[i].lower() not in _STOP]
    chosen: list[tuple[int, int, Any, dict[str, Any]]] = []
    used: set[int] = set()
    for c in sorted(cands, key=lambda c: (-(c.t1 - c.t0), -best.get((c.lang, c.key), {}).get("score", 0), c.t0)):
        k = (c.lang if c.lang in ("ru", "en", "de") else src, c.key)
        if k not in best or any(i in used for i in range(c.t0, c.t1 + 1)):
            continue
        chosen.append((c.t0, c.t1, c, best[k]))
        used.update(range(c.t0, c.t1 + 1))
        if len(chosen) >= max_terms:
            break
    chosen.sort(key=lambda x: x[0])
    covered = sum(1 for i in content if i in used)
    out["coverage"] = round(covered / len(content), 3) if content else 0.0
    parts: list[tuple[int, str]] = [(t0, e["translation"]) for t0, _t1, _c, e in chosen]
    if src == "ru" and tgt != "ru":                           # language-neutral tokens stay (InSAR, K0, σv)
        for i, (tok, sc) in enumerate(zip(toks, scripts)):
            if i not in used and sc == "lat" and len(tok) >= 2:
                parts.append((i, tok))
    parts.sort()
    out["terms"] = [{"span": c.surface, "key": c.key, "source_language": c.lang, "target_language": tgt,
                     "translation": e["translation"], "score": round(e["score"], 4), "methods": e["methods"],
                     "pair_id": e["pair_id"]} for _t0, _t1, c, e in chosen]
    if chosen and out["coverage"] >= min_coverage:
        out["translation"] = " ".join(dict.fromkeys(x for _, x in parts))
    return out


def _analyze_tokens(text: str) -> tuple[list[str], list[str]]:
    from vkm_corpus.navigation.concepts import _tokenize  # noqa: PLC0415

    return _tokenize(text)


__all__ = ["translate_term", "synonyms", "translate_query", "NOTE", "EXPANSION_MIN_SCORE", "EXPANSION_MIN_COVERAGE"]
