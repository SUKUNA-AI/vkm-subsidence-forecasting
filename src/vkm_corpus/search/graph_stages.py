"""Graph stages of the hybrid search (agent GS; GRAPH_SEARCH_V1, ``benchmarks/graph_search_v1``): the structure the
navigation layer already has — duplicate groups, the section tree, the concept graph with the term dictionary, CITES
and the topic tree — used around E's ranking without any model change (no new encoder or reranker, no retraining, no
re-encoding; the deployed models stay exactly as they are).

E (``search.hybrid``) = BM25 + dense → RRF → the late window (RRF top ``late_candidates``) re-scored by mLateOn MaxSim.
Every stage is a separate switch (request ``graph``; server default :data:`DEFAULTS`, ``VKM_HYBRID_GRAPH``):

* **G1 ``collapse``** — a page whose text is at least half shared with a better-ranked page leaves the ranking and is
  listed on that hit (``copies``). The pairs come from NAV ``dup_members``: clusters SAME_WORK_COPY, REPRINT and
  SHARED_ABSTRACT; a quote (PARTIAL_OVERLAP) or a template (BOILERPLATE) never makes a copy. The share is the shared
  text of the page's units (``shared_chars``) over its characters. An object hit (figure, table, formula) leaves when a
  better-ranked object of the same NAV ``object_dup_clusters`` group is shown (BOILERPLATE groups never collapse).
  Runs last, over all kinds: the top k holds distinct content.
* **G2 ``cohesion``** — when ≥ 2 of the first 10 pages (after late) fall in the same deep NAV section (level ≥ 2,
  ≤ 16 pages; chapters never), the other pages of that section — nearest to the hits first, ≤ 4 per section, ≤ 12 —
  form a leg fused after late: a section the ranking already found twice lends its other pages (tables, figures and
  formulas the text scores miss) the section's relevance.
* **G3 ``concepts``** — the query's phrases (the concept graph's morphology) → their synonyms and abbreviations from
  the term dictionary (score ≥ 0.8) and one narrower term of the concept graph: ≤ 2 other wordings
  (``navigation.expansion_query``), searched by BM25 only.
* **G4 ``cites``** — the works cited by and citing the works of the RRF top-10 sources (canon CITES): BM25 over
  the pages of those sources.
* **G5 ``topics``** — the level-1 NAV topics that hold ≥ 2 of the first 10 pages: E's own later candidates whose deepest
  section belongs to such a topic are lifted (a prior over E's candidates; no new page).

Fusion — the TERM_DICTIONARY_V1 lesson (extra legs inside E's RRF push relevant pages out of the late window): a
graph leg never enters E's RRF. ``post`` (after late): the first ``head`` positions stay as E served them and the rest
of E's order is fused with the legs by weighted RRF (k = 60, E wins ties). ``window`` (G3, G4 only): the leg's pages
widen the late window — every E candidate stays in it — and the late score decides.

Everything the stages read is DERIVED navigation (``AUTO_EXTRACTED_UNREVIEWED``): it reorders or adds candidates and
never becomes evidence. The functions are pure over ranked lists and a :class:`GraphMaps` lookup; ``search.hybrid``
runs them in the served path and the benchmark runs the same functions over its local copy of E.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

STAGES: tuple[str, ...] = ("collapse", "cohesion", "concepts", "cites", "topics")
CODES: dict[str, str] = {"collapse": "G1", "cohesion": "G2", "concepts": "G3", "cites": "G4", "topics": "G5"}
PAGE_STAGES: tuple[str, ...] = ("cohesion", "concepts", "cites", "topics")
# server default until GRAPH_SEARCH_V1 decides (benchmarks/graph_search_v1/RESULTS.md); VKM_HYBRID_GRAPH overrides
DEFAULTS: frozenset[str] = frozenset()
MODES: tuple[str, ...] = ("post", "window")
REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
NOTE = ("graph stages: DERIVED navigation (AUTO_EXTRACTED_UNREVIEWED) reorders or adds candidates of the hybrid "
        "search; copies, sections, topics, citations and term pairs are navigation, never evidence")


def parse_stages(value: Any) -> tuple[str, ...] | None:
    """``None`` → None (the server default); ``"none"``/``""``/``[]`` → (); ``"a,b"`` or an iterable → the named
    stages in :data:`STAGES` order. Unknown names raise ``ValueError``."""
    if value is None:
        return None
    if isinstance(value, str):
        items = [x.strip().lower() for x in value.replace(";", ",").split(",")]
        items = [x for x in items if x and x != "none"]
    else:
        items = [str(x).strip().lower() for x in value]
    if "all" in items:
        return STAGES
    bad = sorted(set(items) - set(STAGES))
    if bad:
        raise ValueError(f"unknown graph stages {bad}; known: {', '.join(STAGES)}")
    return tuple(s for s in STAGES if s in set(items))


# ---------------------------------------------------------------------------------------------------- parameters
@dataclass(frozen=True)
class GraphParams:
    """Parameters of the stages (GRAPH_SEARCH_V1 preregistration; MODEL_CHOICE). The weights and modes marked
    ``dev`` were chosen on the dev split of the benchmark (results: ``benchmarks/graph_search_v1/RESULTS.md``)."""

    head: int = 10                              # post-late legs never change the first ``head`` positions
    rrf_k: int = 60
    # G1 collapse
    copy_min_coverage: float = 0.5              # shared text / page characters (the NAV containment threshold)
    copy_kinds: tuple[str, ...] = ("SAME_WORK_COPY", "REPRINT", "SHARED_ABSTRACT")
    object_skip_kinds: tuple[str, ...] = ("BOILERPLATE",)
    # G2 cohesion
    cohesion_top: int = 10
    cohesion_min_hits: int = 2
    cohesion_min_level: int = 2                 # never level-1 chapters
    cohesion_max_pages: int = 16
    cohesion_per_section: int = 4
    cohesion_leg: int = 12
    cohesion_weight: float = 1.0                # dev: {0.5, 1.0}
    # G3 concepts
    concepts_min_score: float = 0.8
    concepts_narrower: bool = True              # dev: {False, True}
    concepts_narrower_min_df: int = 5
    concepts_depth: int = 50
    concepts_mode: str = "post"                 # dev: {post, window}
    concepts_weight: float = 1.0
    concepts_window: int = 50
    # G4 cites
    cites_seed: int = 10
    cites_depth: int = 20
    cites_mode: str = "post"                    # dev: {post, window}
    cites_weight: float = 0.5
    cites_window: int = 20
    # G5 topics
    topics_top: int = 10
    topics_min_hits: int = 2
    topics_level: int = 1
    topics_leg: int = 20
    topics_weight: float = 1.0                  # dev: {0.5, 1.0}

    def validate(self) -> "GraphParams":
        if self.concepts_mode not in MODES or self.cites_mode not in MODES:
            raise ValueError(f"graph modes must be one of {MODES}")
        if self.head < 0 or self.rrf_k < 1:
            raise ValueError("head >= 0 and rrf_k >= 1")
        return self

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------------------------------- lookups
@dataclass(frozen=True)
class SectionRef:
    section_id: str
    level: int
    n_pages: int


@dataclass
class GraphMaps:
    """The lookup the stages read in one request (built once per NAV build; never mutated after the build)."""

    snapshot_id: str | None = None
    coverage: dict[str, dict[str, float]] = field(default_factory=dict)       # page → {covering page: share}
    copy_kinds: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    object_groups: dict[str, tuple[str, str]] = field(default_factory=dict)   # object → (cluster, kind)
    page_sections: dict[str, tuple[SectionRef, ...]] = field(default_factory=dict)
    section_pages: dict[str, tuple[tuple[str, int], ...]] = field(default_factory=dict)
    section_topics: dict[tuple[str, int], str] = field(default_factory=dict)  # (section, level) → topic
    cite_neighbours: dict[str, frozenset[str]] = field(default_factory=dict)  # source → sources (CITES)
    parts: dict[str, str] = field(default_factory=dict)                       # part → OK | MISSING … | ERROR …
    build_ms: float | None = None

    def sections_of(self, page_id: str) -> tuple[SectionRef, ...]:
        return self.page_sections.get(page_id, ())


class GraphSignals(Protocol):
    """What ``search.hybrid`` needs from the navigation layer: the lookup (:meth:`view`) and G3's other wordings."""

    def view(self) -> GraphMaps: ...

    def expansions(self, query: str, params: GraphParams) -> dict[str, Any]: ...


