"""Read functions over the topic tree of NAV (§7): ``get_topic``, ``find_topics``, ``similar_sections``,
``section_topics`` — pure: a DuckDB connection in, plain dicts out, no writes.

The connection must expose the topic datasets as ``nav_topics``, ``nav_topic_members``, ``nav_topic_edges``,
``nav_section_vectors``, ``nav_section_aggregates`` and the N1 sections as ``nav_sections`` (the serving store and
``vkm-corpus nav build`` both name them so; :func:`attach` creates the views over Parquet files). N3 tables
(``nav_terms`` / ``nav_term_edges`` or ``terms`` / ``term_edges``), when present, let ``find_topics`` match a phrase
through its lemma and its SAME_AS equivalents (``ВЗТ`` ↔ «водозащитная толща»). Everything returned is DERIVED
navigation (``AUTO_EXTRACTED_UNREVIEWED``): a topic groups sections that read alike, it is not a claim.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
NOTE = ("DERIVED navigation layer (AUTO_EXTRACTED_UNREVIEWED): a topic groups sections whose texts read alike "
        "(vectors of their units); it is not a physical claim. Read the pages of the member sections.")
DATASETS = ("topics", "topic_members", "topic_edges", "section_vectors", "section_aggregates")
MAX_MEMBERS = 50
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _lit(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def attach(con: Any, nav_dir: str | Path, *, sections_dir: str | Path | None = None) -> None:
    """Temporary views ``nav_<dataset>`` over ``<nav_dir>/<dataset>.parquet`` (+ ``nav_sections`` from
    ``sections_dir`` or ``nav_dir`` when the file is there)."""
    d = Path(nav_dir)
    for name in DATASETS:
        if (d / f"{name}.parquet").exists():
            con.execute(f"CREATE OR REPLACE TEMP VIEW nav_{name} AS SELECT * FROM read_parquet({_lit(d / (name + '.parquet'))})")
    s = Path(sections_dir) if sections_dir else d
    for name in ("sections", "section_pages"):
        if (s / f"{name}.parquet").exists():
            con.execute(f"CREATE OR REPLACE TEMP VIEW nav_{name} AS SELECT * FROM read_parquet({_lit(s / (name + '.parquet'))})")


def _rows(con: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, params if params is not None else [])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _exists(con: Any, name: str) -> bool:
    try:
        con.execute(f"SELECT 1 FROM {name} LIMIT 0")
        return True
    except Exception:  # noqa: BLE001
        return False


def _section_briefs(con: Any, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Title, path, level and page range of sections (N1), keyed by id; missing columns are skipped."""
    ids = [i for i in ids if i]
    if not ids or not _exists(con, "nav_sections"):
        return {}
    rows = _rows(con, """
        SELECT section_id, source_id, level, numbering, title, title_path, page_start_id, page_end_id,
               page_start_index, page_end_index
        FROM nav_sections WHERE section_id IN (SELECT unnest($ids::VARCHAR[]))""", {"ids": list(ids)})
    return {r["section_id"]: r for r in rows}


def _source_titles(con: Any, source_ids: Sequence[str]) -> dict[str, str]:
    """Titles of the works of sources from the canon (``canonical.source_work_links`` + ``canonical.works``)."""
    if not source_ids:
        return {}
    try:
        rows = con.execute("""
            SELECT l.source_id, any_value(w.title ORDER BY l.is_primary DESC NULLS LAST, w.work_id) AS title
            FROM canonical.source_work_links l JOIN canonical.works w ON w.work_id = l.work_id
            WHERE l.source_id IN (SELECT unnest($s::VARCHAR[])) GROUP BY 1""", {"s": list(source_ids)}).fetchall()
    except Exception:  # noqa: BLE001 — no canonical tables behind this connection
        return {}
    return {s: t for s, t in rows}


def _topic_brief(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r.get(k) for k in ("topic_id", "level", "n_sections", "n_sources", "label_terms", "coherence")}


