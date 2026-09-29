"""Nightly scoring of TOPIC_BENCHMARK_V1 on the deployed hybrid search (agent OPS, 29.09.2026; user decision B1.5).

Runs INSIDE the ``vkm-nightly`` container on CORE (the platform image: ``vkm_corpus.retrieval_lab.topic_bench`` is
the pre-registered metric code of ``benchmarks/topic_v1/scripts/score.py``); the data root is mounted read-only at
/data. Input: the raw answers of ``harness_core.py --systems hybrid_late`` (IDs only), the frozen set and the metric
spec (sha256 checked against the spec), optionally ``results_v1.json`` (the registered run, for reference).
Output (stdout): one JSON object — means over the 351 queries, by track, the pre-registered acceptance levels,
errors, latency. It is a regression signal of the nightly checks, not a new benchmark result.

    python - --runs R.jsonl --set topic_set_v1.jsonl --spec metrics_spec_v1.json [--registered results_v1.json]
"""
import argparse
import hashlib
import json
import sys

SYSTEM = "hybrid_late"


def sha256_lf(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()


def load_runs(path):
    runs, outlines, meta = {}, {}, []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("kind") == "run":
                runs[(d["query_id"], d["system"])] = d
            elif d.get("kind") == "outline":
                outlines[d["source_id"]] = d["sections"]
            else:
                meta.append(d)
    return runs, outlines, meta


def pct(xs, p):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))], 1)


def score(TB, topics, runs, outlines, system=SYSTEM):
    index = TB.SectionIndex.from_outlines(outlines)
    rows = []
    for t in topics:
        for q in t.queries:
            line = runs.get((q.query_id, system))
            if line is None:
                continue
            r = TB.ranking_from_hits(line.get("hits") or [], index, error=line.get("error"))
            rows.append({"query_id": q.query_id, "topic_id": t.topic_id, "system": system, "track": t.track,
                         "group": t.group, "variant": q.variant, "error": line.get("error"),
                         **TB.query_metrics(t, r)})
    return rows


def main(argv):
    ap = argparse.ArgumentParser(prog="topic_score.py")
    ap.add_argument("--runs", required=True)
    ap.add_argument("--set", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--registered")
    args = ap.parse_args(argv)
    from vkm_corpus.retrieval_lab import topic_bench as TB

    with open(args.spec, encoding="utf-8") as fh:
        spec = json.load(fh)
    set_sha = sha256_lf(args.set)
    if set_sha != spec["set"]["sha256"]:
        raise SystemExit("topic_set_v1.jsonl does not match the pre-registered sha256")
    topics = TB.load_set(args.set)
    runs, outlines, meta = load_runs(args.runs)
    rows = score(TB, topics, runs, outlines)
    if not rows:
        raise SystemExit("no hybrid_late answers in the runs file")
    summary = TB.aggregate(rows, ["system"]).get(SYSTEM, {})
    by_track = {k.split("|", 1)[1]: v for k, v in TB.aggregate(rows, ["system", "track"]).items()}
    lines = [runs[k] for k in runs if k[1] == SYSTEM]
    registered = None
    if args.registered:
        with open(args.registered, encoding="utf-8") as fh:
            reg = json.load(fh)
        registered = (reg.get("summary") or {}).get(SYSTEM)
    head = next((m for m in meta if m.get("kind") == "meta"), {})
    end = next((m for m in meta if m.get("kind") == "end"), {})
    out = {"schema": "vkm.topic_v1_nightly/1", "system": SYSTEM, "set_sha256": set_sha,
           "canonical_snapshot_id": head.get("canonical_snapshot_id"), "nav_snapshot_id": end.get("nav_snapshot_id"),
           "harness_version": head.get("harness_version"), "n_queries": len(rows),
           "n_expected": sum(len(t.queries) for t in topics), "outline_sources": len(outlines),
           "errors": sum(1 for r in rows if r.get("error")), "calls": end.get("calls"), "api_errors": end.get("errors"),
           "summary": summary, "by_track": by_track,
           "acceptance": TB.acceptance(summary, spec["acceptance_levels"]["levels"]),
           "latency": {"p50_ms": pct([x.get("api_ms") for x in lines], 0.5),
                       "p95_ms": pct([x.get("api_ms") for x in lines], 0.95)},
           "registered": registered and {k: registered.get(k) for k in ("page_recall@10", "page_recall@20",
                                                                         "page_recall@50", "mrr@50",
                                                                         "source_recall@10", "success@10")},
           "note": "nightly regression signal of the deployed search on the frozen TOPIC_BENCHMARK_V1 set; not a "
                   "new pre-registered result"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