class StaticSignals:
    """Fixed lookups (tests, the benchmark): ``expansions`` maps a query to the answer of ``expand_query``."""

    def __init__(self, maps: GraphMaps, expansions: Mapping[str, dict[str, Any]] | None = None) -> None:
        self.maps = maps
        self._expansions = dict(expansions or {})

    def view(self) -> GraphMaps:
        return self.maps

    def expansions(self, query: str, params: GraphParams) -> dict[str, Any]:
        return self._expansions.get(query) or {"query": query, "expansions": []}


def _pages_of_rows(rows: Iterable[tuple[str, str, int]]) -> tuple[dict[str, tuple[tuple[str, int], ...]],
                                                                      dict[str, list[str]]]:
    by_section: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for sid, pid, idx in rows:
        by_section[sid].append((pid, int(idx if idx is not None else 0)))
    pages = {sid: tuple(sorted(v, key=lambda x: (x[1], x[0]))) for sid, v in by_section.items()}
    of_page: dict[str, list[str]] = defaultdict(list)
    for sid, v in pages.items():
        for pid, _i in v:
            of_page[pid].append(sid)
    return pages, of_page


def build_maps(*, sections: Iterable[tuple[str, int]] = (), section_pages: Iterable[tuple[str, str, int]] = (),
               topic_members: Iterable[tuple[str, int, str]] = (),
               dup_members: Iterable[tuple[str, str, str, int]] = (), page_chars: Mapping[str, int] | None = None,
               object_members: Iterable[tuple[str, str, str]] = (),
               cites: Iterable[tuple[str, str]] = (), source_works: Iterable[tuple[str, str]] = (),
               copy_kinds: Sequence[str] = GraphParams.copy_kinds, snapshot_id: str | None = None,
               parts: Mapping[str, str] | None = None) -> GraphMaps:
    """The lookup from plain rows: ``sections`` (id, level), ``section_pages`` (section, page, page index),
    ``topic_members`` (section, level, topic), ``dup_members`` (cluster, kind, page, shared_chars), ``page_chars``
    (page → characters), ``object_members`` (object, cluster, kind), ``cites`` (citing work, cited work),
    ``source_works`` (source, work)."""
    level = {sid: int(lv or 0) for sid, lv in sections}
    pages, of_page = _pages_of_rows(section_pages)
    refs = {pid: tuple(SectionRef(s, level.get(s, 0), len(pages[s])) for s in sids) for pid, sids in of_page.items()}
    topics = {(sid, int(lv)): tid for sid, lv, tid in topic_members}
    keep = set(copy_kinds)
    by_cluster: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for cid, kind, pid, shared in dup_members:
        if kind in keep and pid:
            by_cluster[cid].append((pid, int(shared or 0), kind))
    acc: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    kinds: dict[tuple[str, str], set[str]] = defaultdict(set)
    for members in by_cluster.values():
        on = {p for p, _s, _k in members}
        for p, shared, kind in members:
            for q in on - {p}:
                acc[p][q] += shared
                kinds[(p, q)].add(kind)
    chars = dict(page_chars or {})
    coverage = {p: {q: round(min(1.0, v / chars[p]), 4) for q, v in d.items()} for p, d in acc.items()
                if chars.get(p)}
    objects = {oid: (cid, kind) for oid, cid, kind in object_members}
    srcs_of_work: dict[str, set[str]] = defaultdict(set)
    for sid, wid in source_works:
        if sid and wid:
            srcs_of_work[wid].add(sid)
    nb: dict[str, set[str]] = defaultdict(set)
    for a, b in cites:
        for sa in srcs_of_work.get(a, ()):
            for sb in srcs_of_work.get(b, ()):
                if sa != sb:
                    nb[sa].add(sb)
                    nb[sb].add(sa)
    return GraphMaps(snapshot_id=snapshot_id, coverage=coverage,
                     copy_kinds={k: tuple(sorted(v)) for k, v in kinds.items()}, object_groups=objects,
                     page_sections=refs, section_pages=pages, section_topics=topics,
                     cite_neighbours={s: frozenset(v) for s, v in nb.items()}, parts=dict(parts or {}))


