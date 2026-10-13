"""Query side of the retrieval projection: BM25 candidates with stable canonical IDs (task §28, §32, §59).

* Filters come from a closed whitelist (unknown name → ``E_UNKNOWN_FILTER``; the API maps it to 400). The source area
  filter is ``source_scope`` (H-18: it is the *source's* area, inherited by every object). Availability
  ``available_until`` requires an explicit ``unknown_policy`` (EXCLUDE | INCLUDE) and the answer carries counts by
  ``available_basis`` (H-19). By default only the primary text layer is searched (H-30).
* Per type the query is ``multi_match`` (best fields, boosts ``FIELDS``) + phrase boost on the exact sub-field.
  Ties break on ``id``.
* An explicit object number («рис. 3.1», «табл. 2», «(3.12)») is a *gated* boost (agent L, smoke Q5): every book has
  its own «рис. 3.1», so the figure/table/formula kinds are ranked by the query **without** the number (the topic),
  and among the topic's top ``LABEL_TOPIC_K`` hits those whose printed number equals the asked one move to the front
  (topic order kept); a labelled object that does not match the topic is never pulled up. A query that is only a
  number («рис. 3.1») keeps the plain term boost. Hits carry ``fields.topic_rank`` and ``fields.label_promoted``.
* Numbered objects on pages and blocks (agent Б, 13.10; ``vkm_corpus.search.object_refs``): «скв. 75», «скважина № 75»,
  «профильная линия 12», «блок 2», «панель 5», «репер 104», «рис. 3.1», «табл. 2.4», «формула (3.12)». A query with
  a topic next to the reference is ranked by the topic (``cross_fields`` over the text and the work's authors and
  title, so «рис. 3.1 Соловьев Секунцов» names the work), and among the topic's first :data:`OBJECT_REF_TOPIC_K`
  hits those holding a reference phrase («скв 75», «скважина 75», «borehole 75» … — the named query
  :data:`OBJECT_REF_NAME`, boost 0) move to the front, topic order kept (``fields.object_ref_matched``,
  ``fields.object_ref_promoted``, ``fields.topic_rank``). A query that is only references matches the phrase or the
  terms, the phrase boosted (:data:`OBJECT_REF_BOOST`). On figures, tables and formulas a printed number stays with
  the label gate above; a non-label reference only adds the phrase boost. A query without a reference takes exactly
  the old body.
* The filter ``source_scope_or_source_id`` (``{source_scope, source_id}``) is the area OR one of the sources — the
  API's ``include_ambiguous`` (``vkm_corpus.search.scope_filter``).
* Blocks collapse by ``page_id`` (``best_blocks``); pages collapse duplicate pages by ``dup_group_id``.
* Fragments of one figure (``group_fragments``, agent Д; the API turns it on for FIGURE): figures of one page with the
  same ``caption_block_id`` are one hit — the largest region, at the place of the best-ranked one; the others in
  ``fields.fragments`` (``vkm_corpus.search.fragments``). The FIGURE kind is then ranked from the top at least
  ``FRAGMENT_DEPTH`` deep and the window is cut from the grouped list.
* Several kinds are never ranked by raw BM25 across indices (their statistics differ): one query per kind
  (``_msearch``), then reciprocal-rank fusion (H-44).
* Rerank candidates are IDs plus a passage reference by rule ``rerank_text_v1``; the text is read from agent D's
  DuckDB view ``rerank_text``, never from the index (H-04, H-13): ≤ 24 text candidates, ≤ 8 images per call.
* Long questions (agent M, 13.10; ``long_query``, ``vkm_corpus.search.long_query``; opt-in): ``strict`` (default) is
  the path above, byte for byte. Otherwise a query with at least ``LONG_QUERY_MIN_TERMS`` (6) content terms (after
  the ``_russian_``/``_english_`` stop words, numbered objects left out) runs with ``relax`` —
  ``minimum_should_match`` ``RELAXED_MSM`` (``"3<70% 5<50%"``) instead of the request's —, ``strip`` — the question
  without its frame («какие», «в каком году», «что известно о», «наблюдались» …; terms and numbered objects stay) as
  the text of every clause (match, phrase, title boost, label and reference gates) — or ``relax_strip`` (both). A
  shorter query takes the old path in every mode. The answer then carries ``long_query`` (the plan: ``mode``,
  ``status``, ``long``, ``terms``, ``msm``, ``strip``, ``stripped_text`` …) and keeps the query as asked.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any

from vkm_corpus.contracts import vocab
from vkm_corpus.search import long_query as LQ
from vkm_corpus.search.fragments import FRAGMENT_DEPTH, group_hits
from vkm_corpus.search.mappings import KIND_INDEX, TEXT_FIELDS, alias_name, properties
from vkm_corpus.search.object_refs import ObjectRef, has_topic, parse_refs, strip_refs

MAX_SIZE = 200
MAX_WINDOW = 1000
RRF_K = 60
MAX_TEXT_CANDIDATES = 24          # listwise text reranker: one call, no batch splicing (H-13)
MAX_VISUAL_CANDIDATES = 8         # visual reranker per call (H-13)
RERANK_TEXT_RULE = vocab.TextRule.RERANK_TEXT_V1.value
UNKNOWN_POLICIES = ("EXCLUDE", "INCLUDE")
KINDS = tuple(KIND_INDEX)         # PAGE, BLOCK, FIGURE, TABLE, FORMULA

# Field boosts (MODEL_CHOICE; calibrated only on labelled synthetic/approved queries, never on test truth).
# The match itself uses the stemmed fields only, so two word forms of one query score identically; the work title
# only boosts (a page that matches nothing but its book title is not a hit); `exact=True` switches to unstemmed forms.
FIELDS: dict[str, list[str]] = {
    "pages": ["text^1.0"],
    "blocks": ["text^1.0"],
    "figures": ["caption^3.0", "text^1.0"],
    "tables": ["caption^3.0", "text^1.0"],
    "formulas": ["recognized_latex^1.0", "text^1.0"],
}
TITLE_BOOST_FIELDS = ["work_title^0.3"]
PHRASE_FIELDS: dict[str, list[str]] = {"pages": ["text"], "blocks": ["text"], "figures": ["caption"],
                                       "tables": ["caption", "text"], "formulas": ["text"]}
_EXACT_CAPABLE = ("text", "caption")


def _exact(field_spec: str) -> str:
    name, _, boost = field_spec.partition("^")
    return f"{name}.exact^{boost}" if name in _EXACT_CAPABLE and boost else (
        f"{name}.exact" if name in _EXACT_CAPABLE else field_spec)
HIGHLIGHT: dict[str, dict[str, Any]] = {
    "pages": {"text": {"fragment_size": 180, "number_of_fragments": 3}},
    "blocks": {"text": {"fragment_size": 180, "number_of_fragments": 2}},
    "figures": {"caption": {"number_of_fragments": 0}},
    "tables": {"caption": {"number_of_fragments": 0}, "text": {"fragment_size": 150, "number_of_fragments": 1}},
    "formulas": {"recognized_latex": {"number_of_fragments": 0}, "text": {"fragment_size": 150,
                                                                          "number_of_fragments": 1}},
}
LABEL_FIELD = {"figures": "object_label", "tables": "object_label", "formulas": "equation_label"}
LABEL_TOPIC_K = 20                # topical candidates among which an exact object number is promoted (MODEL_CHOICE)
# numbered objects of a query (``vkm_corpus.search.object_refs``; agent Б 13.10; MODEL_CHOICE): pages and blocks
OBJECT_REF_NAME = "object_ref"    # named query: a hit that holds one of the reference phrases
OBJECT_REF_BOOST = 10.0           # a query that is only references: the phrase outweighs scattered terms
OBJECT_REF_TOPIC_K = MAX_SIZE     # topical pages among which the pages holding the reference are promoted
REF_TOPIC_FIELDS = ["authors.stem^1.0", "work_title^1.0"]   # the topic may name the work («… Соловьев Секунцов»)
REF_KINDS = ("pages", "blocks")   # figures/tables/formulas: printed numbers keep the label gate below (Q5)
_LABEL_QUERY = re.compile(r"(?:рис(?:унок|унке|\.)?|fig(?:ure|\.)?|табл(?:ица|ице|\.)?|table)\s*"
                          r"(\d+(?:[.\-]\d+)*[a-zа-я]?)", re.IGNORECASE)
_EQUATION_QUERY = re.compile(r"\((\d+(?:\.\d+)*)\)")

# public filter name → (index field, kind)
FILTERS: dict[str, tuple[str | None, str]] = {
    "source_id": ("source_id", "terms"), "work_id": ("work_id", "terms"), "page_id": ("page_id", "terms"),
    "language": ("language", "terms"), "origin": ("origin", "terms"), "review_status": ("review_status", "terms"),
    "text_layer": ("text_layer", "terms"), "page_status": ("page_status", "terms"),
    "page_kind": ("page_kind", "terms"), "page_class": ("page_class", "terms"),
    "block_type": ("block_type", "terms"),
    "figure_type": ("figure_type", "terms"), "layout_class": ("layout_class", "terms"),
    "source_scope": ("source_site_scope", "terms"), "source_scope_raw": ("source_site_scope_raw", "terms"),
    "source_scope_mapping": ("source_site_scope_mapping", "terms"),
    "available_basis": ("available_basis", "terms"),
    "foreign_content_work_id": ("foreign_content_work_ids", "terms"),
    "quality_flags_any": ("quality_flags", "terms"), "quality_flags_none": ("quality_flags", "not_terms"),
    "projection_flags_any": ("projection_flags", "terms"), "projection_flags_none": ("projection_flags", "not_terms"),
    "year": ("year", "range"), "page_index": ("page_index", "range"),
    "has_preview": ("has_preview", "bool"),
    "available_until": ("available_latest_day", "availability"), "unknown_policy": (None, "policy"),
    # {"source_scope": [...], "source_id": [...]}: the area OR one of these sources (``include_ambiguous`` of the API;
    # ``vkm_corpus.search.scope_filter``) — every document type carries both fields, the vector documents too
    "source_scope_or_source_id": (None, "scope_or_sources"),
}
_RANGE_KEYS = frozenset({"gte", "lte", "gt", "lt"})


class SearchRequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class SearchRequest:
    query: str
    kinds: tuple[str, ...] = ("PAGE",)
    filters: dict[str, Any] = field(default_factory=dict)
    size: int = 20
    offset: int = 0
    include_secondary_layers: bool = False
    include_duplicates: bool = False
    collapse_pages: bool = True
    highlight: bool = True
    exact: bool = False                      # unstemmed word forms (sub-field .exact) instead of morphology
    minimum_should_match: str = "3<70%"
    rrf_k: int = RRF_K
    window: int = 50
    group_fragments: bool = False            # FIGURE: one hit per page + caption block (vkm_corpus.search.fragments)
    long_query: str = LQ.LONG_QUERY_DEFAULT  # strict | relax | strip | relax_strip (vkm_corpus.search.long_query)

    def validate(self) -> None:
        if not self.query or not self.query.strip() or len(self.query) > 512:
            raise SearchRequestError("E_BAD_QUERY", "query must be 1..512 characters")
        if self.long_query not in LQ.LONG_QUERY_MODES:
            raise SearchRequestError("E_BAD_MODE", f"long_query is one of {LQ.LONG_QUERY_MODES}")
        self.kinds = tuple(k.upper() for k in self.kinds)
        if not self.kinds or any(k not in KINDS for k in self.kinds) or len(set(self.kinds)) != len(self.kinds):
            raise SearchRequestError("E_BAD_KIND", f"kinds must be distinct values of {KINDS}")
        if not 1 <= self.size <= MAX_SIZE or self.offset < 0 or self.offset + self.size > MAX_WINDOW:
            raise SearchRequestError("E_BAD_SIZE", f"size 1..{MAX_SIZE}, offset + size ≤ {MAX_WINDOW}")
        compile_filters(self.filters)


def parse_object_label(query: str) -> str | None:
    """«рис. 3.1», «Рисунок 3.1», «Fig. 3.1», «табл. 2», «(3.12)» → the printed number."""
    match = _LABEL_QUERY.search(query) or _EQUATION_QUERY.search(query)
    return match.group(1).lower() if match else None


def topic_of(query: str) -> str:
    """The query without its object-number phrase («рис. 3.1 мульда сдвижения» → «мульда сдвижения»)."""
    match = _LABEL_QUERY.search(query) or _EQUATION_QUERY.search(query)
    if not match:
        return query.strip()
    rest = query[:match.start()] + " " + query[match.end():]
    return re.sub(r"\s+", " ", rest).strip(" \t,.;:–—-")


def promote_labels(hits: list["SearchHit"], label: str, field_name: str, k: int = LABEL_TOPIC_K) -> list["SearchHit"]:
    """Hits of one kind in topic order → the hits among the first ``k`` whose number equals ``label`` first (topic
    order kept), then the others; ranks renumbered from 1."""
    head, tail = hits[:k], hits[k:]
    promoted = {id(h) for h in head if str(h.fields.get(field_name) or "").lower() == label}
    out = [h for h in head if id(h) in promoted] + [h for h in head if id(h) not in promoted] + tail
    for i, h in enumerate(out, 1):
        h.fields.setdefault("topic_rank", h.rank)
        h.fields["label_promoted"] = id(h) in promoted
        h.rank = i
        h.rank_in_kind = i
    return out


def body_refs(index_type: str, query: str) -> list[ObjectRef]:
    """The references that change the BM25 body of ``index_type``: every one on pages and blocks; on figures, tables
    and formulas only those that are not printed object numbers (those stay with the label gate)."""
    refs = parse_refs(query)
    return refs if index_type in REF_KINDS else [r for r in refs if not r.is_label]


def ref_gate(req: "SearchRequest", kind: str) -> tuple[list[ObjectRef], str] | None:
    """(references, topic) when ``kind`` is pages/blocks and the query names a numbered object next to a topic: the
    topic ranks, the pages holding the reference are promoted among its first :data:`OBJECT_REF_TOPIC_K` hits."""
    if KIND_INDEX[kind] not in REF_KINDS:
        return None
    refs = parse_refs(req.query)
    topic = strip_refs(req.query, refs) if refs else ""
    return (refs, topic) if refs and has_topic(topic) else None


def _ref_clause(refs: list[ObjectRef], phrase_fields: list[str], boost: float, name: str | None) -> dict[str, Any]:
    """Any phrase «<type word> <number>» of the references (no slop: «скважина № 75» is two adjacent tokens)."""
    clause: dict[str, Any] = {"bool": {"should": [{"match_phrase": {f: {"query": ph}}} for r in refs for ph in r.phrases
                                                  for f in phrase_fields], "minimum_should_match": 1, "boost": boost}}
    if name:
        clause["bool"]["_name"] = name
    return clause


def promote_refs(hits: list["SearchHit"], k: int = OBJECT_REF_TOPIC_K) -> list["SearchHit"]:
    """Hits in topic order → among the first ``k`` those holding a reference phrase first (topic order kept), then
    the others; ranks renumbered from 1 (``fields.topic_rank``, ``fields.object_ref_promoted``)."""
    head, tail = hits[:k], hits[k:]
    promoted = {id(h) for h in head if h.fields.get("object_ref_matched")}
    out = [h for h in head if id(h) in promoted] + [h for h in head if id(h) not in promoted] + tail
    for i, h in enumerate(out, 1):
        h.fields.setdefault("topic_rank", h.rank)
        h.fields["object_ref_promoted"] = id(h) in promoted
        h.rank = i
        h.rank_in_kind = i
    return out


def _as_list(name: str, value: Any) -> list[Any]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    values = [v for v in values if v is not None]
    if not values:
        raise SearchRequestError("E_BAD_FILTER", f"filter {name} needs at least one value")
    return sorted(values, key=str)


def compile_filters(filters: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Whitelisted filters → (filter clauses, must_not clauses)."""
    unknown = sorted(set(filters) - set(FILTERS))
    if unknown:
        raise SearchRequestError("E_UNKNOWN_FILTER", f"unknown filter(s) {unknown}; allowed: {sorted(FILTERS)}")
    clauses: list[dict[str, Any]] = []
    must_not: list[dict[str, Any]] = []
    policy = filters.get("unknown_policy")
    if policy is not None and policy not in UNKNOWN_POLICIES:
        raise SearchRequestError("E_BAD_FILTER", f"unknown_policy must be one of {UNKNOWN_POLICIES}")
    if policy is not None and "available_until" not in filters:
        raise SearchRequestError("E_BAD_FILTER", "unknown_policy is meaningful only with available_until")
    for name, value in sorted(filters.items()):
        index_field, kind = FILTERS[name]
        if kind == "terms":
            clauses.append({"terms": {index_field: _as_list(name, value)}})
        elif kind == "not_terms":
            must_not.append({"terms": {index_field: _as_list(name, value)}})
        elif kind == "bool":
            if not isinstance(value, bool):
                raise SearchRequestError("E_BAD_FILTER", f"{name} must be true or false")
            clauses.append({"term": {index_field: value}})
        elif kind == "range":
            if not isinstance(value, dict) or not value or set(value) - _RANGE_KEYS:
                raise SearchRequestError("E_BAD_FILTER", f"{name} must be a range object with {sorted(_RANGE_KEYS)}")
            clauses.append({"range": {index_field: dict(sorted(value.items()))}})
        elif kind == "scope_or_sources":
            if not isinstance(value, dict) or set(value) - {"source_scope", "source_id"}:
                raise SearchRequestError("E_BAD_FILTER", f"{name} is {{source_scope: [...], source_id: [...]}}")
            should = [{"terms": {field_name: _as_list(name, value[key])}} for key, field_name in
                      (("source_scope", "source_site_scope"), ("source_id", "source_id")) if value.get(key)]
            if not should:
                raise SearchRequestError("E_BAD_FILTER", f"filter {name} needs at least one value")
            clauses.append({"bool": {"should": should, "minimum_should_match": 1}})
        elif kind == "availability":
            if policy is None:
                raise SearchRequestError("E_UNKNOWN_POLICY_REQUIRED",
                                         "available_until requires unknown_policy = EXCLUDE | INCLUDE (H-19)")
            try:
                t0 = date.fromisoformat(str(value)).isoformat()
            except ValueError as exc:
                raise SearchRequestError("E_BAD_FILTER", "available_until must be a date YYYY-MM-DD") from exc
            dated = {"range": {index_field: {"lte": t0}}}
            if policy == "INCLUDE":
                clauses.append({"bool": {"should": [dated, {"bool": {"must_not": [{"exists": {"field": index_field}}]}}],
                                         "minimum_should_match": 1}})
            else:
                clauses.append(dated)
    return clauses, must_not


