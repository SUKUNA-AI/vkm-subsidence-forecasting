"""MCP contract of the stdio server ``vkm-drawio``: tool set, annotations, input schemas, result shape, errors,
images, and "stdout carries only the protocol" (MCP-12)."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402

from vkm_drawio.locate import DrawioLocation  # noqa: E402
from vkm_drawio.mcp_server import RESULT_SCHEMA, build_server  # noqa: E402
from vkm_drawio.service import DrawioService  # noqa: E402
from vkm_drawio.workspace import Workspace  # noqa: E402
from vkm_world.governance.leakage import FORBIDDEN_COLUMNS, _json_keys  # noqa: E402

from test_drawio_service import FakeCli  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TOOLS = {
    "drawio_status": ([], True),
    "drawio_create_diagram": (["root", "path", "spec", "overwrite"], False),
    "drawio_read_diagram": (["root", "path", "include_geometry", "page", "max_cells"], True),
    "drawio_update_diagram": (["root", "path", "expected_sha256", "ops"], False),
    "drawio_export": (["root", "path", "format", "page", "all_pages", "scale", "border", "transparent",
                       "embed_diagram", "out_root", "out_path", "overwrite", "embed_fonts"], False),
    "drawio_render_preview": (["root", "path", "page", "max_side"], True),
    "drawio_open": (["root", "path"], True),
    "drawio_list_diagrams": (["root", "pattern"], True),
}
SPEC = {"pages": [{"id": "p", "name": "P", "nodes": [{"id": "a", "label": "A", "x": 10, "y": 10}]}]}


def _server(tmp_path: Path, found: bool = False):
    ws = Workspace(public_root=tmp_path / "public", work_root=tmp_path / "work")
    if found:
        loc = DrawioLocation(True, "ENV", tmp_path / "draw.io.exe", "$VKM_DRAWIO_EXE")
        return build_server(DrawioService(ws, locator=lambda: loc, cli=FakeCli()))
    return build_server(DrawioService(ws, locator=lambda: DrawioLocation(False, reason="test")))


def _run(coro):
    return asyncio.run(coro)


def test_tool_set_annotations_and_schemas(tmp_path):
    async def go():
        async with Client(_server(tmp_path)) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in _run(go())}
    assert set(tools) == set(TOOLS)
    for name, (props, read_only) in TOOLS.items():
        tool = tools[name]
        assert list(tool.input_schema.get("properties", {})) == props, name
        assert tool.annotations.read_only_hint is read_only, name
        assert tool.annotations.destructive_hint is False, name
        assert tool.description and len(tool.description) > 40, name
        assert not (FORBIDDEN_COLUMNS & _json_keys(tool.input_schema)), name
    assert tools["drawio_open"].annotations.open_world_hint is True
    assert set(tools["drawio_create_diagram"].input_schema["required"]) == {"root", "path", "spec"}
    root_schema = json.dumps(tools["drawio_read_diagram"].input_schema["properties"]["root"])
    assert '"public"' in root_schema and '"work"' in root_schema


def test_results_are_structured_and_errors_are_machine_readable(tmp_path):
    async def go():
        async with Client(_server(tmp_path)) as client:
            created = await client.call_tool("drawio_create_diagram", {"root": "work", "path": "a.drawio",
                                                                       "spec": SPEC})
            again = await client.call_tool("drawio_create_diagram", {"root": "work", "path": "a.drawio",
                                                                     "spec": SPEC})
            escape = await client.call_tool("drawio_read_diagram", {"root": "work", "path": "../a.drawio"})
            export = await client.call_tool("drawio_export", {"root": "work", "path": "a.drawio", "format": "png"})
            invalid = await client.call_tool("drawio_create_diagram", {"root": "work", "path": "b.drawio",
                                                                       "spec": {"pages": []}})
            status = await client.call_tool("drawio_status", {})
            return created, again, escape, export, invalid, status

    created, again, escape, export, invalid, status = _run(go())
    body = created.structured_content
    assert created.is_error is False and body["schema"] == RESULT_SCHEMA and body["ok"] is True
    assert body["tool"] == "drawio_create_diagram" and len(body["result"]["sha256"]) == 64
    assert json.loads(created.content[-1].text) == body                  # text block mirrors structured content
    for result, code in ((again, "WOULD_OVERWRITE"), (escape, "PATH_OUTSIDE_WORKSPACE"),
                         (export, "DRAWIO_UNAVAILABLE")):
        assert result.is_error is True and result.structured_content["ok"] is False
        assert result.structured_content["error"]["code"] == code
        assert set(result.structured_content["error"]) == {"code", "message", "retryable", "details"}
    assert invalid.is_error is True                                       # SDK argument validation
    assert status.structured_content["result"]["capabilities"]["create"] is True
    assert str(tmp_path) not in json.dumps(status.structured_content)


def test_preview_is_image_content(tmp_path):
    async def go():
        async with Client(_server(tmp_path, found=True)) as client:
            await client.call_tool("drawio_create_diagram", {"root": "work", "path": "a.drawio", "spec": SPEC})
            return await client.call_tool("drawio_render_preview", {"root": "work", "path": "a.drawio",
                                                                    "max_side": 256})

    result = _run(go())
    image, text = result.content
    assert result.is_error is False and image.type == "image" and image.mime_type == "image/png"
    assert base64.b64decode(image.data).startswith(b"\x89PNG")
    assert json.loads(text.text)["result"]["px"] == [320, 200]


def _stdio_exchange(cmd: list[str], env: dict[str, str], messages: list[dict], last_id: int,
                    timeout: float = 90.0) -> list[str]:
    """Send JSON-RPC lines, keep stdin open until the response ``last_id`` arrives, return all stdout lines."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", env=env)
    out: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        for line in proc.stdout:
            out.put(line)
        out.put(None)

    threading.Thread(target=reader, daemon=True).start()
    lines: list[str] = []
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = out.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            if line.strip():
                lines.append(line)
                try:
                    if json.loads(line).get("id") == last_id:
                        break
                except json.JSONDecodeError:
                    pass                                                  # reported by the caller's json.loads
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    return lines


def test_stdio_server_writes_only_protocol_to_stdout(tmp_path):
    """MCP-12: every stdout line of the real process is a JSON-RPC message."""
    from mcp.types import LATEST_PROTOCOL_VERSION

    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1", "VKM_WORK": str(tmp_path / "w"),
           "CLAUDE_PROJECT_DIR": str(ROOT), "VKM_DRAWIO_EXE": str(tmp_path / "missing.exe")}
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "drawio_status", "arguments": {}}},
    ]
    lines = _stdio_exchange([sys.executable, "-m", "vkm_drawio.mcp_server"], env, messages, last_id=3)
    parsed = [json.loads(line) for line in lines]                         # any stray print would fail here
    assert all(m.get("jsonrpc") == "2.0" for m in parsed)
    by_id = {m.get("id"): m for m in parsed}
    assert {t["name"] for t in by_id[2]["result"]["tools"]} == set(TOOLS)
    status = by_id[3]["result"]["structuredContent"]["result"]
    assert status["drawio"]["found"] is False and status["roots"]["work"]["available"] is True
