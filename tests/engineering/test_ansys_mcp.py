"""MCP contract of the stdio server ``vkm-ansys`` without Ansys: tools, annotations, envelope, the optional-module
hook ``register(server, ctx)`` and a real stdio process that writes only the protocol to stdout."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402

from vkm_ansys.context import CORE_TOOLS, RESULT_SCHEMA, AnsysContext, validate_sim_root  # noqa: E402
from vkm_ansys.mcp_server import build_server, register_optional  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "corpus"))
from test_drawio_mcp import _stdio_exchange  # noqa: E402


def _ctx(tmp_path: Path, **extra: str) -> AnsysContext:
    env = {"VKM_SIM_ROOT": str(tmp_path / "sim"), "VKM_ANSYS_ROOT": str(tmp_path / "no_ansys" / "v261"), **extra}
    return AnsysContext.from_env(env)


def _fake_module(name: str, body) -> None:
    module = types.ModuleType(name)
    module.register = body
    sys.modules[name] = module


def test_core_tools_have_annotations_and_status_envelope(tmp_path):
    ctx = _ctx(tmp_path)

    async def go():
        async with Client(build_server(ctx, optional_modules=())) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            status = await client.call_tool("ansys_status", {})
            return tools, status

    tools, status = asyncio.run(go())
    assert "ansys_status" in tools and set(tools) <= CORE_TOOLS
    assert tools["ansys_status"].annotations.read_only_hint is True
    body = status.structured_content
    assert body["schema"] == RESULT_SCHEMA and body["ok"] is True and body["server"] == "vkm-ansys"
    assert body["result"]["installed"] is False                     # the fake root does not exist
    assert body["result"]["sim_root"]["available"] is True
    assert str(tmp_path) not in json.dumps(body)


def test_optional_module_hook(tmp_path):
    ctx = _ctx(tmp_path)

    def good(server, c):
        @server.tool(name="mech_fake", annotations=c.READ_ONLY)
        async def mech_fake() -> object:
            """fake"""
            return await c.call("mech_fake", lambda: {"hello": 1})

    def broken(server, c):
        raise RuntimeError("boom")

    def clash(server, c):
        @server.tool(name="ansys_compare", annotations=c.READ_ONLY)
        async def ansys_compare() -> object:
            """fake"""
            return await c.call("ansys_compare", lambda: 1)

    _fake_module("vkm_ansys_test_good", good)
    _fake_module("vkm_ansys_test_broken", broken)
    _fake_module("vkm_ansys_test_clash", clash)
    try:
        server = build_server(ctx, optional_modules=("vkm_ansys_test_good", "vkm_ansys_test_broken",
                                                     "vkm_ansys_test_missing_module", "vkm_ansys_test_clash"))
    finally:
        for name in ("vkm_ansys_test_good", "vkm_ansys_test_broken", "vkm_ansys_test_clash"):
            sys.modules.pop(name, None)
    assert ctx.modules["vkm_ansys_test_good"] == "LOADED"
    assert ctx.modules["vkm_ansys_test_broken"].startswith("UNAVAILABLE:RuntimeError")
    assert ctx.modules["vkm_ansys_test_missing_module"] == "NOT_INSTALLED"
    assert ctx.modules["vkm_ansys_test_clash"].startswith("LOADED_WITH_CLASHES:ansys_compare")
    assert ctx.tools["mech_fake"] == "vkm_ansys_test_good"

    async def go():
        async with Client(server) as client:
            return await client.call_tool("mech_fake", {})

    body = asyncio.run(go()).structured_content
    assert body["ok"] and body["result"] == {"hello": 1} and body["tool"] == "mech_fake"
    assert register_optional(server, ctx, "vkm_ansys_test_missing_module") == "NOT_INSTALLED"


def test_failures_become_envelopes(tmp_path):
    ctx = _ctx(tmp_path)

    async def go():
        known = await ctx.call("t", lambda: (_ for _ in ()).throw(ctx.failure("NOT_FOUND", "nope", job_id="x")))
        unknown = await ctx.call("t", lambda: 1 / 0)
        return known, unknown

    known, unknown = asyncio.run(go())
    assert known.is_error and known.structured_content["error"]["code"] == "NOT_FOUND"
    assert known.structured_content["error"]["details"] == {"job_id": "x"}
    assert unknown.is_error and unknown.structured_content["error"]["code"] == "INTERNAL"
    assert "log_ref" in unknown.structured_content["error"]["details"]


def test_sim_root_rules(tmp_path):
    assert validate_sim_root({})[0] is None
    assert "ASCII" in validate_sim_root({"VKM_SIM_ROOT": str(tmp_path / "симуляции")})[1]
    assert "spaces" in validate_sim_root({"VKM_SIM_ROOT": str(tmp_path / "a b")})[1]
    assert "absolute" in validate_sim_root({"VKM_SIM_ROOT": "relative/dir"})[1]
    assert "repository" in validate_sim_root({"VKM_SIM_ROOT": str(ROOT / "work" / "sim")})[1] \
        or not str(ROOT).isascii()
    assert validate_sim_root({"VKM_SIM_ROOT": "${VKM_SIM_ROOT}"})[0] is None      # unexpanded placeholder
    root, reason = validate_sim_root({"VKM_SIM_ROOT": str(tmp_path / "sim")})
    if str(tmp_path).isascii() and " " not in str(tmp_path):
        assert root is not None and reason is None
    ctx = _ctx(tmp_path)
    if ctx.sim_root_status()["available"]:
        assert ctx.logical(tmp_path / "sim" / "jobs" / "x") == "<VKM_SIM_ROOT>/jobs/x"


def test_stdio_server_writes_only_protocol_to_stdout(tmp_path):
    from mcp.types import LATEST_PROTOCOL_VERSION

    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1", "VKM_SIM_ROOT": str(tmp_path / "sim")}
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ansys_status", "arguments": {}}},
    ]
    lines = _stdio_exchange([sys.executable, "-m", "vkm_ansys.mcp_server"], env, messages, last_id=3)
    parsed = [json.loads(line) for line in lines]                         # any stray print would fail here
    by_id = {m.get("id"): m for m in parsed}
    assert all(m.get("jsonrpc") == "2.0" for m in parsed)
    assert "ansys_status" in {t["name"] for t in by_id[2]["result"]["tools"]}
    assert by_id[3]["result"]["structuredContent"]["ok"] is True