def _source_fields(index_type: str) -> list[str]:
    return sorted(k for k in properties(index_type) if k not in TEXT_FIELDS)


def build_body(index_type: str, req: SearchRequest, *, size: int, offset: int) -> dict[str, Any]:
    clauses, must_not = compile_filters(req.filters)
    if not req.include_secondary_layers:
        must_not.append({"term": {"is_primary_layer": False}})
    fields = [_exact(f) for f in FIELDS[index_type]] if req.exact else FIELDS[index_type]
    phrase_fields = [_exact(f) for f in PHRASE_FIELDS[index_type]] if req.exact else PHRASE_FIELDS[index_type]
    # numbered objects (object_refs): pages/blocks with a topic → the topic (with the work's authors and title) is the
    # query and the reference phrase only names the hits that hold it (boost 0; ``promote_refs`` lifts them); only
    # references → the phrase may match instead of the scattered terms and outweighs them; figures/tables/formulas →
    # a boost for a non-label reference («скважина 75» in a table caption), the query itself unchanged
    refs = body_refs(index_type, req.query)
    topic = strip_refs(req.query, refs) if refs else ""
    gated = bool(refs) and index_type in REF_KINDS and has_topic(topic)
    text = topic if gated else req.query
    should: list[dict[str, Any]] = [{"match_phrase": {f: {"query": text, "slop": 3, "boost": 2.0}}}
                                    for f in phrase_fields]
    should.append({"multi_match": {"query": text, "type": "best_fields", "fields": TITLE_BOOST_FIELDS}})
    label = parse_object_label(req.query) if index_type in LABEL_FIELD else None
    if label:
        should.append({"term": {LABEL_FIELD[index_type]: {"value": label, "boost": 5.0}}})
    must: dict[str, Any] = {"multi_match": {"query": req.query, "type": "best_fields", "fields": fields,
                                            "tie_breaker": 0.2, "minimum_should_match": req.minimum_should_match}}
    if gated:
        ref_topic = ["authors^1.0", "work_title.exact^1.0"] if req.exact else REF_TOPIC_FIELDS
        must = {"multi_match": {"query": topic, "type": "cross_fields", "fields": [*fields, *ref_topic],
                                "tie_breaker": 0.2, "minimum_should_match": req.minimum_should_match}}
        should.append(_ref_clause(refs, phrase_fields, 0.0, OBJECT_REF_NAME))
    elif refs:
        should.append(_ref_clause(refs, phrase_fields, OBJECT_REF_BOOST, OBJECT_REF_NAME))
        if not has_topic(topic):
            must = {"bool": {"should": [must, _ref_clause(refs, phrase_fields, 1.0, None)],
                             "minimum_should_match": 1}}
    body: dict[str, Any] = {
        "size": size, "from": offset, "track_total_hits": True,
        "_source": {"includes": _source_fields(index_type)},
        "query": {"bool": {"must": [must], "should": should, "filter": clauses, "must_not": must_not}},
        "sort": [{"_score": "desc"}, {"id": "asc"}],
        "aggs": {"availability": {"terms": {"field": "available_basis", "size": 10}}},
    }
    if req.highlight:
        spec = HIGHLIGHT[index_type]
        body["highlight"] = {"type": "unified",
                             "fields": {(_exact(f) if req.exact else f): cfg for f, cfg in spec.items()}}
    if index_type == "blocks" and req.collapse_pages:
        body["collapse"] = {"field": "page_id", "inner_hits": {
            "name": "best_blocks", "size": 3, "_source": {"includes": ["id", "reading_order", "is_primary_layer"]},
            "sort": [{"_score": "desc"}, {"id": "asc"}]}}
    elif index_type == "pages" and not req.include_duplicates:
        body["collapse"] = {"field": "dup_group_id", "inner_hits": {
            "name": "duplicates", "size": 5, "_source": {"includes": ["id", "source_id", "work_id"]},
            "sort": [{"id": "asc"}]}}
    return body


