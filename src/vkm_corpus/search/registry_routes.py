"""Registry routes of the hybrid search (agent Г, 13.10): pages of an author's own works and pages of catalogued
objects, as extra RRF legs of the PAGE candidates plus a promotion — the topic ranks, the registry lifts.

**Author route** (``stages.author_route``; ``vkm_corpus.search.authors``). The review of 12.10 found that «работы
Баряха по сдвижению» served the pages where Barjakh is *cited* (the bibliographic route ranks reference-list
entries). When the query names an author whom the work registry confirms (EXACT_KEY / INFLECTED / TRANSLIT name key:
«работы Баряха», «Барях А.А. и др.», «по Кудряшову», «papers by Baryakh», a registry surname alone), the works of the
matched clusters give their registered sources and

* with a topic (the query without the name and cue words): BM25 of the topic inside those sources (the request's
  filters and duplicate collapsing apply) → leg ``author:PAGE``; after late, the graph legs and the visual route, the
  leg's pages that are among E's first :data:`AUTHOR_PROMOTE_K` pages move to the front, order kept;
* without a topic («работы Баряха»): the first :data:`FIRST_PAGES` pages of each source (title pages), lead-author
  works first, newer first → leg ``author:PAGE``; up to :data:`FRONT_MAX` of them go in front of the page list.

The bibliographic route is untouched (its BIB_ENTRY leg still runs on its cues); the author's own pages now precede
the pages that cite him — unless the query asks for citations or reference lists («список литературы», «литература
о …», «в списках литературы», «ссылки на», «цитирует»: the cues ``bibliography_phrase`` / ``citation_phrase`` of
``search.intent``): then the leg stays one RRF leg and nothing is promoted (``promotion`` OFF_CITATION_QUERY). Several
clusters of one surname (homonyms) all count and are listed — the registry never chooses. Queries without a registry
author are unchanged (status NOT_DETECTED / NO_REGISTRY_MATCH, no leg).

**Entity route** (``stages.object_ref.entity_route``; ``vkm_corpus.search.entities``). A recognised «скв. 75»,
«профильная линия 12», «блок 201», «панель 15», «репер 13» (``search.object_refs``: BOREHOLE, LINE, BLOCK, PANEL,
BENCHMARK) looks the number up in the entity index of the catalogue pack: every row of that kind and number — all
sites, all sources, each its own object — gives its page. Pages that pass the request's filters (duplicates
collapsed) form the leg ``entity:PAGE``: with a topic in BM25 order of the topic inside them, the others (and,
without a topic, all) by the number of catalogue records of the object on the page (a table of picks before a map
label), then index order. They join the pages flagged by the reference phrase (``matched``
``entity``) — with a topic promoted inside E's first ``OBJECT_REF_PROMOTE_K`` pages (Q5), without a topic up to
:data:`FRONT_MAX` of them go in front («скважина 75» → the index pages of borehole 75 first, among them table 3.1 of
SRC-012, where «скважина» and «75» are in different cells and the phrase cannot meet). The index row is navigation
(AUTO, the catalogue's site and status), never identity: the same number of two mines are two rows.

Both routes need their registry (``RegistrySignals``: the canon's authors, the catalogue pack's ``entity_index``); a
missing one is the status UNAVAILABLE, never an error. ``author_route`` / ``entity_route`` = False switch them off
(benchmarks). Depths and K are MODEL_CHOICE.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Mapping

from vkm_corpus.search.authors import AuthorIndex, CanonAuthors, mentions, resolve_route
from vkm_corpus.search.entities import IDENTITY_RULE, REF_KIND, CatalogueEntities, EntityIndex, objects
from vkm_corpus.search.graph_stages import on_pages
from vkm_corpus.search.intent import bibliographic_intent
from vkm_corpus.search.mappings import alias_name
from vkm_corpus.search.object_refs import ObjectRef, has_topic
from vkm_corpus.search.query import MAX_SIZE, SearchRequest, compile_filters, search

AUTHOR_LEG_DEPTH = 40          # pages of the author leg
# «работы X» are the works X wrote: a supervised thesis, an edited volume or a translation is not one (live check
# 13.10: «работы Баряха» put the dissertations Барях supervised, VKM-SRC-065 / 066, first); a missing role counts
OWN_ROLES = frozenset({"AUTHOR", "CORPORATE_AUTHOR", "UNKNOWN", None})
AUTHOR_MAX_SOURCES = 60        # sources of the matched authors' works searched (lead-author works, newer first)
FIRST_PAGES = 2                # title pages per source when the query names no topic
AUTHOR_PROMOTE_K = 100         # with a topic: the author's pages move up inside E's first K pages
# a bibliographic query with a topic («работы X о …», «articles by X in …») reads both ways — his own works or the records of them:
# at most this many of his pages move up, the rest stay one RRF leg (E11 route check 13.10: promoting 7–8 own pages
# lowered T-BIB-003 nDCG@10 P 1.000 → 0.762 and T-BIB-006 0.833 → 0.578, whose qrels are mostly reference entries)
AUTHOR_PROMOTE_MAX_BIB = 3
ENTITY_LEG_DEPTH = 60          # pages of the entity leg
FRONT_MAX = 20                 # without a topic: at most this many leg pages are put in front
CITATION_CUES = frozenset({"bibliography_phrase", "citation_phrase"})   # the query asks for citations: no promotion


class RegistrySignals:
    """The registry the routes read: author clusters of the canon (``CanonAuthors``) and the entity index of the
    catalogue pack (``CatalogueEntities``). Either may be given ready (an :class:`AuthorIndex` / :class:`EntityIndex`,
    or an object with ``index()``) — tests and benchmarks; a failing source is reported as unavailable."""

    def __init__(self, canon: Any = None, catalogues: Any = None, *, authors: Any = None,
                 entities: Any = None) -> None:
        self._authors = authors if authors is not None else (CanonAuthors(canon) if canon is not None else None)
        self._entities = entities if entities is not None else (
            CatalogueEntities(catalogues) if catalogues is not None else None)
        self.errors: dict[str, str] = {}

    def _get(self, src: Any, name: str, strict: bool) -> Any:
        if src is None:
            return None
        if isinstance(src, (AuthorIndex, EntityIndex)):
            return src
        try:
            out = src.index()
            self.errors.pop(name, None)
            return out
        except Exception as exc:  # noqa: BLE001 - a route without its registry reports UNAVAILABLE
            self.errors[name] = type(exc).__name__
            if strict:                             # the lookup endpoints answer with the cause
                raise
            return None

    def author_index(self, *, strict: bool = False) -> AuthorIndex | None:
        return self._get(self._authors, "authors", strict)

    def entity_index(self, *, strict: bool = False) -> EntityIndex | None:
        return self._get(self._entities, "entities", strict)

    def status(self) -> dict[str, Any]:
        a, e = self.author_index(), self.entity_index()
        return {"authors": None if a is None else {"clusters": len(a.authors), "works": len(a.works),
                                                   "snapshot_id": a.snapshot_id},
                "entities": None if e is None else {"rows": len(e.rows), "pack_id": e.pack_id},
                **({"errors": dict(self.errors)} if self.errors else {})}


def _with_sources(filters: Mapping[str, Any], sources: list[str]) -> dict[str, Any] | None:
    """The request's filters narrowed to ``sources`` (meeting its own ``source_id`` filter); None when empty."""
    out = dict(filters)
    own = out.get("source_id")
    allowed = None if not own else set(own if isinstance(own, (list, tuple)) else [own])
    ids = [s for s in sources if allowed is None or s in allowed]
    if not ids:
        return None
    out["source_id"] = ids
    return out