# ============================================================================================ get_topic
def get_topic(con: Any, topic_id: str, *, max_members: int = MAX_MEMBERS) -> dict[str, Any] | None:
    """A topic with its path to the root, children, member sections (source, title, path, pages; best first),
    central sections, sources and neighbour topics of the same level."""
    rows = _rows(con, "SELECT * FROM nav_topics WHERE topic_id = ?", [topic_id])
    if not rows:
        return None
    t = rows[0]
    path = []
    seen = {topic_id}
    parent = t.get("parent_topic_id")
    while parent and parent not in seen:
        seen.add(parent)
        p = _rows(con, "SELECT * FROM nav_topics WHERE topic_id = ?", [parent])
        if not p:
            break
        path.append(_topic_brief(p[0]))
        parent = p[0].get("parent_topic_id")
    path.reverse()
    children = [_topic_brief(r) for r in _rows(con, """
        SELECT * FROM nav_topics WHERE parent_topic_id = ? ORDER BY n_sections DESC, topic_id""", [topic_id])]
    lim = max(1, int(max_members))
    agg = _exists(con, "nav_section_aggregates")
    members = _rows(con, f"""
        SELECT m.section_id, m.source_id, m.similarity, m.rank
               {', a.key_terms[1:5] AS key_terms, a.n_units, a.central_page_ids[1:2] AS central_page_ids' if agg else ''}
        FROM nav_topic_members m {'LEFT JOIN nav_section_aggregates a USING (section_id)' if agg else ''}
        WHERE m.topic_id = ? ORDER BY m.rank LIMIT ?""", [topic_id, lim])
    briefs = _section_briefs(con, [m["section_id"] for m in members] + list(t.get("central_section_ids") or []))
    for m in members:
        b = briefs.get(m["section_id"], {})
        m.update({k: b.get(k) for k in ("title", "title_path", "page_start_id", "page_end_id", "page_start_index",
                                        "page_end_index")})
    central = [{"section_id": s, "source_id": briefs.get(s, {}).get("source_id"),
                "title": briefs.get(s, {}).get("title"), "page_start_id": briefs.get(s, {}).get("page_start_id"),
                "page_end_id": briefs.get(s, {}).get("page_end_id")} for s in (t.get("central_section_ids") or [])]
    by_source = _rows(con, """
        SELECT source_id, count(*) AS n_sections, max(similarity) AS best_similarity
        FROM nav_topic_members WHERE topic_id = ? GROUP BY 1 ORDER BY n_sections DESC, source_id""", [topic_id])
    titles = _source_titles(con, [r["source_id"] for r in by_source])
    for r in by_source:
        r["work_title"] = titles.get(r["source_id"])
    neighbours = []
    if _exists(con, "nav_topic_edges"):
        neighbours = _rows(con, """
            SELECT CASE WHEN e.topic_id_a = $t THEN e.topic_id_b ELSE e.topic_id_a END AS topic_id, e.cosine,
                   e.n_links, x.n_sections, x.n_sources, x.label_terms
            FROM nav_topic_edges e
            JOIN nav_topics x ON x.topic_id = CASE WHEN e.topic_id_a = $t THEN e.topic_id_b ELSE e.topic_id_a END
            WHERE e.topic_id_a = $t OR e.topic_id_b = $t
            ORDER BY e.cosine DESC, topic_id LIMIT 10""", {"t": topic_id})
    return {**{k: t.get(k) for k in ("topic_id", "level", "parent_topic_id", "n_children", "n_sections",
                                      "n_sources", "label_terms", "label_term_ids", "coherence", "rule_version")},
            "path": path, "children": children, "central_sections": central, "sources": by_source,
            "members": members, "members_truncated": int(t.get("n_sections") or 0) > len(members),
            "neighbours": neighbours, "review_status": REVIEW_STATUS, "note": NOTE}


# ============================================================================================ find_topics
def _norm(text: str) -> str:
    return (text or "").casefold().replace("ё", "е")


def _stems(phrase: str) -> list[str]:
    """Prefixes (≈ 3/4 of each word, at least 4 letters) of the words of a phrase — crude, inflection-tolerant."""
    out = []
    for w in _WORD.findall(_norm(phrase)):
        if len(w) < 3:
            continue
        out.append(w if len(w) <= 4 else w[: max(4, round(len(w) * 0.75))])
    return out


_MORPH: Any = None


def _morph() -> Any:
    """One analyser per process (the dictionary load is slow); the same morphology as the N3 builder."""
    global _MORPH  # noqa: PLW0603
    if _MORPH is None:
        from vkm_corpus.navigation.concepts import Morphology  # noqa: PLC0415

        _MORPH = Morphology()
    return _MORPH