# ---------------------------------------------------------------- results
@dataclass
class SearchHit:
    id: str
    object_type: str
    index: str
    build_id: str | None
    score: float
    rank: int
    source_id: str | None = None
    work_id: str | None = None
    page_id: str | None = None
    page_index: int | None = None
    highlights: list[str] = field(default_factory=list)      # origin SEARCH_INDEX: never the object's text
    best_blocks: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    fields: dict[str, Any] = field(default_factory=dict)      # non-text metadata from the index
    rank_in_kind: int | None = None
    rrf_score: float | None = None
    highlight_origin: str = "SEARCH_INDEX"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchResponse:
    query: str
    kinds: tuple[str, ...]
    hits: list[SearchHit]
    totals: dict[str, int]
    availability_counts: dict[str, int]
    fusion: str
    warnings: list[str] = field(default_factory=list)
    long_query: dict[str, Any] | None = None       # the long-question plan (only when ``long_query`` is not strict)

    def as_dict(self) -> dict[str, Any]:
        out = {"query": self.query, "kinds": list(self.kinds), "hits": [h.as_dict() for h in self.hits],
               "totals": self.totals, "availability_counts": self.availability_counts, "fusion": self.fusion,
               "warnings": self.warnings}
        if self.long_query is not None:
            out["long_query"] = self.long_query
        return out