# ---------------------------------------------------------------------------------------------- author route
def first_pages(client: Any, req: Any, prefix: str, sources: list[str], per_source: int = FIRST_PAGES
                ) -> list[str]:
    """The first ``per_source`` pages of each source that pass the request's filters, round by round in the order of
    ``sources`` (page 1 of every source, then page 2 …); duplicates collapsed unless ``include_duplicates``."""
    filters = _with_sources(req.filters, sources)
    if filters is None:
        return []
    clauses, must_not = compile_filters(filters)
    body = {"size": min(len(sources) * per_source * 2, 1000),
            "_source": {"includes": ["id", "source_id", "page_index", "dup_group_id"]},
            "query": {"bool": {"filter": [*clauses, {"range": {"page_index": {"lte": int(per_source)}}}],
                               "must_not": must_not}},
            "sort": [{"source_id": "asc"}, {"page_index": "asc"}, {"id": "asc"}]}
    resp = client.search(index=alias_name(prefix, "pages"), body=body)
    by_source: dict[str, list[tuple[int, str, str]]] = {}
    for h in resp.get("hits", {}).get("hits", []):
        src = h.get("_source") or {}
        pid = str(src.get("id") or h.get("_id"))
        by_source.setdefault(str(src.get("source_id") or pid.split(":")[0]), []).append(
            (int(src.get("page_index") or 0), pid, str(src.get("dup_group_id") or pid)))
    out, groups = [], set()
    for rnd in range(per_source):
        for s in sources:
            pages = sorted(by_source.get(s, []))
            if rnd < len(pages):
                _i, pid, group = pages[rnd]
                g = pid if req.include_duplicates else group
                if g not in groups:
                    groups.add(g)
                    out.append(pid)
    return out


