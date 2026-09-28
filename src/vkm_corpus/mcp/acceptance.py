"""Acceptance scenario §60 as a scripted MCP client (gate before the ``claude -p`` run; CP-19, K-21).

Chain, only through MCP tools of ``vkm-corpus``::

    search_text → candidate pages → rerank_text → get_page → figures (search_objects / page objects, get_figure)
      → rerank_visual → get_object → trace_document_provenance

Every step records the tool, its arguments, ``ok``/``is_error``, duration, the sha256 of the structured result and
envelope checks (all envelopes carry the fields §35 requires; automatic objects are AUTO_EXTRACTED_UNREVIEWED; a
visual result points at an image artifact). The receipt is JSON with numbers, IDs and hashes only — no document text.

Usage::

    python -m vkm_corpus.mcp.acceptance --url "$VKM_MCP_URL" --query "оседание земной поверхности" --out receipt.json
    python -m vkm_corpus.mcp.acceptance --dry-run --out receipt.json     # synthetic canon, in-process API + MCP

The token is read from ``VKM_MCP_TOKEN[_FILE]`` (never an argument).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA = "vkm.mcp_acceptance/1"
ENVELOPE_REQUIRED = ("envelope_version", "object_id", "object_kind", "review_status", "layer", "payload_form",
                     "provenance")
DOC_KINDS = {"PAGE", "BLOCK", "FIGURE", "TABLE", "FORMULA", "BIBLIOGRAPHY_ENTRY", "DOCUMENT"}
CHAIN = ("search_text", "rerank_text", "get_page", "get_figure", "rerank_visual", "get_object",
         "trace_document_provenance")


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def envelope_checks(body: dict[str, Any]) -> list[str]:
    """Problems of the envelopes in one ApiResponse (empty = fine)."""
    problems: list[str] = []
    items = ([body["item"]] if body.get("item") else []) + list(body.get("items") or [])
    for item in items:
        env = item.get("envelope") or {}
        oid = env.get("object_id", "?")
        for key in ENVELOPE_REQUIRED:
            if env.get(key) in (None, ""):
                problems.append(f"{oid}: envelope.{key} missing")
        if env.get("envelope_version") != "vkm.envelope/1":
            problems.append(f"{oid}: envelope_version")
        if env.get("layer") == "CANONICAL" and not env.get("canonical_snapshot_id"):
            problems.append(f"{oid}: CANONICAL without canonical_snapshot_id")
        if env.get("object_kind") in DOC_KINDS:
            if env.get("layer") == "CANONICAL" and env.get("review_status") != "AUTO_EXTRACTED_UNREVIEWED":
                problems.append(f"{oid}: automatic object is not AUTO_EXTRACTED_UNREVIEWED")
            for key in ("source_id", "origin"):
                if not env.get(key):
                    problems.append(f"{oid}: {key} missing")
            if not (env.get("provenance") or {}).get("processing_run_id"):
                problems.append(f"{oid}: provenance.processing_run_id missing")
            if env.get("object_kind") != "DOCUMENT" and not env.get("page_id"):
                problems.append(f"{oid}: page_id missing")
    return problems


class Scenario:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.steps: list[dict[str, Any]] = []

    async def call(self, tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        started = time.perf_counter()
        result = await self.client.call_tool(tool, args)
        body = result.structured_content or {}
        problems = envelope_checks(body) if body.get("ok") else []
        images = [c for c in result.content if getattr(c, "type", None) == "image"]
        self.steps.append({"step": len(self.steps) + 1, "tool": tool, "arguments": args,
                           "ok": bool(body.get("ok")), "is_error": bool(result.is_error),
                           "error_code": (body.get("error") or {}).get("code"),
                           "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                           "result_sha256": _sha(body), "n_items": len(body.get("items") or []),
                           "n_images": len(images), "envelope_problems": problems,
                           "request_id": (body.get("meta") or {}).get("request_id")})
        return body, result

    async def run(self, query: str, visual_query: str | None = None) -> dict[str, Any]:
        tools = await self.client.list_tools()
        names = sorted(t.name for t in tools.tools)
        tools_sha = _sha([{"name": t.name, "input_schema": t.input_schema} for t in sorted(tools.tools,
                                                                                           key=lambda t: t.name)])
        notes: list[str] = []
        body, _ = await self.call("search_text", {"query": query, "kinds": ["BLOCK", "PAGE"], "limit": 20})
        candidates: list[str] = []
        passages: list[dict[str, Any]] = []
        for item in body.get("items") or []:
            cand = (item.get("record") or {}).get("rerank_candidate") or {}
            cid = cand.get("candidate_id") or item["envelope"]["object_id"]
            if cid not in candidates and len(candidates) < 24:
                candidates.append(cid)
                if cand.get("object_ids") and cand["object_ids"] != [cid]:
                    passages.append({"candidate_id": cid, "object_ids": cand["object_ids"]})
        if not candidates:
            return self.receipt(names, tools_sha, "FAIL", ["search_text returned no candidates"])
        ranked, _ = await self.call("rerank_text", {"query": query, "candidate_ids": candidates,
                                                    "passages": passages})
        pages = [i["envelope"]["page_id"] for i in ranked.get("items") or [] if i["envelope"].get("page_id")]
        if not pages:
            return self.receipt(names, tools_sha, "FAIL", ["rerank_text produced no ranked pages"])
        figures: list[str] = []
        for page_id in pages[:5]:
            page, _ = await self.call("get_page", {"page_id": page_id, "include": ["objects"], "max_chars": 2000})
            objects = ((page.get("item") or {}).get("record") or {}).get("objects") or []
            figures += [o["object_id"] for o in objects if o.get("object_kind") == "FIGURE"]
            if figures:
                break
        if not figures:
            sources = sorted({p.split(":")[0] for p in pages})
            found, _ = await self.call("search_objects", {"kinds": ["FIGURE"], "source_ids": sources[:50],
                                                          "has_image": True, "limit": 8})
            figures = [i["envelope"]["object_id"] for i in found.get("items") or []]
            notes.append("figures found by search_objects in the sources of the ranked pages")
        if not figures:
            return self.receipt(names, tools_sha, "FAIL", ["no figures near the ranked pages"])
        _fig, fig_result = await self.call("get_figure", {"figure_id": figures[0], "include_image": True})
        visual_candidates = list(dict.fromkeys(figures))[:6] + [p for p in pages[:2]]
        visual, _ = await self.call("rerank_visual", {"query": visual_query or query,
                                                      "candidate_ids": visual_candidates[:8]})
        top = [i for i in visual.get("items") or []]
        if not top:
            return self.receipt(names, tools_sha, "FAIL", ["rerank_visual ranked nothing"])
        best = top[0]["envelope"]["object_id"]
        artifact_ok = all(str((i.get("record") or {}).get("image_artifact_id", "")).startswith("sha256:")
                          for i in top)
        await self.call("get_object", {"object_id": best})
        prov, _ = await self.call("trace_document_provenance", {"object_id": best})
        answers = ((prov.get("item") or {}).get("record") or {}).get("interpretability") or {}
        missing = [k for k in ("what", "original", "created_by", "native_or_ocr", "auto_or_reviewed",
                               "pipeline_version", "rebuildable", "projections") if answers.get(k) in (None, "", [])]
        problems = [f"step {s['step']} {s['tool']}: {s['error_code']}" for s in self.steps if not s["ok"]]
        problems += [f"step {s['step']} {s['tool']}: {p}" for s in self.steps for p in s["envelope_problems"]]
        if not artifact_ok:
            problems.append("rerank_visual results without image_artifact_id")
        if missing:
            problems.append(f"provenance answers missing: {missing}")
        if not any(s["tool"] == "get_figure" and s["n_images"] >= 1 for s in self.steps):
            problems.append("get_figure returned no ImageContent")
        order = [s["tool"] for s in self.steps if s["tool"] in CHAIN]
        if [t for t in CHAIN if t in order] != list(CHAIN):
            problems.append(f"chain incomplete: {order}")
        return self.receipt(names, tools_sha, "PASS" if not problems else "FAIL", problems, notes,
                            {"best_visual_object_id": best, "n_text_candidates": len(candidates),
                             "n_visual_candidates": len(visual_candidates[:8])})

    def receipt(self, tools: list[str], tools_sha: str, verdict: str, problems: list[str],
                notes: list[str] | None = None, summary: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"schema": RECEIPT_SCHEMA, "verdict": verdict, "created_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"), "tools": tools, "tools_sha256": tools_sha, "steps": self.steps,
                "problems": problems, "notes": notes or [], "summary": summary or {}}


async def run_http(url: str, token: str, query: str, visual_query: str | None) -> dict[str, Any]:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=400.0) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            receipt = await Scenario(client).run(query, visual_query)
            receipt["transport"] = "streamable-http"
            receipt["server_version"] = getattr(client, "server_version", None) or None
            return receipt


async def run_dry(query: str, visual_query: str | None) -> dict[str, Any]:
    """In-process: synthetic canon → VKM API (ASGI) → MCP read server (in memory) → scenario."""
    import httpx
    from mcp import Client

    from vkm_corpus.api.app import ApiConfig, create_app
    from vkm_corpus.api.fixtures import synthetic_service
    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server

    with tempfile.TemporaryDirectory(prefix="vkm-acceptance-") as tmp:
        service, _canon, _fakes = synthetic_service(Path(tmp))
        token = "dry-run-token"
        app = create_app(service, ApiConfig(read_tokens={token: "read"}))
        api = ApiClient("http://vkm-api", token, transport=httpx.ASGITransport(app=app))
        async with Client(build_read_server(api)) as client:
            receipt = await Scenario(client).run(query, visual_query)
        await api.aclose()
        receipt["transport"] = "in-process (synthetic canon, fake backends)"
        return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vkm_corpus.mcp.acceptance", description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="MCP endpoint of vkm-corpus (streamable HTTP), e.g. $VKM_MCP_URL")
    parser.add_argument("--dry-run", action="store_true", help="synthetic canon, in-process API and MCP")
    parser.add_argument("--query", default="оседание земной поверхности мульда сдвижения")
    parser.add_argument("--visual-query", default=None)
    parser.add_argument("--out", help="receipt JSON path (default: stdout)")
    args = parser.parse_args(argv)
    if args.dry_run:
        receipt = asyncio.run(run_dry(args.query, args.visual_query))
    else:
        from vkm_corpus.mcp.http import _secret

        if not args.url:
            parser.error("--url is required unless --dry-run")
        token = _secret(os.environ, "VKM_MCP_TOKEN")
        if not token:
            parser.error("set VKM_MCP_TOKEN or VKM_MCP_TOKEN_FILE")
        receipt = asyncio.run(run_http(args.url, token, args.query, args.visual_query))
    text = json.dumps(receipt, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    return 0 if receipt["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