def _build_id_of(index: str) -> str | None:
    match = re.search(r"-m\d+-(.+)$", index)
    return match.group(1) if match else None


def parse_hits(resp: dict[str, Any], kind: str, offset: int = 0) -> tuple[list[SearchHit], list[str]]:
    hits: list[SearchHit] = []
    warnings: list[str] = []
    order = [f for base in HIGHLIGHT[KIND_INDEX[kind]] for f in (base, f"{base}.exact")]
    for i, h in enumerate(resp["hits"]["hits"], offset + 1):
        src = h.get("_source") or {}
        if src.get("id") != h["_id"]:
            warnings.append(f"ID_MISMATCH {h['_id']}")
        highlight = h.get("highlight") or {}
        fragments = [frag for f in order for frag in highlight.get(f, [])][:3]
        inner = h.get("inner_hits") or {}
        best = [{"id": x["_id"], "score": x.get("_score"), "reading_order": (x.get("_source") or {}).get("reading_order")}
                for x in (inner.get("best_blocks") or {}).get("hits", {}).get("hits", [])]
        dups = [x["_id"] for x in (inner.get("duplicates") or {}).get("hits", {}).get("hits", []) if x["_id"] != h["_id"]]
        named = h.get("matched_queries") or ()              # a list, or {name: score} with named-query scores
        fields = {k: v for k, v in src.items() if k not in ("id", "object_type", "source_id", "work_id", "page_id",
                                                            "page_index")}
        if OBJECT_REF_NAME in named:
            fields["object_ref_matched"] = True
        hits.append(SearchHit(
            id=h["_id"], object_type=src.get("object_type") or kind, index=h["_index"],
            build_id=_build_id_of(h["_index"]), score=float(h.get("_score") or 0.0), rank=i,
            source_id=src.get("source_id"), work_id=src.get("work_id"), page_id=src.get("page_id"),
            page_index=src.get("page_index"), highlights=fragments, best_blocks=best, duplicates=dups,
            fields=fields, rank_in_kind=i))
    return hits, warnings