def author_leg(client: Any, req: Any, prefix: str, registry: RegistrySignals | None,
               bm25: Callable[[str, list[str] | None, int], list[str]], timings: dict[str, float]
               ) -> tuple[list[tuple[str, float]], dict[str, dict[str, Any]], dict[str, Any]]:
    """(leg [(page id, score)], page → its author trace, ``stages.author_route``)."""
    if getattr(req, "author_route", None) is False:
        return [], {}, {"status": "OFF"}
    if "PAGE" not in req.kinds:
        return [], {}, {"status": "SKIPPED_NO_PAGE_KIND"}
    if not mentions(req.query):                    # no candidate word: the registry is not even opened
        return [], {}, {"status": "NOT_DETECTED"}
    t1 = time.perf_counter()
    index = registry.author_index() if registry is not None else None
    route = resolve_route(req.query, index)
    stage: dict[str, Any] = {"status": route.status}
    if route.mentions:
        stage["mentions"] = route.mentions
    if route.ignored:
        stage["fuzzy_ignored"] = route.ignored
    if route.status != "MATCHED":
        return [], {}, stage
    assert index is not None
    all_works = index.works_of([a.author_id for a in route.authors])
    works = [w for w in all_works if any(a.get("role") in OWN_ROLES for a in w["matched"])]
    other_roles = sorted({str(a.get("role")) for w in all_works if w not in works for a in w["matched"]})
    sources: list[str] = []
    of_source: dict[str, dict[str, Any]] = {}
    for w in works:
        for s in sorted(w["sources"], key=lambda s: (not s.get("is_primary"), s["source_id"])):
            sid = s["source_id"]
            if sid not in of_source:
                sources.append(sid)
                of_source[sid] = {"work_id": w["work"].get("work_id"),
                                  "author_ids": sorted({a["author_id"] for a in w["matched"]}),
                                  "ordinal": min((a.get("ordinal") or 99) for a in w["matched"]) if w["matched"]
                                  else None}
    searched = sources[:AUTHOR_MAX_SOURCES]
    topic = route.topic if has_topic(route.topic) else ""
    pages = bm25(topic, searched, AUTHOR_LEG_DEPTH) if topic else \
        first_pages(client, req, prefix, searched)[:AUTHOR_LEG_DEPTH]
    leg = [(pid, 1.0 / rank) for rank, pid in enumerate(pages, 1)]
    info = {pid: {"rank": rank, "mode": "TOPIC" if topic else "FIRST_PAGES", "source_id": pid.split(":")[0],
                  **of_source.get(pid.split(":")[0], {})} for rank, pid in enumerate(pages, 1)}
    timings["author_route"] = round((time.perf_counter() - t1) * 1e3, 2)
    citations = sorted(CITATION_CUES & set(bibliographic_intent(req.query).cues))
    stage.update({"status": "APPLIED" if leg else "NO_PAGES", "authors": [a.as_record() for a in route.authors],
                  "works": len(works), "works_other_roles": len(all_works) - len(works),
                  "other_roles": other_roles, "sources": len(sources), "sources_searched": len(searched),
                  "topic": topic or None, "mode": "TOPIC" if topic else "FIRST_PAGES", "pages": len(leg),
                  "depth": AUTHOR_LEG_DEPTH,
                  "fusion": "one more RRF leg of the PAGE candidates (pages of the author's own works)",
                  "rule": (f"the leg's pages among E's first {AUTHOR_PROMOTE_K} pages move to the front, order kept "
                           "(the topic ranks)") if topic else
                          (f"no topic: up to {FRONT_MAX} first pages of the author's works go in front"),
                  "promotion": "OFF_CITATION_QUERY" if citations else ("INSIDE_HEAD" if topic else "FRONT"),
                  "identity_note": "authors are name-key clusters (NAME_KEY_ONLY), not identified persons"})
    if citations:
        stage["rule"] = (f"the query asks for citations ({', '.join(citations)}): the author's pages are one RRF leg, "
                         "nothing is promoted (the bibliographic route keeps the citing pages)")
    if len(route.authors) > 1:
        stage["homonym_note"] = "several clusters matched: the works of all of them are searched, none is chosen"
    return leg, info, stage


