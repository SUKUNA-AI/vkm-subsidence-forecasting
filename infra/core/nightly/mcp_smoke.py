"""Nightly MCP smoke of every read tool of ``vkm-corpus`` (agent OPS, 29.09.2026; user decision A3).

Runs INSIDE the ``mcp`` container on CORE (``docker compose exec -T mcp python - < mcp_smoke.py``): the MCP SDK of the
platform image talks to the server of this very container over streamable HTTP (``http://127.0.0.1:8765/mcp``) with
the ``Host`` header the server accepts (first entry of ``VKM_MCP_ALLOWED_HOSTS`` — its DNS-rebinding guard) and the
client token of the mounted file ``VKM_MCP_TOKEN_FILE`` (never printed). It lists the tools, calls every read tool
once with small arguments — ids are harvested from earlier answers (a page, a figure with an image, a table, a
formula, a block, a section, a topic, a work, an artifact, a structured table, a digitized series) or fixed query
words — and prints one JSON report: per tool the status, the error code, the duration, the number of items/images and
the sha256 of the structured answer. No document text.

Status of a call: PASS (``ok``), WARN (the tool answered with a data error such as NOT_FOUND — the service works,
the smoke's input did not fit), FAIL (transport error, timeout, DEPENDENCY_*/INTERNAL errors, an exception), SKIP (no
input id could be harvested). Verdict: FAIL if a call failed or an expected tool is missing, WARN if a call warned or
was skipped, else PASS.

Options: --url, --deadline-s (stop after N s; remaining tools SKIP), --call-timeout-s, --dry-run (print the plan).
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

VERSION = "nightly-mcp-smoke-6"
EXPECTED_TOOLS = (
    "search_text", "search_hybrid", "retrieval_trace", "search_objects", "get_source", "get_work", "get_page",
    "get_page_image", "get_figure", "get_table", "get_formula", "get_object", "get_document_neighbors",
    "get_citations", "concept_paths", "graph_neighbourhood", "get_outline", "get_section", "search_sections",
    "get_formula_context", "find_formulas", "explore_concept", "find_topics", "get_topic", "similar_sections",
    "section_topics", "copies_of", "source_overlap", "find_parameters", "parameter_summary", "reconstruct_topic",
    "rerank_text", "rerank_visual", "get_processing_status", "trace_document_provenance", "get_artifact",
    "list_source_pages", "get_corpus_status", "translate_term",
    # structured tables and repeated figures/tables/formulas (agent G2, 29.09)
    "get_table_structured", "find_tables", "copies_of_object", "shared_formulas",
    # digitized chart series (agent FD2, 29.09)
    "find_figure_series", "get_figure_series",
    # versioned, access-filtered evidence (production data program, 01.10)
    "list_evidence", "get_evidence_record", "get_evidence_dependencies", "get_evidence_review_packet",
)
EVIDENCE_TOOLS = frozenset({"list_evidence", "get_evidence_record", "get_evidence_dependencies",
                            "get_evidence_review_packet"})
# service failures (the tool layer or a dependency is broken); other error codes are data errors of the input
FAIL_CODES = {"DEPENDENCY_UNAVAILABLE", "DEPENDENCY_TIMEOUT", "DEPENDENCY_ERROR", "INTERNAL", "INTERNAL_ERROR",
              "UNAUTHORIZED", "FORBIDDEN", "SNAPSHOT_UNAVAILABLE", "TIMEOUT", "EXCEPTION", "NO_ANSWER"}
QUERY = "оседание земной поверхности"
RE = {
    "source": re.compile(r"\bVKM-SRC-\d{3}\b(?!:)"),
    "page": re.compile(r"\bVKM-SRC-\d{3}:[prs]\d{4}\b(?!:)"),
    "figure": re.compile(r"\bVKM-SRC-\d{3}:(?:[prs]\d{4}|doc):f[0-9a-f]{12}\b"),
    "table": re.compile(r"\bVKM-SRC-\d{3}:(?:[prs]\d{4}|doc):t[0-9a-f]{12}\b"),
    "formula": re.compile(r"\bVKM-SRC-\d{3}:(?:[prs]\d{4}|doc):m[0-9a-f]{12}\b"),
    "block": re.compile(r"\bVKM-SRC-\d{3}:(?:[prs]\d{4}|doc):b[0-9a-f]{12}\b"),
    "work": re.compile(r"\bVKM-WRK-\d{3}\b"),
    "section": re.compile(r"\bSEC-[0-9a-f]{16}\b"),
    "topic": re.compile(r"\bTOP-[0-9a-f]{16}\b"),
    "nav_table": re.compile(r"\bTBL-[0-9a-f]{16}\b"),
    "artifact": re.compile(r"\bsha256:[0-9a-f]{64}\b"),
    "figure_series": re.compile(r"\bFS-[0-9a-f]{16}\b"),
}


def sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class Harvest:
    """Ids seen in answers, in order of appearance (first = most relevant)."""

    def __init__(self):
        self.ids = {k: [] for k in RE}
        self.ids["evidence_record"] = []
        self.image_figures = []
        self.evidence_journal = None             # production_controls.evidence_journal of get_corpus_status

    def add(self, body):
        text = json.dumps(body, ensure_ascii=False)
        for kind, rx in RE.items():
            for m in rx.findall(text):
                if m not in self.ids[kind]:
                    self.ids[kind].append(m)

    def first(self, kind):
        return self.ids[kind][0] if self.ids[kind] else None

    def add_status(self, body):
        """Record whether the API itself reports an evidence journal (CONFIGURED / NOT_PUBLISHED)."""
        stack, seen = [body], 0
        while stack and seen < 200:
            node = stack.pop(); seen += 1
            if isinstance(node, dict):
                controls = node.get("production_controls")
                if isinstance(controls, dict) and isinstance(controls.get("evidence_journal"), str):
                    self.evidence_journal = controls["evidence_journal"]
                    return
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    def add_evidence_page(self, body):
        """Use only actual permitted records, never an id embedded in quoted text or an error."""
        if not body.get("ok"):
            return
        for record in ((body.get("item") or {}).get("record") or {}).get("items") or []:
            record_id = record.get("record_id") if isinstance(record, dict) else None
            if isinstance(record_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,199}", record_id):
                if record_id not in self.ids["evidence_record"]:
                    self.ids["evidence_record"].append(record_id)


def plan():
    """(tool, arguments or a function of the harvest → arguments or None) in call order."""
    h = lambda kind: (lambda hv: hv.first(kind))  # noqa: E731
    return [
        ("get_corpus_status", {}),
        ("list_evidence", {"limit": 1}),
        ("get_evidence_record", lambda hv: hv.first("evidence_record") and
         {"record_id": hv.first("evidence_record")}),
        ("get_evidence_dependencies", lambda hv: hv.first("evidence_record") and
         {"record_id": hv.first("evidence_record"), "limit": 3}),
        ("get_evidence_review_packet", lambda hv: hv.first("evidence_record") and
         {"record_id": hv.first("evidence_record")}),
        ("search_text", {"query": QUERY, "kinds": ["PAGE", "BLOCK"], "limit": 10}),
        ("search_hybrid", {"query": "ползучесть каменной соли", "limit": 5}),
        ("retrieval_trace", {"query": "мульда сдвижения", "limit": 5}),
        ("search_objects", {"kinds": ["FIGURE"], "has_image": True, "limit": 5}),
        ("search_objects", {"kinds": ["TABLE"], "limit": 3}),
        ("search_objects", {"kinds": ["FORMULA"], "limit": 3}),
        ("get_source", lambda hv: hv.first("source") and {"source_id": hv.first("source")}),
        ("list_source_pages", lambda hv: hv.first("source") and {"source_id": hv.first("source"), "limit": 3}),
        ("get_processing_status", lambda hv: hv.first("source") and {"source_id": hv.first("source")}),
        ("get_outline", lambda hv: hv.first("source") and {"source_id": hv.first("source")}),
        ("source_overlap", lambda hv: hv.first("source") and {"source_id": hv.first("source"), "limit": 5}),
        ("get_work", lambda hv: hv.first("work") and {"work_id": hv.first("work")}),
        ("get_citations", lambda hv: hv.first("work") and {"work_id": hv.first("work"), "limit": 5}),
        ("get_page", lambda hv: hv.first("page") and {"page_id": hv.first("page"), "include": ["objects"],
                                                       "max_chars": 500}),
        ("get_page_image", lambda hv: hv.first("page") and {"page_id": hv.first("page"), "max_side": 256}),
        ("get_document_neighbors", lambda hv: hv.first("page") and {"object_id": hv.first("page"), "limit": 5}),
        ("copies_of", lambda hv: hv.first("page") and {"ref": hv.first("page"), "limit": 5}),
        ("get_figure", lambda hv: (hv.image_figures or hv.ids["figure"])[:1] and
         {"figure_id": (hv.image_figures or hv.ids["figure"])[0], "include_image": True, "max_side": 256}),
        ("get_table", lambda hv: hv.first("table") and {"table_id": hv.first("table"), "max_chars": 500}),
        ("get_formula", lambda hv: hv.first("formula") and {"formula_id": hv.first("formula")}),
        ("get_formula_context", lambda hv: hv.first("formula") and {"formula_id": hv.first("formula")}),
        ("get_object", lambda hv: (hv.first("block") or hv.first("figure")) and
         {"object_id": hv.first("block") or hv.first("figure"), "max_chars": 500}),
        ("trace_document_provenance", lambda hv: (hv.first("figure") or hv.first("block")) and
         {"object_id": hv.first("figure") or hv.first("block")}),
        ("get_artifact", lambda hv: hv.first("artifact") and {"artifact_id": hv.first("artifact")}),
        ("search_sections", {"query": "ползучесть", "limit": 5}),
        ("get_section", lambda hv: hv.first("section") and {"section_id": hv.first("section")}),
        ("similar_sections", lambda hv: hv.first("section") and {"section_id": hv.first("section"), "k": 3}),
        ("section_topics", lambda hv: hv.first("section") and {"section_id": hv.first("section")}),
        ("graph_neighbourhood", lambda hv: (hv.first("section") or hv.first("page")) and
         {"node_id": hv.first("section") or hv.first("page"), "limit": 10}),
        ("find_topics", {"terms": ["ползучесть"], "limit": 3}),
        ("get_topic", lambda hv: hv.first("topic") and {"topic_id": hv.first("topic")}),
        ("find_formulas", {"concept": "скорость ползучести", "limit": 5}),
        ("explore_concept", {"term": "ползучесть", "limit": 5}),
        ("concept_paths", {"term_a": "ползучесть", "term_b": "оседание", "max_len": 3, "limit": 2}),
        ("find_parameters", {"property": "модуль деформации", "limit": 5}),
        ("parameter_summary", {"property": "модуль деформации"}),
        ("translate_term", {"term": "ползучесть", "limit": 5}),
        ("find_tables", {"property": "модуль деформации", "limit": 3}),
        ("get_table_structured", lambda hv: (hv.first("nav_table") or hv.first("table")) and
         {"table_id": hv.first("nav_table") or hv.first("table"), "max_rows": 20, "max_chars": 1000}),
        ("copies_of_object", lambda hv: (hv.image_figures or hv.ids["figure"])[:1] and
         {"object_id": (hv.image_figures or hv.ids["figure"])[0], "limit": 5}),
        ("shared_formulas", lambda hv: hv.first("formula") and {"ref": hv.first("formula"), "limit": 5}),
        ("find_figure_series", {"text": "оседание", "limit": 3}),
        ("get_figure_series", lambda hv: hv.first("figure_series") and {"ref": hv.first("figure_series"),
                                                                         "max_points": 20}),
        ("reconstruct_topic", {"query": "механика закладки", "budget_chars": 2000}),
        ("rerank_text", lambda hv: hv.ids["page"][:2] and {"query": QUERY, "candidate_ids": hv.ids["page"][:5]}),
        ("rerank_visual", lambda hv: (hv.image_figures or hv.ids["figure"])[:1] and
         {"query": "схема мульды сдвижения", "candidate_ids": (hv.image_figures or hv.ids["figure"])[:1]}),
    ]


def classify(body, is_error, exc=None):
    if exc is not None:
        return "FAIL", exc
    if body.get("ok"):
        return "PASS", None
    code = (body.get("error") or {}).get("code") or ("NO_ANSWER" if not body else "UNKNOWN")
    return ("FAIL" if code in FAIL_CODES or code.startswith("DEPENDENCY") else "WARN"), code


async def run(client, deadline_s=600.0, call_timeout_s=240.0):
    started = time.monotonic()
    names, cursor = [], None
    for _page in range(20):                      # list_tools is paginated (next_cursor)
        res = await (client.list_tools(cursor=cursor) if cursor else client.list_tools())
        names += [t.name for t in res.tools]
        cursor = getattr(res, "next_cursor", None)
        if not cursor:
            break
    names = sorted(set(names))
    hv = Harvest()
    calls = []
    for tool, args in plan():
        entry = {"tool": tool}
        if tool not in names:
            entry.update(status="FAIL", error_code="TOOL_NOT_LISTED")
            calls.append(entry)
            continue
        if tool in EVIDENCE_TOOLS and hv.evidence_journal == "NOT_PUBLISHED":
            # The API reports no published evidence journal: these tools have nothing to serve yet.
            # Not a success (SKIP -> verdict WARN); once the journal is CONFIGURED a failure is a FAIL again.
            entry.update(status="SKIP", error_code="EVIDENCE_NOT_PUBLISHED")
            calls.append(entry)
            continue
        if callable(args):
            args = args(hv)
        if not args and args != {}:
            entry.update(status="SKIP", error_code="NO_INPUT_ID")
            calls.append(entry)
            continue
        if time.monotonic() - started > deadline_s:
            entry.update(status="SKIP", error_code="DEADLINE")
            calls.append(entry)
            continue
        t0 = time.perf_counter()
        body, is_error, n_images, exc = {}, True, 0, None
        try:
            result = await asyncio.wait_for(client.call_tool(tool, args), call_timeout_s)
            body = result.structured_content or {}
            is_error = bool(result.is_error)
            n_images = sum(1 for c in result.content if getattr(c, "type", None) == "image")
        except asyncio.TimeoutError:
            exc = "TIMEOUT"
        except Exception as e:  # noqa: BLE001 - any client/transport failure is a FAIL of this tool
            exc = f"EXCEPTION:{type(e).__name__}"
        status, code = classify(body, is_error, exc)
        hv.add(body)
        if tool == "get_corpus_status" and body.get("ok"):
            hv.add_status(body)
        if tool == "list_evidence":
            hv.add_evidence_page(body)
        if tool == "search_objects" and args.get("has_image") and body.get("ok"):
            for it in body.get("items") or []:
                oid = (it.get("envelope") or {}).get("object_id")
                if oid and oid not in hv.image_figures:
                    hv.image_figures.append(oid)
        entry.update(status=status, ok=bool(body.get("ok")), is_error=is_error, error_code=code,
                     ms=round((time.perf_counter() - t0) * 1000, 1), n_items=len(body.get("items") or []),
                     n_images=n_images, sha256=sha(body) if body else None,
                     # Evidence record ids are caller-defined and can themselves carry private labels.
                     args={k: v for k, v in args.items() if k not in ("query", "record_id")})
        calls.append(entry)
    return report(names, calls, started)


def report(names, calls, started):
    per_tool = {}
    for c in calls:                              # a tool called several times keeps its worst status
        prev = per_tool.get(c["tool"])
        order = {"PASS": 0, "SKIP": 1, "WARN": 2, "FAIL": 3}
        if prev is None or order[c["status"]] > order[prev]:
            per_tool[c["tool"]] = c["status"]
    missing = [t for t in EXPECTED_TOOLS if t not in names]
    for t in missing:
        per_tool[t] = "FAIL"
    summary = {s: sum(1 for v in per_tool.values() if v == s) for s in ("PASS", "WARN", "FAIL", "SKIP")}
    verdict = "FAIL" if summary["FAIL"] else ("WARN" if summary["WARN"] or summary["SKIP"] else "PASS")
    return {"schema": "vkm.mcp_smoke/1", "version": VERSION, "verdict": verdict,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seconds": round(time.monotonic() - started, 1), "tools_listed": len(names),
            "expected": len(EXPECTED_TOOLS), "missing_tools": missing,
            "unexpected_tools": sorted(t for t in names if t not in EXPECTED_TOOLS),
            "not_called": sorted(t for t in EXPECTED_TOOLS if t in names and t not in {c["tool"] for c in calls}),
            "summary": summary, "per_tool": dict(sorted(per_tool.items())), "calls": calls}


def mcp_url_and_host(env):
    url = env.get("VKM_NIGHTLY_MCP_URL") or "http://127.0.0.1:8765/mcp"
    hosts = [h.strip() for h in (env.get("VKM_MCP_ALLOWED_HOSTS") or "").split(",") if h.strip()]
    host = None
    if hosts:
        host = hosts[0]
        if host.endswith(":*"):
            host = host[:-2] + ":" + (url.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[-1] or "8765")
    return url, host


async def run_http(url, host, token, deadline_s, call_timeout_s):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"}
    if host:
        headers["Host"] = host
    async with httpx2.AsyncClient(headers=headers, timeout=call_timeout_s + 30) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            rep = await run(client, deadline_s, call_timeout_s)
            rep["transport"] = "streamable-http"
            return rep


def main(argv):
    ap = argparse.ArgumentParser(prog="mcp_smoke.py")
    ap.add_argument("--url", default=None)
    ap.add_argument("--deadline-s", type=float, default=600.0)
    ap.add_argument("--call-timeout-s", type=float, default=240.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    env = dict(os.environ)
    if args.url:
        env["VKM_NIGHTLY_MCP_URL"] = args.url
    url, host = mcp_url_and_host(env)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.dry_run:
        steps = [{"tool": t, "arguments": a if isinstance(a, dict) else "from earlier answers"} for t, a in plan()]
        out = {"schema": "vkm.mcp_smoke/1", "dry_run": True, "url": url, "host_header": bool(host),
               "expected": len(EXPECTED_TOOLS), "planned_tools": sorted({s["tool"] for s in steps}), "plan": steps}
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
        return 0
    token = None
    path = env.get("VKM_MCP_TOKEN_FILE")
    if path:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
    if not token:
        token = env.get("VKM_MCP_TOKEN")
    if not token:
        sys.stderr.write("no MCP client token (VKM_MCP_TOKEN_FILE)\n")
        return 2
    # loopback with the allowed Host header first; then the published address itself (the route a client takes)
    routes = [("loopback", url, host)] + ([("published", f"http://{host}/mcp", None)] if host else [])
    rep, errors = None, []
    for route, u, h in routes:
        try:
            rep = asyncio.run(run_http(u, h, token, args.deadline_s, args.call_timeout_s))
            rep["route"] = route
            break
        except Exception as e:  # noqa: BLE001 - connection-level failure: try the next route, then one FAIL report
            errors.append(f"{route}: {type(e).__name__}: {str(e)[:160]}")
    if rep is None:
        rep = {"schema": "vkm.mcp_smoke/1", "version": VERSION, "verdict": "FAIL", "tools_listed": 0,
               "expected": len(EXPECTED_TOOLS), "missing_tools": list(EXPECTED_TOOLS), "calls": [],
               "summary": {"PASS": 0, "WARN": 0, "FAIL": len(EXPECTED_TOOLS), "SKIP": 0}}
    if errors:
        rep["connection_errors"] = errors
    rep["started_at"] = started_at
    sys.stdout.write(json.dumps(rep, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    return 0 if rep["verdict"] != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
