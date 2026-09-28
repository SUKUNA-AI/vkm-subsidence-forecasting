"""Graph navigation over the NAV layer for Claude (API ``/v1/nav/graph/*``, MCP ``concept_paths`` and
``graph_neighbourhood``): compact answers — IDs, short names and page locators — never corpus passages.

* ``concept_paths(term_a, term_b, max_len, limit, via)`` — all shortest paths between two terms through ``Term``,
  ``FormulaSymbol``, ``Formula``, ``NavSection`` and ``NavTopic`` (relationship families ``concepts``, ``formulas``,
  ``sections``, ``topics``), ranked among equals by the product of hop strengths (NPMI × support for co-occurrence,
  rank for mentions, match quality for symbol links); every hop carries up to three page IDs;
* ``graph_neighbourhood(node_id, depth, limit)`` — edges of any NAV or DOCUMENT node grouped by type and direction
  (total count + the strongest neighbours), and at depth 2 the neighbours of those neighbours.

Cypher reads run in READ transactions with a timeout. Node and relationship maps are built from ``NODE_KEYS`` /
``REL_KEYS`` so the offline fake driver mirrors them exactly. A path is navigation — «discussed together», «defined in
the same where-clause» — not a physical or causal chain.
"""
from __future__ import annotations

import math
from typing import Any

from vkm_corpus.graph import nav_schema as N
from vkm_corpus.graph import schema as S
from vkm_corpus.graph.schema import Namespace, q

READ_TIMEOUT_S = 20.0
MAX_PATH_LEN = 6
PATH_CAP = 200
NAME_CHARS = 90
NOTE = ("NAV graph (DERIVED, AUTO_EXTRACTED_UNREVIEWED): a path or a neighbour is a navigation hint — terms discussed "
        "in the same sections, a symbol defined in a formula's where-clause, a textual reference — never a physical "
        "or causal claim. Read the pages to answer.")

NODE_KEYS: tuple[str, ...] = (
    "id", "lemma", "title", "numbering", "symbol", "definition", "unit", "label", "source_id", "page_id",
    "page_index", "page_start_id", "page_end_id", "level", "df_units", "df_sources", "value_text", "block_type",
    "formula_kind", "language")
REL_KEYS: tuple[str, ...] = (
    "npmi", "n_units", "n_sources", "examples", "page_ids", "tf", "tfidf", "rank_in_term", "rank_in_section", "kind",
    "match", "n_formulas", "definition_block_id", "definition", "unit", "similarity", "cosine", "weight",
    "number_text", "block_id", "deepest", "equation_number", "page_id", "page_index", "rule_version")
KIND_OF_LABEL: dict[str, str] = {
    "Term": "TERM", "NavSection": "SECTION", "FormulaSymbol": "SYMBOL", "ParameterCandidate": "PARAMETER",
    "NavTopic": "TOPIC", "NavMeta": "NAV_META", "Formula": "FORMULA", "Page": "PAGE", "Block": "BLOCK",
    "Source": "SOURCE", "Work": "WORK", "Figure": "FIGURE", "Table": "TABLE", "BibliographyEntry": "BIBLIOGRAPHY_ENTRY",
    "Author": "AUTHOR", "Venue": "VENUE"}


def timed(text: str, timeout: float | None) -> Any:
    """A ``neo4j.Query`` with a transaction timeout; the plain text when the driver is not installed (fake driver)."""
    try:
        from neo4j import Query
    except ImportError:
        return text
    return Query(text, timeout=timeout) if timeout else text


# ---------------------------------------------------------------- Cypher
def _node_map(ns: Namespace, var: str) -> str:
    fields = ", ".join(f"{k}: {var}.{k}" for k in NODE_KEYS)
    formula, symbol = q(ns.label("Formula")), q(ns.label("FormulaSymbol"))
    in_section, defined_for = q(ns.rel("IN_SECTION")), q(ns.rel("DEFINED_FOR"))
    return (f"{{{fields}, labels: labels({var}), "
            f"equation_number: CASE WHEN {var}:{formula} THEN head([({var})-[s_:{in_section}]->() | "
            f"s_.equation_number]) END, "
            f"definition_block_id: CASE WHEN {var}:{symbol} THEN head([({var})-[d_:{defined_for}]->() | "
            f"d_.definition_block_id]) END}}")


