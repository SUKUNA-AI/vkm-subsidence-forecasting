"""Ansys optiSLang in ``vkm-ansys`` (optional module, agent ANS2): ``register(server, ctx)`` adds the tools.

Universal channel ``optislang_run`` (optiSLang's own Python API script that builds a workflow, or an existing project,
run in batch through PyOptiSLang), ``optislang_project_summary``, result reading without a licence
(``optislang_results``) and discovery (``optislang_status``, ``optislang_node_types``). Gate (CLAUDE.md): sensitivity,
robustness and Monte Carlo only by explicit task — runs need ``authorized_by``.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import Field

from vkm_ansys import product_jobs as pj
from vkm_ansys.product_schema import (Checks, DryRun, Label, LicenseProbe, ModelChoices, Params, Ref, ScriptText,
                                      WaitS, dump)

TOOLS = ("optislang_status", "optislang_node_types", "optislang_run", "optislang_project_summary",
         "optislang_results")
SaveOpf = Annotated[str | None, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.opf$",
                                      description="save a copy of the project to out/<name> (e.g. project.opf)")]
Timeout = Annotated[int, Field(ge=1, le=259200, description="seconds; the job is killed after this")]
JobId = Annotated[str, Field(pattern=pj.JOB_ID)]


def _job_tool(ctx: Any, build, wait_s: int, dry_run: bool):
    job = build()
    if dry_run:
        return {"dry_run": True, "job": job.describe()}
    return pj.submit(ctx, job, wait_s)


def register(server: Any, ctx: Any) -> list[str]:
    from vkm_ansys.optislang.service import OptislangService

    svc = OptislangService(ctx)

    @server.tool(name="optislang_status", annotations=ctx.READ_ONLY)
    async def optislang_status() -> CallToolResult:
        """optiSLang is not started: executable and build, PyOptiSLang version, running optiSLang processes with
        listening ports, and the optiSLang licence features reported by the licence server (read-only lmstat)."""
        return await ctx.call("optislang_status", svc.status)

    @server.tool(name="optislang_node_types", annotations=ctx.READ_ONLY)
    async def optislang_node_types(filter: Annotated[str | None, Field(max_length=100)] = None,
                                   limit: Annotated[int, Field(ge=1, le=500)] = 200) -> CallToolResult:
        """Node types PyOptiSLang knows (algorithms such as Sensitivity, ARSM, MOP; integrations such as Python2,
        Mechanical, MAPDL …): name, id, subtype, class. Static list; optiSLang is not started."""
        return await ctx.call("optislang_node_types", lambda: svc.node_types(filter, limit))

    @server.tool(name="optislang_run", annotations=ctx.EXEC)
    async def optislang_run(authorized_by: Annotated[str | None, Field(
                                max_length=300, description="the explicit task that allows this run (≥ 10 chars; "
                                                            "CLAUDE.md gate)")] = None,
                            mode: Literal["script", "run_project"] = "script", script: ScriptText = None,
                            script_path: Ref | None = None, project: Ref | None = None, run: bool = True,
                            export_designs: bool = True, save_as: SaveOpf = "project.opf", params: Params = None,
                            model_choices: ModelChoices = None, checks: Checks = None, timeout_s: Timeout = 86400,
                            wait_s: WaitS = 0, label: Label = None, license_probe: LicenseProbe = False,
                            dry_run: DryRun = False) -> CallToolResult:
        """Universal channel: optiSLang in batch through PyOptiSLang as a job. mode='script': a new project (or
        `project`) plus an optiSLang Python API script (actors, add_actor, connect, parameter managers …) that builds
        the workflow, then the project runs when run=true; mode='run_project': run an existing .opf. Afterwards every
        parametric system's design table is exported to out/designs/ with a linear fit per response, and the
        project copy is saved to out/<save_as>. Runs need authorized_by (gate). Returns a JobRef. Executes code."""
        return await ctx.call("optislang_run", lambda: _job_tool(ctx, lambda: svc.build_run(
            mode=mode, script=script, script_path=script_path, project=project, run=run,
            authorized_by=authorized_by, export_designs=export_designs, save_as=save_as, params=dump(params),
            model_choices=model_choices, checks=dump(checks), timeout_s=timeout_s, label=label,
            license_probe=license_probe), wait_s, dry_run))

    @server.tool(name="optislang_project_summary", annotations=ctx.EXEC)
    async def optislang_project_summary(project: Ref, timeout_s: Timeout = 900, wait_s: WaitS = 0,
                                        label: Label = None, dry_run: DryRun = False) -> CallToolResult:
        """Open an .opf without running it: node tree (types, statuses), parametric systems with parameters,
        responses, criteria and the design tables already in the project. Job (pool ansys)."""
        return await ctx.call("optislang_project_summary", lambda: _job_tool(ctx, lambda: svc.build_summary(
            project=project, timeout_s=timeout_s, label=label), wait_s, dry_run))

    @server.tool(name="optislang_results", annotations=ctx.READ_ONLY)
    async def optislang_results(job_id: JobId, system: Annotated[str | None, Field(max_length=200)] = None,
                                state: Annotated[str | None, Field(max_length=50)] = None,
                                max_rows: Annotated[int, Field(ge=1, le=2000)] = 200) -> CallToolResult:
        """Read the result of a finished optislang_run / optislang_project_summary job: project status, systems,
        states, design rows (parameters, responses, status) and the linear fit per response. Nothing is started."""
        return await ctx.call("optislang_results", lambda: svc.results(job_id, system, state, max_rows))

    return list(TOOLS)
