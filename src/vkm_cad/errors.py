"""Machine-readable failures of ``vkm-cad`` (``is_error`` results with ``{code, message, retryable, details}``)."""
from __future__ import annotations

from typing import Any

ERROR_CODES = frozenset({
    "CAD_UNAVAILABLE",          # AutoCAD not installed, not Windows, or pywin32 missing
    "CAD_NOT_RUNNING",          # installed, but no running AutoCAD to attach to (the bridge never starts it)
    "CAD_BUSY",                 # AutoCAD rejected COM calls (modal dialog, command in progress) after retries
    "CAD_TIMEOUT",              # a COM call did not return in time
    "CAD_COM_ERROR",            # other COM failure
    "CAD_POLICY_VIOLATION",     # a COM member outside the read allow-list was requested
    "CAD_DOCUMENT_NOT_FOUND",   # doc_ref does not name an open document
    "EZDXF_UNAVAILABLE",        # scratch operations need ezdxf (extra 'desktop')
    "SCRATCH_UNAVAILABLE",      # scratch root not configured or not allowed
    "SCRATCH_DOC_NOT_FOUND",
    "NO_VECTOR_ARTIFACT",       # vector artifact not found in any configured location
    "VECTOR_FORMAT_UNSUPPORTED",
    "ARTIFACT_HASH_MISMATCH",
    "SOURCE_CHANGED_DURING_COPY",
    "FORMAT_NOT_SUPPORTED",
    "CRS_STATUS_NOT_ALLOWED",   # only UNKNOWN_CRS, or SCHEMATIC with a rationale
    "WOULD_OVERWRITE",
    "INVALID_ARGUMENT",
    "PAYLOAD_TOO_LARGE",
    "DXF_READ_ERROR",
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
