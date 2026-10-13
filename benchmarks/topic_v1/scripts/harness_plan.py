"""Generic plan harness of the 13.10.2026 search experiments (PREREGISTRATION_EXPERIMENTS_2026-10-13.md): runs INSIDE
the VKM API container on CORE (stdlib only, read-only API calls), like ``harness_core.py``.

``run_plan.sh`` prepends a plan as ``PLAN_JSONL = r'''…'''`` and pipes the bundle to ``python -`` in the container. A
plan line is one request: ``{"query_id", "topic_id" (optional), "system", "route": "/v1/search" | "/v1/search/hybrid",
"request": {...}}`` — the request body is sent as is (query text included; texts never come back). The API token is
read from the file mounted in the container (``VKM_API_TOKEN_FILE``) and is never printed.

Output (stdout), the line format of ``harness_core.py``: a ``meta`` line, one ``kind = run`` line per plan line —
``hits`` = ``[{page_id, source_id, duplicates, copies}]`` in rank order (``duplicates``: the collapsed duplicate pages
of the hit; ``copies``: the G1 copies listed on the hit, page ids only), ``error``, ``api_ms`` and, for the hybrid
route, ids / statuses / counters of the stages (late, translation, graph, formulations, object_ref, routes) — then an
``end`` line. No page text, snippets, highlights, titles or formulation texts. ``score.load_runs`` and
``score.ranking_of`` (frozen) read the lines unchanged (copies ignored there; the 13.10 scorer adds them).

A preflight request per distinct set of request fields (limit 1) stops the run before the first measurement when the
API refuses a field (an image without it answers 400): a variant is never measured as the default in disguise.
Progress goes to stderr. Concurrency is 1 (the RX580 retrieval service is shared with production).
No ``from __future__`` import: the plan is prepended to this file (Python >= 3.10 in the API image).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HARNESS_VERSION = "plan-harness-20261013-3"
BASE = os.environ.get("VKM_BENCH_API", "http://127.0.0.1:8000")
PAGE_RE = re.compile(r"VKM-SRC-\d{3}:[a-z]\d{4}")
ROUTES = ("/v1/search", "/v1/search/hybrid")
STATS = {"calls": 0, "errors": 0, "retries": 0}


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


def _page_ids(value) -> list[str]:
    return PAGE_RE.findall(json.dumps(value or []))


def page_hits(payload: dict) -> list[dict]:
    """Ranked pages: page id, source, the collapsed duplicate pages and the G1 copies listed on the hit (ids only)."""
    out = []
    for it in payload.get("items") or []:
        env = it.get("envelope") or {}
        rec = it.get("record") or {}
        pid = env.get("page_id") or env.get("object_id")
        if not pid or not PAGE_RE.fullmatch(str(pid)):
            continue
        is_page = env.get("object_kind") in (None, "PAGE") and env.get("object_id") in (None, pid)
        dups = [p for p in dict.fromkeys(_page_ids(rec.get("duplicates"))) if p != pid] if is_page else []
        copies = ([p for p in dict.fromkeys(_page_ids([c.get("page_id") for c in rec.get("copies") or []
                                                        if isinstance(c, dict)])) if p != pid] if is_page else [])
        out.append({"page_id": pid, "source_id": env.get("source_id"), "duplicates": dups, "copies": copies})
    return out


def _status(stage):
    if isinstance(stage, dict):
        return stage.get("status")
    return stage if isinstance(stage, str) else None


def diagnostics(rec: dict) -> dict:
    """Ids, statuses and counters of the hybrid stages (no texts)."""
    stages = rec.get("stages") or {}
    out: dict = {"late": rec.get("late"), "late_candidates": rec.get("late_candidates"),
                 "candidates": rec.get("candidates"), "fused_total": rec.get("fused_total"), "route": rec.get("route"),
                 "server_ms": (rec.get("timings_ms") or {}).get("total"),
                 "translation_status": (rec.get("translation") or {}).get("status")
                 if isinstance(rec.get("translation"), dict) else None}
    g = stages.get("graph")
    out["graph"] = ({"requested": g.get("requested"), "status": g.get("status")} if isinstance(g, dict)
                    else (str(g)[:80] if g is not None else None))
    fs = stages.get("formulations")
    if isinstance(fs, dict):
        out["formulations"] = {"status": fs.get("status"), "answered": fs.get("answered"),
                               "duplicates_merged": fs.get("duplicates_merged"),
                               "runs": [{"n": r.get("n"), "origin": r.get("origin"), "status": r.get("status")}
                                        for r in fs.get("runs") or []]}
    oref = stages.get("object_ref")
    if isinstance(oref, dict):
        out["object_ref"] = {"n_refs": len(oref.get("refs") or []), "label_route": _status(oref.get("label_route")),
                             "entity_route": _status(oref.get("entity_route")),
                             "moved": oref.get("moved"), "flagged": oref.get("flagged")}
    out["routes"] = {k: _status(stages.get(k)) for k in ("bib_route", "visual_route") if k in stages}
    ar = stages.get("author_route")
    if isinstance(ar, dict):
        out["author_route"] = {"status": ar.get("status"), "in_head": ar.get("in_head"), "moved": ar.get("moved"),
                               "n_authors": len(ar.get("authors") or []), "promotion": ar.get("promotion"),
                               "promote_max": ar.get("promote_max"), "mode": ar.get("mode")}
    late = stages.get("late")
    if isinstance(late, dict):
        out["late_stage"] = {k: late.get(k) for k in ("status", "candidates", "scored", "query")
                             if k in late and not isinstance(late.get(k), (dict, list))}
        fusion = late.get("fusion")
        out["late_stage"]["fusion"] = fusion.get("mode") if isinstance(fusion, dict) else None
        lq = late.get("late_query")
        out["late_stage"]["late_query"] = ({"status": lq.get("status"), "best_counts": lq.get("best_counts")}
                                           if isinstance(lq, dict) else None)
    return out


def preflight(api: Api, plan: list[dict]) -> dict:
    """One request (limit 1) per distinct (route, request fields): every field must be accepted (else: stop)."""
    seen: dict[str, int] = {}
    for line in plan:
        req = line["request"]
        fields = sorted(k for k in req if k != "query")
        key = line["route"] + "|" + json.dumps({k: req[k] for k in fields if k not in ("formulations",)},
                                                sort_keys=True, ensure_ascii=False) + "|" + str("formulations" in req)
        if key in seen:
            continue
        probe = {**req, "query": "оседание земной поверхности", "limit": 1}
        if "formulations" in probe:
            probe["formulations"] = ["проверка"]
        code, body, _ms = api.call("POST", line["route"], probe, count_errors=False)
        seen[key] = code
        if code != 200:
            raise SystemExit(f"preflight: {line['route']} refused fields {fields} (HTTP {code} "
                             f"{(body.get('error') or {}).get('code')}): deploy the image with these fields first")
    return seen


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def load_plan(text: str) -> list[dict]:
    plan = [json.loads(line) for line in text.splitlines() if line.strip()]
    keys = set()
    for p in plan:
        if p.get("route") not in ROUTES:
            raise SystemExit(f"plan line {p.get('query_id')}: route must be one of {ROUTES}")
        if not isinstance(p.get("request"), dict) or not p["request"].get("query"):
            raise SystemExit(f"plan line {p.get('query_id')}: request with a query is required")
        k = (p["query_id"], p["system"])
        if k in keys:
            raise SystemExit(f"plan: duplicate (query_id, system) {k}")
        keys.add(k)
    return plan


def main(argv: list[str]) -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--no-preflight", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="first N plan lines only (smoke)")
    ap.add_argument("--label", default="", help="experiment label written into the meta line")
    args = ap.parse_args(argv)
    text = globals().get("PLAN_JSONL")
    if not text:
        raise SystemExit("PLAN_JSONL is not embedded (use run_plan.sh)")
    plan = load_plan(text)
    if args.limit:
        plan = plan[: args.limit]
    api = Api()
    code, st, _ = api.call("GET", "/v1/status")
    can = (((st.get("item") or {}).get("record") or st.get("status") or {}).get("canonical") or {})
    checked = {} if args.no_preflight else preflight(api, plan)
    systems = list(dict.fromkeys(p["system"] for p in plan))
    emit({"kind": "meta", "harness_version": HARNESS_VERSION, "label": args.label, "systems": systems,
          "n_lines": len(plan), "canonical_snapshot_id": can.get("snapshot_id"), "status_http": code,
          "preflight": checked, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "requests": {s: sorted({k for p in plan if p["system"] == s for k in p["request"] if k != "query"})
                       for s in systems}})
    for i, p in enumerate(plan, 1):
        line = {"kind": "run", "query_id": p["query_id"], "topic_id": p.get("topic_id"), "system": p["system"]}
        t0 = time.time()
        code, body, ms = api.call("POST", p["route"], p["request"])
        line.update(hits=page_hits(body), error=err_code(code, body), api_ms=ms,
                    warnings=sorted({w.get("code") for w in (body.get("meta") or {}).get("warnings") or []
                                     if isinstance(w, dict) and w.get("code")}))
        if p["route"] == "/v1/search/hybrid":
            line.update(diagnostics((body.get("item") or {}).get("record") or {}))
        line["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        emit(line)
        if i % 50 == 0 or i == len(plan):
            sys.stderr.write(f"lines {i}/{len(plan)} calls={STATS['calls']} errors={STATS['errors']}\n")
            sys.stderr.flush()
    emit({"kind": "end", "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **STATS})


if __name__ == "__main__":
    main(sys.argv[1:])
