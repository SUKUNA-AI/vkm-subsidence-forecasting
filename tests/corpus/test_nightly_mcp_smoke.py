"""Nightly MCP smoke (agent OPS, ``infra/core/nightly/mcp_smoke.py``): every read tool of ``vkm-corpus`` is called
once, with ids harvested from earlier answers; a data error is a warning, a dependency error, a timeout or a missing
tool is a failure. A fake MCP client stands in for the SDK (no server, no token)."""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("mcp_smoke", ROOT / "infra" / "core" / "nightly" / "mcp_smoke.py")
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)

PAGE, FIG, TAB, FORM, BLOCK = ("VKM-SRC-014:p0118", "VKM-SRC-014:p0118:f0123456789ab", "VKM-SRC-014:p0120:t0123456789ab",
                               "VKM-SRC-014:p0121:m0123456789ab", "VKM-SRC-014:p0118:b0123456789ab")
SEC, TOP, WORK, ART = "SEC-" + "1" * 16, "TOP-" + "2" * 16, "VKM-WRK-014", "sha256:" + "3" * 64


def env(oid, **extra):
    return {"envelope": {"object_id": oid, **extra}, "record": {}}


class FakeClient:
    """Answers like the VKM MCP server: ids in envelopes and records, ok/error bodies, image content."""

    def __init__(self, tools=SMOKE.EXPECTED_TOOLS, errors=None, slow=(), page_size=20):
        self.tools = list(tools)
        self.errors = errors or {}
        self.slow = set(slow)
        self.page_size = page_size
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self, cursor=None):
        start = int(cursor or 0)
        chunk = self.tools[start:start + self.page_size]
        nxt = str(start + self.page_size) if start + self.page_size < len(self.tools) else None
        return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in chunk], next_cursor=nxt)

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if name in self.slow:
            await asyncio.sleep(5)
        if name in self.errors:
            code = self.errors[name]
            return SimpleNamespace(structured_content={"ok": False, "error": {"code": code}}, is_error=True,
                                   content=[])
        items = []
        if name == "search_text":
            items = [env(PAGE, page_id=PAGE, source_id="VKM-SRC-014"), env(BLOCK, page_id=PAGE)]
        elif name == "search_objects":
            kind = arguments["kinds"][0]
            items = [env({"FIGURE": FIG, "TABLE": TAB, "FORMULA": FORM}[kind])]
        body = {"ok": True, "items": items}
        if name == "get_source":
            body["item"] = {"record": {"works": [{"work_id": WORK}]}}
        elif name == "get_outline":
            body["item"] = {"record": {"sections": [{"section_id": SEC}]}}
        elif name == "find_topics":
            body["items"] = [env(TOP)]
        elif name == "get_page":
            body["item"] = {"record": {"render_artifact_id": ART}}
        content = [SimpleNamespace(type="image")] if name in ("get_page_image", "get_figure") else []
        return SimpleNamespace(structured_content=body, is_error=False, content=content)


def run(client, **kw):
    return asyncio.run(SMOKE.run(client, **kw))


def test_all_read_tools_are_called_with_harvested_ids():
    client = FakeClient(page_size=10)                       # four pages of tools: pagination is followed
    rep = run(client)
    assert rep["verdict"] == "PASS" and rep["tools_listed"] == 38 and rep["missing_tools"] == []
    assert rep["summary"] == {"PASS": 38, "WARN": 0, "FAIL": 0, "SKIP": 0} and rep["not_called"] == []
    args = {}
    for name, a in client.calls:
        args.setdefault(name, a)
    assert args["get_page"]["page_id"] == PAGE and args["get_figure"]["figure_id"] == FIG
    assert args["get_table"]["table_id"] == TAB and args["get_formula_context"]["formula_id"] == FORM
    assert args["get_work"]["work_id"] == WORK and args["get_section"]["section_id"] == SEC
    assert args["get_topic"]["topic_id"] == TOP and args["get_artifact"]["artifact_id"] == ART
    assert args["get_object"]["object_id"] == BLOCK and args["rerank_visual"]["candidate_ids"] == [FIG]
    assert args["reconstruct_topic"]["budget_chars"] == 2000
    calls = {c["tool"]: c for c in rep["calls"]}
    assert calls["get_figure"]["n_images"] == 1 and len(calls["search_text"]["sha256"]) == 64
    assert "query" not in calls["search_text"]["args"]                   # the report keeps no query text


def test_data_errors_warn_and_dependency_errors_fail():
    rep = run(FakeClient(errors={"copies_of": "NOT_FOUND"}))
    assert rep["verdict"] == "WARN" and rep["per_tool"]["copies_of"] == "WARN"
    rep = run(FakeClient(errors={"rerank_visual": "DEPENDENCY_UNAVAILABLE", "copies_of": "NOT_FOUND"}))
    assert rep["verdict"] == "FAIL" and rep["per_tool"]["rerank_visual"] == "FAIL"


def test_missing_tool_timeout_and_unharvestable_ids():
    tools = [t for t in SMOKE.EXPECTED_TOOLS if t != "get_topic"]
    rep = run(FakeClient(tools=tools, slow={"explore_concept"}), call_timeout_s=0.05)
    assert rep["verdict"] == "FAIL" and rep["missing_tools"] == ["get_topic"]
    calls = {c["tool"]: c for c in rep["calls"]}
    assert calls["explore_concept"]["status"] == "FAIL" and calls["explore_concept"]["error_code"] == "TIMEOUT"
    assert calls["get_topic"]["error_code"] == "TOOL_NOT_LISTED"

    class Empty(FakeClient):
        async def call_tool(self, name, arguments=None):
            self.calls.append((name, arguments))
            return SimpleNamespace(structured_content={"ok": True, "items": []}, is_error=False, content=[])

    rep = run(Empty())
    assert rep["verdict"] == "WARN" and rep["per_tool"]["get_page"] == "SKIP"
    assert rep["per_tool"]["get_corpus_status"] == "PASS"


def test_deadline_skips_the_rest():
    rep = run(FakeClient(), deadline_s=-1)
    assert rep["per_tool"]["get_corpus_status"] == "SKIP" and rep["verdict"] == "WARN"


def test_host_header_from_the_allowed_hosts():
    assert SMOKE.mcp_url_and_host({"VKM_MCP_ALLOWED_HOSTS": "core.lan:8765,other:1"}) == \
        ("http://127.0.0.1:8765/mcp", "core.lan:8765")
    assert SMOKE.mcp_url_and_host({"VKM_MCP_ALLOWED_HOSTS": "core.lan:*"})[1] == "core.lan:8765"
    assert SMOKE.mcp_url_and_host({})[1] is None


def test_dry_run_plans_every_expected_tool():
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert SMOKE.main(["--dry-run"]) == 0
    out = json.loads(buf.getvalue())
    assert out["planned_tools"] == sorted(SMOKE.EXPECTED_TOOLS) and out["expected"] == 38


def test_expected_tools_match_the_read_server():
    src = (ROOT / "src" / "vkm_corpus" / "mcp" / "servers.py").read_text(encoding="utf-8")
    read_part = src.split("def build_admin_server")[0]
    import re
    names = set(re.findall(r'@server\.tool\(name="([a-z_]+)"', read_part))
    assert names == set(SMOKE.EXPECTED_TOOLS)