def _rel_map(var: str) -> str:
    fields = ", ".join(f"{k}: {var}.{k}" for k in REL_KEYS)
    return f"{{type: type({var}), from: startNode({var}).id, to: endNode({var}).id, {fields}}}"


def cy_meta(ns: Namespace) -> str:
    return (f"// vkm-nav:meta-get\n"
            f"MATCH (m:{q(ns.label(N.META_LABEL))} {{id: $id}}) RETURN properties(m) AS props, NULL AS age")


def cy_find_terms(ns: Namespace) -> str:
    return (f"// vkm-nav:find-terms\n"
            f"MATCH (t:{q(ns.label('Term'))}) WHERE t.id = $text OR t.lemma_key IN $keys\n"
            f"RETURN t.id AS id, t.lemma AS lemma, t.lemma_key AS lemma_key, t.df_units AS df_units, "
            f"t.df_sources AS df_sources, t.language AS language, t.kind AS kind\n"
            f"ORDER BY t.df_units DESC, t.id LIMIT $limit")


def cy_find_terms_by_name(ns: Namespace) -> str:
    return (f"// vkm-nav:find-terms-names\n"
            f"MATCH (t:{q(ns.label('Term'))}) WHERE $norm IN t.name_keys\n"
            f"RETURN t.id AS id, t.lemma AS lemma, t.lemma_key AS lemma_key, t.df_units AS df_units, "
            f"t.df_sources AS df_sources, t.language AS language, t.kind AS kind\n"
            f"ORDER BY t.df_units DESC, t.id LIMIT $limit")


def path_rel_types(via: list[str] | None) -> list[str]:
    fams = via or list(N.FAMILIES)
    out: list[str] = []
    for f in fams:
        for t in N.FAMILIES[f]:
            if t not in out:
                out.append(t)
    return out


def _strength(ns: Namespace, var: str) -> str:
    """Cypher form of :func:`hop_strength` (ranking among equally short paths and among neighbours)."""
    t = f"type({var})"
    rank = (f"toFloat(CASE WHEN coalesce({var}.rank_in_term, 99) < coalesce({var}.rank_in_section, 99) "
            f"THEN coalesce({var}.rank_in_term, 99) ELSE coalesce({var}.rank_in_section, 99) END)")
    return (f"coalesce(CASE {t} WHEN '{ns.rel('CO_OCCURS')}' THEN {var}.npmi * {var}.n_units / ({var}.n_units + 2.0) "
            f"WHEN '{ns.rel('SAME_TERM_AS')}' THEN 0.9 WHEN '{ns.rel('CONTAINS_TERM')}' THEN 0.6 "
            f"WHEN '{ns.rel('MENTIONED_IN')}' THEN 1.0 / (1.0 + log({rank})) "
            f"WHEN '{ns.rel('SYMBOL_OF')}' THEN CASE {var}.match WHEN 'FULL' THEN 0.9 WHEN 'PREFIX' THEN 0.7 "
            f"ELSE 0.5 END "
            f"WHEN '{ns.rel('IN_TOPIC')}' THEN coalesce({var}.similarity, 0.5) "
            f"WHEN '{ns.rel('RELATED_TOPIC')}' THEN coalesce({var}.cosine, 0.5) ELSE 0.8 END, 0.01)")


def _path_pattern(ns: Namespace, rel_types: list[str], max_len: int, labels: tuple[str, ...], fn: str) -> str:
    max_len = int(max_len)
    if not 1 <= max_len <= MAX_PATH_LEN:
        raise ValueError("max_len out of range")
    types = "|".join(q(ns.rel(t)) for t in rel_types)
    allowed = " OR ".join(f"x:{q(ns.label(lb))}" for lb in labels)
    term = q(ns.label("Term"))
    return (f"MATCH (a:{term} {{id: $a}}), (b:{term} {{id: $b}})\n"
            f"MATCH p = {fn}((a)-[:{types}*..{max_len}]-(b))\n"
            f"WHERE all(x IN nodes(p) WHERE {allowed})\n")


