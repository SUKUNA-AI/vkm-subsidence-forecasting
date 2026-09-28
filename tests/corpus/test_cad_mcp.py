"""MCP contract of the stdio server ``vkm-cad``: eleven tools (§37), annotations (read vs scratch), schemas, results
and machine-readable errors, and a real stdio process that writes only the protocol to stdout (MCP-12)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402

from vkm_cad.mcp_server import RESULT_SCHEMA, build_server  # noqa: E402
from vkm_cad.scratch import ScratchStore  # noqa: E402
from vkm_cad.service import CadService  # noqa: E402
from vkm_world.governance.leakage import FORBIDDEN_COLUMNS, _json_keys  # noqa: E402

from test_drawio_mcp import _stdio_exchange  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
READ_TOOLS = {"cad_status", "cad_list_open_documents", "cad_get_layers", "cad_get_extents", "cad_list_entities",
              "cad_get_coordinate_system"}
SCRATCH_TOOLS = {"cad_create_scratch_document", "cad_import_pdf_vector", "cad_extract_geometry", "cad_export_dxf",
                 "cad_save_copy"}


def _server(tmp_path: Path):
    report = {"overall": "NOT_INSTALLED", "products": [], "capabilities": {
        "read_open_documents": "UNAVAILABLE:AUTOCAD_NOT_INSTALLED"}}
    return build_server(CadService(ScratchStore(tmp_path / "scratch"), detector=lambda: report))


def test_tools_annotations_and_schemas(tmp_path):
    async def go():
        async with Client(_server(tmp_path)) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in asyncio.run(go())}
    assert set(tools) == READ_TOOLS | SCRATCH_TOOLS
    for name, tool in tools.items():
        assert tool.annotations.read_only_hint is (name in READ_TOOLS), name
        assert tool.annotations.destructive_hint is False, name
        assert not (FORBIDDEN_COLUMNS & _json_keys(tool.input_schema)), name
    crs = json.dumps(tools["cad_import_pdf_vector"].input_schema["properties"]["crs_status"])
    assert "UNKNOWN_CRS" in crs and "SCHEMATIC" in crs and "EXACT_COORDINATED" not in crs
    assert tools["cad_import_pdf_vector"].input_schema["required"] == ["vector_artifact_id"]
    assert tools["cad_list_entities"].input_schema["properties"]["limit"]["maximum"] == 1000


def test_results_and_errors(tmp_path):
    async def go():
        async with Client(_server(tmp_path)) as client:
            status = await client.call_tool("cad_status", {})
            docs = await client.call_tool("cad_list_open_documents", {})
            created = await client.call_tool("cad_create_scratch_document", {"label": "test"})
            missing = await client.call_tool("cad_import_pdf_vector", {"vector_artifact_id": "sha256:" + "a" * 64})
            exact = await client.call_tool("cad_import_pdf_vector", {"vector_artifact_id": "sha256:" + "a" * 64,
                                                                     "crs_status": "EXACT_COORDINATED"})
            return status, docs, created, missing, exact

    status, docs, created, missing, exact = asyncio.run(go())
    assert status.structured_content["schema"] == RESULT_SCHEMA and status.structured_content["ok"]
    assert status.structured_content["result"]["scratch"]["available"] is True
    assert docs.is_error and docs.structured_content["error"]["code"] == "CAD_UNAVAILABLE"
    body = created.structured_content
    assert body["ok"] and body["result"]["crs_status"] == "UNKNOWN_CRS" and body["result"]["insunits"] == 0
    assert missing.is_error and missing.structured_content["error"]["code"] == "NO_VECTOR_ARTIFACT"
    assert exact.is_error                                                  # rejected by the tool schema itself
    assert str(tmp_path) not in json.dumps(status.structured_content)


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
    assert {t["name"] for t in by_id[2]["result"]["tools"]} == READ_TOOLS | SCRATCH_TOOLS
    result = by_id[3]["result"]["structuredContent"]
    assert result["ok"] is True and result["result"]["scratch_doc_id"].startswith("CADS-")
