"""Machine-readable failures of ``vkm-ansys`` (``is_error`` results with ``{code, message, retryable, details}``).

The codes are the shared set of the engineering servers (plan §2.4) plus a few Ansys-specific ones. Product modules
raise :class:`ToolFailure` (or ``ctx.failure(...)``); any exception object with ``code`` and ``as_dict()`` — e.g. the
job layer's own failure type — is reported the same way by :meth:`vkm_ansys.context.AnsysContext.call`.
"""
from __future__ import annotations

from typing import Any

ERROR_CODES = frozenset({
    # shared (plan §2.4)
    "INVALID_ARGUMENT",
    "NOT_FOUND",
    "APP_UNAVAILABLE",          # product not installed, Python package missing, or not Windows
    "LICENSE_UNAVAILABLE",      # licence checkout refused (detected in .out/.err or by the product API)
    "POOL_BUSY",                # the licence pool is held and the caller asked not to queue
    "TIMEOUT",
    "SESSION_NOT_FOUND",
    "SESSION_DEAD",
    "PATH_OUTSIDE_ROOT",
    "WOULD_OVERWRITE",
    "GATE_CLOSED",              # an action behind a project gate (CLAUDE.md), e.g. Monte Carlo without a task
    "INTERNAL",
    # vkm-ansys
    "SIM_ROOT_UNAVAILABLE",     # VKM_SIM_ROOT unset or rejected (not ASCII, spaces, inside a repository, ...)
    "JOB_LAYER_UNAVAILABLE",    # the shared job layer vkm_jobs is not importable
    "JOB_NOT_FINISHED",         # a result was requested from a job that is still queued or running
    "RESULT_NOT_FOUND",         # the job has no result file of the requested kind
    "PAYLOAD_TOO_LARGE",
    "NOT_LOOPBACK",             # a product server listens on a non-loopback address (session refused and stopped)
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
