"""API error codes ↔ HTTP status ↔ retryable (one table; every error body is ``ApiResponse(ok=false, error=…)``).

There is no 410: an object absent from the current snapshot is 404 with a hint (H-38). There is no fallback from a
missing canonical store to projection text: that is 503 ``SNAPSHOT_UNAVAILABLE``.
"""
from __future__ import annotations

from typing import Any

HTTP_STATUS: dict[str, int] = {
    "INVALID_ARGUMENT": 400,          # request does not match the schema
    "INVALID_ID": 400,                # id does not match the grammar of vkm_corpus.ids
    "UNAUTHORIZED": 401,              # missing or wrong bearer token
    "FORBIDDEN": 403,                 # read token on a write endpoint
    "NOT_FOUND": 404,                 # valid id, not in the current snapshot (hint says where to look)
    "NOT_REPROCESSABLE": 409,         # e.g. a source absent or retired by the register
    "PLAN_NOT_READY": 409,            # confirmation before the worker published a plan
    "PLAN_CHANGED": 409,              # confirmed hash is not the stored plan (re-read the job)
    "JOB_STATE_CONFLICT": 409,        # the job's state does not allow this (e.g. cancel a running job)
    "ARTIFACT_NOT_MATERIALIZED": 409, # reproducible artifact without stored bytes
    "PAYLOAD_TOO_LARGE": 413,         # rerank limits (H-13), artifact size
    "NO_IMAGE_ARTIFACT": 422,         # visual rerank: no candidate has an image
    "NO_RERANK_TEXT": 422,            # text rerank: no candidate has canonical text
    "RATE_LIMITED": 429,              # too many active reprocess jobs
    "INTERNAL": 500,
    "ARTIFACT_HASH_MISMATCH": 500,    # stored bytes differ from their content address
    "ARTIFACT_NOT_DECODABLE": 500,    # stored bytes are not an image of their media type (a producer defect)
    "DEPENDENCY_ERROR": 502,          # a dependency answered with an error
    "SNAPSHOT_UNAVAILABLE": 503,      # DuckDB/canonical root missing or not CANONICAL
    "DEPENDENCY_UNAVAILABLE": 503,    # OpenSearch / Neo4j / rerank gateway / PostgreSQL not reachable or not ready
    "DEPENDENCY_TIMEOUT": 504,
}
RETRYABLE = frozenset({"RATE_LIMITED", "INTERNAL", "DEPENDENCY_ERROR", "SNAPSHOT_UNAVAILABLE",
                       "DEPENDENCY_UNAVAILABLE", "DEPENDENCY_TIMEOUT", "ARTIFACT_NOT_MATERIALIZED"})


class ApiFailure(Exception):
    def __init__(self, code: str, message: str, *, stage: str | None = None, tool: str | None = None,
                 hint: str | None = None, object_id: str | None = None, source_id: str | None = None,
                 page_id: str | None = None, details: dict[str, Any] | None = None,
                 retryable: bool | None = None) -> None:
        if code not in HTTP_STATUS:
            raise ValueError(f"unknown API error code {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.tool = tool
        self.hint = hint
        self.object_id = object_id
        self.source_id = source_id
        self.page_id = page_id
        self.details = details or {}
        self.retryable = code in RETRYABLE if retryable is None else retryable

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]

    def body(self, log_ref: str | None) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "stage": self.stage,
                "tool": self.tool, "object_id": self.object_id, "source_id": self.source_id,
                "page_id": self.page_id, "log_ref": log_ref, "hint": self.hint, "details": self.details}