# ---------------------------------------------------------------------------------------------- entity route
def entity_leg(client: Any, req: Any, prefix: str, registry: RegistrySignals | None, refs: list[ObjectRef],
               topic: str, page_filter: Callable[..., dict[str, str]], timings: dict[str, float]
               ) -> tuple[list[tuple[str, float]], dict[str, dict[str, Any]], dict[str, Any] | None]:
    """(leg [(page id, score)], page → its entity trace, ``stages.object_ref.entity_route`` or None when the query
    names no catalogued kind)."""
    wanted = [r for r in refs if r.kind in REF_KIND]
    if not wanted:
        return [], {}, None
    if getattr(req, "entity_route", None) is False:
        return [], {}, {"status": "OFF"}
    t1 = time.perf_counter()
    index = registry.entity_index() if registry is not None else None
    if index is None:
        return [], {}, {"status": "UNAVAILABLE", "reason": "no entity index in the served catalogue pack"}
    rows: list[dict[str, Any]] = []
    for r in wanted:
        rows += index.lookup(REF_KIND[r.kind], r.number)
    by_page: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["page_id"]:
            by_page.setdefault(row["page_id"], []).append(row)
    allowed = page_filter(client, prefix, list(by_page), req.filters) if by_page else {}
    kept, groups = [], set()
    for pid in by_page:
        if pid not in allowed:
            continue
        g = pid if req.include_duplicates else allowed[pid]
        if g not in groups:
            groups.add(g)
            kept.append(pid)
    ranked: list[str] = []
    with_topic = bool(topic) and has_topic(topic)
    if with_topic and kept:
        bm = search(client, SearchRequest(query=topic, kinds=("PAGE",), filters={**req.filters, "page_id": kept},
                                          size=min(len(kept), MAX_SIZE), include_duplicates=req.include_duplicates,
                                          exact=req.exact), prefix)
        ranked = [h.id for h in bm.hits if h.id in by_page]
    # the rest: pages with more catalogue records of the object first (a table of picks before a map label), then
    # index order (MODEL_CHOICE)
    weight = {p: sum(len([x for x in str(r["record_ids"]).split(";") if x]) for r in by_page[p]) for p in kept}
    rest = sorted((p for p in kept if p not in ranked), key=lambda p: (-weight[p], kept.index(p)))
    order = ranked + rest
    order = order[:ENTITY_LEG_DEPTH]
    leg = [(pid, 1.0 / rank) for rank, pid in enumerate(order, 1)]
    info = {pid: {"rank": rank, "topic_match": pid in ranked,
                  "entity_ids": sorted({x["entity_id"] or x["entity_row_id"] for x in by_page[pid]}),
                  "sites": sorted({x["site"] for x in by_page[pid] if x["site"]}),
                  "rows": sorted({x["entity_row_id"] for x in by_page[pid]})}
            for rank, pid in enumerate(order, 1)}
    timings["entity_route"] = round((time.perf_counter() - t1) * 1e3, 2)
    objs = objects(rows)
    stage = {"status": "APPLIED" if leg else ("NO_PAGES" if rows else "NOT_IN_INDEX"),
             "pack_id": index.pack_id, "rows": len(rows), "objects": len(objs),
             "sites": sorted({o["site"] or "UNKNOWN" for o in objs}), "pages": len(leg),
             "pages_filtered_out": len(by_page) - len(kept), "topic_matched": len(ranked),
             "depth": ENTITY_LEG_DEPTH,
             "fusion": "one more RRF leg of the PAGE candidates (pages of the catalogued objects with this number)",
             "rule": ("with a topic: the index pages join the promotion inside E's first pages (the topic ranks)"
                      if with_topic else f"no topic: up to {FRONT_MAX} index pages go in front"),
             "identity_rule": IDENTITY_RULE}
    return leg, info, stage


# ---------------------------------------------------------------------------------------------- ordering
def promote(order: list[str], kind_of: Mapping[str, str], flagged: Iterable[str], *, k: int | None = None,
            front: bool = False, front_max: int = FRONT_MAX, limit: int | None = None
            ) -> tuple[list[str], dict[str, int]]:
    """Pages of ``flagged`` move to the front of the PAGE positions: among E's first ``k`` pages (order kept) or,
    with ``front``, the first ``front_max`` of ``flagged`` in their own order wherever they are (added when absent);
    ``limit`` caps how many move. → (new order, moved page → its PAGE rank before or None if it was not in the
    list)."""
    pages = [key for key in order if kind_of.get(key, "PAGE") == "PAGE"]
    before = {p: i for i, p in enumerate(pages, 1)}
    flagged = list(dict.fromkeys(flagged))
    if front:
        up = flagged[:front_max]
    else:
        wanted = set(flagged)
        up = [p for p in pages[:k] if p in wanted]
    if limit is not None:
        up = up[:limit]
    if not up or pages[:len(up)] == up:
        return list(order), {}
    taken = set(up)
    new_pages = up + [p for p in pages if p not in taken]
    moved = {p: before.get(p) for i, p in enumerate(up, 1) if before.get(p) != i}
    return on_pages(order, kind_of, new_pages), moved
