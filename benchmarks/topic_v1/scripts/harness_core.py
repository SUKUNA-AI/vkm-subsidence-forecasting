"""TOPIC_BENCHMARK_V1 collector: runs INSIDE the VKM API container on CORE (stdlib only, read-only API calls).

``run_core.sh`` prepends the frozen set as ``SET_JSONL = r'''…'''`` and pipes the bundle to ``python -`` in the
container. The API token is read from the file mounted in the container (``VKM_API_TOKEN_FILE``) and is never printed.
Output (stdout): one JSON line per (query, system) with IDs only — page, section, unit and term ids, ranks, flags and
timings; no page text, snippets, highlights or titles — then one line per source outline (section ranges) and an end
line. Progress goes to stderr. Concurrency is 1 (the RX580 retrieval service is shared).

Systems: bm25, hybrid_late (server default late stage), hybrid_nolate, nav (search_sections + explore_concept and
the top units of its neighbours), dossier (adapter for the ``reconstruct_topic`` route; opt-in with --dossier-route).
No ``from __future__`` import: the set is prepended to this file (Python >= 3.10 in the API image).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HARNESS_VERSION = "topic_v1-harness-1"
BASE = os.environ.get("VKM_BENCH_API", "http://127.0.0.1:8000")
PAGE_RE = re.compile(r"VKM-SRC-\d{3}:[a-z]\d{4}")
SEC_RE = re.compile(r"SEC-[0-9a-f]{16}")
NAV_NEIGHBOURS = 5
STATS = {"calls": 0, "errors": 0, "retries": 0}


class Api:
    def __init__(self, base: str = BASE) -> None:
        self.base = base
        path = os.environ.get("VKM_API_TOKEN_FILE")
        self._auth = {}
        if path:
            with open(path, encoding="utf-8") as f:
                self._auth = {"Authorization": "Bearer " + f.read().strip()}

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 120.0,
             count_errors: bool = True) -> tuple[int, dict, float]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in (0, 1):
            req = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={**self._auth, "Content-Type": "application/json"})
            t0 = time.time()
            STATS["calls"] += 1
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, json.loads(r.read().decode("utf-8")), round((time.time() - t0) * 1000, 1)
            except urllib.error.HTTPError as e:
                code = e.code
                try:
                    payload = json.loads(e.read().decode("utf-8") or "{}")
                except Exception:  # noqa: BLE001
                    payload = {}
                if code < 500 or attempt:
                    STATS["errors"] += 1 if count_errors else 0
                    return code, payload, round((time.time() - t0) * 1000, 1)
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt:
                    STATS["errors"] += 1 if count_errors else 0
                    return 0, {}, round((time.time() - t0) * 1000, 1)
            STATS["retries"] += 1
            time.sleep(2.0)
        return 0, {}, 0.0


def err_code(code: int, payload: dict) -> str | None:
    if code == 200 and payload.get("ok", True):
        return None
    return f"HTTP{code}:" + str(((payload.get("error") or {}).get("code")) or "")


def page_hits(payload: dict) -> list[dict]:
    """Ranked pages of /v1/search and /v1/search/hybrid answers: page id + ids of the collapsed duplicate pages."""
    out = []
    for it in payload.get("items") or []:
        env = it.get("envelope") or {}
        rec = it.get("record") or {}
        pid = env.get("page_id") or env.get("object_id")
        if not pid or not PAGE_RE.fullmatch(str(pid)):
            continue
        dups = [p for p in PAGE_RE.findall(json.dumps(rec.get("duplicates") or [])) if p != pid]
        out.append({"page_id": pid, "source_id": env.get("source_id"), "duplicates": dups})
    return out


def extract_ids(obj) -> tuple[list[str], list[str]]:
    """Same rule as vkm_corpus.retrieval_lab.topic_bench.extract_ids (kept in sync by a test)."""
    pages: list[str] = []
    secs: list[str] = []

    def walk(x) -> None:
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, str):
            for p in PAGE_RE.findall(x):
                if p not in pages:
                    pages.append(p)
            for s in SEC_RE.findall(x):
                if s not in secs:
                    secs.append(s)

    walk(obj)
    return pages, secs


def units(rec: dict) -> list[dict]:
    return [{"unit_id": u.get("unit_id"), "unit_kind": u.get("unit_kind"), "section_id": u.get("section_id"),
             "source_id": u.get("source_id"), "page_ids": [p for p in (u.get("page_ids") or []) if isinstance(p, str)]}
            for u in rec.get("top_units") or []]


def run_nav(api: Api, text: str) -> tuple[dict, str | None, float]:
    q = urllib.parse.quote(text)
    ms = 0.0
    code, body, dt = api.call("GET", f"/v1/nav/sections?q={q}&limit=20")
    ms += dt
    errors = []
    rec = (body.get("item") or {}).get("record") or {}
    sections = [{"section_id": s.get("section_id"), "source_id": s.get("source_id"), "level": s.get("level"),
                 "page_start_id": s.get("page_start_id"), "page_end_id": s.get("page_end_id"), "score": s.get("score")}
                for s in rec.get("items") or []]
    if err_code(code, body):
        errors.append("sections " + err_code(code, body))
    code, body, dt = api.call("GET", f"/v1/nav/concept?term={q}&limit=20")
    ms += dt
    rec = (body.get("item") or {}).get("record") or {}
    if err_code(code, body):
        errors.append("concept " + err_code(code, body))
    match = rec.get("match") or {}
    focus = rec.get("focus") or {}
    neighbours = [{"term_id": n.get("term_id"), "score": n.get("score"), "n_units": n.get("n_units"),
                   "n_sources": n.get("n_sources")} for n in rec.get("neighbours") or []]
    nb_units = []
    for n in neighbours[:NAV_NEIGHBOURS]:
        if not n["term_id"]:
            continue
        code, body, dt = api.call("GET", "/v1/nav/concept?term=" + urllib.parse.quote(n["term_id"]) + "&limit=20")
        ms += dt
        if err_code(code, body):
            errors.append("neighbour " + err_code(code, body))
        nb_units.append(units((body.get("item") or {}).get("record") or {}))
    raw = {"sections": sections, "term": {"term_id": match.get("term_id"), "match": match.get("match"),
                                          "df_units": match.get("df_units"), "focus_term_id": focus.get("term_id")},
           "term_units": units(rec), "neighbours": neighbours, "neighbour_units": nb_units}
    return raw, ("; ".join(errors) or None), ms


def flatten_outline(nodes: list, out: list) -> None:
    for n in nodes or []:
        out.append({"section_id": n.get("section_id"), "level": n.get("level"),
                    "page_start_index": n.get("page_start_index"), "page_end_index": n.get("page_end_index"),
                    "page_start_id": n.get("page_start_id"), "page_end_id": n.get("page_end_id")})
        flatten_outline(n.get("children") or [], out)


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--systems", default="bm25,hybrid_late,hybrid_nolate,nav")
    ap.add_argument("--dossier-route", default=None, help="API route of reconstruct_topic, e.g. /v1/nav/topic")
    ap.add_argument("--dossier-method", default="GET", choices=["GET", "POST"])
    ap.add_argument("--dossier-param", default="q", help="query parameter (GET) or body field (POST) for the topic")
    ap.add_argument("--dossier-extra", default="{}", help="extra JSON params/body fields for the dossier call")
    ap.add_argument("--limit-queries", type=int, default=0)
    ap.add_argument("--only-topics", default="")
    ap.add_argument("--outlines", default="all", choices=["all", "seen", "none"])
    args = ap.parse_args(argv)
    systems = [s for s in args.systems.split(",") if s]
    if "dossier" in systems and not args.dossier_route:
        raise SystemExit("dossier needs --dossier-route")
    set_jsonl = globals().get("SET_JSONL")
    if not set_jsonl:
        raise SystemExit("SET_JSONL is not embedded (use run_core.sh)")
    topics = [json.loads(line) for line in set_jsonl.splitlines() if line.strip()]
    if args.only_topics:
        keep = set(args.only_topics.split(","))
        topics = [t for t in topics if t["topic_id"] in keep]
    queries = [(t["topic_id"], q) for t in topics for q in t["queries"]]
    if args.limit_queries:
        queries = queries[: args.limit_queries]
    api = Api()
    code, st, _ = api.call("GET", "/v1/status")
    can = (((st.get("item") or {}).get("record") or st.get("status") or {}).get("canonical") or {})
    emit({"kind": "meta", "harness_version": HARNESS_VERSION, "systems": systems, "n_queries": len(queries),
          "canonical_snapshot_id": can.get("snapshot_id"), "status_http": code,
          "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "args": {k: v for k, v in vars(args).items() if k != "dossier_extra"}})
    seen_sources = {s for t in topics for s in t.get("sources") or []}
    nav_snapshot = None
    extra = json.loads(args.dossier_extra)
    for i, (topic_id, q) in enumerate(queries, 1):
        text = q["text"]
        for system in systems:
            line = {"kind": "run", "query_id": q["query_id"], "topic_id": topic_id, "system": system}
            t0 = time.time()
            if system == "bm25":
                code, body, ms = api.call("POST", "/v1/search", {"query": text, "kinds": ["PAGE"], "limit": 50})
                line.update(hits=page_hits(body), error=err_code(code, body), api_ms=ms)
            elif system in ("hybrid_late", "hybrid_nolate"):
                req = {"query": text, "kinds": ["PAGE"], "limit": 50, "candidates": 100}
                if system == "hybrid_nolate":
                    req["late"] = False
                code, body, ms = api.call("POST", "/v1/search/hybrid", req)
                rec = (body.get("item") or {}).get("record") or {}
                line.update(hits=page_hits(body), error=err_code(code, body), api_ms=ms,
                            late=rec.get("late"), late_candidates=rec.get("late_candidates"),
                            candidates=rec.get("candidates"), server_ms=(rec.get("timings_ms") or {}).get("total"))
            elif system == "nav":
                raw, error, ms = run_nav(api, text)
                line.update(nav=raw, error=error, api_ms=ms)
            elif system == "dossier":
                if args.dossier_method == "GET":
                    params = {args.dossier_param: text, **extra}
                    code, body, ms = api.call("GET", args.dossier_route + "?" + urllib.parse.urlencode(params))
                else:
                    code, body, ms = api.call("POST", args.dossier_route, {args.dossier_param: text, **extra})
                pages, secs = extract_ids({k: v for k, v in body.items() if k != "meta"})
                line.update(page_ids=pages, section_ids=secs, error=err_code(code, body), api_ms=ms)
            else:
                line.update(error=f"unknown system {system}")
            line["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
            for h in line.get("hits") or []:
                seen_sources.add(h["page_id"].split(":")[0])
            emit(line)
        if i % 25 == 0 or i == len(queries):
            sys.stderr.write(f"queries {i}/{len(queries)} calls={STATS['calls']} errors={STATS['errors']}\n")
            sys.stderr.flush()
    if args.outlines != "none":
        sources = ([f"VKM-SRC-{n:03d}" for n in range(1, 260)] if args.outlines == "all" else sorted(seen_sources))
        n_ok = 0
        for s in sources:
            code, body, _ = api.call("GET", f"/v1/nav/outline/{s}", count_errors=False)
            if code != 200:
                continue
            rec = (body.get("item") or {}).get("record") or {}
            nav_snapshot = nav_snapshot or rec.get("nav_snapshot_id")
            flat: list = []
            flatten_outline(rec.get("sections") or [], flat)
            if flat:
                n_ok += 1
                emit({"kind": "outline", "source_id": s, "sections": flat})
        sys.stderr.write(f"outlines {n_ok}\n")
    emit({"kind": "end", "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "nav_snapshot_id": nav_snapshot, **STATS})


if __name__ == "__main__":
    main(sys.argv[1:])
