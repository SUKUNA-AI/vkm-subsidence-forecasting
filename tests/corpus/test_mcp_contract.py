"""VKM Corpus MCP contract (MCP-01…MCP-14 of the design): tool sets and annotations of the read and admin servers,
schemas, ApiResponse results, errors, images, plan-first reprocessing, configuration (no write token in the read
server), the streamable-HTTP wrapper (bearer, allowed hosts) and the §60 scenario in process."""
from __future__ import annotations

import ast
import asyncio
import base64
import io
import json
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("PIL")
pytest.importorskip("pytz", reason="DuckDB TIMESTAMPTZ values in Python need pytz (decision note: extra corpus)")

import httpx  # noqa: E402
from mcp import Client  # noqa: E402

from vkm_corpus.api.app import ApiConfig, create_app  # noqa: E402
from vkm_corpus.api.fixtures import synthetic_service  # noqa: E402
from vkm_corpus.config import ConfigError  # noqa: E402
from vkm_corpus.mcp.acceptance import Scenario  # noqa: E402
from vkm_corpus.mcp.api_client import ApiClient  # noqa: E402
from vkm_corpus.mcp.http import McpHttpConfig, build_http_app  # noqa: E402
from vkm_corpus.mcp.servers import build_admin_server, build_read_server  # noqa: E402
from vkm_world.governance.leakage import FORBIDDEN_COLUMNS, _json_keys  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
READ, WRITE = "mcp-test-read-token-000000000000000", "mcp-test-write-token-00000000000000"
READ_TOOLS = {"search_text", "search_hybrid", "retrieval_trace", "search_objects", "get_source", "get_work",
              "get_page", "get_page_image", "get_figure", "get_table", "get_formula", "get_object",
              "get_document_neighbors", "get_citations", "rerank_text", "rerank_visual", "get_processing_status",
              "trace_document_provenance", "get_artifact", "list_source_pages", "get_corpus_status",
              # NAV graph in Neo4j (agent G)
              "concept_paths", "graph_neighbourhood",
              # navigation layer (derived, not evidence)
              "get_outline", "get_section", "search_sections", "get_formula_context", "find_formulas",
              "explore_concept", "reconstruct_topic",
              # topics (agent T) and duplicates (agent U)
              "find_topics", "get_topic", "similar_sections", "section_topics", "copies_of", "source_overlap",
              # parameter candidates (agent P)
              "find_parameters", "parameter_summary",
              # term dictionary (agent TR)
              "translate_term",
              # digitized chart series (agent FD2)
              "find_figure_series", "get_figure_series"}
ADMIN_TOOLS = {"reprocess_source", "reprocess_page", "get_job", "cancel_job"}


@pytest.fixture()
def stack(tmp_path):
    service, canon, fakes = synthetic_service(tmp_path)
    app = create_app(service, ApiConfig(read_tokens={READ: "read"}, write_tokens={WRITE: "write"}))
    read_api = ApiClient("http://vkm-api", READ, transport=httpx.ASGITransport(app=app))
    admin_api = ApiClient("http://vkm-api", WRITE, transport=httpx.ASGITransport(app=app))
    return build_read_server(read_api), build_admin_server(admin_api), canon, fakes, app


def _run(coro):
    return asyncio.run(coro)


async def _tools(server):
    async with Client(server) as client:
        return {t.name: t for t in (await client.list_tools()).tools}


async def _call(server, tool, args):
    async with Client(server) as client:
        return await client.call_tool(tool, args)


def test_read_and_admin_tool_sets_are_separate(stack):
    read, admin, *_ = stack
    read_tools, admin_tools = _run(_tools(read)), _run(_tools(admin))
    assert set(read_tools) == READ_TOOLS and not (set(read_tools) & {"reprocess_source", "reprocess_page"})  # MCP-01
    assert set(admin_tools) == ADMIN_TOOLS                                                                   # MCP-02
    assert all(t.annotations.read_only_hint for t in read_tools.values())                                   # MCP-03
    assert not admin_tools["reprocess_page"].annotations.read_only_hint
    assert admin_tools["reprocess_page"].annotations.destructive_hint is False
    assert admin_tools["cancel_job"].annotations.destructive_hint is True
    options = admin_tools["reprocess_page"].input_schema["properties"]
    assert {"force", "recall_model", "no_ocr"} <= set(options) and "stages" not in options      # the worker's options
    for tool in list(read_tools.values()) + list(admin_tools.values()):
        assert tool.description and not (FORBIDDEN_COLUMNS & _json_keys(tool.input_schema)), tool.name
    page_schema = json.dumps(read_tools["get_page"].input_schema)
    assert "VKM-SRC-[0-9]{3}:[prs][0-9]{4}" in page_schema                                       # MCP-04: ids grammar
    assert read_tools["rerank_text"].input_schema["properties"]["candidate_ids"]["maxItems"] == 24
    assert read_tools["rerank_visual"].input_schema["properties"]["candidate_ids"]["maxItems"] == 8


