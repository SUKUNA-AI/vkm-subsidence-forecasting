"""Machine-readable failures of the job layer and of the servers built on it (``vkm-matlab``, ``vkm-ansys``).

A failure travels to the MCP client as ``error = {code, message, retryable, details}`` inside the result envelope
(``vkm_jobs.mcp_tools.Envelope``). Codes follow the engineering-tools plan §2.4; ``SIM_ROOT_UNAVAILABLE`` and
``PUBLISH_BLOCKED`` are additions of the implementation.
"""
from __future__ import annotations

from typing import Any

ERROR_CODES = frozenset({
    "INVALID_ARGUMENT",        # argument outside the contract (validated before a job is created)
    "NOT_FOUND",               # job, file or cached object does not exist
    "APP_UNAVAILABLE",         # application not installed / not found / not runnable on this host
    "LICENSE_UNAVAILABLE",     # application reported a licence failure
    "POOL_BUSY",               # licence pool busy and the caller asked not to queue
    "TIMEOUT",                 # a synchronous wait inside a tool ran out
    "SESSION_NOT_FOUND",
    "SESSION_DEAD",
    "PATH_OUTSIDE_ROOT",       # a path escapes its root ('..', absolute, drive, UNC, link)
    "WOULD_OVERWRITE",
    "GATE_CLOSED",             # project gate (e.g. ML before the pre-registered benchmark, D-19)
    "SIM_ROOT_UNAVAILABLE",    # VKM_SIM_ROOT unset or not acceptable (ASCII, local disk, outside the clones)
    "PUBLISH_BLOCKED",         # a public receipt failed the leakage / hygiene checks
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
