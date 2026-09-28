"""Machine-readable tool failures of ``vkm-drawio`` (returned as ``is_error`` results, never as stack traces).

Codes: ``DRAWIO_UNAVAILABLE``, ``DRAWIO_EXPORT_FAILED``, ``DRAWIO_TIMEOUT``, ``DIAGRAM_PARSE_ERROR``,
``DIAGRAM_CONFLICT`` (``expected_sha256`` differs: the file changed, e.g. in the GUI), ``INVALID_SPEC``,
``INVALID_ARGUMENT``, ``INVALID_DIAGRAM_OP``, ``PATH_OUTSIDE_WORKSPACE``, ``ROOT_UNAVAILABLE``, ``NOT_FOUND``,
``WOULD_OVERWRITE``, ``FORMAT_NOT_ALLOWED_IN_ROOT``, ``FORMAT_NOT_SUPPORTED``, ``LEAKAGE_POLICY_VIOLATION``,
``INTERNAL``.
"""
from __future__ import annotations

from typing import Any

ERROR_CODES = frozenset({
    "DRAWIO_UNAVAILABLE", "DRAWIO_EXPORT_FAILED", "DRAWIO_TIMEOUT", "DIAGRAM_PARSE_ERROR", "DIAGRAM_CONFLICT",
    "INVALID_SPEC", "INVALID_ARGUMENT", "INVALID_DIAGRAM_OP", "PATH_OUTSIDE_WORKSPACE", "ROOT_UNAVAILABLE",
    "NOT_FOUND", "WOULD_OVERWRITE", "FORMAT_NOT_ALLOWED_IN_ROOT", "FORMAT_NOT_SUPPORTED", "LEAKAGE_POLICY_VIOLATION",
    "INTERNAL",
})


class ToolFailure(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}
