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
    # v1: jobs, the headless engine (accoreconsole), the .NET host, the hidden instance and the pure-Python fallback
    "JOBS_UNAVAILABLE",         # jobs root not configured or not allowed
    "CAD_JOB_NOT_FOUND",
    "CAD_ENGINE_UNAVAILABLE",   # accoreconsole (headless engine) not found, or not Windows
    "CAD_ENGINE_BUSY",          # another bridge job holds the engine (one licensed AutoCAD process at a time)
    "CAD_RUN_TIMEOUT",          # the AutoCAD process did not finish in time and was killed (process tree)
    "CAD_ENGINE_CRASHED",       # abnormal exit / crash report (CER): the job's processes are killed, nothing promoted
    "CAD_DIALOG_BLOCKED",       # a window (dialog) of the job's processes appeared: killed, headless runs have no UI
    "CAD_SCRIPT_NOT_READ",      # the engine never echoed the job script: nothing ran (path, encoding)
    "CAD_SCRIPT_FAILED",        # the run did not reach its end marker or a step failed: the drawing is not promoted
    "CIVIL3D_UNAVAILABLE",      # Civil 3D objects need a C3D job and a Civil 3D install (or engine=fallback)
    "DOTNET_UNAVAILABLE",       # no Roslyn compiler, no .NET runtime reference set or no AutoCAD managed API
    "DOTNET_COMPILE_FAILED",
    "HOST_OP_FAILED",           # an operation of the .NET host reported an error
    "HIDDEN_INSTANCE_NOT_ALLOWED",  # python_com needs VKM_CAD_ALLOW_HIDDEN_INSTANCE=1 (it changes the user profile)
    "USER_SESSION_RUNNING",     # a user AutoCAD is running: the hidden instance is refused
    "FALLBACK_UNAVAILABLE",     # scipy missing for the pure-Python fallback
    "INPUT_NOT_FOUND",
    "TABLE_FORMAT_ERROR",
    "SURFACE_NOT_FOUND",
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