def _term_ids(con: Any, phrase: str) -> tuple[list[str], list[str], list[str]]:
    """N3 term ids of a phrase: (the whole phrase and its SAME_AS equivalents, longer terms containing the whole
    phrase, terms of a part of the phrase); ([], [], []) without N3 tables."""
    names = None
    for cand in ({"terms": "nav_terms", "term_edges": "nav_term_edges", "term_mentions": "nav_term_mentions"},
                 {"terms": "terms", "term_edges": "term_edges", "term_mentions": "term_mentions"}):
        if _exists(con, cand["terms"]) and _exists(con, cand["term_edges"]):
            names = cand
            break
    if names is None:
        return [], [], []
    try:
        from vkm_corpus.navigation.concepts import phrase_keys  # noqa: PLC0415
        from vkm_corpus.navigation.concepts_query import find_terms  # noqa: PLC0415

        keys = phrase_keys(phrase, _morph())
        found = find_terms(con, phrase, limit=12, tables=names, morph=_morph())
    except Exception:  # noqa: BLE001 — morphology or tables unusable: text match only
        return [], [], []
    full = keys[0] if keys else None
    strong, contain, part = [], [], []
    for r in found:
        m = r.get("match")
        if m in ("term_id", "same_as") or (m == "lemma_key" and r.get("lemma_key") == full):
            strong.append(r["term_id"])
        elif m == "contains":
            contain.append(r["term_id"])
        else:
            part.append(r["term_id"])
    if strong:
        same = con.execute(f"""
            SELECT CASE WHEN list_contains($ids, src_term_id) THEN dst_term_id ELSE src_term_id END
            FROM {names['term_edges']} WHERE kind = 'SAME_AS' AND dst_term_id IS NOT NULL
              AND (list_contains($ids, src_term_id) OR list_contains($ids, dst_term_id))""",
                           {"ids": strong}).fetchall()
        strong += [x for (x,) in same if x and x not in strong]
    return strong, [c for c in contain if c not in strong], [x for x in part if x not in strong]


def find_topics(con: Any, terms: str | Iterable[str], *, limit: int = 10, level: int | None = None) -> list[dict]:
    """Topics for one phrase or several (all must match), by their labels and by the key terms of their member
    sections. A phrase is matched through N3: its lemma and SAME_AS equivalents (label weight 1.0), longer terms
    containing it (0.8), word prefixes of the phrase inside one label (0.8), a term of a part of the phrase (0.3);
    minus 0.05 per label position. Score per phrase = label weight + share of member sections whose key terms carry
    the phrase (its lemma, a containing term or all its word prefixes)."""
    phrases = [terms] if isinstance(terms, str) else [t for t in terms if t and str(t).strip()]
    phrases = [str(p).strip() for p in phrases if str(p).strip()]
    if not phrases:
        return []
    topics = _rows(con, f"""SELECT topic_id, level, parent_topic_id, n_sections, n_sources, label_terms, label_term_ids,
                                  central_section_ids, coherence FROM nav_topics
                           {'WHERE level = ' + str(int(level)) if level is not None else ''}""")
    if not topics:
        return []
    agg = _exists(con, "nav_section_aggregates")
    scores: dict[str, float] = {t["topic_id"]: 0.0 for t in topics}
    matched: dict[str, list[str]] = {t["topic_id"]: [] for t in topics}
    ok_all = {t["topic_id"]: True for t in topics}
    for phrase in phrases:
        strong, contain, part = _term_ids(con, phrase)
        stems = _stems(phrase)
        member_share: dict[str, float] = {}
        if agg:
            conds, params = [], {}
            if strong or contain:
                conds.append("list_has_any(a.key_term_ids, $ids)")
                params["ids"] = strong + contain
            if stems:
                conds.append("(" + " AND ".join(
                    f"strpos(lower(replace(array_to_string(a.key_terms, ' | '), 'ё', 'е')), $s{i}) > 0"
                    for i in range(len(stems))) + ")")
                params.update({f"s{i}": x for i, x in enumerate(stems)})
            if conds:
                for tid, share in con.execute(f"""
                        SELECT m.topic_id, avg(CASE WHEN {' OR '.join(conds)} THEN 1.0 ELSE 0.0 END)
                        FROM nav_topic_members m JOIN nav_section_aggregates a USING (section_id)
                        GROUP BY 1""", params).fetchall():
                    member_share[tid] = float(share or 0.0)
        weight = {**{x: 0.3 for x in part}, **{x: 0.8 for x in contain}, **{x: 1.0 for x in strong}}
        for t in topics:
            tid = t["topic_id"]
            label = 0.0
            for pos, lid in enumerate(t.get("label_term_ids") or []):
                if lid in weight:
                    label = max(label, weight[lid] - 0.05 * pos)
            if stems:
                for pos, lab in enumerate(_norm(x) for x in (t.get("label_terms") or [])):
                    if all(st in lab for st in stems):
                        label = max(label, 0.8 - 0.05 * pos)
            share = member_share.get(tid, 0.0)
            if label <= 0 and share <= 0:
                ok_all[tid] = False
                continue
            scores[tid] += max(0.0, label) + share
            matched[tid].append(phrase)
    out = []
    for t in topics:
        tid = t["topic_id"]
        if not ok_all[tid] or scores[tid] <= 0:
            continue
        out.append({**{k: t[k] for k in ("topic_id", "level", "parent_topic_id", "n_sections", "n_sources",
                                         "label_terms", "coherence")},
                    "central_section_ids": list(t.get("central_section_ids") or [])[:3],
                    "score": round(scores[tid], 4), "matched": matched[tid]})
    out.sort(key=lambda r: (-r["score"], r["level"], -r["n_sections"], r["topic_id"]))
    return out[: max(1, int(limit))]


