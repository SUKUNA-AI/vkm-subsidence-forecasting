"""Ansys Workbench in ``vkm-ansys`` (optional module, agent ANS2): ``register(server, ctx)`` adds the tools.

Universal channel ``workbench_run_journal`` (any Workbench scripting journal, IronPython, in ``RunWB2 -B``), project
helpers (summary, update, archive) and discovery (``workbench_status``). Runs are jobs of the shared job layer (pool
``ansys``). PyWorkbench's server mode is not used for jobs: it launches Workbench through WMI outside the job's
process tree (no tree kill on timeout) — batch journals give the same scripting API with a clean process life cycle.
"""
from __future__ import annotations

from typing import Annotated, Any

from mcp.types import CallToolResult
from pydantic import Field

from vkm_ansys import product_jobs as pj
from vkm_ansys.product_schema import (Checks, DryRun, Label, LicenseProbe, ModelChoices, Params, Ref, RefList,
                                      ScriptText, WaitS, dump)

TOOLS = ("workbench_status", "workbench_run_journal", "workbench_project_summary", "workbench_project_update",
         "workbench_project_archive")
SaveWbpj = Annotated[str | None, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.wbpj$",
                                       description="save the project to out/<name> (e.g. project.wbpj)")]
SaveWbpz = Annotated[str | None, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.wbpz$",
                                       description="archive the project to out/<name> (e.g. project.wbpz)")]
Timeout = Annotated[int, Field(ge=1, le=86400, description="seconds; the job is killed after this")]
ProjectRef = Annotated[str, Field(min_length=1, max_length=1000,
                                  description=".wbpj (its _files folder goes along) or .wbpz archive: absolute path "
                                              "or job:<job_id>/<path>")]


def _job_tool(ctx: Any, build, wait_s: int, dry_run: bool):
    job = build()
    if dry_run:
        return {"dry_run": True, "job": job.describe()}
    return pj.submit(ctx, job, wait_s)


def register(server: Any, ctx: Any) -> list[str]:
    from vkm_ansys.workbench.service import WorkbenchService

    svc = WorkbenchService(ctx)

    @server.tool(name="workbench_status", annotations=ctx.READ_ONLY)
    async def workbench_status() -> CallToolResult:
        """Workbench is not started: RunWB2 executable and framework build, PyWorkbench version, channels, running
        Workbench processes with listening ports, and licence features of typical Workbench systems (geometry,
        Mechanical) reported by the licence server (read-only lmstat: names and seat counts)."""
        return await ctx.call("workbench_status", svc.status)

    @server.tool(name="workbench_run_journal", annotations=ctx.EXEC)
    async def workbench_run_journal(journal: ScriptText = None, journal_path: Ref | None = None,
                                    project: Annotated[str | None, Field(max_length=1000)] = None,
                                    inputs: RefList = None, save_as: SaveWbpj = None, archive_as: SaveWbpz = None,
                                    archive_include_results: bool = True, params: Params = None,
                                    model_choices: ModelChoices = None, checks: Checks = None,
                                    timeout_s: Timeout = 3600, wait_s: WaitS = 0, label: Label = None,
                                    license_probe: LicenseProbe = False, dry_run: DryRun = False
                                    ) -> CallToolResult:
        """Universal channel: run any Workbench scripting journal (IronPython 2.7) in batch Workbench as a job.
        Globals: the Workbench scripting API (GetTemplate, CreateSystemFromTemplate, GetAllSystems, GetSystem, Open,
        Save, Update, Archive, Unarchive, Parameters, …) plus JOB_DIR, IN_DIR, WORK_DIR, OUT_DIR, REQUEST and a dict
        `result` → out/result.json. project (.wbpj or .wbpz) is opened first; inputs are copied to in/inputs/;
        save_as / archive_as write into out/. Returns a JobRef (follow with job_wait / job_read). Executes code."""
        return await ctx.call("workbench_run_journal", lambda: _job_tool(ctx, lambda: svc.build_run_journal(
            journal=journal, journal_path=journal_path, project=project, inputs=inputs, save_as=save_as,
            archive_as=archive_as, archive_include_results=archive_include_results, params=dump(params),
            model_choices=model_choices, checks=dump(checks), timeout_s=timeout_s, label=label,
            license_probe=license_probe), wait_s, dry_run))

    @server.tool(name="workbench_project_summary", annotations=ctx.EXEC)
    async def workbench_project_summary(project: ProjectRef, timeout_s: Timeout = 900, wait_s: WaitS = 0,
                                        label: Label = None, dry_run: DryRun = False) -> CallToolResult:
        """Open a project in batch Workbench without changing it and list systems with their components and states,
        parameters (usage, value) and design points; framework version. Job (pool ansys)."""
        return await ctx.call("workbench_project_summary", lambda: _job_tool(ctx, lambda: svc.build_summary(
            project=project, timeout_s=timeout_s, label=label), wait_s, dry_run))

    @server.tool(name="workbench_project_update", annotations=ctx.EXEC)
    async def workbench_project_update(project: ProjectRef,
                                       systems: Annotated[list[Annotated[str, Field(max_length=200)]] | None,
                                                          Field(max_length=50, description="system names (e.g. "
                                                                "SYS); empty = Update() of the whole project")] = None,
                                       save_as: SaveWbpj = "project.wbpj", archive_as: SaveWbpz = None,
                                       timeout_s: Timeout = 3600, wait_s: WaitS = 0, label: Label = None,
                                       license_probe: LicenseProbe = False, dry_run: DryRun = False
                                       ) -> CallToolResult:
        """Update a project (all out-of-date cells, or the listed systems with their dependencies), save it to
        out/<save_as> (optionally archive it) and report the states after the update. Solver results are
        MODEL_RESULT. Job (pool ansys)."""
        return await ctx.call("workbench_project_update", lambda: _job_tool(ctx, lambda: svc.build_update(
            project=project, systems=systems, save_as=save_as, archive_as=archive_as, timeout_s=timeout_s,
            label=label, license_probe=license_probe), wait_s, dry_run))

    @server.tool(name="workbench_project_archive", annotations=ctx.EXEC)
    async def workbench_project_archive(project: ProjectRef, archive_as: SaveWbpz = "project.wbpz",
                                        include_results: bool = True, timeout_s: Timeout = 1800,
                                        wait_s: WaitS = 0, label: Label = None, dry_run: DryRun = False
                                        ) -> CallToolResult:
        """Archive a project into one .wbpz in out/ (optionally without solution/result files). Job (pool ansys)."""
        return await ctx.call("workbench_project_archive", lambda: _job_tool(ctx, lambda: svc.build_archive(
            project=project, archive_as=archive_as, include_results=include_results, timeout_s=timeout_s,
            label=label), wait_s, dry_run))

    return list(TOOLS)