def test_results_are_api_responses_with_envelopes(stack):
    read, _admin, canon, *_ = stack
    result = _run(_call(read, "get_source", {"source_id": "VKM-SRC-013"}))
    body = result.structured_content
    assert result.is_error is False and body["ok"] and json.loads(result.content[-1].text) == body          # MCP-05
    assert body["item"]["envelope"]["lifecycle_status"] == "ABSENT_BY_REGISTER"
    missing = _run(_call(read, "get_object", {"object_id": "VKM-SRC-001:p0001:f000000000000"}))
    assert missing.is_error and missing.structured_content["error"]["code"] == "NOT_FOUND"                 # MCP-06
    invalid = _run(_call(read, "get_object", {"object_id": "not-an-id"}))
    assert invalid.is_error and invalid.structured_content["error"]["code"] == "INVALID_ID"
    rejected = _run(_call(read, "get_source", {"source_id": "SRC-1"}))
    assert rejected.is_error                                                                   # schema pattern check


def test_unreachable_api_is_a_dependency_error():
    def refuse(request):
        raise httpx.ConnectError("refused")

    server = build_read_server(ApiClient("http://vkm-api", READ, transport=httpx.MockTransport(refuse)))
    result = _run(_call(server, "get_source", {"source_id": "VKM-SRC-001"}))
    assert result.is_error and result.structured_content["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert result.structured_content["error"]["retryable"] is True


def test_images_are_image_content_with_limits(stack):
    read, _admin, canon, *_ = stack
    result = _run(_call(read, "get_page_image", {"page_id": "VKM-SRC-001:p0001", "max_side": 256}))
    image, text = result.content
    assert image.type == "image" and image.mime_type == "image/png"                                        # MCP-07
    from PIL import Image

    size = Image.open(io.BytesIO(base64.b64decode(image.data))).size
    meta = result.structured_content["image"]
    assert max(size) == 256 and meta["px"] == list(size) and meta["pixel_to_page"]["sx"] > 0
    figure = _run(_call(read, "get_figure", {"figure_id": canon.ids["figure"]}))
    assert [c.type for c in figure.content] == ["image", "text"]
    no_image = _run(_call(read, "get_page_image", {"page_id": "VKM-SRC-001:p0002"}))
    assert no_image.structured_content["image"]["error"]["code"] == "ARTIFACT_NOT_MATERIALIZED"
    assert [c.type for c in no_image.content] == ["text"]


def test_rerank_limits_are_enforced_before_the_api(stack):
    read, *_ = stack
    too_many = _run(_call(read, "rerank_text", {"query": "q", "candidate_ids": [f"x{i}" for i in range(25)]}))
    assert too_many.is_error                                                                   # schema: maxItems 24
    ranked = _run(_call(read, "rerank_text", {"query": "оседание", "candidate_ids": ["VKM-SRC-001:p0001",
                                                                                    "VKM-SRC-002:p0001"]}))
    items = ranked.structured_content["items"]
    assert items[0]["envelope"]["object_id"] == "VKM-SRC-001:p0001" and items[0]["record"]["rank"] == 1


def test_admin_reprocess_is_two_step(stack):
    _read, admin, _canon, fakes, _app = stack
    first = _run(_call(admin, "reprocess_page", {"page_id": "VKM-SRC-001:p0002", "reason": "synthetic OCR rerun"}))
    record = first.structured_content["item"]["record"]
    assert record["state"] == "PLAN_REQUESTED"                                                              # MCP-08
    early = _run(_call(admin, "reprocess_page", {"page_id": "VKM-SRC-001:p0002", "reason": "synthetic OCR rerun",
                                                 "job_id": record["job_id"], "plan_sha256": "0" * 64}))
    assert early.is_error and early.structured_content["error"]["code"] == "PLAN_NOT_READY"
    digest = fakes["control"].worker_plans(record["job_id"], {"pages": ["VKM-SRC-001:p0002"]})
    job = _run(_call(admin, "get_job", {"job_id": record["job_id"]})).structured_content["item"]["record"]
    assert job["state"] == "PLANNED" and job["plan_sha256"] == digest
    done = _run(_call(admin, "reprocess_page", {"page_id": "VKM-SRC-001:p0002", "reason": "synthetic OCR rerun",
                                                "job_id": record["job_id"], "plan_sha256": digest}))
    assert done.structured_content["item"]["record"]["state"] == "CONFIRMED"
    assert fakes["control"].jobs[record["job_id"]]["request"]["options"] == {"force": False, "recall_model": False,
                                                                             "no_ocr": False}
    cancelled = _run(_call(admin, "cancel_job", {"job_id": record["job_id"], "reason": "the human changed their mind"}))
    assert cancelled.structured_content["item"]["record"]["state"] == "CANCELLED"
    again = _run(_call(admin, "cancel_job", {"job_id": record["job_id"], "reason": "the human changed their mind"}))
    assert again.is_error and again.structured_content["error"]["code"] == "JOB_STATE_CONFLICT"


def test_read_server_is_configured_without_the_write_token(tmp_path):
    token = tmp_path / "mcp_token"
    token.write_text("client-token", encoding="utf-8")
    base = {"VKM_API_URL": "http://api:8000", "VKM_MCP_TOKEN_FILE": str(token)}
    with pytest.raises(ConfigError, match="never uses"):                                                   # MCP-09
        McpHttpConfig.from_env("read", {**base, "VKM_API_WRITE_TOKEN": "write-only"})
    cfg = McpHttpConfig.from_env("read", {**base, "VKM_API_TOKEN": "read-t", "VKM_API_WRITE_TOKEN": "write-t"})
    assert cfg.api_token == "read-t" and cfg.client_tokens == {"client-token": "mcp-read"}
    with pytest.raises(ConfigError):
        McpHttpConfig.from_env("read", {**base, "VKM_API_TOKEN": "read-t", "VKM_MCP_TOKEN_FILE": "",
                                        "VKM_MCP_TOKEN": "${VKM_MCP_TOKEN}"})                              # MCP-13
    admin = McpHttpConfig.from_env("admin", {"VKM_API_URL": "http://api:8000", "VKM_API_WRITE_TOKEN": "write-t",
                                             "VKM_MCP_ADMIN_TOKEN": "admin-client",
                                             "VKM_MCP_ALLOWED_HOSTS": "core.lan:8766, 127.0.0.1:*"})
    assert admin.api_token == "write-t" and admin.allowed_hosts == ["core.lan:8766", "127.0.0.1:*"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_streamable_http_wrapper_auth_hosts_and_session(stack):
    """MCP-10: real uvicorn on loopback (ephemeral port): 401 without token, 421 on a foreign Host, a session."""
    uvicorn = pytest.importorskip("uvicorn")
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    read, *_ = stack
    port = _free_port()
    config = McpHttpConfig(kind="read", api_url="http://unused", api_token="unused",
                           client_tokens={"client-token": "mcp-read"}, allowed_hosts=[f"127.0.0.1:{port}"])
    server = uvicorn.Server(uvicorn.Config(build_http_app(read, config), host="127.0.0.1", port=port,
                                           log_level="warning", log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        url = f"http://127.0.0.1:{port}/mcp"
        health = httpx.get(f"http://127.0.0.1:{port}/healthz")
        unauthorized = httpx.post(url, json={})
        foreign = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                             headers={"Authorization": "Bearer client-token", "Host": "evil.example",
                                      "Accept": "application/json, text/event-stream"})
        ping = httpx.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
                          headers={"Authorization": "Bearer client-token", "Host": f"127.0.0.1:{port}",
                                   "Accept": "application/json, text/event-stream"})
        assert [r.status_code for r in (health, unauthorized, foreign, ping)] == [200, 401, 421, 200]
        # no pooled connection outlives a response: a client behind a local proxy never sees the idle close
        assert all(r.headers.get("connection") == "close" for r in (health, unauthorized, foreign, ping))

        async def session():
            async with httpx2.AsyncClient(headers={"Authorization": "Bearer client-token"}) as http:
                async with Client(streamable_http_client(url, http_client=http)) as client:
                    tools = await client.list_tools()
                    result = await client.call_tool("get_source", {"source_id": "VKM-SRC-001"})
                    return {t.name for t in tools.tools}, result

        names, result = _run(session())
        assert names == READ_TOOLS and result.structured_content["ok"] is True
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_scenario_60_in_process(stack):
    """MCP-11: the scripted chain passes on the synthetic canon and every step carries valid envelopes."""
    read, *_ = stack

    async def go():
        async with Client(read) as client:
            return await Scenario(client).run("оседание земной поверхности мульда сдвижения")

    receipt = _run(go())
    assert receipt["verdict"] == "PASS", receipt["problems"]
    tools = [s["tool"] for s in receipt["steps"]]
    for name in ("search_text", "rerank_text", "get_page", "get_figure", "rerank_visual", "get_object",
                 "trace_document_provenance"):
        assert name in tools
    assert all(not s["envelope_problems"] for s in receipt["steps"])
    assert "оседание" not in json.dumps([s["result_sha256"] for s in receipt["steps"]])


def test_mcp_package_has_no_database_drivers():
    """The MCP layer is a thin adapter over the API: no DuckDB/Neo4j/OpenSearch/PostgreSQL imports."""
    forbidden = {"duckdb", "neo4j", "opensearchpy", "psycopg", "pyarrow"}
    hits = []
    for path in (ROOT / "src" / "vkm_corpus" / "mcp").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            hits += [f"{path.name}: {n}" for n in names if n.split(".")[0] in forbidden]
    assert hits == []
