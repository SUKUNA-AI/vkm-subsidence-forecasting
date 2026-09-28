"""Topic-level acceptance benchmark (``benchmarks/topic_v1``): does a tool find the pages that matter for a topic?

Ground truth is human-made: the evidence pages of the 72 physical processes (``catalogues/physics/
process_evidence_links.csv``) and the source locators of the model families of the mathematical model registry
(``catalogues/mathematics/MATHEMATICAL_MODEL_REGISTRY.csv``). A topic has three Russian queries (its name and two
paraphrases) and a list of *targets*: one canonical page each, plus acceptable alternates (the page where the quote
anchors when the catalogue locator is off by a page, a DOCX render drift, duplicate pages). The target lists are a
sample of the relevant pages, not an exhaustive judgement: the benchmark measures recall, never precision.

Everything here is pure (stdlib only): the set loader, rankings of the evaluated systems built from the raw answers
the harness collected (IDs only), per-query metrics, aggregation and the paired tests. The metric definitions are
pre-registered in ``benchmarks/topic_v1/metrics_spec_v1.json``; changing them means a new version of the benchmark.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from vkm_corpus.retrieval_lab.metrics import bootstrap_ci, paired_randomization

SET_VERSION = "topic_v1"
CUTOFFS = (10, 20, 50)
SOURCE_K = 10
SECTION_K = 10
MRR_K = 50
MAX_PAGES = 50
RRF_K = 60
NAV_WEIGHTS = {"sections": 1.0, "term": 1.0, "neighbour": 0.5}
NAV_NEIGHBOURS = 5
TRACKS = ("PROCESS", "MODEL_FAMILY")
VARIANTS = ("NAME", "PARA1", "PARA2")
SYSTEMS = ("bm25", "hybrid_late", "hybrid_nolate", "nav", "dossier")
PAGE_ID = re.compile(r"^(VKM-SRC-\d{3}):([a-z])(\d{4})$")
PAGE_ID_IN_TEXT = re.compile(r"VKM-SRC-\d{3}:[a-z]\d{4}")
SECTION_ID = re.compile(r"^SEC-[0-9a-f]{16}$")
FORBIDDEN_KEYS = frozenset({"quote", "text_snippet", "highlights", "normalized_text", "snippet", "page_text"})

# primary metrics of the pre-registration (the comparisons are tested on these, Holm over the family)
PRIMARY_METRICS = ("page_recall@20", "mrr@50")
PRIMARY_COMPARISONS = (("hybrid_late", "hybrid_nolate"), ("hybrid_late", "nav"), ("hybrid_late", "bm25"))


# ------------------------------------------------------------------ set
@dataclass(frozen=True)
class Target:
    target_id: str
    page_id: str
    source_id: str
    alt_page_ids: tuple[str, ...] = ()
    grade: int = 1
    mapping: str = "IDENTITY"

    @property
    def pages(self) -> frozenset[str]:
        return frozenset((self.page_id, *self.alt_page_ids))


@dataclass(frozen=True)
class Query:
    query_id: str
    topic_id: str
    variant: str
    text: str


@dataclass(frozen=True)
class Topic:
    topic_id: str
    track: str
    group: str
    title: str
    queries: tuple[Query, ...]
    targets: tuple[Target, ...]

    @property
    def sources(self) -> frozenset[str]:
        return frozenset(t.source_id for t in self.targets)


def topic_from_json(d: Mapping[str, Any]) -> Topic:
    queries = tuple(Query(q["query_id"], d["topic_id"], q["variant"], q["text"]) for q in d["queries"])
    targets = tuple(Target(t["target_id"], t["page_id"], t["source_id"], tuple(t.get("alt_page_ids") or ()),
                           int(t.get("grade", 1)), t.get("mapping", "IDENTITY")) for t in d["targets"])
    return Topic(d["topic_id"], d["track"], d["group"], d["title"], queries, targets)


def load_set(path: str | Path) -> list[Topic]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [topic_from_json(json.loads(line)) for line in lines if line.strip()]


def _keys(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


def validate_set(raw_lines: Sequence[str]) -> list[str]:
    """Problems of a set file: ids, variants, page id grammar, duplicates, forbidden (text) keys."""
    problems: list[str] = []
    seen_topics: set[str] = set()
    seen_queries: set[str] = set()
    for i, line in enumerate(raw_lines, 1):
        if not line.strip():
            continue
        d = json.loads(line)
        bad = _keys(d) & FORBIDDEN_KEYS
        if bad:
            problems.append(f"line {i}: forbidden keys {sorted(bad)}")
        t = topic_from_json(d)
        if t.topic_id in seen_topics:
            problems.append(f"{t.topic_id}: duplicate topic")
        seen_topics.add(t.topic_id)
        if t.track not in TRACKS:
            problems.append(f"{t.topic_id}: track {t.track!r}")
        if sorted(q.variant for q in t.queries) != sorted(VARIANTS):
            problems.append(f"{t.topic_id}: variants {[q.variant for q in t.queries]}")
        texts = [q.text.strip().lower() for q in t.queries]
        if len(set(texts)) != len(texts) or not all(texts):
            problems.append(f"{t.topic_id}: empty or repeated query texts")
        for q in t.queries:
            if q.query_id in seen_queries:
                problems.append(f"{q.query_id}: duplicate query id")
            seen_queries.add(q.query_id)
        if not t.targets:
            problems.append(f"{t.topic_id}: no targets")
        primaries = [x.page_id for x in t.targets]
        if len(set(primaries)) != len(primaries):
            problems.append(f"{t.topic_id}: duplicate target pages")
        for x in t.targets:
            m = PAGE_ID.match(x.page_id)
            if not m or m.group(1) != x.source_id:
                problems.append(f"{x.target_id}: page id {x.page_id!r}")
            for p in x.alt_page_ids:            # duplicate-group alternates may live in another copy (source)
                if not PAGE_ID.match(p) or p == x.page_id:
                    problems.append(f"{x.target_id}: alternate page id {p!r}")
            if x.grade not in (1, 2):
                problems.append(f"{x.target_id}: grade {x.grade}")
    return problems


# ------------------------------------------------------------------ sections (outlines)
def split_page_id(page_id: str) -> tuple[str, str, int]:
    m = PAGE_ID.match(page_id)
    if not m:
        raise ValueError(f"not a page id: {page_id!r}")
    return m.group(1), m.group(2), int(m.group(3))


@dataclass
class SectionIndex:
    """Section ranges per source (from ``/v1/nav/outline``) and the page-id prefix of each source."""

    sections: dict[str, tuple[str, int, int, int]] = field(default_factory=dict)   # id -> (source, level, lo, hi)
    by_source: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    prefix: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_outlines(cls, outlines: Mapping[str, Sequence[Mapping[str, Any]]]) -> "SectionIndex":
        idx = cls()
        for source_id, rows in outlines.items():
            for r in rows:
                sid = r["section_id"]
                idx.sections[sid] = (source_id, int(r["level"]), int(r["page_start_index"]), int(r["page_end_index"]))
                idx.by_source[source_id].append(sid)
                if r.get("page_start_id") and source_id not in idx.prefix:
                    idx.prefix[source_id] = split_page_id(r["page_start_id"])[1]
        return idx

    def page_id(self, source_id: str, index: int) -> str:
        return f"{source_id}:{self.prefix.get(source_id, 'p')}{int(index):04d}"

    def pages_of(self, section_id: str) -> list[str]:
        if section_id not in self.sections:
            return []
        src, _level, lo, hi = self.sections[section_id]
        return [self.page_id(src, i) for i in range(lo, hi + 1)]

    def deepest(self, page_id: str) -> list[str]:
        """The deepest sections whose range holds the page (two on a boundary shared by siblings)."""
        src, _p, index = split_page_id(page_id)
        best: list[str] = []
        best_level = -1
        for sid in self.by_source.get(src, ()):
            _s, level, lo, hi = self.sections[sid]
            if lo <= index <= hi:
                if level > best_level:
                    best, best_level = [sid], level
                elif level == best_level:
                    best.append(sid)
        return best


# ------------------------------------------------------------------ rankings of the systems
@dataclass
class Ranking:
    """Ranked pages (with duplicate aliases) and ranked section nodes of one system for one query."""

    pages: list[str] = field(default_factory=list)
    aliases: list[frozenset[str]] = field(default_factory=list)
    nodes: list[frozenset[str]] = field(default_factory=list)
    node_ids: list[str] = field(default_factory=list)
    error: str | None = None

    def add_page(self, page_id: str, duplicates: Iterable[str] = ()) -> None:
        if page_id in self.pages or len(self.pages) >= MAX_PAGES:
            return
        self.pages.append(page_id)
        self.aliases.append(frozenset((page_id, *duplicates)))

    def add_node(self, node_id: str, pages: Iterable[str]) -> None:
        if node_id in self.node_ids:
            return
        self.node_ids.append(node_id)
        self.nodes.append(frozenset(pages))


def _nodes_from_pages(r: Ranking, index: SectionIndex, k: int = SECTION_K) -> None:
    """Section nodes of a page ranking: the deepest sections of its top-k pages (a page outside every section is its
    own node)."""
    for page in r.pages[:k]:
        secs = index.deepest(page)
        if not secs:
            r.add_node("PAGE:" + page, [page])
        for sid in secs:
            r.add_node(sid, index.pages_of(sid))


def ranking_from_hits(hits: Sequence[Mapping[str, Any]], index: SectionIndex, *, error: str | None = None
                      ) -> Ranking:
    """BM25 / hybrid answers: ``[{page_id, duplicates}]`` in rank order (the API collapses duplicate pages)."""
    r = Ranking(error=error)
    for h in hits:
        if h.get("page_id"):
            r.add_page(h["page_id"], h.get("duplicates") or ())
    _nodes_from_pages(r, index)
    return r


def _unit_node(unit: Mapping[str, Any]) -> tuple[str, list[str]] | None:
    """A concept unit as a node: its section (entry pages = the unit's mention pages) or, for heading groups and
    single pages outside the section tree, a pseudo-node made of those pages."""
    pages = [p for p in unit.get("page_ids") or [] if PAGE_ID.match(p)]
    if unit.get("section_id"):
        return unit["section_id"], pages
    return ("UNIT:" + (unit.get("unit_id") or pages[0]), pages) if pages else None


def ranking_from_nav(raw: Mapping[str, Any], index: SectionIndex, *, rrf_k: int = RRF_K,
                     weights: Mapping[str, float] = NAV_WEIGHTS) -> Ranking:
    """NAV-only system: weighted RRF of section lists — ``search_sections`` (w 1), the matched term's top units
    (w 1) and the top units of each of its neighbour terms (w 0.5 each). Pages are read in two passes: first the entry
    pages of every node in rank order (a unit's mention pages; a section's first page), then the rest of each node's
    range in rank order."""
    lists: list[tuple[float, list[tuple[str, list[str]]]]] = []
    lists.append((weights["sections"], [(s["section_id"], []) for s in raw.get("sections") or []
                                        if s.get("section_id")]))
    units = [n for n in (_unit_node(u) for u in raw.get("term_units") or []) if n]
    lists.append((weights["term"], units))
    for nb in raw.get("neighbour_units") or []:
        lists.append((weights["neighbour"], [n for n in (_unit_node(u) for u in nb or []) if n]))
    score: dict[str, float] = defaultdict(float)
    first: dict[str, int] = {}
    entry: dict[str, list[str]] = {}
    order = 0
    for w, items in lists:
        seen: set[str] = set()
        rank = 0
        for node_id, pages in items:
            if node_id in seen:
                continue
            seen.add(node_id)
            rank += 1
            score[node_id] += w / (rrf_k + rank)
            if node_id not in first:
                first[node_id] = order
                order += 1
            entry.setdefault(node_id, [])
            entry[node_id].extend(p for p in pages if p not in entry[node_id])
    ranked = sorted(score, key=lambda n: (-score[n], first[n]))
    r = Ranking(error=raw.get("error"))
    full: dict[str, list[str]] = {}
    for node_id in ranked:
        if SECTION_ID.match(node_id):
            full[node_id] = index.pages_of(node_id)
            if not entry[node_id] and full[node_id]:
                entry[node_id] = [full[node_id][0]]
        else:
            full[node_id] = list(entry[node_id])
        r.add_node(node_id, full[node_id] or entry[node_id])
    for node_id in ranked:                       # pass 1: entry pages
        for p in entry[node_id]:
            r.add_page(p)
    for node_id in ranked:                       # pass 2: the rest of each node
        for p in full[node_id]:
            r.add_page(p)
    return r


def ranking_from_dossier(raw: Mapping[str, Any], index: SectionIndex) -> Ranking:
    """``reconstruct_topic`` dossier: page ids in document order, then the pages of the sections it names."""
    r = Ranking(error=raw.get("error"))
    for p in raw.get("page_ids") or []:
        r.add_page(p)
    secs = [s for s in raw.get("section_ids") or [] if s in index.sections]
    for sid in secs:
        for p in index.pages_of(sid):
            r.add_page(p)
    if secs:
        for sid in secs:
            r.add_node(sid, index.pages_of(sid))
    else:
        _nodes_from_pages(r, index)
    return r


def extract_ids(obj: Any) -> tuple[list[str], list[str]]:
    """Page ids (also the page part of block/object ids) and section ids of any JSON answer, in document order."""
    pages: list[str] = []
    secs: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            for p in PAGE_ID_IN_TEXT.findall(x):
                if p not in pages:
                    pages.append(p)
            for s in re.findall(r"SEC-[0-9a-f]{16}", x):
                if s not in secs:
                    secs.append(s)

    walk(obj)
    return pages, secs


# ------------------------------------------------------------------ metrics
def target_ranks(targets: Sequence[Target], r: Ranking) -> dict[str, int | None]:
    """1-based rank of the first retrieved page matching each target (primary, alternate or duplicate alias)."""
    out: dict[str, int | None] = {}
    for t in targets:
        pages = t.pages
        out[t.target_id] = next((i + 1 for i, al in enumerate(r.aliases) if al & pages), None)
    return out


def query_metrics(topic: Topic, r: Ranking) -> dict[str, float]:
    ranks = target_ranks(topic.targets, r)
    n = len(topic.targets)
    m: dict[str, float] = {}
    for k in CUTOFFS:
        hits = sum(1 for v in ranks.values() if v is not None and v <= k)
        m[f"page_recall@{k}"] = hits / n
        m[f"capped_recall@{k}"] = hits / min(k, n)
    m["success@10"] = 1.0 if any(v is not None and v <= 10 for v in ranks.values()) else 0.0
    best = min((v for v in ranks.values() if v is not None and v <= MRR_K), default=None)
    m[f"mrr@{MRR_K}"] = 1.0 / best if best else 0.0
    got = set()
    for al in r.aliases[:SOURCE_K]:
        got |= {split_page_id(p)[0] for p in al}
    m[f"source_recall@{SOURCE_K}"] = len(topic.sources & got) / len(topic.sources)
    pages_k: set[str] = set()
    for node in r.nodes[:SECTION_K]:
        pages_k |= node
    covered = sum(1 for t in topic.targets if t.pages & pages_k)
    m[f"section_hit@{SECTION_K}"] = 1.0 if covered else 0.0
    m[f"section_recall@{SECTION_K}"] = covered / n
    m[f"section_pages@{SECTION_K}"] = float(len(pages_k))
    return m


METRIC_NAMES = tuple([f"page_recall@{k}" for k in CUTOFFS] + [f"capped_recall@{k}" for k in CUTOFFS]
                     + ["success@10", f"mrr@{MRR_K}", f"source_recall@{SOURCE_K}", f"section_hit@{SECTION_K}",
                        f"section_recall@{SECTION_K}", f"section_pages@{SECTION_K}"])


def mean(xs: Iterable[float]) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def aggregate(rows: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Means of every metric over rows grouped by the values of ``keys`` (rows: system, topic, track, … + metrics)."""
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups["|".join(str(row[k]) for k in keys)].append(row)
    out: dict[str, dict[str, Any]] = {}
    for g, rs in sorted(groups.items()):
        out[g] = {"n_queries": len(rs), "n_topics": len({x["topic_id"] for x in rs})}
        for name in METRIC_NAMES:
            vals = [x[name] for x in rs if name in x and not (isinstance(x[name], float) and math.isnan(x[name]))]
            out[g][name] = round(mean(vals), 4) if vals else None
    return out


def topic_means(rows: Sequence[Mapping[str, Any]], system: str, metric: str,
                topics: Iterable[str] | None = None) -> dict[str, float]:
    """Topic-level means (the three variants of a topic are not independent) of one system and metric."""
    keep = set(topics) if topics is not None else None
    acc: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row["system"] == system and (keep is None or row["topic_id"] in keep):
            acc[row["topic_id"]].append(row[metric])
    return {t: mean(v) for t, v in acc.items()}


def holm(pvalues: Mapping[str, float]) -> dict[str, float]:
    """Holm–Bonferroni adjusted p-values (monotone)."""
    items = sorted(pvalues.items(), key=lambda kv: kv[1])
    m = len(items)
    out: dict[str, float] = {}
    running = 0.0
    for i, (k, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return out


def compare_systems(rows: Sequence[Mapping[str, Any]], comparisons: Sequence[tuple[str, str]] = PRIMARY_COMPARISONS,
                    metrics: Sequence[str] = PRIMARY_METRICS, *, n: int = 10000, seed: int = 20260928,
                    topics: Iterable[str] | None = None) -> dict[str, dict[str, float]]:
    """Paired sign-flip tests and bootstrap CIs over topic-level means; Holm over the whole family of tests."""
    topics = list(topics) if topics is not None else None
    out: dict[str, dict[str, float]] = {}
    for a, b in comparisons:
        for metric in metrics:
            ta, tb = topic_means(rows, a, metric, topics), topic_means(rows, b, metric, topics)
            if not ta or not tb:
                continue
            test = paired_randomization(ta, tb, n=n, seed=seed)
            ci = bootstrap_ci(ta, tb, n=n, seed=seed)
            out[f"{a} - {b} | {metric}"] = {"n_topics": test["n"], "delta": round(test["delta"], 4),
                                            "ci_lo": round(ci["lo"], 4), "ci_hi": round(ci["hi"], 4),
                                            "p_value": round(test["p_value"], 5)}
    adj = holm({k: v["p_value"] for k, v in out.items()})
    for k in out:
        out[k]["p_holm"] = round(adj[k], 5)
    return out


def acceptance(summary: Mapping[str, Any], levels: Mapping[str, float]) -> dict[str, Any]:
    """PASS/FAIL of each pre-registered acceptance level (``metric`` → minimum of the mean over all queries)."""
    res = {m: {"value": summary.get(m), "min": lvl, "pass": summary.get(m) is not None and summary[m] >= lvl}
           for m, lvl in levels.items()}
    return {"levels": res, "pass": all(v["pass"] for v in res.values())}