def fuse_rrf(per_kind: dict[str, list[SearchHit]], kinds: tuple[str, ...], k: int = RRF_K) -> list[SearchHit]:
    """Reciprocal-rank fusion of per-kind rankings (each kind keeps its own BM25 statistics, H-44)."""
    scored = []
    for order, kind in enumerate(kinds):
        for hit in per_kind.get(kind, []):
            hit.rank_in_kind = hit.rank
            hit.rrf_score = 1.0 / (k + hit.rank)
            scored.append((-hit.rrf_score, order, hit.id, hit))
    scored.sort(key=lambda x: x[:3])
    fused = []
    for i, (_, _, _, hit) in enumerate(scored, 1):
        hit.rank = i
        fused.append(hit)
    return fused


def _availability(resp: dict[str, Any]) -> dict[str, int]:
    buckets = (((resp.get("aggregations") or {}).get("availability") or {}).get("buckets") or [])
    return {b["key"]: int(b["doc_count"]) for b in buckets}


def _gated_label(req: SearchRequest, kind: str) -> tuple[str, SearchRequest] | None:
    """(label, topical request) when ``kind`` has object numbers and the query names one next to a topic."""
    if KIND_INDEX[kind] not in LABEL_FIELD:
        return None
    label = parse_object_label(req.query)
    topic = topic_of(req.query) if label else ""
    if not label or len(topic) < 3:
        return None
    return label, replace(req, query=topic)


