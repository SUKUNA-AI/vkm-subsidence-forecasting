"""Ansys Mechanical in ``vkm-ansys`` (optional module, agent ANS2): ``register(server, ctx)`` adds the tools.

Layers (engineering-tools plan §4): the universal channel ``mechanical_run_script`` (any Mechanical scripting API,
embedded PyMechanical or Mechanical's own batch engine), typed helpers (geometry import, mesh, solve, results,
project summary) and discovery (``mechanical_status``, ``mechanical_api_search``). Runs are jobs of the shared job
layer (pool ``ansys``); PyMechanical is imported only inside the job process.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field

from vkm_ansys import product_jobs as pj
from vkm_ansys.product_schema import (Checks, DryRun, Label, LicenseProbe, ModelChoices, Params, Ref, RefList,
                                      ScriptText, WaitS, dump)

TOOLS = ("mechanical_status", "mechanical_api_search", "mechanical_run_script", "mechanical_import_geometry",
         "mechanical_mesh", "mechanical_solve", "mechanical_results", "mechanical_project_summary")
SaveAs = Annotated[str | None, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.mechdb$",
                                     description="save the project to out/<name> (e.g. model.mechdb)")]
Timeout = Annotated[int, Field(ge=1, le=86400, description="seconds; the job is killed after this")]
AnalysisRef = Annotated[int | str, Field(description="analysis index (0 = first) or its name")]


class ResultQuantity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["total_deformation", "directional_deformation", "equivalent_stress", "normal_stress", "shear_stress",
                  "maximum_principal_stress", "minimum_principal_stress", "equivalent_elastic_strain",
                  "normal_elastic_strain", "force_reaction"]
    axis: Literal["x", "y", "z"] | None = None
    scope: Annotated[str | None, Field(max_length=200, description="named selection to scope to")] = None
    boundary_condition: Annotated[str | None, Field(max_length=200, description="support name (force_reaction)")] = None
    time: Annotated[float | None, Field(ge=0, description="display time, s")] = None
    name: Annotated[str | None, Field(max_length=200)] = None


def _job_tool(ctx: Any, build, wait_s: int, dry_run: bool):
    job = build()
    if dry_run:
        return {"dry_run": True, "job": job.describe()}
    return pj.submit(ctx, job, wait_s)


def register(server: Any, ctx: Any) -> list[str]:
    from vkm_ansys.mechanical.service import MechanicalService

    svc = MechanicalService(ctx)

    @server.tool(name="mechanical_status", annotations=ctx.READ_ONLY)
    async def mechanical_status() -> CallToolResult:
        """Mechanical is not started: executable and build, PyMechanical / stubs versions, engines available
        (embedded, batch IronPython, batch CPython), running Mechanical processes with listening ports, and the licence
        features the licence server reports for Mechanical and its solver (read-only lmstat: names and seat counts)."""
        return await ctx.call("mechanical_status", svc.status)

    @server.tool(name="mechanical_api_search", annotations=ctx.READ_ONLY)
    async def mechanical_api_search(query: Annotated[str, Field(min_length=1, max_length=200)],
                                    limit: Annotated[int, Field(ge=1, le=100)] = 20,
                                    kinds: Annotated[list[Literal["class", "enum", "enum_member", "property",
                                                                  "method"]] | None, Field(max_length=5)] = None,
                                    release: Annotated[str | None, Field(pattern=r"^\d{3}$")] = None) -> CallToolResult:
        """Offline search of the Mechanical scripting API (ansys-mechanical-stubs, release of the installation by
        default): classes, methods with signatures, properties (settable or not), enums and members with their first
        doc line. Words must all match (name, qualified name or doc); exact names rank first. Mechanical is not
        started."""
        return await ctx.call("mechanical_api_search", lambda: svc.api_search(query, limit, kinds, release))

    @server.tool(name="mechanical_run_script", annotations=ctx.EXEC)
    async def mechanical_run_script(script: ScriptText = None, script_path: Ref | None = None,
                                    engine: Literal["embedded", "batch"] = "embedded",
                                    batch_engine_type: Literal["ironpython", "cpython"] = "ironpython",
                                    db_file: Ref | None = None, inputs: RefList = None, save_as: SaveAs = None,
                                    readonly: bool = False,
                                    unit_system: Literal["StandardMKS", "StandardCGS", "StandardNMM", "StandardBFT",
                                                         "StandardBIN", "StandardUMKS"] | None = None,
                                    params: Params = None, model_choices: ModelChoices = None, checks: Checks = None,
                                    timeout_s: Timeout = 3600, wait_s: WaitS = 0, label: Label = None,
                                    license_probe: LicenseProbe = False, dry_run: DryRun = False) -> CallToolResult:
        """Universal channel: run any Mechanical scripting code as a job. Globals: app (embedded), Model, ExtAPI,
        DataModel, Tree, Graphics, Quantity, enums; JOB_DIR, IN_DIR, WORK_DIR, OUT_DIR, REQUEST; fill the dict
        `result` — it is written to out/result.json. engine='embedded' (default) embeds Mechanical in the job's
        CPython (PyMechanical App; numpy etc. available; readonly=true needs no licence); engine='batch' runs the
        script inside AnsysWBU -b (IronPython 2.7, or CPython with batch_engine_type='cpython'). db_file opens a
        project (.mechdb; its _Mech_Files folder goes along); inputs are copied to in/inputs/; save_as saves to out/.
        Returns a JobRef at once (or after wait_s): follow with job_wait / job_read / job_receipt. Executes code."""
        return await ctx.call("mechanical_run_script", lambda: _job_tool(ctx, lambda: svc.build_run_script(
            script=script, script_path=script_path, engine=engine, batch_engine_type=batch_engine_type,
            db_file=db_file, inputs=inputs, save_as=save_as, readonly=readonly, unit_system=unit_system,
            params=dump(params), model_choices=model_choices, checks=dump(checks), timeout_s=timeout_s, label=label,
            license_probe=license_probe), wait_s, dry_run))

    @server.tool(name="mechanical_import_geometry", annotations=ctx.EXEC)
    async def mechanical_import_geometry(geometry: Ref | None = None,
                                         box_m: Annotated[list[float] | None, Field(
                                             min_length=3, max_length=3,
                                             description="TOY block [lx, ly, lz] in metres generated as STEP")] = None,
                                         analysis: Literal["static_structural", "transient_structural", "modal",
                                                           "steady_state_thermal", "transient_thermal",
                                                           "eigenvalue_buckling"] | None = "static_structural",
                                         named_selections: bool = False,
                                         save_as: SaveAs = "model.mechdb", timeout_s: Timeout = 1800,
                                         wait_s: WaitS = 0, label: Label = None,
                                         license_probe: LicenseProbe = False, dry_run: DryRun = False
                                     ) -> CallToolResult:
        """New Mechanical model from a CAD file (STEP, IGES, Parasolid, SCDOC, AGDB, PMDB …) or from a generated TOY
        block (box_m), in the MKS unit system; optionally adds an analysis; saves out/<save_as>. The result lists the
        bodies (volume, faces, material). Job (pool ansys)."""
        return await ctx.call("mechanical_import_geometry", lambda: _job_tool(ctx, lambda: svc.build_import_geometry(
            geometry=geometry, box_m=box_m, analysis=analysis, named_selections=named_selections, save_as=save_as,
            timeout_s=timeout_s, label=label, license_probe=license_probe), wait_s, dry_run))

    @server.tool(name="mechanical_mesh", annotations=ctx.EXEC)
    async def mechanical_mesh(db_file: Ref,
                              element_size_m: Annotated[float | None, Field(gt=0, le=1e4)] = None,
                              element_order: Literal["quadratic", "linear", "program_controlled"] = "quadratic",
                              method: Literal["tetrahedrons", "hex_dominant", "sweep", "multizone"] | None = None,
                              save_as: SaveAs = "model.mechdb", timeout_s: Timeout = 3600, wait_s: WaitS = 0,
                              label: Label = None, license_probe: LicenseProbe = False, dry_run: DryRun = False
                          ) -> CallToolResult:
        """Mesh a project (global element size and order, optional method on all bodies) and report nodes, elements
        and element quality; saves out/<save_as>. Mesh settings are recorded as MODEL_CHOICE. Job (pool ansys)."""
        return await ctx.call("mechanical_mesh", lambda: _job_tool(ctx, lambda: svc.build_mesh(
            db_file=db_file, element_size_m=element_size_m, element_order=element_order, method=method,
            save_as=save_as, timeout_s=timeout_s, label=label, license_probe=license_probe), wait_s, dry_run))

    @server.tool(name="mechanical_solve", annotations=ctx.EXEC)
    async def mechanical_solve(db_file: Ref, analysis: AnalysisRef = 0, save_as: SaveAs = "model.mechdb",
                               timeout_s: Timeout = 3600, wait_s: WaitS = 0, label: Label = None,
                               license_probe: LicenseProbe = False, dry_run: DryRun = False) -> CallToolResult:
        """Solve one analysis of a project (MAPDL solver behind Mechanical) and report the solution status, solver
        files and error/warning counts of solve.out (copied to out/solver/ with ds.dat); saves the project with its
        results to out/<save_as>. Results are MODEL_RESULT. Job (pool ansys)."""
        return await ctx.call("mechanical_solve", lambda: _job_tool(ctx, lambda: svc.build_solve(
            db_file=db_file, analysis=analysis, save_as=save_as, timeout_s=timeout_s, label=label,
            license_probe=license_probe), wait_s, dry_run))

    @server.tool(name="mechanical_results", annotations=ctx.EXEC)
    async def mechanical_results(db_file: Ref,
                                 quantities: Annotated[list[ResultQuantity], Field(min_length=1, max_length=30)],
                                 analysis: AnalysisRef = 0, export_tables: bool = True, save_as: SaveAs = None,
                                 timeout_s: Timeout = 1800, wait_s: WaitS = 0, label: Label = None,
                                 license_probe: LicenseProbe = False, dry_run: DryRun = False) -> CallToolResult:
        """Add and evaluate result objects on a solved project: deformation, stress, strain (axis x/y/z where it
        applies, optional named-selection scope and display time) and force reactions of a support. Reports
        minimum / maximum / average (value + unit) and exports node tables to out/results/*.txt. MODEL_RESULT.
        Job (pool ansys)."""
        return await ctx.call("mechanical_results", lambda: _job_tool(ctx, lambda: svc.build_results(
            db_file=db_file, quantities=dump(quantities), analysis=analysis, export_tables=export_tables,
            save_as=save_as, timeout_s=timeout_s, label=label, license_probe=license_probe), wait_s, dry_run))

    @server.tool(name="mechanical_project_summary", annotations=ctx.EXEC)
    async def mechanical_project_summary(db_file: Ref | None = None,
                                         max_depth: Annotated[int, Field(ge=1, le=12)] = 6,
                                         max_nodes: Annotated[int, Field(ge=10, le=10000)] = 2000,
                                         readonly: bool = True, timeout_s: Timeout = 900, wait_s: WaitS = 0,
                                         label: Label = None, dry_run: DryRun = False) -> CallToolResult:
        """Summary of a project without changing it: unit system, bodies, mesh counts, analyses and solution states,
        the object tree (result minima/maxima where evaluated) and Mechanical's licence preference list. Read-only
        mode by default (no licence checkout). Without db_file: an empty session (licence list only). Job."""
        return await ctx.call("mechanical_project_summary", lambda: _job_tool(ctx, lambda: svc.build_summary(
            db_file=db_file, max_depth=max_depth, max_nodes=max_nodes, readonly=readonly, timeout_s=timeout_s,
            label=label), wait_s, dry_run))

    return list(TOOLS)
