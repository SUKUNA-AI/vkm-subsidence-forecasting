"""vkm-ansys: Ansys 2026 R1 bridge of the VKM project (stdio MCP server ``vkm-ansys`` on WORKSTATION, decision D-19).

Three layers (docs/planning/ENGINEERING_TOOLS_MCP_PLAN_RU.md §4):

1. universal channel — any APDL deck as a batch job (``mapdl_run``), interactive MAPDL sessions over local gRPC
   (``mapdl_session_*``), any PyAnsys script in a job process (``ansys_run_python``), DPF result extraction
   (``dpf_extract``);
2. typed tools — the solver ladder S01…S09 on TOY problems with analytic checks (``ansys_ladder_*``), convergence
   (``mapdl_convergence``), model subsidence trough (``ansys_subsidence``);
3. discovery — installation, versions, processes, listening addresses, licences (read-only), APDL command help, DPF
   operators.

Every run is a job with a receipt, a timeout and the ``ansys`` licence pool (one licensed process at a time); outputs
live under ``$VKM_SIM_ROOT`` (ASCII path on a data drive), never in the repository. Solver results are MODEL_RESULT:
never observations, never field validation.

Optional product modules (``vkm_ansys.mechanical``, ``vkm_ansys.workbench``, ``vkm_ansys.optislang``) plug in through
``register(server, ctx)`` — see :mod:`vkm_ansys.context`.
"""
__version__ = "0.1.0"

__all__ = ["__version__"]