def search(client: Any, req: SearchRequest, prefix: str, *, indices: dict[str, str] | None = None) -> SearchResponse:
    """Run a request against the type aliases (or explicit ``indices`` — build checks run before the alias swap).
    ``req.long_query`` other than ``strict`` runs a long question's plan (``vkm_corpus.search.long_query``: the
    stripped text and/or the relaxed ``minimum_should_match``); the answer keeps the query as asked and carries the
    plan (``long_query``)."""
    req.validate()
    if req.long_query == LQ.STRICT:
        return _search(client, req, prefix, indices)
    lq = LQ.plan(req.query, req.long_query, req.minimum_should_match)
    run = replace(req, query=lq.bm25_text, minimum_should_match=lq.msm) if lq.changed else req
    response = _search(client, run, prefix, indices)
    response.query = req.query
    response.long_query = lq.record()
    return response


def _search(client: Any, req: SearchRequest, prefix: str, indices: dict[str, str] | None) -> SearchResponse:
    """:func:`search` of a validated request (the query and ``minimum_should_match`` it runs)."""
    targets ={k: (indices or {}).get(KIND_INDEX[k]) or alias_name(prefix, KIND_INDEX[k]) for k in req.kinds}
    totals: dict[str, int] = {}
    availability: dict[str, int] = {}
    warnings: list[str] = []
    if len(req.kinds) == 1:
        kind = req.kinds[0]
        if req.group_fragments and kind == "FIGURE":      # fragments of one figure: group from the top, then the window
            return _grouped_figures(client, req, targets[kind])
        gate = _gated_label(req, kind)
        rgate = ref_gate(req, kind) if gate is None else None
        if rgate is not None:                  # the topic first, then the pages holding the numbered object
            window = min(max(req.size + req.offset, OBJECT_REF_TOPIC_K), MAX_WINDOW)
            resp = client.search(index=targets[kind], body=build_body(KIND_INDEX[kind], req, size=window, offset=0))
            hits, warnings = parse_hits(resp, kind)
            hits = promote_refs(hits)[req.offset:req.offset + req.size]
        elif gate is None:
            resp = client.search(index=targets[kind], body=build_body(KIND_INDEX[kind], req, size=req.size,
                                                                      offset=req.offset))
            hits, warnings = parse_hits(resp, kind, req.offset)
        else:                                  # topic first, then the exact number among the topic's top hits
            label, topical = gate
            window = min(max(req.size + req.offset, LABEL_TOPIC_K), MAX_WINDOW)
            resp = client.search(index=targets[kind], body=build_body(KIND_INDEX[kind], topical, size=window,
                                                                      offset=0))
            hits, warnings = parse_hits(resp, kind)
            hits = promote_labels(hits, label, LABEL_FIELD[KIND_INDEX[kind]])[req.offset:req.offset + req.size]
        totals[kind] = int(resp["hits"]["total"]["value"])
        availability = _availability(resp)
        return SearchResponse(req.query, req.kinds, hits, totals, availability, "NONE", warnings)
    window = min(max(req.size + req.offset, req.window), MAX_WINDOW)
    lines: list[dict[str, Any]] = []
    gates = {kind: _gated_label(req, kind) for kind in req.kinds}
    rgates = {kind: ref_gate(req, kind) if gates[kind] is None else None for kind in req.kinds}
    for kind in req.kinds:
        body_req = gates[kind][1] if gates[kind] else req
        depth = max(window, LABEL_TOPIC_K) if gates[kind] else (
            min(max(window, OBJECT_REF_TOPIC_K), MAX_WINDOW) if rgates[kind] else window)
        if req.group_fragments and kind == "FIGURE":
            depth = min(max(depth, FRAGMENT_DEPTH), MAX_WINDOW)
        lines += [{"index": targets[kind]}, build_body(KIND_INDEX[kind], body_req, size=depth, offset=0)]
    responses = client.msearch(body=lines)["responses"]
    per_kind: dict[str, list[SearchHit]] = {}
    for kind, resp in zip(req.kinds, responses):
        if "error" in resp:
            raise SearchRequestError("E_SEARCH_FAILED", f"{kind}: {resp['error']}")
        per_kind[kind], w = parse_hits(resp, kind)
        if gates[kind]:
            per_kind[kind] = promote_labels(per_kind[kind], gates[kind][0], LABEL_FIELD[KIND_INDEX[kind]])
        elif rgates[kind]:
            per_kind[kind] = promote_refs(per_kind[kind])
        if req.group_fragments and kind == "FIGURE":
            per_kind[kind] = group_hits(per_kind[kind])
        warnings += w
        totals[kind] = int(resp["hits"]["total"]["value"])
        for key, n in _availability(resp).items():
            availability[key] = availability.get(key, 0) + n
    fused = fuse_rrf(per_kind, req.kinds, req.rrf_k)[req.offset:req.offset + req.size]
    return SearchResponse(req.query, req.kinds, fused, totals, availability, "RRF", warnings)