class NavGraphSignals:
    """:class:`GraphSignals` over the served navigation layer (``NavStore``; the canon through its ``canonical.*``
    views, CITES through ``canon`` — a ``CanonStore`` — when given). The lookup is built once per NAV build (well
    under a second) and kept in memory; G3 runs ``expand_query`` on the NAV connection per query."""

    def __init__(self, nav: Any, canon: Any = None) -> None:
        self.nav, self.canon = nav, canon
        self._lock = threading.Lock()
        self._maps: tuple[Any, GraphMaps] | None = None

    def _key(self) -> tuple[Any, ...]:
        snap = self.nav.snapshot_id()
        stamp = getattr(self.nav, "_stamp", None)
        canon = self.canon.snapshot_id() if self.canon is not None and hasattr(self.canon, "snapshot_id") else None
        return (snap, stamp, canon)

    def view(self) -> GraphMaps:
        key = self._key()
        cached = self._maps
        if cached is not None and cached[0] == key:
            return cached[1]
        with self._lock:
            if self._maps is not None and self._maps[0] == key:
                return self._maps[1]
            t0 = time.perf_counter()
            maps = self._build()
            maps.build_ms = round((time.perf_counter() - t0) * 1000, 1)
            self._maps = (key, maps)
            return maps

    def _q(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return self.nav.query(sql, list(params))

    def _has(self, name: str) -> bool:
        try:
            self._q(f"SELECT 1 FROM {name} LIMIT 0")
            return True
        except Exception:  # noqa: BLE001 - a build without the dataset
            return False

    def _build(self) -> GraphMaps:
        parts: dict[str, str] = {}
        sections: list[tuple[str, int]] = []
        spages: list[tuple[str, str, int]] = []
        if self._has("sections") and self._has("section_pages"):
            sections = [(r["section_id"], r["level"]) for r in self._q("SELECT section_id, level FROM sections")]
            spages = [(r["section_id"], r["page_id"], r["page_index"]) for r in self._q(
                "SELECT section_id, page_id, page_index FROM section_pages")]
            parts["sections"] = "OK"
        else:
            parts["sections"] = "MISSING (no sections / section_pages in the NAV build)"
        topics: list[tuple[str, int, str]] = []
        if self._has("topic_members"):
            topics = [(r["section_id"], r["level"], r["topic_id"]) for r in self._q(
                "SELECT section_id, level, topic_id FROM topic_members")]
            parts["topics"] = "OK"
        else:
            parts["topics"] = "MISSING (no topic_members in the NAV build)"
        dups: list[tuple[str, str, str, int]] = []
        chars: dict[str, int] = {}
        if self._has("dup_members"):
            dups = [(r["cluster_id"], r["kind"], r["page_id"], r["shared_chars"]) for r in self._q(
                "SELECT cluster_id, kind, page_id, shared_chars FROM dup_members WHERE page_id IS NOT NULL")]
            pages = sorted({d[2] for d in dups})
            try:
                chars = {r["page_id"]: int(r["char_count"] or 0) for r in self._q(
                    "SELECT page_id, char_count FROM canonical.pages WHERE page_id IN (SELECT unnest(?::VARCHAR[]))",
                    [pages])} if pages else {}
                parts["copies"] = "OK"
            except Exception as exc:  # noqa: BLE001 - no canonical pages behind the store
                parts["copies"] = f"ERROR (page characters: {type(exc).__name__})"
        else:
            parts["copies"] = "MISSING (no dup_members in the NAV build)"
        objects: list[tuple[str, str, str]] = []
        if self._has("object_dup_members"):
            objects = [(r["object_id"], r["cluster_id"], r["kind"]) for r in self._q(
                "SELECT object_id, cluster_id, kind FROM object_dup_members")]
            parts["object_copies"] = "OK"
        else:
            parts["object_copies"] = "MISSING (no object_dup_members in the NAV build)"
        cites: list[tuple[str, str]] = []
        works: list[tuple[str, str]] = []
        try:
            if self.canon is not None:
                cites = [(r["citing_work_id"], r["cited_work_id"]) for r in self.canon.query(
                    "SELECT citing_work_id, cited_work_id FROM cites")]
                works = [(r["source_id"], r["work_id"]) for r in self.canon.query(
                    "SELECT source_id, work_id FROM source_work_links")]
            else:
                cites = [(r["citing_work_id"], r["cited_work_id"]) for r in self._q(
                    "SELECT citing_work_id, cited_work_id FROM canon.main.cites")]
                works = [(r["source_id"], r["work_id"]) for r in self._q(
                    "SELECT source_id, work_id FROM canonical.source_work_links")]
            parts["cites"] = "OK"
        except Exception as exc:  # noqa: BLE001 - an older canon without the CITES view
            parts["cites"] = f"ERROR ({type(exc).__name__})"
        return build_maps(sections=sections, section_pages=spages, topic_members=topics, dup_members=dups,
                          page_chars=chars, object_members=objects, cites=cites, source_works=works,
                          snapshot_id=self.nav.snapshot_id(), parts=parts)

    def expansions(self, query: str, params: GraphParams) -> dict[str, Any]:
        return self.nav.run("expand_query", query, min_score=params.concepts_min_score,
                            narrower=params.concepts_narrower, narrower_min_df=params.concepts_narrower_min_df)


# ---------------------------------------------------------------------------------------------------- G1 collapse
def collapse(order: Sequence[str], kind_of: Mapping[str, str], maps: GraphMaps, params: GraphParams
             ) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    """G1 over the served order (every kind). A page leaves when a page already shown covers ≥ ``copy_min_coverage``
    of its text (the best-ranked such page keeps it as a copy); an object leaves when an object of its NAV group is
    already shown. Returns the order without the copies and the copies of every kept hit (in the order they left)."""
    kept: list[str] = []
    shown: dict[str, int] = {}
    group_rep: dict[str, str] = {}
    copies: dict[str, list[dict[str, Any]]] = {}
    for key in order:
        kind = kind_of.get(key, "PAGE")
        if kind == "PAGE":
            cov = maps.coverage.get(key) or {}
            best = None
            for q, share in cov.items():
                if share >= params.copy_min_coverage and q in shown and (best is None or shown[q] < shown[best]):
                    best = q
            if best is not None:
                copies.setdefault(best, []).append({"page_id": key, "coverage": cov[best],
                                                    "kinds": list(maps.copy_kinds.get((key, best), ()))})
                continue
            shown[key] = len(kept)
        else:
            g = maps.object_groups.get(key)
            if g is not None and g[1] not in params.object_skip_kinds:
                if g[0] in group_rep:
                    copies.setdefault(group_rep[g[0]], []).append({"object_id": key, "cluster_id": g[0], "kind": g[1]})
                    continue
                group_rep[g[0]] = key
        kept.append(key)
    return kept, copies


# ---------------------------------------------------------------------------------------------------- G2 cohesion
def cohesion_leg(pages: Sequence[str], maps: GraphMaps, params: GraphParams
                 ) -> tuple[list[str], list[dict[str, Any]]]:
    """G2: the cohesive sections of the first ``cohesion_top`` pages (deep sections — level ≥ ``cohesion_min_level``,
    ≤ ``cohesion_max_pages`` pages — holding ≥ ``cohesion_min_hits`` of them) and the leg of their other pages: by
    section (best hit first), inside a section nearest to a hit first (ties: reading order), ≤ ``cohesion_per_section``
    each, round robin, ≤ ``cohesion_leg`` in all."""
    top = list(pages[:params.cohesion_top])
    ranks: dict[str, list[int]] = defaultdict(list)
    for r, p in enumerate(top, 1):
        for s in maps.sections_of(p):
            if s.level >= params.cohesion_min_level and s.n_pages <= params.cohesion_max_pages:
                ranks[s.section_id].append(r)
    cohesive = sorted((s for s, rs in ranks.items() if len(set(rs)) >= params.cohesion_min_hits),
                      key=lambda s: (min(ranks[s]), s))
    in_top = set(top)
    per_section: list[list[str]] = []
    info: list[dict[str, Any]] = []
    for s in cohesive:
        spages = maps.section_pages.get(s, ())
        hits = [idx for p, idx in spages if p in in_top]
        cands = sorted((min(abs(idx - h) for h in hits), idx, p) for p, idx in spages if p not in in_top) if hits \
            else []
        chosen = [p for _d, _i, p in cands[:params.cohesion_per_section]]
        per_section.append(chosen)
        info.append({"section_id": s, "hit_ranks": sorted(set(ranks[s])), "n_pages": len(spages),
                     "pages": chosen})
    leg: list[str] = []
    seen: set[str] = set()
    for i in range(params.cohesion_per_section):
        for lst in per_section:
            if i < len(lst) and lst[i] not in seen:
                seen.add(lst[i])
                leg.append(lst[i])
    return leg[:params.cohesion_leg], info


# ---------------------------------------------------------------------------------------------------- G5 topics
def topic_leg(pages: Sequence[str], maps: GraphMaps, params: GraphParams) -> tuple[list[str], list[dict[str, Any]]]:
    """G5: the level-``topics_level`` topics holding ≥ ``topics_min_hits`` of the first ``topics_top`` pages (through
    the pages' deepest sections) and the leg of E's later pages (after the head) whose deepest section is in such a
    topic, in E's order, ≤ ``topics_leg``."""
    lv = params.topics_level

    def topics_of(p: str) -> set[str]:
        return {t for s in maps.sections_of(p) for t in [maps.section_topics.get((s.section_id, lv))] if t}

    ranks: dict[str, list[int]] = defaultdict(list)
    for r, p in enumerate(pages[:params.topics_top], 1):
        for t in topics_of(p):
            ranks[t].append(r)
    best = sorted((t for t, rs in ranks.items() if len(set(rs)) >= params.topics_min_hits),
                  key=lambda t: (min(ranks[t]), t))
    if not best:
        return [], []
    chosen = set(best)
    leg = [p for p in pages[params.head:] if topics_of(p) & chosen][:params.topics_leg]
    return leg, [{"topic_id": t, "hit_ranks": sorted(set(ranks[t]))} for t in best]


# ---------------------------------------------------------------------------------------------------- G4 cites
def cite_sources(seed_pages: Sequence[str], maps: GraphMaps, params: GraphParams) -> tuple[list[str], list[str]]:
    """G4: (the seed sources — of the first ``cites_seed`` pages, in rank order; their CITES neighbours outside the
    seed, sorted)."""
    seeds = list(dict.fromkeys(p.split(":")[0] for p in seed_pages[:params.cites_seed] if ":" in p))
    nb: set[str] = set()
    for s in seeds:
        nb |= maps.cite_neighbours.get(s, frozenset())
    return seeds, sorted(nb - set(seeds))


# ---------------------------------------------------------------------------------------------------- G3 wordings
def expansion_texts(result: Mapping[str, Any] | None, query: str) -> list[str]:
    """The distinct other wordings of an ``expand_query`` answer (never the query itself)."""
    out: list[str] = []
    seen = {" ".join(query.lower().split())}
    for x in (result or {}).get("expansions") or []:
        text = " ".join(str(x.get("text") or "").split())
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text[:512])
    return out


