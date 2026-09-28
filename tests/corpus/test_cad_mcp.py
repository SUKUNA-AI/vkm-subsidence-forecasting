"""MCP contract of the stdio server ``vkm-cad``: v0 tools (§37) and v1 job tools, annotations (read / job / code),
schemas, results and machine-readable errors, and a real stdio process that writes only the protocol to stdout
(MCP-12)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402

from vkm_cad.cadjobs import CadJobs  # noqa: E402
from vkm_cad.jobs import JobStore  # noqa: E402
from vkm_cad.mcp_server import RESULT_SCHEMA, build_server  # noqa: E402
from vkm_cad.scratch import ScratchStore  # noqa: E402
from vkm_cad.service import CadService  # noqa: E402
from vkm_world.governance.leakage import FORBIDDEN_COLUMNS, _json_keys  # noqa: E402

from test_drawio_mcp import _stdio_exchange  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
READ_TOOLS = {"cad_status", "cad_list_open_documents", "cad_get_layers", "cad_get_extents", "cad_list_entities",
              "cad_get_coordinate_system", "cad_capabilities", "cad_job_status", "cad_job_list"}
SCRATCH_TOOLS = {"cad_create_scratch_document", "cad_import_pdf_vector", "cad_extract_geometry", "cad_export_dxf",
                 "cad_save_copy"}
JOB_TOOLS = {"cad_job_create", "cad_draw", "cad_convert", "c3d_points_from_table", "c3d_tin_surface", "c3d_contours",
             "c3d_difference_surface", "c3d_alignment_profile", "cad_layout_sheet", "cad_plot_pdf", "cad_pdf_import"}
CODE_TOOLS = {"cad_exec", "cad_query"}
ALL_TOOLS = READ_TOOLS | SCRATCH_TOOLS | JOB_TOOLS | CODE_TOOLS


def _server(tmp_path: Path):
    report = {"overall": "NOT_INSTALLED", "products": [], "capabilities": {
        "read_open_documents": "UNAVAILABLE:AUTOCAD_NOT_INSTALLED"}}
    jobs = CadJobs(JobStore(tmp_path / "jobs"), env={}, installation=lambda: None,
                   toolchain=lambda _j: (None, ["no compiler in this test"]), audit=False)
    return build_server(CadService(ScratchStore(tmp_path / "scratch"), detector=lambda: report, jobs=jobs))


def test_tools_annotations_and_schemas(tmp_path):
    async def go():
        async with Client(_server(tmp_path)) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in asyncio.run(go())}
    assert set(tools) == ALL_TOOLS
    for name, tool in tools.items():
        assert tool.annotations.read_only_hint is (name in READ_TOOLS), name
        assert tool.annotations.destructive_hint is (name in CODE_TOOLS), name   # arbitrary code: confirm each call
        assert tool.annotations.open_world_hint is False, name
        assert not (FORBIDDEN_COLUMNS & _json_keys(tool.input_schema)), name
    crs = json.dumps(tools["cad_import_pdf_vector"].input_schema["properties"]["crs_status"])
    assert "UNKNOWN_CRS" in crs and "SCHEMATIC" in crs and "EXACT_COORDINATED" not in crs
    assert tools["cad_import_pdf_vector"].input_schema["required"] == ["vector_artifact_id"]
    assert tools["cad_list_entities"].input_schema["properties"]["limit"]["maximum"] == 1000
    kinds = json.dumps(tools["cad_exec"].input_schema["properties"]["kind"])
    assert all(k in kinds for k in ("scr", "lisp", "csharp", "python_com"))
    assert set(tools["cad_exec"].input_schema["required"]) == {"job_id", "kind", "code"}
    assert "fallback" in json.dumps(tools["c3d_tin_surface"].input_schema["properties"]["engine"])


def test_results_and_errors(tmp_path):
    async def go():
        async with Client(_server(tmp_path)) as client:
            status = await client.call_tool("cad_status", {})
            docs = await client.call_tool("cad_list_open_documents", {})
            created = await client.call_tool("cad_create_scratch_document", {"label": "test"})
            missing = await client.call_tool("cad_import_pdf_vector", {"vector_artifact_id": "sha256:" + "a" * 64})
            exact = await client.call_tool("cad_import_pdf_vector", {"vector_artifact_id": "sha256:" + "a" * 64,
                                                                     "crs_status": "EXACT_COORDINATED"})
            caps = await client.call_tool("cad_capabilities", {})
            job = await client.call_tool("cad_job_create", {"label": "mcp test", "product": "C3D"})
            job_id = job.structured_content["result"]["job_id"]
            run = await client.call_tool("cad_exec", {"job_id": job_id, "kind": "scr", "code": "_.LINE 0,0 1,1\n"})
            com = await client.call_tool("cad_exec", {"job_id": job_id, "kind": "python_com", "code": "x = 1"})
            points = await client.call_tool("c3d_points_from_table", {
                "job_id": job_id, "rows": [{"name": "P1", "x": 1, "y": 2, "z": 3}, {"name": "P2", "x": 2, "y": 2}],
                "engine": "fallback"})
            listing = await client.call_tool("cad_job_list", {})
            bad_id = await client.call_tool("cad_job_status", {"job_id": "CADJ-20260101T000000Z-00000000"})
            return status, docs, created, missing, exact, caps, job, run, com, points, listing, bad_id

    status, docs, created, missing, exact, caps, job, run, com, points, listing, bad_id = asyncio.run(go())
    assert status.structured_content["schema"] == RESULT_SCHEMA and status.structured_content["ok"]
    assert status.structured_content["result"]["scratch"]["available"] is True
    assert status.structured_content["result"]["jobs"]["available"] is True
    assert docs.is_error and docs.structured_content["error"]["code"] == "CAD_UNAVAILABLE"
    body = created.structured_content
    assert body["ok"] and body["result"]["crs_status"] == "UNKNOWN_CRS" and body["result"]["insunits"] == 0
    assert missing.is_error and missing.structured_content["error"]["code"] == "NO_VECTOR_ARTIFACT"
    assert exact.is_error                                                  # rejected by the tool schema itself
    cap = caps.structured_content["result"]
    assert cap["channels"]["core_console"]["available"] is False and cap["channels"]["hidden_instance"]["allowed"] is False
    assert {row["area"] for row in cap["civil3d_api_reach"]} and cap["exec_kinds"]["python_com"]["available"] is False
    assert job.structured_content["ok"] and job.structured_content["result"]["product"] == "C3D"
    assert run.is_error and run.structured_content["error"]["code"] == "CAD_ENGINE_UNAVAILABLE"
    assert com.is_error and com.structured_content["error"]["code"] == "HIDDEN_INSTANCE_NOT_ALLOWED"
    pts = points.structured_content["result"]
    assert pts["engine"] == "PURE_PYTHON_FALLBACK" and pts["with_z"] == 1 and pts["without_z"] == 1
    assert pts["crs_status"] == "UNKNOWN_CRS" and pts["epsg"] is None
    assert listing.structured_content["result"]["count"] == 1
    assert bad_id.is_error and bad_id.structured_content["error"]["code"] == "CAD_JOB_NOT_FOUND"
    for result in (status, caps, job, points):
        assert str(tmp_path) not in json.dumps(result.structured_content)         # logical paths only


def test_stdio_server_writes_only_protocol_to_stdout(tmp_path):
    from mcp.types import LATEST_PROTOCOL_VERSION

    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1", "VKM_WORK": str(tmp_path / "w")}
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cad_create_scratch_document",
                                                                       "arguments": {}}},
    ]
    lines = _stdio_exchange([sys.executable, "-m", "vkm_cad.mcp_server"], env, messages, last_id=3)
    parsed = [json.loads(line) for line in lines]
    by_id = {m.get("id"): m for m in parsed}
    assert all(m.get("jsonrpc") == "2.0" for m in parsed)
    assert {t["name"] for t in by_id[2]["result"]["tools"]} == ALL_TOOLS
    result = by_id[3]["result"]["structuredContent"]
    assert result["ok"] is True and result["result"]["scratch_doc_id"].startswith("CADS-")
