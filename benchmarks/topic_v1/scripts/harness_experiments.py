"""TOPIC_BENCHMARK_V1 experiments (post hoc, not pre-registered): the opt-in flags of the hybrid search on the frozen
set. Runs INSIDE the VKM API container (stdlib only, read-only API calls), like ``harness_core.py``.

``run_experiments.sh`` prepends the frozen set as ``SET_JSONL = r'''…'''`` and pipes the bundle to ``python -`` in
the container. The API token is read from the file mounted in the container (``VKM_API_TOKEN_FILE``) and is never
printed. Output (stdout): the line format of ``harness_core.py`` — one ``kind = run`` line per (query, variant) with
``system`` = the variant name and the ``hybrid_late`` fields (``hits`` = page ids + collapsed duplicates, ``error``,
``api_ms``, ``late``, ``late_candidates``, ``candidates``, ``server_ms``), so ``score.load_runs`` / ``score.ranking_of``
read it unchanged (``ranking_of`` sends every system it does not know to ``ranking_from_hits``); then the source
outlines and an end line. Extra fields hold ids, origins, statuses and counters only — no page text, snippets,
highlights, titles or formulation texts. Progress goes to stderr. Concurrency is 1 (the RX580 service is shared).

Variants (each is ``hybrid_late`` of ``harness_core.py`` — PAGE, limit 50, candidates 100, the server's late default —
plus the fields below):

* ``baseline`` — nothing more (the deployed default; compare with ``hybrid_late`` of the frozen runs);
* ``terms`` — ``expand=terms``: ≤ 3 formulations from the NAV term dictionary, fused by RRF;
* ``human3`` — ``formulations`` = the other two human formulations of the same topic (an oracle upper bound: it uses
  the set's paraphrases; validates the server-side fusion against the post-hoc RRF of RESULTS_V1 §0.5, R@50 ≈ 0.446);
* ``cap5`` — ``max_per_source=5``;
* ``pool200`` — ``candidates=200, late_candidates=200``; ``pool300`` — ``candidates=200, late_candidates=300``;
* ``terms+cap5``, ``terms+cap5+pool200`` — the combinations.

A preflight request per needed feature stops the run when the API image does not accept the field (an older image
answers 400), so a variant is never measured as the baseline in disguise.
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

HARNESS_VERSION = "topic_v1-experiments-1"
BASE = os.environ.get("VKM_BENCH_API", "http://127.0.0.1:8000")
PAGE_RE = re.compile(r"VKM-SRC-\d{3}:[a-z]\d{4}")
STATS = {"calls": 0, "errors": 0, "retries": 0}
BASE_REQUEST = {"kinds": ["PAGE"], "limit": 50, "candidates": 100}          # harness_core.py, system hybrid_late
HUMAN = object()                                                            # the topic's other human formulations
VARIANTS = {
    "baseline": {},
    "terms": {"expand": "terms"},
    "human3": {"formulations": HUMAN},
    "cap5": {"max_per_source": 5},
    "pool200": {"candidates": 200, "late_candidates": 200},
    "pool300": {"candidates": 200, "late_candidates": 300},
    "terms+cap5": {"expand": "terms", "max_per_source": 5},
    "terms+cap5+pool200": {"expand": "terms", "max_per_source": 5, "candidates": 200, "late_candidates": 200},
}
DEFAULT_VARIANTS = ",".join(VARIANTS)
# one request per field: an API image without the field answers 400 (extra fields are forbidden)
PREFLIGHT = {"expand": {"expand": "terms"}, "formulations": {"formulations": ["проверка"]},
             "max_per_source": {"max_per_source": 5}, "late_candidates": {"late_candidates": 300, "candidates": 200}}


class Api:
    def __init__(self, base: str = BASE) -> None:
        self.base = base
        path = os.environ.get("VKM_API_TOKEN_FILE")
        self._auth = {}
        if path:
            with open(path, encoding="utf-8") as f:
                self._auth = {"Authorization": "Bearer " + f.read().strip()}

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 180.0,
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
    """Ranked pages of a /v1/search/hybrid answer: page id + ids of the collapsed duplicate pages (as harness_core)."""
    out = []
    for it in payload.get("items") or []:
        env = it.get("envelope") or {}
        rec = it.get("record") or {}
        pid = env.get("page_id") or env.get("object_id")
        if not pid or not PAGE_RE.fullmatch(str(pid)):
            continue
        is_page = env.get("object_kind") in (None, "PAGE") and env.get("object_id") in (None, pid)
        dups = [p for p in PAGE_RE.findall(json.dumps(rec.get("duplicates") or [])) if p != pid] if is_page else []
        out.append({"page_id": pid, "source_id": env.get("source_id"), "duplicates": dups})
    return out


def request_of(variant: str, text: str, others: list[str]) -> dict:
    req = {"query": text, **BASE_REQUEST}
    for k, v in VARIANTS[variant].items():
        req[k] = list(others) if v is HUMAN else v
    if req.get("formulations") == []:
        req.pop("formulations")                    # a topic with one formulation: nothing to fuse
    return req


def diagnostics(rec: dict) -> dict:
    """Ids, origins, statuses and counters of the opt-in stages (no texts)."""
    out: dict = {}
    stages = rec.get("stages") or {}
    fs = stages.get("formulations")
    if isinstance(fs, dict):
        out["formulations"] = {"status": fs.get("status"), "answered": fs.get("answered"),
                               "duplicates_merged": fs.get("duplicates_merged"),
                               "runs": [{"n": r.get("n"), "origin": r.get("origin"), "status": r.get("status"),
                                         "ms": r.get("ms"), "error": (r.get("error") or {}).get("code")}
                                        for r in fs.get("runs") or []]}
    plan = rec.get("formulations")
    if isinstance(plan, dict):
        out["formulation_plan"] = {"sent": [f.get("origin") for f in plan.get("sent") or []],
                                   "skipped": [[s.get("origin"), s.get("reason")] for s in plan.get("skipped") or []],
                                   "terms_status": (plan.get("terms") or {}).get("status"),
                                   "terms_parts": {k: (v or {}).get("status")
                                                   for k, v in ((plan.get("terms") or {}).get("parts") or {}).items()}}
    cap = stages.get("source_cap")
    if isinstance(cap, dict):
        out["source_cap"] = {k: cap.get(k) for k in ("max_per_source", "over_cap", "moved", "sources_over_cap")}
    late = stages.get("late")
    if isinstance(late, dict):
        out["late_pool"] = {"candidates": late.get("candidates"), "scored": late.get("scored")}
    out["fused_total"] = rec.get("fused_total")
    return out


def preflight(api: Api, variants: list[str]) -> dict:
    """Every opt-in field the variants use must be accepted by the deployed API (else: stop)."""
    needed = set()
    for v in variants:
        needed |= set(VARIANTS[v]) & (set(PREFLIGHT) - {"late_candidates"})
        if VARIANTS[v].get("late_candidates", 0) > 200:            # ≤ 200 is accepted by every image
            needed.add("late_candidates")
    out = {}
    for field in sorted(needed):
        code, body, _ms = api.call("POST", "/v1/search/hybrid", {"query": "оседание", "kinds": ["PAGE"], "limit": 1,
                                                                 **PREFLIGHT[field]}, count_errors=False)
        out[field] = code
        if code != 200:
            raise SystemExit(f"preflight: the API does not accept {field} (HTTP {code} "
                             f"{(body.get('error') or {}).get('code')}): deploy the image with the opt-in flags first")
    return out


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
    ap.add_argument("--variants", default=DEFAULT_VARIANTS, help=f"comma list of {', '.join(VARIANTS)}")
    ap.add_argument("--limit-queries", type=int, default=0)
    ap.add_argument("--only-topics", default="")
    ap.add_argument("--outlines", default="all", choices=["all", "seen", "none"])
    ap.add_argument("--no-preflight", action="store_true")
    args = ap.parse_args(argv)
    variants = [v for v in args.variants.split(",") if v]
    unknown = [v for v in variants if v not in VARIANTS]
    if unknown:
        raise SystemExit(f"unknown variants {unknown}; known: {', '.join(VARIANTS)}")
    set_jsonl = globals().get("SET_JSONL")
    if not set_jsonl:
        raise SystemExit("SET_JSONL is not embedded (use run_experiments.sh)")
    topics = [json.loads(line) for line in set_jsonl.splitlines() if line.strip()]
    if args.only_topics:
        keep = set(args.only_topics.split(","))
        topics = [t for t in topics if t["topic_id"] in keep]
    queries = [(t, q) for t in topics for q in t["queries"]]
    if args.limit_queries:
        queries = queries[: args.limit_queries]
    api = Api()
    code, st, _ = api.call("GET", "/v1/status")
    can = (((st.get("item") or {}).get("record") or st.get("status") or {}).get("canonical") or {})
    checked = {} if args.no_preflight else preflight(api, variants)
    emit({"kind": "meta", "harness_version": HARNESS_VERSION, "systems": variants, "n_queries": len(queries),
          "canonical_snapshot_id": can.get("snapshot_id"), "status_http": code, "preflight": checked,
          "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "args": vars(args),
          "requests": {v: {k: ("<other human formulations>" if x is HUMAN else x) for k, x in VARIANTS[v].items()}
                       for v in variants}, "base_request": BASE_REQUEST})
    seen_sources = {s for t in topics for s in t.get("sources") or []}
    nav_snapshot = None
    for i, (topic, q) in enumerate(queries, 1):
        others = [x["text"] for x in topic["queries"] if x["query_id"] != q["query_id"]]
        for variant in variants:
            line = {"kind": "run", "query_id": q["query_id"], "topic_id": topic["topic_id"], "system": variant}
            t0 = time.time()
            code, body, ms = api.call("POST", "/v1/search/hybrid", request_of(variant, q["text"], others))
            rec = (body.get("item") or {}).get("record") or {}
            line.update(hits=page_hits(body), error=err_code(code, body), api_ms=ms, late=rec.get("late"),
                        late_candidates=rec.get("late_candidates"), candidates=rec.get("candidates"),
                        server_ms=(rec.get("timings_ms") or {}).get("total"), **diagnostics(rec))
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
