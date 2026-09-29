"""FastAPI application of the VKM API (prefix ``/v1``; task §33, CP-19).

* Bearer tokens: read endpoints accept the read or the write token, write endpoints (``/v1/reprocess/*``) only the
  write token (403 otherwise); ``/v1/health`` needs none. Tokens are compared in constant time and never logged —
  logs carry the token *label*.
* Every JSON answer is an ``ApiResponse`` (``ok``, ``meta``, ``item``/``items``, ``error``) whose objects carry the
  ``vkm.envelope/1``; validation errors, unknown routes and crashes use the same body.
* One JSON log line per request (request id, route, status, duration, error code, token label; never the
  ``Authorization`` header, texts or image bytes; queries only as a hash).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from vkm_corpus.api import images
from vkm_corpus.api.envelope import API_VERSION, ApiError, ApiResponse, Meta
from vkm_corpus.api.errors import ApiFailure
from vkm_corpus.api.service import MAX_TEXT_CANDIDATES, MAX_VISUAL_CANDIDATES, ApiService, Result
from vkm_corpus.contracts.vocab import Origin, ProcessingStatus, ReviewStatus, TextLayer
from vkm_corpus.ids.grammar import SOURCE_ID, WORK_ID

LOG = logging.getLogger("vkm.api")
SearchKind = Literal["PAGE", "BLOCK", "FIGURE", "TABLE", "FORMULA"]
ObjectQueryKind = Literal["BLOCK", "FIGURE", "TABLE", "FORMULA", "BIBLIOGRAPHY_ENTRY"]
IdStr = Annotated[str, Field(min_length=1, max_length=120)]


@dataclass
class ApiConfig:
    read_tokens: dict[str, str] = field(default_factory=dict)     # token → label
    write_tokens: dict[str, str] = field(default_factory=dict)
    max_body_bytes: int = 1_000_000

    @classmethod
    def from_settings(cls, settings: Any) -> "ApiConfig":
        from vkm_corpus.config import ConfigError

        read = {settings.api_token: "read"} if settings.api_token else {}
        write = {settings.api_write_token: "write"} if settings.api_write_token else {}
        if not read and not write:
            raise ConfigError("no API token configured (VKM_API_TOKEN_FILE / VKM_API_WRITE_TOKEN_FILE)")
        return cls(read_tokens=read, write_tokens=write)


# ---------------------------------------------------------------------------------------------------- bodies
class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchFilters(_Body):
    source_ids: list[Annotated[str, Field(pattern=SOURCE_ID)]] = Field(default_factory=list,
                                                                                    max_length=50)
    work_ids: list[Annotated[str, Field(pattern=WORK_ID)]] = Field(default_factory=list, max_length=50)
    page_ids: list[IdStr] = Field(default_factory=list, max_length=50)
    language: list[Annotated[str, Field(max_length=8)]] = Field(default_factory=list, max_length=10)
    origin: list[Origin] = Field(default_factory=list)
    review_status: list[ReviewStatus] = Field(default_factory=list)
    text_layer: list[TextLayer] = Field(default_factory=list)
    page_status: list[ProcessingStatus] = Field(default_factory=list)
    figure_type: list[Annotated[str, Field(max_length=40)]] = Field(default_factory=list, max_length=20)
    source_scope: list[Annotated[str, Field(max_length=60)]] = Field(
        default_factory=list, max_length=20, description="area of the SOURCE (normalised Scope), inherited by objects")
    source_scope_raw: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=20)
    year_from: int | None = Field(None, ge=1500, le=2100)
    year_to: int | None = Field(None, ge=1500, le=2100)
    available_until: date | None = Field(None, description="known at t0: needs unknown_policy (H-19)")
    unknown_policy: Literal["EXCLUDE", "INCLUDE"] | None = None
    quality_flags_any: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=20)
    quality_flags_none: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=20)
    has_preview: bool | None = None

    def to_search(self) -> dict[str, Any]:
        if (self.available_until is None) != (self.unknown_policy is None):
            raise ApiFailure("INVALID_ARGUMENT", "available_until and unknown_policy go together (H-19: an unknown "
                                                 "availability must be explicitly EXCLUDEd or INCLUDEd)")
        out: dict[str, Any] = {}
        for name, target in (("source_ids", "source_id"), ("work_ids", "work_id"), ("page_ids", "page_id"),
                             ("language", "language"), ("origin", "origin"), ("review_status", "review_status"),
                             ("text_layer", "text_layer"), ("page_status", "page_status"),
                             ("figure_type", "figure_type"), ("source_scope", "source_scope"),
                             ("source_scope_raw", "source_scope_raw"), ("quality_flags_any", "quality_flags_any"),
                             ("quality_flags_none", "quality_flags_none")):
            values = getattr(self, name)
            if values:
                out[target] = [str(v) for v in values]
        if self.year_from is not None or self.year_to is not None:
            out["year"] = {k: v for k, v in (("gte", self.year_from), ("lte", self.year_to)) if v is not None}
        if self.available_until is not None:
            out["available_until"] = self.available_until.isoformat()
            out["unknown_policy"] = self.unknown_policy
        if self.has_preview is not None:
            out["has_preview"] = self.has_preview
        return out


class SearchBody(_Body):
    query: str = Field(min_length=1, max_length=512)
    kinds: list[SearchKind] = Field(default_factory=lambda: ["PAGE"], min_length=1, max_length=5)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    limit: int = Field(20, ge=1, le=50)
    cursor: str | None = Field(None, max_length=10)
    include_duplicates: bool = False
    exact: bool = Field(False, description="unstemmed word forms instead of morphology")


HybridKind = Literal["PAGE", "FIGURE", "TABLE", "FORMULA"]


class HybridSearchBody(_Body):
    query: str = Field(min_length=1, max_length=512)
    kinds: list[HybridKind] = Field(default_factory=lambda: ["PAGE"], min_length=1, max_length=4,
                                    description="PAGE fuses every unit of a page (blocks included) at page level")
    filters: SearchFilters = Field(default_factory=SearchFilters,
                                   description="page-level filters only (figure_type / text_layer are refused)")
    limit: int = Field(20, ge=1, le=50)
    cursor: str | None = Field(None, max_length=10)
    candidates: int = Field(100, ge=10, le=200, description="candidates per stage (BM25, dense) and kind")
    include_duplicates: bool = False
    exact: bool = Field(False, description="unstemmed word forms in the BM25 stage")
    late: bool | None = Field(None, description="late interaction (mLateOn MaxSim on the RX580) over the RRF top "
                                                "late_candidates; null = server default")
    late_candidates: int = Field(100, ge=1, le=200, description="RRF candidates re-scored by the late stage")
    bib_route: bool | None = Field(None, description="bibliographic route (BIB_ENTRY channel, pages scored with their "
                                                     "reference-list entries); null = the query's bibliographic cues "
                                                     "decide")
    visual_route: bool | None = Field(None, description="visual route (page-image channel, Qwen3-VL page vectors, "
                                                        "RRF with the served page order); null = the query's picture "
                                                        "words decide (when the server enables the route)")
    translate: bool | None = Field(None, description="also search the query in the other language (RU ↔ EN, NAV "
                                                      "term dictionary) as extra RRF legs; null = server default "
                                                      "(on when the late stage runs; benchmarks/term_dictionary_v1)")


class ObjectsQueryBody(_Body):
    kinds: list[ObjectQueryKind] = Field(default_factory=lambda: ["FIGURE"], min_length=1, max_length=5)
    source_ids: list[Annotated[str, Field(pattern=SOURCE_ID)]] = Field(default_factory=list,
                                                                                    max_length=50)
    work_ids: list[Annotated[str, Field(pattern=WORK_ID)]] = Field(default_factory=list, max_length=50)
    page_from: int | None = Field(None, ge=1, le=9999)
    page_to: int | None = Field(None, ge=1, le=9999)
    figure_types: list[Annotated[str, Field(max_length=40)]] = Field(default_factory=list, max_length=20)
    review_status: list[ReviewStatus] = Field(default_factory=list)
    origin: list[Origin] = Field(default_factory=list)
    quality_flags_any: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=20)
    quality_flags_none: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=20)
    has_image: bool | None = None
    source_scope: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=20)
    source_scope_raw: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=20)
    caption_query: str | None = Field(None, min_length=1, max_length=200)
    limit: int = Field(50, ge=1, le=200)
    cursor: str | None = Field(None, max_length=10)


class TopicBody(_Body):
    query: str = Field(min_length=1, max_length=512, description="a topic or question, Russian or English")
    budget_chars: int = Field(12_000, ge=1_000, le=60_000, description="hard cap of the markdown rendering; the "
                                                                       "lowest-ranked items are trimmed first")
    source_ids: list[Annotated[str, Field(pattern=SOURCE_ID)]] = Field(default_factory=list, max_length=20)
    paraphrases: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=4, description="other wordings of the topic (other terms, English); fused "
                                                        "with the query by RRF")
    max_sources: int = Field(10, ge=1, le=50)
    max_sections: int = Field(12, ge=1, le=50)
    max_formulas: int = Field(10, ge=0, le=50)
    translate: bool | None = Field(None, description="also search the query in the other language (NAV term "
                                                     "dictionary) as one more formulation; null = the server default "
                                                     "(on; benchmarks/term_dictionary_v1)")


class Passage(_Body):
    candidate_id: IdStr
    object_ids: list[IdStr] = Field(min_length=1, max_length=20)


class RerankTextBody(_Body):
    query: str = Field(min_length=1, max_length=2048)
    candidate_ids: list[IdStr] = Field(min_length=1, max_length=100,
                                       description=f"≤ {MAX_TEXT_CANDIDATES} per call; more → 413 (H-13)")
    top_n: int | None = Field(None, ge=1, le=MAX_TEXT_CANDIDATES)
    passages: list[Passage] = Field(default_factory=list, max_length=MAX_TEXT_CANDIDATES,
                                    description="optional passage of a candidate: object ids whose rerank_text is "
                                                "joined (e.g. best blocks of a page)")


class RerankVisualBody(_Body):
    query: str = Field(min_length=1, max_length=2048)
    candidate_ids: list[IdStr] = Field(min_length=1, max_length=32,
                                       description=f"pages, figures, tables, formulas or image artifacts; ≤ "
                                                   f"{MAX_VISUAL_CANDIDATES} per call, more → 413 (H-13)")
    top_n: int | None = Field(None, ge=1, le=MAX_VISUAL_CANDIDATES)
    max_side: int = Field(images.DEFAULT_MAX_SIDE, ge=256, le=1024)


class ReprocessBody(_Body):
    """Options are exactly those the v0 worker executes (``vkm_corpus.ops.worker.ALLOWED_OPTIONS``)."""

    target_id: IdStr
    reason: str = Field(min_length=10, max_length=500)
    force: bool = Field(False, description="recompute rows from caches; never re-calls models by itself (H-05)")
    recall_model: bool = Field(False, description="call the models again even when cached calls exist (GPU time)")
    no_ocr: bool = Field(False, description="skip OCR stages (native text layers only)")
    job_id: int | None = Field(None, ge=1, description="confirm step: the job created by the first call")
    plan_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$", description="confirm step: hash of the plan")

    def options(self) -> dict[str, bool]:
        return {"force": self.force, "recall_model": self.recall_model, "no_ocr": self.no_ocr}


class CancelBody(_Body):
    reason: str = Field(min_length=10, max_length=500)


# ---------------------------------------------------------------------------------------------------- auth
def _token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise ApiFailure("UNAUTHORIZED", "missing bearer token")
    return header[7:].strip()


def _match(token: str, table: dict[str, str]) -> str | None:
    found = None
    for known, label in table.items():            # constant time per token, all tokens compared
        if hmac.compare_digest(token.encode("utf-8"), known.encode("utf-8")):
            found = label
    return found


def read_access(request: Request) -> str:
    config: ApiConfig = request.app.state.config
    token = _token(request)
    label = _match(token, config.read_tokens) or _match(token, config.write_tokens)
    if label is None:
        raise ApiFailure("UNAUTHORIZED", "invalid bearer token")
    request.state.token_label = label
    return label


def write_access(request: Request) -> str:
    config: ApiConfig = request.app.state.config
    token = _token(request)
    label = _match(token, config.write_tokens)
    if label is None:
        if _match(token, config.read_tokens):
            request.state.token_label = "read"
            raise ApiFailure("FORBIDDEN", "the read token cannot request reprocessing")
        raise ApiFailure("UNAUTHORIZED", "invalid bearer token")
    request.state.token_label = label
    return label


Read = Annotated[str, Depends(read_access)]
Write = Annotated[str, Depends(write_access)]


# ---------------------------------------------------------------------------------------------------- app
def create_app(service: ApiService, config: ApiConfig) -> FastAPI:
    app = FastAPI(title="VKM API", version=API_VERSION, docs_url="/v1/docs", openapi_url="/v1/openapi.json",
                  redoc_url=None,
                  description="Semantic access to the VKM document corpus (canonical snapshot + projections). "
                              "Every object carries the vkm.envelope/1; automatic content is "
                              "AUTO_EXTRACTED_UNREVIEWED, never a fact.")
    app.state.service = service
    app.state.config = config

    # ------------------------------------------------------------------------------------------ plumbing
    def request_id(request: Request) -> str:
        return request.state.request_id

    def respond(request: Request, result: Result, status: int = 200) -> JSONResponse:
        snapshot = None
        try:
            snapshot = service.canon.snapshot_id()
        except ApiFailure:
            pass
        meta = Meta(request_id=request.state.request_id, canonical_snapshot_id=snapshot,
                    elapsed_ms=round((time.perf_counter() - request.state.started) * 1000, 1),
                    warnings=result.warnings)
        body = ApiResponse(ok=True, meta=meta, item=result.item, items=result.items, next_cursor=result.next_cursor)
        return JSONResponse(body.model_dump(mode="json"), status_code=status)

    def error_response(request: Request, failure: ApiFailure) -> JSONResponse:
        request.state.error_code = failure.code
        rid = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:16]
        meta = Meta(request_id=rid, elapsed_ms=round((time.perf_counter() - getattr(request.state, "started",
                                                                                    time.perf_counter())) * 1000, 1))
        body = ApiResponse(ok=False, meta=meta, error=ApiError(**failure.body(rid)))
        return JSONResponse(body.model_dump(mode="json"), status_code=failure.http_status)

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex[:16]
        request.state.started = time.perf_counter()
        request.state.token_label = None
        request.state.error_code = None
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > config.max_body_bytes:
            response = error_response(request, ApiFailure("PAYLOAD_TOO_LARGE", "request body too large"))
        else:
            response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        LOG.info("request", extra={"vkm": {
            "stage": "api", "status": "ok" if response.status_code < 400 else "error",
            "error_code": request.state.error_code, "request_id": request.state.request_id,
            "route": request.scope.get("route").path if request.scope.get("route") else request.url.path,
            "method": request.method, "http_status": response.status_code,
            "duration_ms": round((time.perf_counter() - request.state.started) * 1000, 1),
            "token_label": request.state.token_label,
            "query_sha256": getattr(request.state, "query_sha256", None)}})
        return response

    @app.exception_handler(ApiFailure)
    async def _api_failure(request: Request, exc: ApiFailure):
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        fields = sorted({".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()})
        return error_response(request, ApiFailure("INVALID_ARGUMENT", "request does not match the schema",
                                                  details={"fields": fields[:20]}))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "INVALID_ARGUMENT"}.get(exc.status_code, "INVALID_ARGUMENT")
        failure = ApiFailure(code, "no such endpoint" if exc.status_code == 404 else str(exc.detail),
                             hint="see /v1/openapi.json" if exc.status_code == 404 else None)
        return error_response(request, failure)

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception):
        LOG.exception("unhandled error", extra={"vkm": {"request_id": getattr(request.state, "request_id", None)}})
        return error_response(request, ApiFailure("INTERNAL", "internal error; see the API log (log_ref)"))

    JSON_RESPONSES = {"response_model": ApiResponse}

    # ------------------------------------------------------------------------------------------ routes
    @app.get("/v1/health", tags=["service"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "api_version": API_VERSION}

    @app.get("/v1/status", tags=["service"])
    async def status(request: Request, _auth: Read) -> JSONResponse:
        body = await service.status(run_in_threadpool)
        return JSONResponse({"ok": True, "meta": {"request_id": request.state.request_id, "api_version": API_VERSION},
                             "status": body})

    @app.post("/v1/search", tags=["search"], **JSON_RESPONSES)
    def search_post(request: Request, body: SearchBody, _auth: Read) -> JSONResponse:
        request.state.query_sha256 = hashlib.sha256(body.query.encode("utf-8")).hexdigest()
        return respond(request, service.search(body.query, list(body.kinds), body.filters.to_search(), body.limit,
                                               body.cursor, body.include_duplicates, body.exact))

    @app.get("/v1/search", tags=["search"], **JSON_RESPONSES)
    def search_get(request: Request, _auth: Read, q: Annotated[str, Query(min_length=1, max_length=512)],
                   kinds: Annotated[list[SearchKind] | None, Query()] = None,
                   limit: Annotated[int, Query(ge=1, le=50)] = 20,
                   cursor: Annotated[str | None, Query(max_length=10)] = None) -> JSONResponse:
        return respond(request, service.search(q, list(kinds or ["PAGE"]), {}, limit, cursor))

    @app.post("/v1/search/hybrid", tags=["search"], **JSON_RESPONSES)
    def search_hybrid_post(request: Request, body: HybridSearchBody, _auth: Read) -> JSONResponse:
        request.state.query_sha256 = hashlib.sha256(body.query.encode("utf-8")).hexdigest()
        return respond(request, service.search_hybrid(body.query, list(body.kinds), body.filters.to_search(),
                                                      body.limit, body.cursor, body.candidates,
                                                      body.include_duplicates, body.exact, late=body.late,
                                                      late_candidates=body.late_candidates, bib_route=body.bib_route,
                                                      visual_route=body.visual_route, translate=body.translate))

    @app.get("/v1/search/hybrid", tags=["search"], **JSON_RESPONSES)
    def search_hybrid_get(request: Request, _auth: Read, q: Annotated[str, Query(min_length=1, max_length=512)],
                          kinds: Annotated[list[HybridKind] | None, Query()] = None,
                          limit: Annotated[int, Query(ge=1, le=50)] = 20,
                          cursor: Annotated[str | None, Query(max_length=10)] = None,
                          candidates: Annotated[int, Query(ge=10, le=200)] = 100,
                          late: Annotated[bool | None, Query()] = None,
                          late_candidates: Annotated[int, Query(ge=1, le=200)] = 100,
                          bib_route: Annotated[bool | None, Query()] = None,
                          visual_route: Annotated[bool | None, Query()] = None,
                          translate: Annotated[bool | None, Query()] = None) -> JSONResponse:
        request.state.query_sha256 = hashlib.sha256(q.encode("utf-8")).hexdigest()
        return respond(request, service.search_hybrid(q, list(kinds or ["PAGE"]), {}, limit, cursor, candidates,
                                                      late=late, late_candidates=late_candidates,
                                                      bib_route=bib_route, visual_route=visual_route,
                                                      translate=translate))

    @app.post("/v1/objects/query", tags=["search"], **JSON_RESPONSES)
    def objects_query(request: Request, body: ObjectsQueryBody, _auth: Read) -> JSONResponse:
        filters = body.model_dump(exclude={"kinds", "limit", "cursor"})
        filters["review_status"] = [str(v) for v in body.review_status]
        filters["origin"] = [str(v) for v in body.origin]
        return respond(request, service.query_objects(list(body.kinds), body.limit, body.cursor, **filters))

    @app.get("/v1/source/{source_id}", tags=["objects"], **JSON_RESPONSES)
    def get_source(request: Request, source_id: str, _auth: Read) -> JSONResponse:
        return respond(request, service.get_source(source_id))

    @app.get("/v1/source/{source_id}/pages", tags=["objects"], **JSON_RESPONSES)
    def source_pages(request: Request, source_id: str, _auth: Read,
                     from_page: Annotated[int, Query(ge=1, le=9999)] = 1,
                     to_page: Annotated[int, Query(ge=1, le=9999)] = 9999,
                     limit: Annotated[int, Query(ge=1, le=500)] = 100,
                     cursor: Annotated[str | None, Query(max_length=10)] = None) -> JSONResponse:
        return respond(request, service.list_source_pages(source_id, from_page, to_page, limit, cursor))

    @app.get("/v1/work/{work_id}", tags=["objects"], **JSON_RESPONSES)
    def get_work(request: Request, work_id: str, _auth: Read) -> JSONResponse:
        return respond(request, service.get_work(work_id))

    @app.get("/v1/page/{page_id}", tags=["objects"], **JSON_RESPONSES)
    def get_page(request: Request, page_id: str, _auth: Read,
                 include: Annotated[list[Literal["text", "blocks", "objects"]] | None, Query()] = None,
                 max_chars: Annotated[int, Query(ge=1, le=60_000)] = 12_000,
                 text_offset: Annotated[int, Query(ge=0)] = 0) -> JSONResponse:
        return respond(request, service.get_page(page_id, list(include) if include else None, max_chars,
                                                 text_offset))

    def _image_response(request: Request, object_id: str, max_side: int, fmt: str) -> Response:
        prepared, info = service.object_image(object_id, max_side, fmt)
        headers = {"X-VKM-Image-Meta": json.dumps(info, ensure_ascii=True, separators=(",", ":")),
                   "X-VKM-Snapshot": service.canon.snapshot_id() or "", "Cache-Control": "no-store"}
        return Response(content=prepared.data, media_type=prepared.media_type, headers=headers)

    @app.get("/v1/page/{page_id}/image", tags=["objects"])
    def page_image(request: Request, page_id: str, _auth: Read,
                   max_side: Annotated[int, Query(ge=128, le=2048)] = images.DEFAULT_MAX_SIDE,
                   format: Annotated[Literal["auto", "png", "jpeg"], Query()] = "auto") -> Response:
        return _image_response(request, page_id, max_side, format)

    @app.get("/v1/object/{object_id}", tags=["objects"], **JSON_RESPONSES)
    def get_object(request: Request, object_id: str, _auth: Read,
                   max_chars: Annotated[int, Query(ge=100, le=60_000)] = 20_000) -> JSONResponse:
        return respond(request, service.get_object(object_id, max_chars=max_chars))

    @app.get("/v1/object/{object_id}/image", tags=["objects"])
    def object_image(request: Request, object_id: str, _auth: Read,
                     max_side: Annotated[int, Query(ge=128, le=2048)] = images.DEFAULT_MAX_SIDE,
                     format: Annotated[Literal["auto", "png", "jpeg"], Query()] = "auto") -> Response:
        return _image_response(request, object_id, max_side, format)

    for kind_path, kind_name in (("figure", "FIGURE"), ("table", "TABLE"), ("formula", "FORMULA")):
        def make(kind_name: str = kind_name):
            def get_kind(request: Request, object_id: Annotated[str, Path()], _auth: Read,
                         max_chars: Annotated[int, Query(ge=100, le=60_000)] = 20_000) -> JSONResponse:
                from vkm_corpus.api.canon import kind_of

                if kind_of(object_id) != kind_name:
                    raise ApiFailure("INVALID_ID", f"{object_id} is not a {kind_name.lower()} id")
                return respond(request, service.get_object(object_id, max_chars=max_chars))

            def get_kind_image(request: Request, object_id: Annotated[str, Path()], _auth: Read,
                               max_side: Annotated[int, Query(ge=128, le=2048)] = images.DEFAULT_MAX_SIDE,
                               format: Annotated[Literal["auto", "png", "jpeg"], Query()] = "auto") -> Response:
                from vkm_corpus.api.canon import kind_of

                if kind_of(object_id) != kind_name:
                    raise ApiFailure("INVALID_ID", f"{object_id} is not a {kind_name.lower()} id")
                return _image_response(request, object_id, max_side, format)
            return get_kind, get_kind_image

        handler, image_handler = make()
        app.add_api_route(f"/v1/{kind_path}/{{object_id}}", handler, methods=["GET"], tags=["objects"],
                          response_model=ApiResponse, name=f"get_{kind_path}")
        app.add_api_route(f"/v1/{kind_path}/{{object_id}}/image", image_handler, methods=["GET"], tags=["objects"],
                          name=f"get_{kind_path}_image")

    @app.get("/v1/artifact/{artifact_id}", tags=["artifacts"], **JSON_RESPONSES)
    def get_artifact(request: Request, artifact_id: str, _auth: Read) -> JSONResponse:
        return respond(request, service.get_artifact(artifact_id))

    @app.get("/v1/artifact/{artifact_id}/content", tags=["artifacts"])
    def artifact_content(request: Request, artifact_id: str, _auth: Read) -> Response:
        data, row = service.artifact_bytes(artifact_id)
        return Response(content=data, media_type=row.get("media_type") or "application/octet-stream",
                        headers={"X-VKM-Artifact-Kind": str(row.get("artifact_kind")), "Cache-Control": "no-store",
                                 "X-VKM-Snapshot": service.canon.snapshot_id() or ""})

    @app.post("/v1/rerank/text", tags=["rerank"], **JSON_RESPONSES)
    async def rerank_text(request: Request, body: RerankTextBody, _auth: Read) -> JSONResponse:
        passages = {p.candidate_id: list(p.object_ids) for p in body.passages}
        result = await service.rerank_text(body.query, list(body.candidate_ids), body.top_n, passages,
                                           request.state.request_id, run_in_threadpool)
        return respond(request, result)

    @app.post("/v1/rerank/visual", tags=["rerank"], **JSON_RESPONSES)
    async def rerank_visual(request: Request, body: RerankVisualBody, _auth: Read) -> JSONResponse:
        result = await service.rerank_visual(body.query, list(body.candidate_ids), body.top_n,
                                             request.state.request_id, run_in_threadpool, body.max_side)
        return respond(request, result)

    @app.get("/v1/neighbors/{object_id}", tags=["graph"], **JSON_RESPONSES)
    def neighbors(request: Request, object_id: str, _auth: Read,
                  rel_types: Annotated[list[str] | None, Query(max_length=10)] = None,
                  direction: Annotated[Literal["in", "out", "both"], Query()] = "both",
                  limit: Annotated[int, Query(ge=1, le=200)] = 100) -> JSONResponse:
        return respond(request, service.neighbors(object_id, rel_types, direction, limit))

    @app.get("/v1/citations/{work_id}", tags=["graph"], **JSON_RESPONSES)
    def citations(request: Request, work_id: str, _auth: Read,
                  direction: Annotated[Literal["cites", "cited_by", "both"], Query()] = "both",
                  include_unlinked: bool = False,
                  limit: Annotated[int, Query(ge=1, le=500)] = 200) -> JSONResponse:
        return respond(request, service.citations(work_id, direction, include_unlinked, limit))

    # ---------------------------------------------------------------- NAV graph in Neo4j (agent G)
    @app.get("/v1/nav/graph/paths", tags=["navigation"], **JSON_RESPONSES)
    def nav_graph_paths(request: Request, _auth: Read, term_a: Annotated[str, Query(min_length=1, max_length=200)],
                        term_b: Annotated[str, Query(min_length=1, max_length=200)],
                        max_len: Annotated[int, Query(ge=1, le=6)] = 4, limit: Annotated[int, Query(ge=1, le=20)] = 5,
                        via: Annotated[list[Literal["concepts", "formulas", "sections", "topics"]] | None,
                                       Query(max_length=4)] = None) -> JSONResponse:
        return respond(request, service.nav_graph_paths(term_a, term_b, max_len, limit, via))

    @app.get("/v1/nav/graph/neighbourhood/{node_id}", tags=["navigation"], **JSON_RESPONSES)
    def nav_graph_neighbourhood(request: Request, node_id: str, _auth: Read,
                                depth: Annotated[int, Query(ge=1, le=2)] = 1,
                                limit: Annotated[int, Query(ge=1, le=200)] = 50) -> JSONResponse:
        return respond(request, service.nav_graph_neighbourhood(node_id, depth, limit))
    # ---------------------------------------------------------------- end NAV graph (agent G)

    # ---------------------------------------------------------------- navigation layer (derived, not evidence)
    @app.get("/v1/nav/outline/{source_id}", tags=["navigation"], **JSON_RESPONSES)
    def nav_outline(request: Request, source_id: str, _auth: Read) -> JSONResponse:
        return respond(request, service.nav_outline(source_id))

    @app.get("/v1/nav/section/{section_id}", tags=["navigation"], **JSON_RESPONSES)
    def nav_section(request: Request, section_id: str, _auth: Read) -> JSONResponse:
        return respond(request, service.nav_section(section_id))

    @app.get("/v1/nav/sections", tags=["navigation"], **JSON_RESPONSES)
    def nav_sections(request: Request, _auth: Read, q: Annotated[str, Query(min_length=1, max_length=512)],
                     source_id: str | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> JSONResponse:
        return respond(request, service.nav_sections(q, source_id, limit))

    @app.get("/v1/nav/formula/{formula_id}", tags=["navigation"], **JSON_RESPONSES)
    def nav_formula(request: Request, formula_id: str, _auth: Read) -> JSONResponse:
        return respond(request, service.nav_formula(formula_id))

    @app.get("/v1/nav/formulas", tags=["navigation"], **JSON_RESPONSES)
    def nav_formulas(request: Request, _auth: Read, concept: Annotated[str | None, Query(max_length=200)] = None,
                     symbol: Annotated[str | None, Query(max_length=64)] = None, source_id: str | None = None,
                     limit: Annotated[int, Query(ge=1, le=200)] = 50) -> JSONResponse:
        return respond(request, service.nav_formulas(concept, symbol, source_id, limit))

    @app.get("/v1/nav/concept", tags=["navigation"], **JSON_RESPONSES)
    def nav_concept(request: Request, _auth: Read, term: Annotated[str, Query(min_length=1, max_length=200)],
                    limit: Annotated[int, Query(ge=1, le=100)] = 20) -> JSONResponse:
        return respond(request, service.nav_concept(term, limit))

    # topics (RAPTOR tree, agent T) and duplicates / reprints (agent U)
    @app.get("/v1/nav/topic/{topic_id}", tags=["navigation"], **JSON_RESPONSES)
    def nav_topic(request: Request, _auth: Read, topic_id: str) -> JSONResponse:
        return respond(request, service.nav_topic(topic_id))

    @app.get("/v1/nav/topics", tags=["navigation"], **JSON_RESPONSES)
    def nav_topics(request: Request, _auth: Read, term: Annotated[list[str], Query(min_length=1, max_length=5)],
                   limit: Annotated[int, Query(ge=1, le=50)] = 10,
                   level: Annotated[int | None, Query(ge=1, le=3)] = None) -> JSONResponse:
        return respond(request, service.nav_topics(term, limit, level))

    @app.get("/v1/nav/similar/{section_id}", tags=["navigation"], **JSON_RESPONSES)
    def nav_similar(request: Request, _auth: Read, section_id: str, k: Annotated[int, Query(ge=1, le=50)] = 10,
                    other_sources_only: bool = True) -> JSONResponse:
        return respond(request, service.nav_similar_sections(section_id, k, other_sources_only))

    @app.get("/v1/nav/section_topics/{section_id}", tags=["navigation"], **JSON_RESPONSES)
    def nav_section_topics(request: Request, _auth: Read, section_id: str) -> JSONResponse:
        return respond(request, service.nav_section_topics(section_id))

    @app.get("/v1/nav/copies", tags=["navigation"], **JSON_RESPONSES)
    def nav_copies(request: Request, _auth: Read, ref: Annotated[str, Query(min_length=1, max_length=200)],
                   limit: Annotated[int, Query(ge=1, le=200)] = 50) -> JSONResponse:
        return respond(request, service.nav_copies(ref, limit))

    @app.get("/v1/nav/overlap/{source_id}", tags=["navigation"], **JSON_RESPONSES)
    def nav_overlap(request: Request, _auth: Read, source_id: str,
                    limit: Annotated[int, Query(ge=1, le=200)] = 50) -> JSONResponse:
        return respond(request, service.nav_source_overlap(source_id, limit))

    # parameter-value candidates (agent P): navigation, never recommended values
    @app.get("/v1/nav/parameters", tags=["navigation"], **JSON_RESPONSES)
    def nav_parameters(request: Request, _auth: Read,
                       property: Annotated[str | None, Query(max_length=200)] = None,  # noqa: A002
                       material: Annotated[str | None, Query(max_length=200)] = None,
                       site: Annotated[str | None, Query(max_length=100)] = None,
                       scale: Annotated[str | None, Query(max_length=20)] = None,
                       source_id: Annotated[str | None, Query(max_length=20)] = None,
                       limit: Annotated[int, Query(ge=1, le=200)] = 50) -> JSONResponse:
        return respond(request, service.nav_parameters(property, material, site, scale, source_id, limit))

    @app.get("/v1/nav/parameter_summary", tags=["navigation"], **JSON_RESPONSES)
    def nav_parameter_summary(request: Request, _auth: Read,
                              property: Annotated[str, Query(min_length=1, max_length=200)],  # noqa: A002
                              material: Annotated[str | None, Query(max_length=200)] = None) -> JSONResponse:
        return respond(request, service.nav_parameter_summary(property, material))

    # term dictionary (agent TR): RU ↔ EN (DE) equivalents, synonyms, abbreviations — navigation, not evidence
    @app.get("/v1/nav/translate", tags=["navigation"], **JSON_RESPONSES)
    def nav_translate(request: Request, _auth: Read, term: Annotated[str, Query(min_length=1, max_length=200)],
                      target: Annotated[Literal["ru", "en", "de"] | None, Query()] = None,
                      limit: Annotated[int, Query(ge=1, le=50)] = 10) -> JSONResponse:
        return respond(request, service.nav_translate(term, target, limit))
    # end term dictionary (agent TR)

    # ---------------------------------------------------------------- topic dossier (navigation + catalogues)
    @app.get("/v1/topic", tags=["navigation"], **JSON_RESPONSES)
    def topic_get(request: Request, _auth: Read, q: Annotated[str, Query(min_length=1, max_length=512)],
                  budget: Annotated[int, Query(ge=1_000, le=60_000)] = 12_000,
                  source_id: Annotated[list[Annotated[str, Field(pattern=SOURCE_ID)]] | None,
                                       Query(max_length=20)] = None,
                  paraphrase: Annotated[list[Annotated[str, Field(min_length=1, max_length=512)]] | None,
                                        Query(max_length=4)] = None,
                  max_sources: Annotated[int, Query(ge=1, le=50)] = 10,
                  max_sections: Annotated[int, Query(ge=1, le=50)] = 12,
                  max_formulas: Annotated[int, Query(ge=0, le=50)] = 10,
                  translate: Annotated[bool | None, Query()] = None) -> JSONResponse:
        request.state.query_sha256 = hashlib.sha256(q.encode("utf-8")).hexdigest()
        return respond(request, service.reconstruct_topic(q, budget_chars=budget, source_ids=list(source_id or []),
                                                          max_sources=max_sources, max_sections=max_sections,
                                                          max_formulas=max_formulas,
                                                          paraphrases=list(paraphrase or []), translate=translate))

    @app.post("/v1/topic", tags=["navigation"], **JSON_RESPONSES)
    def topic_post(request: Request, body: TopicBody, _auth: Read) -> JSONResponse:
        request.state.query_sha256 = hashlib.sha256(body.query.encode("utf-8")).hexdigest()
        return respond(request, service.reconstruct_topic(body.query, budget_chars=body.budget_chars,
                                                          source_ids=list(body.source_ids),
                                                          max_sources=body.max_sources,
                                                          max_sections=body.max_sections,
                                                          max_formulas=body.max_formulas,
                                                          paraphrases=list(body.paraphrases),
                                                          translate=body.translate))

    @app.get("/v1/provenance/{object_id}", tags=["provenance"], **JSON_RESPONSES)
    def provenance(request: Request, object_id: str, _auth: Read) -> JSONResponse:
        return respond(request, service.provenance(object_id))

    @app.get("/v1/processing/status", tags=["processing"], **JSON_RESPONSES)
    def processing_status(request: Request, _auth: Read, source_id: str | None = None, page_id: str | None = None,
                          run_id: str | None = None, job_id: Annotated[int | None, Query(ge=1)] = None
                          ) -> JSONResponse:
        return respond(request, service.processing_status(source_id=source_id, page_id=page_id, run_id=run_id,
                                                          job_id=job_id))

    @app.get("/v1/jobs/{job_id}", tags=["processing"], **JSON_RESPONSES)
    def get_job(request: Request, job_id: Annotated[int, Path(ge=1)], _auth: Read) -> JSONResponse:
        return respond(request, service.get_job(job_id))

    @app.post("/v1/jobs/{job_id}/cancel", tags=["write"], **JSON_RESPONSES)
    def cancel_job(request: Request, job_id: Annotated[int, Path(ge=1)], body: CancelBody, label: Write
                   ) -> JSONResponse:
        return respond(request, service.cancel_job(job_id, body.reason, label))

    @app.post("/v1/reprocess/source", tags=["write"], **JSON_RESPONSES)
    def reprocess_source(request: Request, body: ReprocessBody, label: Write) -> JSONResponse:
        status_code, result = service.reprocess("REPROCESS_SOURCE", body.target_id, body.reason, body.options(),
                                                body.job_id, body.plan_sha256, label)
        return respond(request, result, status_code)

    @app.post("/v1/reprocess/page", tags=["write"], **JSON_RESPONSES)
    def reprocess_page(request: Request, body: ReprocessBody, label: Write) -> JSONResponse:
        status_code, result = service.reprocess("REPROCESS_PAGE", body.target_id, body.reason, body.options(),
                                                body.job_id, body.plan_sha256, label)
        return respond(request, result, status_code)

    return app


def build_from_settings(settings: Any = None) -> FastAPI:
    """Production wiring: canon of the CANONICAL root + configured projections, rerank gateway and control plane."""
    from vkm_corpus.api.backends import (ArtifactBlobs, GatewayRerankBackend, HybridBackend, Neo4jBackend,
                                         OpenSearchBackend, PgControlPlane)
    from vkm_corpus.api.canon import CanonStore
    from vkm_corpus.api.service import ApiDeps
    from vkm_corpus.config import load_settings

    settings = settings or load_settings()
    root = settings.require_data_root()
    search = OpenSearchBackend(settings) if settings.opensearch_url else None
    deps = ApiDeps(canon=CanonStore.from_data_root(root), blobs=ArtifactBlobs(root / "artifacts"),
                   search=search, hybrid=HybridBackend(settings, search) if search is not None else None,
                   graph=Neo4jBackend(settings) if settings.neo4j_uri else None,
                   rerank=GatewayRerankBackend(settings) if settings.rerank_url else None,
                   control=PgControlPlane(settings) if settings.pg_dsn else None)
    from vkm_corpus.catalogues.store import CatalogueStore
    from vkm_corpus.navigation.store import NavStore

    deps.nav = NavStore(root)                  # served only once derived/navigation/CURRENT is published
    deps.catalogues = CatalogueStore(root)     # served only once derived/catalogues/CURRENT is published
    return create_app(ApiService(deps), ApiConfig.from_settings(settings))
