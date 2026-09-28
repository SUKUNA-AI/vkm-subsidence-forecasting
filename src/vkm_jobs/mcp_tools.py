"""MCP layer shared by the application servers: the result envelope and the seven job tools (plan §2.4).

:class:`Envelope` turns a plain function into a tool result ``{"schema": "vkm-<server>.result/1", ok, server,
server_version, tool, request_id, result, error}`` (``structured_content`` plus the same JSON as text, ``is_error =
not ok``), logs one line per call and maps :class:`~vkm_jobs.errors.ToolFailure` to ``error = {code, message,
retryable, details}``. :func:`register_job_tools` adds ``job_list``, ``job_status``, ``job_wait`` (progress
notifications every few seconds), ``job_read``, ``job_receipt``, ``job_cancel`` and ``job_publish_receipt`` to an
``MCPServer``.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Annotated, Any, Awaitable, Callable, Literal

import anyio
from pydantic import Field

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from vkm_jobs.errors import ToolFailure
from vkm_jobs.service import MAX_READ_CHARS, JobsService
from vkm_jobs.spec import JOB_ID_PATTERN, STATUSES, TERMINAL

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
EXEC = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
CANCEL = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)
MAX_WAIT_S = 1200
PROGRESS_EVERY_S = 5.0

JobIdArg = Annotated[str, Field(pattern=JOB_ID_PATTERN, description="job id <APP>-YYYYMMDDTHHMMSSZ-xxxxxxxx")]
AppFilter = Literal["MATLAB", "MAPDL", "MECH", "WB", "DPF", "OSL", "PY"]
StatusFilter = Literal[STATUSES]  # type: ignore[valid-type]
Limit = Annotated[int, Field(ge=1, le=200)]
WaitTimeout = Annotated[int, Field(ge=0, le=MAX_WAIT_S, description="seconds to wait (≤ 1200)")]
RelPath = Annotated[str, Field(min_length=1, max_length=400,
                               description="path relative to the job directory, e.g. logs/stdout.log or out/result.json")]
Offset = Annotated[int, Field(ge=0, description="byte offset (use next_offset of the previous call)")]
MaxChars = Annotated[int, Field(ge=1, le=MAX_READ_CHARS)]
Grep = Annotated[str | None, Field(max_length=500, description="regular expression; returns matching lines only")]
Reason = Annotated[str, Field(min_length=10, max_length=500)]
PublishName = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,80}$", description="file name without .json")]


class Envelope:
    """Result envelope and call logging of one server."""

    def __init__(self, server: str, version: str, logger: logging.Logger | None = None) -> None:
        self.server = server
        self.version = version
        self.schema = f"{server}.result/1"
        self.log = logger or logging.getLogger(f"vkm.{server}.mcp")

    def payload(self, tool: str, ok: bool, result: Any = None, error: dict[str, Any] | None = None,
                request_id: str | None = None) -> dict[str, Any]:
        return {"schema": self.schema, "ok": ok, "server": self.server, "server_version": self.version, "tool": tool,
                "request_id": request_id, "result": result, "error": error}

    @staticmethod
    def as_result(payload: dict[str, Any]) -> CallToolResult:
        text = TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        return CallToolResult(content=[text], structured_content=payload, is_error=not payload["ok"])

    async def call(self, tool: str, fn: Callable[[], Any]) -> CallToolResult:
        """Run a blocking ``fn`` in a worker thread."""
        return await self.call_async(tool, lambda: anyio.to_thread.run_sync(fn))

    async def call_async(self, tool: str, fn: Callable[[], Awaitable[Any]]) -> CallToolResult:
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        status, code = "ok", None
        try:
            return self.as_result(self.payload(tool, True, await fn(), request_id=request_id))
        except ToolFailure as exc:
            status, code = "error", exc.code
            return self.as_result(self.payload(tool, False, error=exc.as_dict(), request_id=request_id))
        except Exception:  # noqa: BLE001 - reported as INTERNAL with a log reference
            status, code = "error", "INTERNAL"
            self.log.exception("tool failed", extra={"vkm": {"stage": tool, "request_id": request_id}})
            return self.as_result(self.payload(tool, False, error={
                "code": "INTERNAL", "message": "internal error; see the server log", "retryable": False,
                "details": {"log_ref": request_id}}, request_id=request_id))
        finally:
            self.log.info("tool call", extra={"vkm": {"stage": tool, "status": status, "error_code": code,
                                                      "request_id": request_id,
                                                      "duration_ms": round((time.perf_counter() - started) * 1000,
                                                                           1)}})


async def wait_with_progress(jobs: JobsService, job_id: str, timeout_s: float, ctx: Context | None) -> dict[str, Any]:
    """Poll the job until it ends or ``timeout_s`` passes; progress notifications every few seconds."""
    start = time.monotonic()
    last = -PROGRESS_EVERY_S
    while True:
        st = await anyio.to_thread.run_sync(lambda: jobs.status(job_id))
        elapsed = time.monotonic() - start
        if st.get("status") in TERMINAL or elapsed >= timeout_s:
            return await anyio.to_thread.run_sync(lambda: jobs.ref(job_id, st))
        if ctx is not None and elapsed - last >= PROGRESS_EVERY_S:
            last = elapsed
            note = st.get("status")
            if st.get("queue_position") is not None:
                note += f", queue position {st['queue_position']}"
            prog = st.get("progress") or {}
            if prog.get("elapsed_s") is not None:
                note += f", running {prog['elapsed_s']:.0f} s"
            try:
                await ctx.report_progress(round(elapsed, 1), float(timeout_s), f"{job_id}: {note}")
            except Exception:  # noqa: BLE001 - progress is best effort
                pass
        await anyio.sleep(min(0.5, max(0.05, timeout_s - elapsed)))


def register_job_tools(server: MCPServer, jobs: JobsService, env: Envelope, *, app: str | None = None) -> None:
    """Add the seven shared job tools to ``server`` (``app`` is kept for future per-server filters)."""

    @server.tool(name="job_list", annotations=READ_ONLY)
    async def job_list(app: AppFilter | None = None, status: StatusFilter | None = None, limit: Limit = 50,
                       cursor: Annotated[str | None, Field(pattern=JOB_ID_PATTERN)] = None) -> CallToolResult:
        """Jobs in the job root, newest first: id, status, app, kind, label, pool, times, exit code. Filter by app and
        status; page with cursor (next_cursor of the previous call)."""
        return await env.call("job_list", lambda: jobs.list(app, status, limit, cursor))

    @server.tool(name="job_status", annotations=READ_ONLY)
    async def job_status(job_id: JobIdArg) -> CallToolResult:
        """Status of one job: QUEUED (with queue position) / RUNNING (elapsed time, progress) / SUCCEEDED / FAILED /
        CHECK_FAILED / TIMED_OUT / CANCELLED / LICENSE_UNAVAILABLE / LOST, exit code, times; the compact receipt
        when finished."""
        return await env.call("job_status", lambda: jobs.ref(job_id))

    @server.tool(name="job_wait", annotations=READ_ONLY)
    async def job_wait(job_id: JobIdArg, timeout_s: WaitTimeout = 600, ctx: Context = None) -> CallToolResult:
        """Wait until the job ends or timeout_s passes (≤ 1200 s), sending progress notifications; returns the final
        state with the compact receipt, or the current state (still QUEUED/RUNNING) — then call again."""
        return await env.call_async("job_wait", lambda: wait_with_progress(jobs, job_id, timeout_s, ctx))

    @server.tool(name="job_read", annotations=READ_ONLY)
    async def job_read(job_id: JobIdArg, path: RelPath, offset: Offset = 0, max_chars: MaxChars = 20000,
                       grep: Grep = None) -> CallToolResult:
        """Read a text file of a job (logs/stdout.log, logs/matlab.log, out/result.json, …) from a byte offset, at most
        max_chars; with grep only the matching lines. Paths stay inside the job directory (PATH_OUTSIDE_ROOT
        otherwise); machine paths in the text are shown as logical names."""
        return await env.call("job_read", lambda: jobs.read(job_id, path, offset, max_chars, grep))

    @server.tool(name="job_receipt", annotations=READ_ONLY)
    async def job_receipt(job_id: JobIdArg) -> CallToolResult:
        """Full receipt vkm.sim_receipt/1: command (logical paths), app version, git commit, times, exit code,
        status, log summary, inputs and outputs with SHA-256, checks, params with epistemic status, model choices,
        result_status MODEL_RESULT (a computed result, never an observation)."""
        return await env.call("job_receipt", lambda: jobs.receipt(job_id))

    @server.tool(name="job_cancel", annotations=CANCEL)
    async def job_cancel(job_id: JobIdArg, reason: Reason) -> CallToolResult:
        """Cancel a queued or running job (reason ≥ 10 characters): the runner kills the whole process tree (Windows
        Job Object) and writes the receipt with status CANCELLED. A finished job is reported as is
        (already_terminal)."""
        return await env.call("job_cancel", lambda: jobs.cancel(job_id, reason))

    @server.tool(name="job_publish_receipt", annotations=WRITE)
    async def job_publish_receipt(job_id: JobIdArg, name: PublishName, overwrite: bool = False) -> CallToolResult:
        """Copy the job's receipt into the PUBLIC repository (docs/engineering_tools/receipts/<name>.json) after
        sanitising paths and passing the hygiene checks (no drive letters, host names, private IPs, secrets) and the
        leakage scan. Refuses to overwrite without overwrite=true."""
        return await env.call("job_publish_receipt", lambda: jobs.publish_receipt(job_id, name, overwrite))