def cy_shortest_length(ns: Namespace, rel_types: list[str], max_len: int,
                       labels: tuple[str, ...] = N.PATH_LABELS) -> str:
    """Length of one shortest path (no row: none within ``max_len``) — decides how the paths are ranked."""
    return (f"// vkm-nav:shortest-length {int(max_len)}\n" + _path_pattern(ns, rel_types, max_len, labels, "shortestPath")
            + "RETURN length(p) AS n LIMIT 1")


def cy_paths(ns: Namespace, rel_types: list[str], max_len: int, labels: tuple[str, ...] = N.PATH_LABELS,
             ordered: bool = False) -> str:
    """All shortest paths (up to ``$cap``). ``ordered`` (short paths, whose number is bounded by the degree of an
    end) ranks them by the product of hop strengths in the database, so the cap keeps the strongest; longer paths
    stream unordered and are ranked by :func:`shape_paths`."""
    order = (f"WITH p, reduce(s = 1.0, r IN relationships(p) | s * ({_strength(ns, 'r')})) AS strength "
             f"ORDER BY strength DESC\n") if ordered else ""
    return (f"// vkm-nav:paths {int(max_len)}\n" + _path_pattern(ns, rel_types, max_len, labels, "allShortestPaths")
            + order
            + f"RETURN [x IN nodes(p) | {_node_map(ns, 'x')}] AS nodes, "
              f"[r IN relationships(p) | {_rel_map('r')}] AS rels\n"
              f"LIMIT $cap")


def layer_of(node_id: str) -> str:
    """``DOCUMENT`` for ids of the canonical grammar (sources, works, pages, objects…), else ``NAVIGATION``."""
    if node_id.startswith(("VKM-SRC-", "VKM-WRK-", "AUT-", "VEN-")):
        return S.LAYER
    return N.LAYER_KEY


def _layer_label(ns: Namespace, layer: str) -> str:
    return q(ns.label(S.LAYER_LABELS[layer]))


# neighbours: co-occurrence by support-weighted NPMI (as explore_concept), then similarity, cosine, tf-idf, weight
_ORDER_R = ("coalesce(r.npmi * r.n_units / (r.n_units + 2.0), r.similarity, r.cosine, r.tfidf, r.weight, 0.0)")


def cy_neighbourhood(ns: Namespace, layer: str) -> str:
    return (f"// vkm-nav:neighbourhood {layer}\n"
            f"MATCH (n:{_layer_label(ns, layer)} {{id: $id}})\n"
            f"CALL (n) {{\n"
            f"  OPTIONAL MATCH (n)-[r]-(m)\n"
            f"  WITH r, m, startNode(r) = n AS out\n"
            f"  ORDER BY {_ORDER_R} DESC, m.id\n"
            f"  WITH type(r) AS t, out, count(r) AS total, collect({{r: {_rel_map('r')}, m: {_node_map(ns, 'm')}}})"
            f"[0..$per_type] AS items\n"
            f"  WHERE t IS NOT NULL\n"
            f"  RETURN collect({{type: t, out: out, total: total, items: items}}) AS groups\n"
            f"}}\n"
            f"RETURN {_node_map(ns, 'n')} AS node, groups")


def cy_neighbourhood_2(ns: Namespace, layer: str) -> str:
    return (f"// vkm-nav:neighbourhood-2 {layer}\n"
            f"UNWIND $ids AS mid\n"
            f"MATCH (m:{_layer_label(ns, layer)} {{id: mid}})\n"
            f"CALL (m) {{\n"
            f"  MATCH (m)-[r]-(x) WHERE x.id <> $root\n"
            f"  WITH r, x, startNode(r) = m AS out\n"
            f"  ORDER BY {_ORDER_R} DESC, x.id\n"
            f"  WITH type(r) AS t, out, count(r) AS total, collect({{r: {_rel_map('r')}, m: {_node_map(ns, 'x')}}})"
            f"[0..$per_type] AS items\n"
            f"  RETURN collect({{type: t, out: out, total: total, items: items}}) AS groups\n"
            f"}}\n"
            f"RETURN mid, groups")