def _grouped_figures(client: Any, req: SearchRequest, target: str) -> SearchResponse:
    """FIGURE alone with ``group_fragments``: E's ranking (with the label gate) from the top, at least
    ``FRAGMENT_DEPTH`` deep and deeper while the grouped list is shorter than ``offset + size`` (up to MAX_WINDOW),
    grouped (``vkm_corpus.search.fragments``); the window is cut from the grouped list."""
    gate = _gated_label(req, "FIGURE")
    body_req = gate[1] if gate else req
    need = req.offset + req.size
    depth = min(max(need, FRAGMENT_DEPTH), MAX_WINDOW)
    while True:
        resp = client.search(index=target, body=build_body("figures", body_req, size=depth, offset=0))
        hits, warnings = parse_hits(resp, "FIGURE")
        if gate:
            hits = promote_labels(hits, gate[0], LABEL_FIELD["figures"])
        grouped = group_hits(hits)
        if len(grouped) >= need or len(hits) < depth or depth >= MAX_WINDOW:
            break
        depth = min(depth * 2, MAX_WINDOW)
    total = {"FIGURE": int(resp["hits"]["total"]["value"])}         # index objects, not groups
    return SearchResponse(req.query, req.kinds, grouped[req.offset:need], total, _availability(resp), "NONE", warnings)