def rrf_pages(lists: Sequence[Sequence[str]], k: int = 60) -> list[str]:
    """Plain RRF of page lists (equal weights; ties: first list, then rank, then id)."""
    score: dict[str, float] = defaultdict(float)
    first: dict[str, tuple[int, int]] = {}
    for li, lst in enumerate(lists):
        for r, p in enumerate(dict.fromkeys(lst), 1):
            score[p] += 1.0 / (k + r)
            first.setdefault(p, (li, r))
    return sorted(score, key=lambda p: (-score[p], first[p], p))


# ---------------------------------------------------------------------------------------------------- fusion
def widen_window(window: Sequence[str], legs: Sequence[tuple[str, Sequence[str], int]]
                 ) -> tuple[list[str], dict[str, str]]:
    """``window`` mode: the window keeps every E candidate and gains ≤ n new pages of each leg (in leg order).
    Returns (the widened window, added page → leg name)."""
    out = list(window)
    seen = set(out)
    added: dict[str, str] = {}
    for name, leg, n in legs:
        k = 0
        for p in leg:
            if k >= n:
                break
            if p not in seen:
                seen.add(p)
                out.append(p)
                added[p] = name
                k += 1
    return out, added


def fuse_post(order: Sequence[str], legs: Sequence[tuple[str, Sequence[str], float]], params: GraphParams
              ) -> tuple[list[str], dict[str, dict[str, int]]]:
    """``post`` fusion: the first ``head`` pages stay; the rest of ``order`` (weight 1) and the legs (their weights,
    head pages dropped) are fused by weighted RRF (k = ``rrf_k``); ties: E's order first, then leg order, then id.
    Returns (the new order, leg → {page: rank in the leg})."""
    head = list(order[:params.head])
    in_head = set(head)
    rest = [p for p in order[params.head:]]
    score: dict[str, float] = defaultdict(float)
    tie: dict[str, tuple[int, int]] = {}
    for r, p in enumerate(dict.fromkeys(rest), 1):
        score[p] += 1.0 / (params.rrf_k + r)
        tie[p] = (0, r)
    ranks: dict[str, dict[str, int]] = {}
    for li, (name, leg, w) in enumerate(legs, 1):
        lst = [p for p in dict.fromkeys(leg) if p not in in_head]
        if not lst or w <= 0:
            continue
        ranks[name] = {p: r for r, p in enumerate(lst, 1)}
        for r, p in enumerate(lst, 1):
            score[p] += float(w) / (params.rrf_k + r)
            tie.setdefault(p, (li, r))
    if not ranks:
        return list(order), {}
    return head + sorted(score, key=lambda p: (-score[p], tie[p], p)), ranks