# ---------------------------------------------------------------- term keys (same morphology as the NAV build)
_MORPH: Any = None


def term_keys(text: str) -> tuple[list[str], str]:
    """Lemma keys of a phrase with the builder's morphology (``concepts.phrase_keys``), else the normalised text."""
    global _MORPH  # noqa: PLW0603 — one analyser per process
    from vkm_corpus.navigation.ids import norm_text

    norm = norm_text(text)
    try:
        from vkm_corpus.navigation.concepts import Morphology, phrase_keys

        if _MORPH is None:
            _MORPH = Morphology()
        keys = phrase_keys(text, _MORPH)
        if keys:
            return list(dict.fromkeys(keys + ([norm] if norm and norm not in keys else []))), _MORPH.name
    except Exception:  # noqa: BLE001 — numpy/pyarrow/pymorphy3 absent: exact normalised text only
        pass
    return ([norm] if norm else []), "normalised-text"


def rank_terms(rows: list[dict[str, Any]], text: str, keys: list[str]) -> list[dict[str, Any]]:
    """Exact id, then the whole-phrase key, then the other keys in order; ties by df_units."""
    pos = {k: i for i, k in enumerate(keys)}

    def score(r: dict[str, Any]) -> tuple[Any, ...]:
        if r.get("id") == text:
            return (0, 0, 0, r["id"])
        return (1, pos.get(r.get("lemma_key"), 99), -(r.get("df_units") or 0), r.get("id") or "")

    out = []
    for r in sorted(rows, key=score):
        match = "term_id" if r.get("id") == text else ("lemma_key" if r.get("lemma_key") in pos else "name")
        out.append({"term_id": r["id"], "lemma": r.get("lemma"), "df_units": r.get("df_units"),
                    "df_sources": r.get("df_sources"), "language": r.get("language"), "match": match})
    return out


# ---------------------------------------------------------------- shaping
def _page_of_block(block_id: str | None) -> str | None:
    if not block_id or ":" not in block_id:
        return None
    head = block_id.rsplit(":", 1)[0]
    return head if not head.endswith(":doc") else None


def _short(text: Any, n: int = NAME_CHARS) -> str | None:
    if text is None:
        return None
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def kind_of(labels: list[str] | None, ns: Namespace = Namespace()) -> str:
    for lb in labels or []:
        base = lb[len(ns.prefix):] if ns.prefix and lb.startswith(ns.prefix) else lb
        if base in KIND_OF_LABEL:
            return KIND_OF_LABEL[base]
    return "NODE"


def node_summary(node: dict[str, Any], ns: Namespace = Namespace()) -> dict[str, Any]:
    """{id, kind, name, pages[, source_id, level, df_units]} of a node map."""
    kind = kind_of(node.get("labels"), ns)
    name: str | None = None
    pages: list[str] = []
    if kind == "TERM":
        name = node.get("lemma")
    elif kind == "SECTION":
        name = _short(" ".join(x for x in (node.get("numbering"), node.get("title")) if x))
        pages = [p for p in dict.fromkeys((node.get("page_start_id"), node.get("page_end_id"))) if p]
    elif kind == "FORMULA":
        eq = node.get("equation_number")
        name = f"({eq})" if eq else (node.get("formula_kind") or "formula").lower()
        pages = [node["page_id"]] if node.get("page_id") else []
    elif kind == "SYMBOL":
        name = _short(" — ".join(x for x in (node.get("symbol"), node.get("definition")) if x))
        page = _page_of_block(node.get("definition_block_id"))
        pages = [page] if page else []
    elif kind == "PARAMETER":
        name = _short(" ".join(x for x in (node.get("symbol"), "=", node.get("value_text"), node.get("unit")) if x))
    elif kind == "TOPIC":
        name = _short(node.get("label") or node.get("title"))
    elif kind == "PAGE":
        name = node.get("id")
        pages = [node["id"]] if node.get("id") else []
    elif kind in ("BLOCK", "FIGURE", "TABLE", "BIBLIOGRAPHY_ENTRY"):
        name = (node.get("block_type") or kind).lower()
        pages = [node["page_id"]] if node.get("page_id") else []
    else:
        name = node.get("id")
    out: dict[str, Any] = {"id": node.get("id"), "kind": kind, "name": name, "pages": pages}
    for k in ("source_id", "level", "df_units", "df_sources", "language"):
        if node.get(k) is not None and not (k == "source_id" and kind in ("SOURCE",)):
            out[k] = node[k]
    return out


