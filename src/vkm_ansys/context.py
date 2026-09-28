"""Shared context of the ``vkm-ansys`` server and the contract for optional product modules.

Optional modules (``vkm_ansys.mechanical``, ``vkm_ansys.workbench``, ``vkm_ansys.optislang``) expose::

    def register(server: mcp.server.mcpserver.MCPServer, ctx: AnsysContext) -> list[str] | None:
        @server.tool(name="mech_run_script", annotations=ctx.EXEC)
        async def mech_run_script(...) -> CallToolResult:
            \"\"\"...\"\"\"
            return await ctx.call("mech_run_script", lambda: ...)
        return ["mech_run_script"]

Rules for modules:

* import PyAnsys lazily inside the tool bodies — the server must start without the product packages; a module whose
  import fails is reported by ``ansys_status`` as ``UNAVAILABLE:<reason>`` and the server starts anyway;
* every tool returns ``await ctx.call(tool_name, fn)``: the envelope ``vkm-ansys.result/1``, error mapping and logging
  are shared; raise ``ctx.failure(code, message, **details)`` (codes: :data:`vkm_ansys.errors.ERROR_CODES`);
* annotations: ``ctx.READ_ONLY`` (discovery, reading jobs), ``ctx.WRITE`` (writes files under the sim root or the
  repository receipts), ``ctx.EXEC`` (runs product code — APDL, Python, journals: equivalent to running code);
* runs that use a licence go through the job layer (``ctx.jobs``, pool ``ansys``); paths in results are logical
  (``ctx.logical(path)``); outputs stay under ``ctx.sim_root()``;
* tool names of the core are listed in :data:`CORE_TOOLS` and may not be reused.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, ClassVar, Mapping

import anyio

from mcp.types import CallToolResult, TextContent, ToolAnnotations

from vkm_ansys import __version__
from vkm_ansys.detect import env_value, find_root
from vkm_ansys.errors import ToolFailure

if TYPE_CHECKING:
    from vkm_ansys.licence_lock import LicenceLock

SERVER_NAME = "vkm-ansys"
RESULT_SCHEMA = "vkm-ansys.result/1"
LOG = logging.getLogger("vkm.ansys.mcp")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
EXEC = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

# names owned by the core (MAPDL, DPF, ladder, discovery, jobs); optional modules must not reuse them
CORE_TOOLS = frozenset({
    "ansys_status", "ansys_license_status", "mapdl_command_help", "dpf_operators",
    "mapdl_run", "mapdl_session_start", "mapdl_session_run", "mapdl_session_stop", "mapdl_session_list",
    "ansys_run_python", "dpf_extract",
    "ansys_ladder_list", "ansys_ladder_run", "mapdl_convergence", "ansys_subsidence", "ansys_compare",
    "job_list", "job_status", "job_wait", "job_read", "job_receipt", "job_cancel", "job_publish_receipt",
})
OPTIONAL_MODULES = ("vkm_ansys.mechanical", "vkm_ansys.workbench", "vkm_ansys.optislang")
MIN_FREE_GB = 50


def repo_root() -> Path | None:
    for c in Path(__file__).resolve().parents:
        if (c / "pyproject.toml").is_file() and (c / "src" / "vkm_ansys").is_dir():
            return c
    return None


def validate_sim_root(env: Mapping[str, str]) -> tuple[Path | None, str | None]:
    """``VKM_SIM_ROOT``: absolute, ASCII, no spaces, outside the repository, PRIVATE and the canonical data root."""
    value = env_value(env, "VKM_SIM_ROOT")
    if not value:
        return None, "VKM_SIM_ROOT is not set"
    if not value.isascii() or " " in value:
        return None, "VKM_SIM_ROOT must be an ASCII path without spaces (MAPDL and product logs need it)"
    root = Path(value)
    if not root.is_absolute():
        return None, "VKM_SIM_ROOT must be absolute"
    root = root.resolve()
    repo = repo_root()
    resources = env_value(env, "VKM_RESOURCES_ROOT")
    data = env_value(env, "VKM_DATA_ROOT")
    if repo and root.is_relative_to(repo):
        return None, "VKM_SIM_ROOT lies inside the repository"
    if resources and root.is_relative_to(Path(resources).resolve()):
        return None, "VKM_SIM_ROOT lies inside VKM_RESOURCES_ROOT"
    if data and root.is_relative_to(Path(data).resolve() / "canonical"):
        return None, "VKM_SIM_ROOT lies inside the canonical data root"
    return root, None


@dataclass
class AnsysContext:
    """What the core and the product modules share: environment, roots, call wrapper, job layer, module states."""

    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    modules: dict[str, str] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)      # tool name -> owner module
    log: logging.Logger = LOG
    READ_ONLY: ClassVar[ToolAnnotations] = READ_ONLY
    WRITE: ClassVar[ToolAnnotations] = WRITE
    EXEC: ClassVar[ToolAnnotations] = EXEC
    _sim_root: tuple[Path | None, str | None] | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AnsysContext":
        return cls(env=dict(os.environ if env is None else env))

    # ------------------------------------------------------------------------------------------------ failures
    @staticmethod
    def failure(code: str, message: str, *, retryable: bool = False, **details: Any) -> ToolFailure:
        return ToolFailure(code, message, retryable=retryable, details=details)

    # ------------------------------------------------------------------------------------------------ roots
    def sim_root_status(self) -> dict[str, Any]:
        root, reason = self._sim_root or validate_sim_root(self.env)
        self._sim_root = (root, reason)
        if root is None:
            return {"available": False, "reason": reason}
        out: dict[str, Any] = {"available": True, "path": "<VKM_SIM_ROOT>", "exists": root.is_dir()}
        probe = root if root.is_dir() else root.anchor
        try:
            free_gb = shutil.disk_usage(probe).free / 1e9
            out["free_gb"] = round(free_gb, 1)
            if free_gb < MIN_FREE_GB:
                out["warning"] = f"less than {MIN_FREE_GB} GB free on the drive of VKM_SIM_ROOT"
        except OSError:
            pass
        return out

    def sim_root(self) -> Path:
        root, reason = self._sim_root or validate_sim_root(self.env)
        self._sim_root = (root, reason)
        if root is None:
            raise ToolFailure("SIM_ROOT_UNAVAILABLE", f"simulation root unavailable: {reason}")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def ansys_root(self) -> tuple[Path, str | None]:
        root, ver, how = find_root(self.env)
        if root is None or not root.is_dir():
            raise ToolFailure("APP_UNAVAILABLE", f"Ansys installation not found: {how}")
        return root, ver

    def logical(self, path: str | Path) -> str:
        """Replace the sim root, the Ansys root and the repository with logical names."""
        text = str(path)
        pairs: list[tuple[str, str]] = []
        root, _reason = self._sim_root or validate_sim_root(self.env)
        if root is not None:
            pairs.append((str(root), "<VKM_SIM_ROOT>"))
        ansys, _ver, _how = find_root(self.env)
        if ansys is not None:
            pairs.append((str(ansys), "<ANSYS_ROOT>"))
        repo = repo_root()
        if repo is not None:
            pairs.append((str(repo), "<PUBLIC>"))
        for old, new in sorted(pairs, key=lambda p: -len(p[0])):
            for variant in {old, old.replace("\\", "/")}:
                if variant and variant in text:
                    text = text.replace(variant, new)
        return text.replace("\\", "/") if any(n in text for _o, n in pairs) else text

    # ------------------------------------------------------------------------------------------------ licence pool
    def licence_lock(self, pool: str = "ansys", *, label: str = "", job_id: str | None = None,
                     session_id: str | None = None) -> "LicenceLock":
        """The machine-wide licence lock shared by every VKM process (see :mod:`vkm_ansys.licence_lock`). Hold it
        from before a product process starts until it has exited: ``with ctx.licence_lock(label=...).hold(t): ...``"""
        from vkm_ansys.licence_lock import LicenceLock
        return LicenceLock(pool, label=label, job_id=job_id, session_id=session_id, env=self.env)

    # ------------------------------------------------------------------------------------------------ job layer
    @property
    def jobs(self) -> Any:
        """The shared job layer (``vkm_jobs``): submit, status, wait, receipts, licence pools."""
        try:
            import vkm_jobs  # noqa: F401
        except ImportError as exc:
            raise ToolFailure("JOB_LAYER_UNAVAILABLE", "the shared job layer vkm_jobs is not importable",
                              details={"reason": str(exc)}) from exc
        return vkm_jobs

    # ------------------------------------------------------------------------------------------------ envelope
    @staticmethod
    def payload(tool: str, ok: bool, result: Any = None, error: dict[str, Any] | None = None,
                request_id: str | None = None) -> dict[str, Any]:
        return {"schema": RESULT_SCHEMA, "ok": ok, "server": SERVER_NAME, "server_version": __version__,
                "tool": tool, "request_id": request_id, "result": result, "error": error}

    @staticmethod
    def as_result(payload: dict[str, Any]) -> CallToolResult:
        text = TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        return CallToolResult(content=[text], structured_content=json.loads(text.text), is_error=not payload["ok"])

    async def call(self, tool: str, fn: Callable[[], Any]) -> CallToolResult:
        """Run a synchronous ``fn`` in a worker thread and wrap its value (or failure) in the envelope."""
        return await self.call_async(tool, lambda: anyio.to_thread.run_sync(fn))

    async def call_async(self, tool: str, fn: Callable[[], Awaitable[Any]]) -> CallToolResult:
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        status, code = "ok", None
        try:
            return self.as_result(self.payload(tool, True, await fn(), request_id=request_id))
        except Exception as exc:  # noqa: BLE001 - failures become envelopes; unknown ones are INTERNAL
            as_dict = getattr(exc, "as_dict", None)
            if isinstance(getattr(exc, "code", None), str) and callable(as_dict):
                status, code = "error", exc.code
                return self.as_result(self.payload(tool, False, error=as_dict(), request_id=request_id))
            status, code = "error", "INTERNAL"
            self.log.exception("tool failed", extra={"vkm": {"stage": tool, "request_id": request_id}})
            return self.as_result(self.payload(tool, False, error={
                "code": "INTERNAL", "message": "internal error; see the server log", "retryable": False,
                "details": {"log_ref": request_id, "type": type(exc).__name__}}, request_id=request_id))
        finally:
            self.log.info("tool call", extra={"vkm": {"stage": tool, "status": status, "error_code": code,
                                                      "request_id": request_id,
                                                      "duration_ms": round((time.perf_counter() - started) * 1000,
                                                                           1)}})
