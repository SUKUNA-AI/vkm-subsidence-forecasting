"""Rerank contract ``vkm.rerank/1``, shared by the EDGE gateway (agent F) and the VKM API (agent G).

Two endpoints of the gateway ``vkm-rerank-gateway`` (decisions CP-18, H-13, H-45):

* ``POST /v1/rerank/text`` — ``{query, candidates: [{id, text}], top_n}``; at most ``MAX_TEXT_CANDIDATES`` candidates
  per call (more → HTTP 413, batches are never glued: v3.5 is listwise, scores of different calls are not comparable);
  the whole listwise prompt is at most ``TEXT_TOKEN_BUDGET`` tokens, counted with the ``tokenizer.json`` of the text
  service (more → 413).
* ``POST /v1/rerank/visual`` — ``{query, candidates: [{id, image_base64}], top_n}``; at most
  ``MAX_VISUAL_CANDIDATES`` images per call (more → 413); images are normalised by the gateway (EXIF, RGB, long side
  ≤ ``IMAGE_MAX_SIDE_PX``, then the Qwen2-VL ``smart_resize`` grid of 28 px within the m0 pixel bounds).

The candidate-count limits are *not* schema constraints: the gateway checks them before validation and answers 413,
``check_limits()`` raises :class:`RerankLimitError` for the same rule on the client side.

Scores are a retrieval signal, never evidence: a response is ``layer = SERVICE`` and ``review_status =
NOT_APPLICABLE``; it is not written into the canonical layer. Scores are comparable only inside one response.
Closed vocabularies of the platform live in ``vkm_corpus.contracts`` (H-01); the service-level literals below are
``Literal`` types, not enums.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT = "vkm.rerank/1"

# --- limits (H-13, H-45, CP-18) -------------------------------------------------------------------------------------
MAX_TEXT_CANDIDATES = 24
MAX_VISUAL_CANDIDATES = 8
TEXT_TOKEN_BUDGET = 4096                 # whole listwise prompt of v3.5, tokenizer.json of the service
IMAGE_MAX_SIDE_PX = 1024                 # H-45: default long side
M0_PATCH_FACTOR = 28                     # Qwen2-VL: patch 14 × merge 2
M0_MIN_PIXELS = 3136                     # preprocessor of jina-reranker-m0 (compute_score)
M0_MAX_PIXELS = 602112                   # → at most 768 visual tokens per image
VISUAL_QUERY_MAX_TOKENS = 224            # prompt of one image ≤ 1024 tokens (= llama-server slot context -c)
MAX_IMAGE_BYTES = 10 * 1024 * 1024       # decoded bytes of one image
MAX_IMAGE_SOURCE_PIXELS = 50_000_000     # decompression-bomb guard, before resizing
MAX_QUERY_CHARS = 2048
MAX_TEXT_CHARS = 32_768                  # one text candidate (the token budget is the real limit)
MAX_BODY_BYTES_TEXT = 4 * 1024 * 1024
MAX_BODY_BYTES_VISUAL = 64 * 1024 * 1024
MIN_TRUNCATE_TOKENS = 16

# --- transport ------------------------------------------------------------------------------------------------------
HEADER_TOKEN = "X-VKM-Rerank-Token"
HEADER_REQUEST_ID = "X-VKM-Request-Id"
DEFAULT_PORT = 18084
# The text service runs inference serially (one lock) and the gateway does not limit it (H-41), so a burst queues
# inside the service: 5–17 s per call alone, ≈ 10 s under visual load → up to ~100 s for 10 queued calls.
TEXT_TIMEOUT_S = 300.0                   # gateway → text backend (own call + the service's queue)
VISUAL_TIMEOUT_S = 300.0                 # gateway → visual backend (8 images × 6.5–8 s + queue)
CLIENT_TIMEOUT_TEXT_S = 330.0            # VKM API → gateway (≥ backend timeout)
CLIENT_TIMEOUT_VISUAL_S = 330.0          # H-13: ≥ 120 s

# --- error codes (HTTP status in brackets); service API codes, not canonical ErrorCode values -------------------------
E_UNAUTHORIZED = "RERANK_UNAUTHORIZED"                    # 401
E_INPUT_INVALID = "RERANK_INPUT_INVALID"                  # 422
E_IMAGE_DECODE_FAILED = "RERANK_IMAGE_DECODE_FAILED"      # 422
E_PAYLOAD_TOO_LARGE = "RERANK_PAYLOAD_TOO_LARGE"          # 413
E_BACKEND_BUSY = "RERANK_BACKEND_BUSY"                    # 503, retryable
E_BACKEND_UNAVAILABLE = "RERANK_BACKEND_UNAVAILABLE"      # 503, retryable
E_BACKEND_TIMEOUT = "RERANK_BACKEND_TIMEOUT"              # 504, retryable
E_BACKEND_ERROR = "RERANK_BACKEND_ERROR"                  # 502
E_NOT_FOUND = "RERANK_NOT_FOUND"                          # 404
ERROR_STATUS = {
    E_UNAUTHORIZED: 401, E_INPUT_INVALID: 422, E_IMAGE_DECODE_FAILED: 422, E_PAYLOAD_TOO_LARGE: 413,
    E_BACKEND_BUSY: 503, E_BACKEND_UNAVAILABLE: 503, E_BACKEND_TIMEOUT: 504, E_BACKEND_ERROR: 502, E_NOT_FOUND: 404,
}
RETRYABLE = frozenset({E_BACKEND_BUSY, E_BACKEND_UNAVAILABLE, E_BACKEND_TIMEOUT})

_ID_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,256}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RerankLimitError(ValueError):
    """A request exceeds a size limit of the contract (→ HTTP 413 ``RERANK_PAYLOAD_TOO_LARGE``)."""

    def __init__(self, what: str, limit: int, actual: int) -> None:
        super().__init__(f"{what}: {actual} > {limit}")
        self.what, self.limit, self.actual = what, limit, actual


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _non_blank(value: str, name: str) -> str:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value


class _RequestBase(_Strict):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    top_n: int | None = Field(default=None, ge=1, description="default: all candidates; larger values are clamped")
    request_id: str | None = Field(default=None, description="caller correlation id (logged, echoed)")

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        return _non_blank(value, "query")

    @field_validator("request_id")
    @classmethod
    def _request_id_format(cls, value: str | None) -> str | None:
        if value is not None and not _REQUEST_ID_RE.match(value):
            raise ValueError("request_id must match [A-Za-z0-9._:-]{1,128}")
        return value

    @model_validator(mode="after")
    def _unique_ids(self):
        ids = [c.id for c in self.candidates]  # type: ignore[attr-defined]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate ids must be unique within a request")
        return self


class TextCandidate(_Strict):
    id: str = Field(description="stable object id (block / passage / page), echoed back")
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS, description="passage text by rule rerank_text_v1")

    @field_validator("id")
    @classmethod
    def _id_format(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError("id must be 1..256 printable characters without whitespace")
        return value

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        return _non_blank(value, "text")


class TextRerankRequest(_RequestBase):
    candidates: list[TextCandidate] = Field(min_length=1)
    truncate_to_tokens: int | None = Field(
        default=None, ge=MIN_TRUNCATE_TOKENS, le=TEXT_TOKEN_BUDGET,
        description="optional: cut every candidate to this many tokens (reported per candidate); default: no cut")

    def check_limits(self) -> None:
        if len(self.candidates) > MAX_TEXT_CANDIDATES:
            raise RerankLimitError("text candidates per call", MAX_TEXT_CANDIDATES, len(self.candidates))


class VisualCandidate(_Strict):
    id: str = Field(description="stable object / artifact id, echoed back")
    image_base64: str = Field(min_length=4, description="PNG, JPEG or WebP bytes, standard base64 without data: prefix")

    @field_validator("id")
    @classmethod
    def _id_format(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError("id must be 1..256 printable characters without whitespace")
        return value


class VisualRerankRequest(_RequestBase):
    candidates: list[VisualCandidate] = Field(min_length=1)

    def check_limits(self) -> None:
        if len(self.candidates) > MAX_VISUAL_CANDIDATES:
            raise RerankLimitError("visual candidates per call", MAX_VISUAL_CANDIDATES, len(self.candidates))


class LatencyMs(_Strict):
    total: float = Field(ge=0)
    preprocess: float = Field(ge=0, description="validation, token counting, image normalisation")
    queue: float = Field(ge=0, description="waiting for a gateway slot of this backend")
    backend: float = Field(ge=0, description="backend HTTP round trip (includes the backend's own queue)")


class RankedCandidate(_Strict):
    id: str
    rank: int = Field(ge=1)
    score: float
    input_index: int = Field(ge=0)
    # text
    input_text_sha256: str | None = Field(default=None, description="sha256 of the candidate text as received")
    text_chars: int | None = None
    text_char_range: tuple[int, int] | None = Field(default=None, description="[start, end) of the text scored")
    truncated: bool = False
    n_tokens: int | None = Field(default=None, description="tokens of the (possibly truncated) candidate alone")
    # visual
    source_image_sha256: str | None = Field(default=None, description="sha256 of the decoded input bytes")
    image_sha256: str | None = Field(default=None, description="sha256 of the normalised PNG sent to the model")
    pixel_sha256: str | None = Field(default=None, description="sha256 of the normalised RGB pixels")
    source_image_size_px: tuple[int, int] | None = None
    image_size_px: tuple[int, int] | None = Field(default=None, description="(width, height) seen by the model")
    image_tokens: int | None = None


class RerankResponse(_Strict):
    contract: Literal["vkm.rerank/1"] = CONTRACT
    kind: Literal["text", "visual"]
    request_id: str
    model_id: str
    model_revision: str
    quant: str
    placement: str
    backend: str
    backend_version: str
    weights_sha256: str | None = None
    model_config_sha256: str = Field(description="hash of the model/service configuration shown by GET /status")
    score_semantics: str
    license: str
    candidate_ids: list[str] = Field(description="ranked ids, length = top_n")
    scores: list[float] = Field(description="scores aligned with candidate_ids")
    results: list[RankedCandidate]
    n_candidates: int
    top_n: int
    query_sha256: str
    input_sha256: str = Field(description="sha256 over query and the ordered (id, content hash) pairs")
    n_tokens_total: int | None = Field(default=None, description="text: exact listwise prompt tokens")
    latency_ms: LatencyMs
    layer: Literal["SERVICE"] = "SERVICE"
    review_status: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    gateway_version: str
    created_at: str
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _aligned(self):
        if len(self.candidate_ids) != len(self.scores) or len(self.results) != len(self.candidate_ids):
            raise ValueError("candidate_ids, scores and results must be aligned")
        if [r.id for r in self.results] != self.candidate_ids:
            raise ValueError("results must follow candidate_ids order")
        if [r.rank for r in self.results] != list(range(1, len(self.results) + 1)):
            raise ValueError("ranks must be 1..top_n")
        for field in ("query_sha256", "input_sha256", "model_config_sha256"):
            if not _SHA256_RE.match(getattr(self, field)):
                raise ValueError(f"{field} must be a sha256 hex digest")
        return self


class ErrorBody(_Strict):
    code: str
    message: str
    retryable: bool = False
    stage: str | None = None
    request_id: str | None = None
    details: dict[str, Any] | None = None


class ErrorResponse(_Strict):
    contract: Literal["vkm.rerank/1"] = CONTRACT
    error: ErrorBody


class BackendStatus(_Strict):
    role: str
    kind: Literal["text", "visual"]
    status: Literal["ready", "loading", "unavailable", "mismatch"]
    model_id: str | None = None
    model_revision: str | None = None
    code_revision: str | None = None
    tokenizer_revision: str | None = None
    quant: str | None = None
    placement: str | None = None
    backend: str | None = None
    backend_version: str | None = None
    weights_sha256: str | None = None
    aux_artifacts: list[dict[str, str]] = Field(default_factory=list)
    score_semantics: str | None = None
    license: str | None = None
    limits: dict[str, Any] = Field(default_factory=dict)
    concurrency: str | None = None
    deviation_note: str | None = None
    model_config_sha256: str | None = None
    detail: str | None = None


class StatusResponse(_Strict):
    contract: Literal["vkm.rerank/1"] = CONTRACT
    service: Literal["vkm-rerank-gateway"] = "vkm-rerank-gateway"
    host_role: Literal["EDGE"] = "EDGE"
    gateway_version: str
    started_at: str
    backends: dict[str, BackendStatus]
    limits: dict[str, Any]


class HealthResponse(_Strict):
    status: Literal["ok", "degraded"]
    backends: dict[str, Literal["ready", "loading", "unavailable", "mismatch"]]


def contract_limits() -> dict[str, Any]:
    """Limits shown by ``GET /status`` (and used by clients)."""
    return {
        "max_text_candidates": MAX_TEXT_CANDIDATES, "text_token_budget": TEXT_TOKEN_BUDGET,
        "max_visual_candidates": MAX_VISUAL_CANDIDATES, "image_max_side_px": IMAGE_MAX_SIDE_PX,
        "m0_min_pixels": M0_MIN_PIXELS, "m0_max_pixels": M0_MAX_PIXELS, "m0_patch_factor": M0_PATCH_FACTOR,
        "visual_query_max_tokens": VISUAL_QUERY_MAX_TOKENS, "max_image_bytes": MAX_IMAGE_BYTES,
        "max_query_chars": MAX_QUERY_CHARS, "max_text_chars": MAX_TEXT_CHARS,
        "max_body_bytes_text": MAX_BODY_BYTES_TEXT, "max_body_bytes_visual": MAX_BODY_BYTES_VISUAL,
        "text_backend_timeout_s": TEXT_TIMEOUT_S, "visual_backend_timeout_s": VISUAL_TIMEOUT_S,
    }