_REL_METRICS = ("npmi", "n_units", "n_sources", "tf", "tfidf", "rank_in_term", "rank_in_section", "kind", "match",
                "n_formulas", "similarity", "cosine", "weight", "number_text", "equation_number", "definition",
                "unit", "deepest")


def hop_pages(rel: dict[str, Any], a: dict[str, Any] | None = None, b: dict[str, Any] | None = None) -> list[str]:
    """Up to three page IDs locating a hop: the edge's own examples, else its blocks, else its endpoints."""
    pages: list[str] = []
    for key in ("examples", "page_ids"):
        pages += [p for p in (rel.get(key) or []) if p]
    for key in ("page_id",):
        if rel.get(key):
            pages.append(rel[key])
    for key in ("definition_block_id", "block_id"):
        page = _page_of_block(rel.get(key))
        if page:
            pages.append(page)
    if not pages:
        for end in (a, b):
            if end:
                pages += node_summary(end).get("pages") or []
    return list(dict.fromkeys(pages))[:3]


def hop_strength(rel: dict[str, Any]) -> float:
    t = rel.get("type") or ""
    base = t.split("_", 1)[1] if t.startswith("VKMTEST") else t
    if base == "CO_OCCURS":
        n = float(rel.get("n_units") or 0)
        return max(0.01, float(rel.get("npmi") or 0.0) * n / (n + 2.0))
    if base == "SAME_TERM_AS":
        return 0.9
    if base == "CONTAINS_TERM":
        return 0.6
    if base == "MENTIONED_IN":
        rank = min(int(rel.get("rank_in_term") or 99), int(rel.get("rank_in_section") or 99))
        return 1.0 / (1.0 + math.log(max(rank, 1)))
    if base == "SYMBOL_OF":
        return {"FULL": 0.9, "PREFIX": 0.7, "NEAR_START": 0.5}.get(rel.get("match") or "", 0.4)
    if base in ("IN_TOPIC", "RELATED_TOPIC"):
        v = rel.get("similarity") if base == "IN_TOPIC" else rel.get("cosine")
        return float(v) if isinstance(v, (int, float)) and v > 0 else 0.5
    return 0.8


def _metrics(rel: dict[str, Any]) -> dict[str, Any]:
    return {k: (round(rel[k], 4) if isinstance(rel[k], float) else rel[k]) for k in _REL_METRICS
            if rel.get(k) is not None}


def shape_paths(raw: list[dict[str, Any]], limit: int, ns: Namespace = Namespace()) -> list[dict[str, Any]]:
    """Rank raw paths (``nodes``, ``rels`` maps in path order) and keep ``limit`` compact ones."""
    shaped = []
    seen: set[tuple[str, ...]] = set()
    for p in raw:
        nodes, rels = p.get("nodes") or [], p.get("rels") or []
        sig = tuple(n.get("id") for n in nodes) + tuple(r.get("type") for r in rels)
        if sig in seen:
            continue
        seen.add(sig)
        by_id = {n.get("id"): n for n in nodes}
        score = 1.0
        hops = []
        for i, r in enumerate(rels):
            a, b = nodes[i], nodes[i + 1]
            score *= hop_strength(r)
            rtype = r.get("type")
            base = rtype[len(ns.prefix) + 1:] if ns.prefix and rtype.startswith(ns.prefix.upper() + "_") else rtype
            hops.append({"rel": base, "from": a.get("id"), "to": b.get("id"),
                         "direction": "forward" if r.get("from") == a.get("id") else "backward",
                         "pages": hop_pages(r, by_id.get(r.get("from")), by_id.get(r.get("to"))), **_metrics(r)})
        shaped.append({"length": len(rels), "score": round(score, 6),
                       "nodes": [node_summary(n, ns) for n in nodes], "hops": hops})
    shaped.sort(key=lambda s: (s["length"], -s["score"], [n["id"] for n in s["nodes"]]))
    return shaped[: max(1, int(limit))]