# ============================================================================================ similar_sections
def _vector_of(con: Any, section_id: str) -> tuple[np.ndarray | None, str, list[str]]:
    """The stored vector of a section, else the unit-weighted mean of the vectors of its descendants (a chapter
    whose pages all belong to its subsections)."""
    row = con.execute("SELECT vector FROM nav_section_vectors WHERE section_id = ?", [section_id]).fetchone()
    if row is not None:
        return np.asarray(row[0], dtype=np.float64), "own", []
    if not _exists(con, "nav_sections"):
        return None, "none", []
    rows = con.execute("""
        WITH RECURSIVE sub(section_id) AS (
            SELECT section_id FROM nav_sections WHERE parent_section_id = $s
            UNION SELECT c.section_id FROM nav_sections c JOIN sub ON c.parent_section_id = sub.section_id)
        SELECT v.section_id, v.n_units, v.vector FROM nav_section_vectors v JOIN sub USING (section_id)
        ORDER BY v.section_id""", {"s": section_id}).fetchall()
    if not rows:
        return None, "none", []
    mat = np.asarray([r[2] for r in rows], dtype=np.float64)
    w = np.asarray([max(1, r[1] or 1) for r in rows], dtype=np.float64)
    v = (mat * w[:, None]).sum(axis=0)
    n = np.linalg.norm(v)
    return (v / n if n else v), "subtree", [r[0] for r in rows]


def similar_sections(con: Any, section_id: str, k: int = 10, other_sources_only: bool = True) -> dict[str, Any]:
    """The ``k`` sections closest to a section by the cosine of the section vectors (exact, in DuckDB), by
    default only from other sources — «where else is this discussed». A section without a vector of its own uses
    the mean of its subsections. Each hit carries its title, pages and level-1 topic."""
    own = con.execute("SELECT source_id FROM nav_section_vectors WHERE section_id = ?", [section_id]).fetchone()
    src = own[0] if own else None
    if src is None and _exists(con, "nav_sections"):
        r = con.execute("SELECT source_id FROM nav_sections WHERE section_id = ?", [section_id]).fetchone()
        src = r[0] if r else None
    vec, basis, subtree = _vector_of(con, section_id)
    base = {"section_id": section_id, "source_id": src, "basis": basis, "n_subtree_sections": len(subtree),
            "other_sources_only": bool(other_sources_only), "review_status": REVIEW_STATUS}
    if vec is None:
        return {**base, "results": []}
    excl = [section_id, *subtree]
    where = ["NOT list_contains($excl, v.section_id)"]
    params: dict[str, Any] = {"q": vec.astype(np.float32).tolist(), "excl": excl, "k": max(1, int(k))}
    if other_sources_only and src is not None:
        where.append("v.source_id <> $src")
        params["src"] = src
    hits = _rows(con, f"""
        SELECT v.section_id, v.source_id, v.n_units,
               round(list_cosine_similarity(v.vector, $q::FLOAT[]), 6) AS cosine
        FROM nav_section_vectors v WHERE {' AND '.join(where)}
        ORDER BY cosine DESC, v.section_id LIMIT $k""", params)
    briefs = _section_briefs(con, [h["section_id"] for h in hits])
    topics = {}
    if hits and _exists(con, "nav_topic_members"):
        topics = {r["section_id"]: r for r in _rows(con, """
            SELECT m.section_id, m.topic_id, t.label_terms[1:5] AS label_terms FROM nav_topic_members m
            JOIN nav_topics t USING (topic_id) WHERE m.level = 1 AND m.section_id IN (SELECT unnest($ids::VARCHAR[]))""",
            {"ids": [h["section_id"] for h in hits]})}
    for h in hits:
        b = briefs.get(h["section_id"], {})
        h.update({kk: b.get(kk) for kk in ("title", "title_path", "level", "page_start_id", "page_end_id")})
        tp = topics.get(h["section_id"])
        h["topic_id"] = tp["topic_id"] if tp else None
        h["topic_labels"] = tp["label_terms"] if tp else None
    return {**base, "results": hits}


def section_topics(con: Any, section_id: str) -> list[dict[str, Any]]:
    """The topics of a section at every level (fine → coarse) with its similarity and rank inside each."""
    return _rows(con, """
        SELECT m.level, m.topic_id, m.similarity, m.rank, t.n_sections, t.n_sources, t.label_terms
        FROM nav_topic_members m JOIN nav_topics t USING (topic_id)
        WHERE m.section_id = ? ORDER BY m.level""", [section_id])