def on_pages(order: Sequence[str], kind_of: Mapping[str, str], new_pages: Sequence[str]) -> list[str]:
    """Refill the PAGE positions of ``order`` with ``new_pages`` (other kinds keep theirs, H-44); pages beyond the
    PAGE slots follow at the end."""
    it = iter(new_pages)
    out: list[str] = []
    for key in order:
        if kind_of.get(key, "PAGE") == "PAGE":
            nxt = next(it, None)
            if nxt is not None:
                out.append(nxt)
        else:
            out.append(key)
    out.extend(it)
    return out


# ---------------------------------------------------------------------------------------------------- one run
@dataclass
class GraphRun:
    """The graph stages of one hybrid request: ``search.hybrid`` (and the benchmark) calls the hooks in pipeline
    order — :meth:`wordings` (G3, before the legs), :meth:`seed` (G4, on the RRF order), :meth:`window` (``window``
    legs, before late), :meth:`after_late` (``post`` legs), :meth:`finish` (G1) — and :meth:`record` describes what
    happened. ``bm25`` runs one BM25 page search: ``(text, source_ids or None, depth) → [page ids]``."""

    stages: tuple[str, ...]
    params: GraphParams
    signals: GraphSignals | None
    bm25: Callable[[str, list[str] | None, int], list[str]] | None = None
    status: dict[str, str] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    legs: dict[str, list[str]] = field(default_factory=dict)
    leg_ranks: dict[str, dict[str, int]] = field(default_factory=dict)
    window_added: dict[str, str] = field(default_factory=dict)
    e_rank: dict[str, int] = field(default_factory=dict)
    copies: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    maps: GraphMaps | None = None
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.params.validate()
        for s in STAGES:
            self.status[s] = "APPLIED" if s in self.stages else "OFF"

    # ---- gates
    def gate(self, *, page_kind: bool, late: bool) -> None:
        """Stages that cannot run: no navigation layer, no PAGE kind (G2–G5), late off (every stage: the measured
        configuration is E with the late stage)."""
        if not self.stages:
            return
        if not late:
            for s in self.stages:
                self.status[s] = "SKIPPED_LATE_OFF"
            return
        if self.signals is None:
            for s in self.stages:
                self.status[s] = "UNAVAILABLE"
            self.warnings.append("GRAPH_UNAVAILABLE: the navigation layer is not configured; graph stages skipped")
            return
        t0 = time.perf_counter()
        try:
            self.maps = self.signals.view()
        except Exception as exc:  # noqa: BLE001 - NavUnavailable, a broken build: search without the stages
            for s in self.stages:
                self.status[s] = "UNAVAILABLE"
            self.warnings.append(f"GRAPH_UNAVAILABLE: navigation layer ({type(exc).__name__}); graph stages skipped")
            return
        self.timings["graph_view"] = round((time.perf_counter() - t0) * 1000, 2)
        if not page_kind:
            for s in PAGE_STAGES:
                if self.status[s] == "APPLIED":
                    self.status[s] = "SKIPPED_NO_PAGE_KIND"

    def on(self, stage: str) -> bool:
        return self.status.get(stage) == "APPLIED"

    # ---- G3: other wordings, their BM25 legs
    def wordings(self, query: str) -> list[str]:
        if not self.on("concepts"):
            return []
        t0 = time.perf_counter()
        try:
            res = self.signals.expansions(query, self.params) if self.signals is not None else {}
        except Exception as exc:  # noqa: BLE001 - a build without the term tables, no morphology …
            self.status["concepts"] = "UNAVAILABLE"
            self.warnings.append(f"GRAPH_CONCEPTS_UNAVAILABLE: {type(exc).__name__}")
            return []
        self.timings["concepts_nav"] = round((time.perf_counter() - t0) * 1000, 2)
        texts = expansion_texts(res, query)
        self.info["concepts"] = {"expansions": [{k: x.get(k) for k in ("kind", "text", "terms")}
                                                for x in (res or {}).get("expansions") or []],
                                 "phrases": (res or {}).get("phrases")}
        if not texts:
            self.status["concepts"] = "NOT_TRIGGERED"
        return texts

    def concepts_leg(self, texts: Sequence[str], lists: Sequence[Sequence[str]]) -> None:
        if not self.on("concepts"):
            return
        leg = rrf_pages(lists, k=self.params.rrf_k)[:self.params.concepts_depth] if lists else []
        self.legs["concepts"] = leg
        self.info.setdefault("concepts", {})["leg"] = len(leg)
        if not leg:
            self.status["concepts"] = "NOT_TRIGGERED"

    # ---- G4: seed on the RRF order
    def seed(self, fused_pages: Sequence[str]) -> list[str]:
        """G4's neighbour sources (from the RRF order's first pages); empty → not triggered."""
        if not self.on("cites"):
            return []
        seeds, nb = cite_sources(fused_pages, self.maps or GraphMaps(), self.params)
        self.info["cites"] = {"seed_sources": seeds, "neighbour_sources": len(nb)}
        if not nb:
            self.status["cites"] = "NOT_TRIGGERED"
        return nb

    def cites_leg(self, leg: Sequence[str]) -> None:
        if not self.on("cites"):
            return
        self.legs["cites"] = list(leg)[:self.params.cites_depth]
        self.info.setdefault("cites", {})["leg"] = len(self.legs["cites"])
        if not self.legs["cites"]:
            self.status["cites"] = "NOT_TRIGGERED"

    # ---- window mode
    def window(self, window: Sequence[str]) -> list[str]:
        wl = []
        for s, mode, n in (("concepts", self.params.concepts_mode, self.params.concepts_window),
                           ("cites", self.params.cites_mode, self.params.cites_window)):
            if self.on(s) and mode == "window" and self.legs.get(s):
                wl.append((s, self.legs[s], n))
        if not wl:
            return list(window)
        out, self.window_added = widen_window(window, wl)
        for s, _l, _n in wl:
            self.info[s]["window_added"] = sum(1 for v in self.window_added.values() if v == s)
        return out

    # ---- post-late legs
    def after_late(self, pages: Sequence[str]) -> list[str]:
        self.e_rank = {p: i for i, p in enumerate(pages, 1)}
        maps = self.maps or GraphMaps()
        legs: list[tuple[str, list[str], float]] = []
        if self.on("cohesion"):
            leg, info = cohesion_leg(pages, maps, self.params)
            self.info["cohesion"] = {"sections": info, "leg": len(leg)}
            if leg:
                legs.append(("cohesion", leg, self.params.cohesion_weight))
            else:
                self.status["cohesion"] = "NOT_TRIGGERED"
        if self.on("topics"):
            leg, info = topic_leg(pages, maps, self.params)
            self.info["topics"] = {"topics": info, "leg": len(leg)}
            if leg:
                legs.append(("topics", leg, self.params.topics_weight))
            else:
                self.status["topics"] = "NOT_TRIGGERED"
        for s, mode, w in (("concepts", self.params.concepts_mode, self.params.concepts_weight),
                           ("cites", self.params.cites_mode, self.params.cites_weight)):
            if self.on(s) and mode == "post" and self.legs.get(s):
                legs.append((s, self.legs[s], w))
        if not legs:
            return list(pages)
        new, self.leg_ranks = fuse_post(pages, legs, self.params)
        return new

    # ---- G1
    def finish(self, order: Sequence[str], kind_of: Mapping[str, str]) -> list[str]:
        if not self.on("collapse"):
            return list(order)
        kept, self.copies = collapse(order, kind_of, self.maps or GraphMaps(), self.params)
        n = sum(len(v) for v in self.copies.values())
        self.info["collapse"] = {"collapsed": n, "pages": sum(1 for v in self.copies.values() for c in v
                                                              if "page_id" in c),
                                 "objects": sum(1 for v in self.copies.values() for c in v if "object_id" in c)}
        if not n:
            self.status["collapse"] = "NOT_TRIGGERED"
        return kept

    # ---- record
    def trace(self, key: str) -> dict[str, Any] | None:
        if not any(v in ("APPLIED", "NOT_TRIGGERED") for v in self.status.values()):
            return None
        t: dict[str, Any] = {}
        if key in self.e_rank:
            t["e_rank"] = self.e_rank[key]
        legs = {name: r[key] for name, r in self.leg_ranks.items() if key in r}
        if legs:
            t["legs"] = legs
        if key in self.window_added:
            t["window_leg"] = self.window_added[key]
        if key in self.copies:
            t["copies"] = len(self.copies[key])
        return t or None

    def record(self) -> dict[str, Any] | str:
        if not self.stages:
            return "NOT_RUN (no graph stage requested; server default: " + (", ".join(sorted(DEFAULTS)) or "none") + ")"
        out: dict[str, Any] = {"requested": list(self.stages),
                               "status": {s: self.status[s] for s in STAGES},
                               "codes": {s: CODES[s] for s in STAGES},
                               "fusion": {"post": f"the first {self.params.head} positions stay; the rest of E's order "
                                                  f"(weight 1) and the legs fused by weighted RRF (k = "
                                                  f"{self.params.rrf_k}, E wins ties)",
                                          "window": "leg pages widen the late window (E's candidates stay); the late "
                                                    "score decides",
                                          "collapse": "last, over every kind"},
                               "params": self.params.as_record(), "review_status": REVIEW_STATUS, "note": NOTE}
        if self.maps is not None:
            out["nav_snapshot_id"] = self.maps.snapshot_id
            out["parts"] = dict(self.maps.parts)
            if self.maps.build_ms is not None:
                out["lookup_build_ms"] = self.maps.build_ms
        for s in STAGES:
            if s in self.info:
                out[s] = self.info[s]
        if self.timings:
            out["timings_ms"] = dict(self.timings)
        return out


def resolve(requested: tuple[str, ...] | None, defaults: Iterable[str] | None = None) -> tuple[str, ...]:
    """The stages of a request: its own list, else the server defaults (else :data:`DEFAULTS`)."""
    if requested is not None:
        return tuple(s for s in STAGES if s in set(requested))
    d = set(DEFAULTS if defaults is None else defaults)
    return tuple(s for s in STAGES if s in d)