def _group_items(group: dict[str, Any], ns: Namespace) -> list[dict[str, Any]]:
    out = []
    for item in group.get("items") or []:
        r, m = item.get("r") or {}, item.get("m") or {}
        s = node_summary(m, ns)
        hop = hop_pages(r)
        if hop:
            s["pages"] = list(dict.fromkeys(hop + (s.get("pages") or [])))[:3]
        s.update(_metrics(r))
        out.append(s)
    return out


def _base_type(t: str, ns: Namespace) -> str:
    return t[len(ns.prefix) + 1:] if ns.prefix and t.startswith(ns.prefix.upper() + "_") else t


def shape_neighbourhood(node: dict[str, Any], groups: list[dict[str, Any]], second: dict[str, list[dict[str, Any]]],
                        limit: int, ns: Namespace = Namespace()) -> dict[str, Any]:
    """Groups by (type, direction) with totals; items shared fairly so the answer has at most ``limit`` neighbours."""
    groups = sorted([g for g in groups or [] if g.get("type")],
                    key=lambda g: (0 if _base_type(g["type"], ns) in N.REL_TYPE_NAMES else 1,
                                   _base_type(g["type"], ns), not g.get("out")))
    items = [_group_items(g, ns) for g in groups]
    taken = _share(items, limit)
    edges = []
    for i, g in enumerate(groups):
        base = _base_type(g["type"], ns)
        edges.append({"rel": base, "direction": "out" if g.get("out") else "in", "total": int(g.get("total") or 0),
                      "layer": "NAV" if base in N.REL_TYPE_NAMES else "DOCUMENT",
                      "neighbours": items[i][: taken[i]]})
    out: dict[str, Any] = {"node": node_summary(node, ns), "edges": edges}
    if second:
        entries = [(mid, g) for mid, mid_groups in second.items() for g in (mid_groups or []) if g.get("type")]
        items2 = [_group_items(g, ns) for _mid, g in entries]
        taken2 = _share(items2, limit)
        out["depth2"] = [{"via": mid, "rel": _base_type(g["type"], ns), "direction": "out" if g.get("out") else "in",
                          "total": int(g.get("total") or 0), "neighbours": items2[i][: taken2[i]]}
                         for i, (mid, g) in enumerate(entries) if taken2[i]]
    return out


def _share(items: list[list[dict[str, Any]]], limit: int) -> list[int]:
    """Round-robin budget: how many items of each list to keep so that the total is at most ``limit``."""
    budget, taken = max(1, int(limit)), [0] * len(items)
    while budget > 0 and any(taken[i] < len(x) for i, x in enumerate(items)):
        for i, x in enumerate(items):
            if budget and taken[i] < len(x):
                taken[i] += 1
                budget -= 1
    return taken


def depth2_ids(shaped: dict[str, Any], per_node: int = 10) -> list[tuple[str, str]]:
    """(id, layer) of the depth-1 neighbours to expand at depth 2 (NAV nodes first)."""
    ids: list[tuple[str, str]] = []
    for e in shaped.get("edges") or []:
        for n in e.get("neighbours") or []:
            nid = n.get("id")
            if nid and nid not in {x for x, _ in ids}:
                ids.append((nid, layer_of(nid)))
    ids.sort(key=lambda x: (x[1] != N.LAYER_KEY,))
    return ids[: max(1, per_node)]
