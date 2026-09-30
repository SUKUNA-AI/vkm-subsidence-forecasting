"""MCP contract of the shared job tools (plan §2.4): names, annotations, schemas, the result envelope, errors, and
progress notifications of ``job_wait`` — on an MCPServer that only carries the job tools and a fake application."""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

from vkm_jobs.mcp_tools import Envelope, register_job_tools  # noqa: E402
from vkm_world.governance.leakage import FORBIDDEN_COLUMNS, _json_keys  # noqa: E402

from jobs_fakes import OK_APP, service, submit  # noqa: E402

TOOLS = {"job_list": True, "job_status": True, "job_wait": True, "job_read": True, "job_receipt": True,
         "job_cancel": False, "job_publish_receipt": False}


def _server(jobs):
    server = MCPServer("vkm-test", instructions="job tools only", version="0")
    register_job_tools(server, jobs, Envelope("vkm-test", "0"))
    return server


def test_tools_annotations_schemas(tmp_path):
    async def go():
        async with Client(_server(service(tmp_path))) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in asyncio.run(go())}
    assert set(tools) == set(TOOLS)
    for name, read_only in TOOLS.items():
        tool = tools[name]
        assert tool.annotations.read_only_hint is read_only, name
        assert tool.description and len(tool.description) > 40, name
        assert "ctx" not in tool.input_schema.get("properties", {}), name
        assert not (FORBIDDEN_COLUMNS & _json_keys(tool.input_schema)), name
    assert tools["job_cancel"].annotations.destructive_hint is True
    assert tools["job_wait"].input_schema["properties"]["timeout_s"]["maximum"] == 1200
    assert tools["job_read"].input_schema["properties"]["max_chars"]["maximum"] == 20000
    assert set(tools["job_read"].input_schema["required"]) == {"job_id", "path"}
    assert "job_id" in tools["job_status"].input_schema["required"]


def test_envelope_results_errors_and_progress(tmp_path):
    jobs = service(tmp_path)
    slow, _ = submit(jobs, "import time; time.sleep(7); print('done')")
    quick, _ = submit(jobs, OK_APP, wait_s=60)
    notes: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress, total, message):
        notes.append((progress, total, message))

    async def go():
        async with Client(_server(jobs)) as client:
            early = await client.call_tool("job_wait", {"job_id": slow["job_id"], "timeout_s": 1})
            final = await client.call_tool("job_wait", {"job_id": slow["job_id"], "timeout_s": 60},
                                           progress_callback=on_progress)
            status = await client.call_tool("job_status", {"job_id": quick["job_id"]})
            listing = await client.call_tool("job_list", {"status": "SUCCEEDED"})
            read = await client.call_tool("job_read", {"job_id": quick["job_id"], "path": "logs/stdout.log",
                                                       "grep": "ERROR"})
            escape = await client.call_tool("job_read", {"job_id": quick["job_id"], "path": "../../x"})
            missing = await client.call_tool("job_status", {"job_id": "PY-20260101T000000Z-00000000"})
            bad_id = await client.call_tool("job_status", {"job_id": "nope"})
            receipt = await client.call_tool("job_receipt", {"job_id": quick["job_id"]})
            short = await client.call_tool("job_cancel", {"job_id": quick["job_id"], "reason": "short"})
            cancel_done = await client.call_tool("job_cancel", {"job_id": quick["job_id"],
                                                                "reason": "already finished job"})
            publish = await client.call_tool("job_publish_receipt", {"job_id": quick["job_id"], "name": "mcp_fake"})
            return early, final, status, listing, read, escape, missing, bad_id, receipt, short, cancel_done, publish

    (early, final, status, listing, read, escape, missing, bad_id, receipt, short, cancel_done,
     publish) = asyncio.run(go())
    body = early.structured_content
    assert body["schema"] == "vkm-test.result/1" and body["ok"] and body["server"] == "vkm-test"
    assert body["result"]["status"] in ("QUEUED", "RUNNING")
    assert json.loads(early.content[-1].text) == body
    assert final.structured_content["result"]["status"] == "SUCCEEDED"
    assert notes and all(m and slow["job_id"] in m for _, _, m in notes)
    assert status.structured_content["result"]["receipt"]["schema"] == "vkm.sim_receipt/1"
    assert {j["job_id"] for j in listing.structured_content["result"]["jobs"]} == {slow["job_id"], quick["job_id"]}
    assert read.structured_content["result"]["count"] == 1
    for res, code in ((escape, "PATH_OUTSIDE_ROOT"), (missing, "NOT_FOUND")):
        assert res.is_error and res.structured_content["error"]["code"] == code
        assert set(res.structured_content["error"]) == {"code", "message", "retryable", "details"}
    assert bad_id.is_error and short.is_error                           # rejected by the input schema
    assert receipt.structured_content["result"]["job_id"] == quick["job_id"]
    assert cancel_done.structured_content["result"]["already_terminal"] is True
    assert publish.structured_content["result"]["leakage_scan"] == "PASS"
    assert str(tmp_path) not in json.dumps([r.structured_content for r in (status, listing, read, receipt)])
