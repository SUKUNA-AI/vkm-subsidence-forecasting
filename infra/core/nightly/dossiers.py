"""Nightly topic dossiers (agent OPS, 29.09.2026; user decision B1.5): ``reconstruct_topic`` for the 117 topics of
TOPIC_BENCHMARK_V1 — 72 processes PC-xx and 45 model families MM-* — runs INSIDE the VKM API container on CORE
(standard library only, read-only API calls, one request at a time).

``nightly_checks.sh`` prepends the frozen set (``benchmarks/topic_v1/topic_set_v1.jsonl``) as ``SET_JSONL = r'''…'''``
and pipes the bundle to ``python -`` in the container. The API read token is read from the file mounted in the
container (``VKM_API_TOKEN_FILE``) and is never printed. Each topic is asked once: its NAME variant as the query and
its two paraphrases (PARA1, PARA2) as ``paraphrase`` — the fusion of formulations that TOPIC_BENCHMARK_V1 measured as
the largest gain. Output (stdout): JSON lines — ``meta``, one ``dossier`` line per topic with the full API answer (the
host keeps it under ``derived/dossiers/<snapshot>/``), ``end``. Progress goes to stderr.

Options: --budget N (budget_chars, default 12000), --deadline-s N (stop starting new topics after N s),
--only-topics PC-01,MM-CREEP, --limit N, --dry-run (print the plan: topics, queries, paraphrases; no API call).
No ``from __future__`` import: the set is prepended to this file.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "nightly-dossiers-1"
BASE = os.environ.get("VKM_NIGHTLY_API", "http://127.0.0.1:8000")


def topics_of(set_jsonl):
    out = []
    for line in set_jsonl.splitlines():
        if not line.strip():
            continue
        t = json.loads(line)
        by = {q.get("variant"): q.get("text") for q in t.get("queries") or []}
        query = by.get("NAME") or t.get("title")
        paraphrases = [by[v] for v in ("PARA1", "PARA2") if by.get(v) and by[v] != query]
        out.append({"topic_id": t["topic_id"], "track": t.get("track"), "group": t.get("group"),
                    "title": t.get("title"), "query": query, "paraphrases": paraphrases})
    return out


def request_path(topic, budget):
    params = [("q", topic["query"]), ("budget", str(budget))] + [("paraphrase", p) for p in topic["paraphrases"]]
    return "/v1/topic?" + urllib.parse.urlencode(params)


def call(path, token, timeout=300.0):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    t0 = time.time()
    for attempt in (0, 1):
        req = urllib.request.Request(BASE + path, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8")), round((time.time() - t0) * 1000, 1)
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001
                body = {}
            if e.code < 500 or attempt:
                return e.code, body, round((time.time() - t0) * 1000, 1)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt:
                return 0, {"ok": False, "error": {"code": type(e).__name__}}, round((time.time() - t0) * 1000, 1)
        time.sleep(3.0)
    return 0, {}, round((time.time() - t0) * 1000, 1)


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def main(argv):
    ap = argparse.ArgumentParser(prog="dossiers.py")
    ap.add_argument("--budget", type=int, default=12000)
    ap.add_argument("--deadline-s", type=float, default=3300.0)
    ap.add_argument("--only-topics", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    set_jsonl = globals().get("SET_JSONL")
    if not set_jsonl:
        raise SystemExit("SET_JSONL is not embedded (nightly_checks.sh prepends the topic set)")
    topics = topics_of(set_jsonl)
    if args.only_topics:
        keep = set(args.only_topics.split(","))
        topics = [t for t in topics if t["topic_id"] in keep]
    if args.limit:
        topics = topics[: args.limit]
    started = time.time()
    emit({"kind": "meta", "version": VERSION, "topics": len(topics), "budget": args.budget, "dry_run": args.dry_run,
          "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    if args.dry_run:
        for t in topics:
            emit({"kind": "plan", **t, "path": request_path(t, args.budget)})
        emit({"kind": "end", "done": 0, "truncated": False})
        return 0
    token = None
    path = os.environ.get("VKM_API_TOKEN_FILE")
    if path:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
    done = errors = 0
    truncated = False
    for i, t in enumerate(topics, 1):
        if time.time() - started > args.deadline_s:
            truncated = True
            break
        code, body, ms = call(request_path(t, args.budget), token)
        ok = code == 200 and bool(body.get("ok"))
        errors += 0 if ok else 1
        done += 1
        emit({"kind": "dossier", **t, "http": code, "ok": ok, "ms": ms,
              "error_code": None if ok else ((body.get("error") or {}).get("code") or f"HTTP{code}"),
              "response": body if ok else None})
        if i % 10 == 0 or i == len(topics):
            sys.stderr.write(f"dossiers {i}/{len(topics)} errors={errors}\n")
            sys.stderr.flush()
    emit({"kind": "end", "done": done, "errors": errors, "truncated": truncated,
          "seconds": round(time.time() - started, 1),
          "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
