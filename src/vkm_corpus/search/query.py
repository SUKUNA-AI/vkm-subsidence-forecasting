"""Query side of the retrieval projection: BM25 candidates with stable canonical IDs (task §28, §32, §59).

* Filters come from a closed whitelist (unknown name → ``E_UNKNOWN_FILTER``; the API maps it to 400). The source area
  filter is ``source_scope`` (H-18: it is the *source's* area, inherited by every object). Availability
  ``available_until`` requires an explicit ``unknown_policy`` (EXCLUDE | INCLUDE) and the answer carries counts by
  ``available_basis`` (H-19). By default only the primary text layer is searched (H-30).
* Per type the query is ``multi_match`` (best fields, boosts ``FIELDS``) + phrase boost on the exact sub-field + a
  term boost when the query names an object number («рис. 3.1», «табл. 2», «(3.12)»). Ties break on ``id``.
* Blocks collapse by ``page_id`` (``best_blocks``); pages collapse duplicate pages by ``dup_group_id``.
* Several kinds are never ranked by raw BM25 across indices (their statistics differ): one query per kind
  (``_msearch``), then reciprocal-rank fusion (H-44).
* Rerank candidates are IDs plus a passage reference by rule ``rerank_text_v1``; the text is read from agent D's
  DuckDB view ``rerank_text``, never from the index (H-04, H-13): ≤ 24 text candidates, ≤ 8 images per call.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from vkm_corpus.contracts import vocab
from vkm_corpus.search.mappings import KIND_INDEX, TEXT_FIELDS, alias_name, properties

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

    def validate(self) -> None:
        if not self.query or not self.query.strip() or len(self.query) > 512:
            raise SearchRequestError("E_BAD_QUERY", "query must be 1..512 characters")
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
    should: list[dict[str, Any]] = [{"match_phrase": {f: {"query": req.query, "slop": 3, "boost": 2.0}}}
                                    for f in phrase_fields]
    should.append({"multi_match": {"query": req.query, "type": "best_fields", "fields": TITLE_BOOST_FIELDS}})
    label = parse_object_label(req.query) if index_type in LABEL_FIELD else None
    if label:
        should.append({"term": {LABEL_FIELD[index_type]: {"value": label, "boost": 5.0}}})
    body: dict[str, Any] = {
        "size": size, "from": offset, "track_total_hits": True,
        "_source": {"includes": _source_fields(index_type)},
        "query": {"bool": {"must": [{"multi_match": {"query": req.query, "type": "best_fields",
                                                     "fields": fields, "tie_breaker": 0.2,
                                                     "minimum_should_match": req.minimum_should_match}}],
                           "should": should, "filter": clauses, "must_not": must_not}},
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

    def as_dict(self) -> dict[str, Any]:
        return {"query": self.query, "kinds": list(self.kinds), "hits": [h.as_dict() for h in self.hits],
                "totals": self.totals, "availability_counts": self.availability_counts, "fusion": self.fusion,
                "warnings": self.warnings}


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
        hits.append(SearchHit(
            id=h["_id"], object_type=src.get("object_type") or kind, index=h["_index"],
            build_id=_build_id_of(h["_index"]), score=float(h.get("_score") or 0.0), rank=i,
            source_id=src.get("source_id"), work_id=src.get("work_id"), page_id=src.get("page_id"),
            page_index=src.get("page_index"), highlights=fragments, best_blocks=best, duplicates=dups,
            fields={k: v for k, v in src.items() if k not in ("id", "object_type", "source_id", "work_id", "page_id",
                                                             "page_index")},
            rank_in_kind=i))
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


def search(client: Any, req: SearchRequest, prefix: str, *, indices: dict[str, str] | None = None) -> SearchResponse:
    """Run a request against the type aliases (or explicit ``indices`` — build checks run before the alias swap)."""
    req.validate()
    targets = {k: (indices or {}).get(KIND_INDEX[k]) or alias_name(prefix, KIND_INDEX[k]) for k in req.kinds}
    totals: dict[str, int] = {}
    availability: dict[str, int] = {}
    warnings: list[str] = []
    if len(req.kinds) == 1:
        kind = req.kinds[0]
        resp = client.search(index=targets[kind], body=build_body(KIND_INDEX[kind], req, size=req.size,
                                                                  offset=req.offset))
        hits, warnings = parse_hits(resp, kind, req.offset)
        totals[kind] = int(resp["hits"]["total"]["value"])
        availability = _availability(resp)
        return SearchResponse(req.query, req.kinds, hits, totals, availability, "NONE", warnings)
    window = min(max(req.size + req.offset, req.window), MAX_WINDOW)
    lines: list[dict[str, Any]] = []
    for kind in req.kinds:
        lines += [{"index": targets[kind]}, build_body(KIND_INDEX[kind], req, size=window, offset=0)]
    responses = client.msearch(body=lines)["responses"]
    per_kind: dict[str, list[SearchHit]] = {}
    for kind, resp in zip(req.kinds, responses):
        if "error" in resp:
            raise SearchRequestError("E_SEARCH_FAILED", f"{kind}: {resp['error']}")
        per_kind[kind], w = parse_hits(resp, kind)
        warnings += w
        totals[kind] = int(resp["hits"]["total"]["value"])
        for key, n in _availability(resp).items():
            availability[key] = availability.get(key, 0) + n
    fused = fuse_rrf(per_kind, req.kinds, req.rrf_k)[req.offset:req.offset + req.size]
    return SearchResponse(req.query, req.kinds, fused, totals, availability, "RRF", warnings)


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
