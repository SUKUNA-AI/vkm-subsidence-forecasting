"""Score TOPIC_BENCHMARK_V1: raw answers (IDs) → rankings → per-query metrics → means, tests, acceptance.

usage: PYTHONPATH=src python benchmarks/topic_v1/scripts/score.py --runs <runs.jsonl> [--runs <dossier.jsonl>]
                                                                  [--out benchmarks/topic_v1/results_v1.json]
The outlines needed for the section metrics come with the runs (``kind = outline`` lines). Output holds IDs and
numbers only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from vkm_corpus.retrieval_lab import topic_bench as TB

BENCH = Path(__file__).resolve().parents[1]


def sha256_lf(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_runs(paths: list[str]):
    runs: dict[tuple[str, str], dict] = {}
    outlines: dict[str, list] = {}
    meta: list[dict] = []
    files = []
    for p in paths:
        path = Path(p).expanduser()
        n = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            n += 1
            if d["kind"] == "run":
                runs[(d["query_id"], d["system"])] = d
            elif d["kind"] == "outline":
                outlines[d["source_id"]] = d["sections"]
            else:
                meta.append(d)
        files.append({"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "lines": n})
    return runs, outlines, meta, files


def ranking_of(line: dict, index: TB.SectionIndex) -> TB.Ranking:
    system = line["system"]
    if system == "nav":
        return TB.ranking_from_nav({**(line.get("nav") or {}), "error": line.get("error")}, index)
    if system == "dossier":
        return TB.ranking_from_dossier(line, index)
    return TB.ranking_from_hits(line.get("hits") or [], index, error=line.get("error"))


def score(topics: list[TB.Topic], runs: dict, index: TB.SectionIndex) -> list[dict]:
    rows = []
    for t in topics:
        for q in t.queries:
            for system in TB.SYSTEMS:
                line = runs.get((q.query_id, system))
                if line is None:
                    continue
                r = ranking_of(line, index)
                row = {"query_id": q.query_id, "topic_id": t.topic_id, "system": system, "track": t.track,
                       "group": t.group, "variant": q.variant, "n_targets": len(t.targets),
                       "error": line.get("error"), **TB.query_metrics(t, r)}
                rows.append(row)
    return rows


def pct(xs: list[float], p: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))], 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", action="append", required=True)
    ap.add_argument("--out", default=str(BENCH / "results_v1.json"))
    args = ap.parse_args()
    spec = json.loads((BENCH / "metrics_spec_v1.json").read_text(encoding="utf-8"))
    set_path = BENCH / "topic_set_v1.jsonl"
    if sha256_lf(set_path) != spec["set"]["sha256"]:
        raise SystemExit("topic_set_v1.jsonl does not match the pre-registered sha256")
    topics = TB.load_set(set_path)
    runs, outlines, meta, files = load_runs(args.runs)
    index = TB.SectionIndex.from_outlines(outlines)
    rows = score(topics, runs, index)
    systems = [s for s in TB.SYSTEMS if any(r["system"] == s for r in rows)]
    summary = TB.aggregate(rows, ["system"])
    levels = spec["acceptance_levels"]["levels"]
    latency = {}
    flags = {}
    for s in systems:
        ms = [runs[k].get("api_ms") for k in runs if k[1] == s]
        latency[s] = {"p50_ms": pct(ms, 0.5), "p95_ms": pct(ms, 0.95)}
        flags[s] = {"errors": sum(1 for k in runs if k[1] == s and runs[k].get("error")),
                    "late_reported": dict(Counter(str(runs[k].get("late")) for k in runs if k[1] == s))
                    if s.startswith("hybrid") else None}
    comparisons = TB.compare_systems(rows, [c for c in TB.PRIMARY_COMPARISONS if c[0] in systems and c[1] in systems])
    by_track_tests = {}
    for track in TB.TRACKS:
        ids = [t.topic_id for t in topics if t.track == track]
        by_track_tests[track] = TB.compare_systems(
            rows, [c for c in TB.PRIMARY_COMPARISONS if c[0] in systems and c[1] in systems], topics=ids)
    cols = ["query_id", "system", "page_recall@10", "page_recall@20", "page_recall@50", "mrr@50", "source_recall@10",
            "section_hit@10", "section_recall@10", "section_pages@10", "success@10", "error"]
    out = {
        "benchmark": "TOPIC_BENCHMARK_V1",
        "spec_sha256": sha256_lf(BENCH / "metrics_spec_v1.json"),
        "set_sha256": spec["set"]["sha256"],
        "canonical_snapshot_id": spec["canonical_snapshot_id"],
        "runs": {"files": files, "meta": [{k: m.get(k) for k in ("kind", "harness_version", "canonical_snapshot_id",
                                                                  "nav_snapshot_id", "started_at", "finished_at",
                                                                  "calls", "errors", "retries", "systems", "n_queries")
                                           if m.get(k) is not None} for m in meta],
                 "outline_sources": len(outlines)},
        "systems": systems,
        "summary": summary,
        "by_track": TB.aggregate(rows, ["system", "track"]),
        "by_group": TB.aggregate([r for r in rows if r["track"] == "PROCESS"], ["system", "group"]),
        "by_variant": TB.aggregate(rows, ["system", "variant"]),
        "comparisons_primary": comparisons,
        "comparisons_by_track": by_track_tests,
        "acceptance": {s: TB.acceptance(summary.get(s, {}), levels) for s in systems},
        "latency": latency,
        "flags": flags,
        "per_query": {"columns": cols,
                      "rows": [[(round(r[c], 4) if isinstance(r[c], float) else r[c]) for c in cols] for r in rows]},
    }
    Path(args.out).write_bytes((json.dumps(out, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    for s in systems:
        m = summary[s]
        print(f"{s:14s} R@10 {m['page_recall@10']:.3f} R@20 {m['page_recall@20']:.3f} R@50 {m['page_recall@50']:.3f} "
              f"MRR {m['mrr@50']:.3f} SrcR@10 {m['source_recall@10']:.3f} SecHit@10 {m['section_hit@10']:.3f} "
              f"SecR@10 {m['section_recall@10']:.3f} pages@10sec {m['section_pages@10']:.0f} "
              f"S@10 {m['success@10']:.3f} | {latency[s]} {flags[s]}")
    for k, v in comparisons.items():
        print(k, v)


if __name__ == "__main__":
    main()