# ---------------------------------------------------------------- rerank candidates (IDs + passage references)
def rerank_candidates(response: SearchResponse, *, mode: str = "text", max_candidates: int | None = None) -> dict[str, Any]:
    """Candidates for one reranker call. Text: a passage reference ``{rule: rerank_text_v1, object_ids}`` whose text
    the caller reads from DuckDB ``rerank_text`` (block IDs of a collapsed page, or the object ID). Visual: hits with a
    preview artifact. Limits are hard (H-13); nothing is split into batches."""
    limit = MAX_TEXT_CANDIDATES if mode == "text" else MAX_VISUAL_CANDIDATES
    if mode not in ("text", "visual"):
        raise SearchRequestError("E_BAD_MODE", "mode is text or visual")
    if max_candidates is not None:
        if max_candidates > limit:
            raise SearchRequestError("E_TOO_MANY_CANDIDATES", f"{mode} reranking takes at most {limit} candidates")
        limit = max_candidates
    pool = response.hits if mode == "text" else [h for h in response.hits if h.fields.get("preview_artifact_id")]
    out = []
    for hit in pool[:limit]:
        if hit.object_type == "BLOCK" and hit.best_blocks:
            ordered = sorted(hit.best_blocks, key=lambda b: (b.get("reading_order") is None, b.get("reading_order"),
                                                             b["id"]))
            candidate_id, kind, object_ids = hit.page_id or hit.id, "PAGE_PASSAGE", [b["id"] for b in ordered]
        else:
            candidate_id, kind, object_ids = hit.id, hit.object_type, [hit.id]
        entry: dict[str, Any] = {"candidate_id": candidate_id, "object_type": kind, "page_id": hit.page_id,
                                 "source_id": hit.source_id, "work_id": hit.work_id, "rank": hit.rank,
                                 "bm25_score": hit.score}
        if mode == "text":
            entry["passage"] = {"rule": RERANK_TEXT_RULE, "object_ids": object_ids}
        else:
            entry["image_artifact_id"] = hit.fields.get("preview_artifact_id")
        out.append(entry)
    return {"mode": mode, "rule": RERANK_TEXT_RULE if mode == "text" else None, "candidates": out,
            "truncated_from": len(pool) if len(pool) > limit else None}
