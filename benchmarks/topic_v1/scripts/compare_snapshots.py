"""TOPIC_BENCHMARK_V1 — search before / after OCR v2 (post hoc, not pre-registered).

The re-run of 05.10 on ``snap-20260929T175107Z-574daaac`` and the re-run of 07.10 on the OCR v2 snapshot
``snap-20261007T103222Z-2d71e9e8`` (the same frozen set, harness and systems, plus the experiment variant ``human3``)
are scored against ONE truth: the catalogue (V) and the catalogue + T1 + T2 + T3 labels ≥ 2 (P). After T2 the first ten
pages of every 05.10 ranking are judged, after T3 those of every 07.10 ranking, so depth-10 metrics compare both sides
on fully judged pages; deeper metrics are reported too but rest on partly judged ranks 11–50 for both.

Paired over the 117 topics (topic means of the three queries): sign-flip randomization test, bootstrap CI, Holm over the
whole family (``topic_bench.compare_systems``). A second view splits the P targets by whether OCR v2 changed the page
text (``changed_pages.json``: text_sha256 differs between the two canonical DuckDBs).

usage: PYTHONPATH=src python benchmarks/topic_v1/scripts/compare_snapshots.py --changed <changed_pages.json>
       [--out benchmarks/topic_v1/results_ocrv2_comparison.json]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pool_t1 as PT  # noqa: E402  (also puts <repo>/src on sys.path)
import score_t1 as ST  # noqa: E402
import score_t2 as S2  # noqa: E402
import score_t3 as S3  # noqa: E402

from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402

REPO, BENCH = PT.REPO, PT.BENCH
OLD, NEW = "snap-20260929T175107Z-574daaac", "snap-20261007T103222Z-2d71e9e8"
FILES = {
    OLD: (REPO / "work" / "topic_v1_rerun_20261005" / "runs_v1_20261005.jsonl",
          REPO / "work" / "topic_v1_experiments_20261005" / "experiments_v1.jsonl"),
    NEW: (REPO / "work" / "topic_v1_rerun_20261007" / "runs_v1_20261007.jsonl",
          REPO / "work" / "topic_v1_experiments_20261007" / "experiments_v1.jsonl"),
}
SYSTEMS = ("hybrid_late", "hybrid_nolate", "bm25", "nav", "human3")
METRICS = ("page_recall@10", "capped_recall@10", "success@10", "mrr@50", "page_recall@20", "page_recall@50",
           "source_recall@10")
TESTED = ("page_recall@10", "success@10", "mrr@50", "page_recall@50")      # depth-10 first, two deeper ones


def rankings(snapshot: str) -> tuple[dict[str, dict[str, TB.Ranking]], list[dict]]:
    """The four re-run systems and ``human3`` of one snapshot, ranked by the frozen ``score.ranking_of``."""
    score = PT.load_module("topic_v1_score", BENCH / "scripts" / "score.py")
    out: dict[str, dict[str, TB.Ranking]] = {}
    info = []
    for path, wanted in zip(FILES[snapshot], (("hybrid_late", "hybrid_nolate", "bm25", "nav"), ("human3",))):
        runs, outlines, meta, files = score.load_runs([str(path)])
        snaps = sorted({m[k] for m in meta for k in ("canonical_snapshot_id", "nav_snapshot_id") if m.get(k)})
        if snaps != [snapshot]:
            raise SystemExit(f"{path.name}: snapshots {snaps}, expected {snapshot}")
        index = TB.SectionIndex.from_outlines(outlines)
        for (qid, system), line in runs.items():
            if system in wanted:
                out.setdefault(system, {})[qid] = score.ranking_of(line, index)
        info.append({"file": path.name, "sha256": files[0]["sha256"], "lines": files[0]["lines"], "systems": wanted})
    return out, info


def subset(topics: list[TB.Topic], keep) -> list[TB.Topic]:
    """Topics with only the targets ``keep(target)`` accepts; a topic left without targets is dropped."""
    out = []
    for t in topics:
        tg = tuple(x for x in t.targets if keep(x))
        if tg:
            out.append(dataclasses.replace(t, targets=tg))
    return out


def metric_rows(truth: list[TB.Topic], ranks: dict[str, dict[str, dict[str, TB.Ranking]]]) -> list[dict]:
    qmap = {q.query_id: t for t in truth for q in t.queries}
    rows = []
    for snap, systems in ranks.items():
        for system, rs in systems.items():
            for qid, r in rs.items():
                if qid in qmap:
                    rows.append({"query_id": qid, "topic_id": qmap[qid].topic_id, "system": f"{system}@{snap[-8:]}",
                                 **TB.query_metrics(qmap[qid], r)})
    return rows


def summarize(rows: list[dict]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for system in sorted({r["system"] for r in rows}):
        xs = [r for r in rows if r["system"] == system]
        out[system] = {"n_queries": len(xs), **{m: round(mean(x[m] for x in xs), 4) for m in METRICS}}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--changed", required=True, type=Path, help="changed_pages.json (outside git)")
    ap.add_argument("--out", default=str(BENCH / "results_ocrv2_comparison.json"))
    args = ap.parse_args()
    topics = PT.load_topics()
    ranks, info = {}, {}
    for snap in (OLD, NEW):
        ranks[snap], info[snap] = rankings(snap)
    _r12, _g12, rows, groups, label_info = S3.merged_labels()
    p_all = ST.p_topics(topics, rows, groups, 2)
    changed_doc = json.loads(args.changed.read_text(encoding="utf-8"))
    changed = frozenset(changed_doc["changed_text"])
    truths = {"V": topics, "P": p_all,
              "P_changed_pages": subset(p_all, lambda t: bool(t.pages & changed)),
              "P_unchanged_pages": subset(p_all, lambda t: not (t.pages & changed))}
    judged = ST.judged_pages(topics, rows, groups)
    qtopic = {q.query_id: t.topic_id for t in topics for q in t.queries}
    pairs = [(f"{s}@{NEW[-8:]}", f"{s}@{OLD[-8:]}") for s in SYSTEMS]
    out = {"benchmark": "TOPIC_BENCHMARK_V1", "kind": "post hoc: search before / after OCR v2, one truth",
           "old_snapshot": OLD, "new_snapshot": NEW, "runs": info, "labels": label_info,
           "changed_pages": {"rule": changed_doc["rule"], "n": len(changed)},
           "targets": {k: sum(len(t.targets) for t in v) for k, v in truths.items()},
           "topics": {k: len(v) for k, v in truths.items()},
           "judged@10_T1T2T3": {f"{s}@{snap[-8:]}": S2.judged_at(r, qtopic, judged)
                                for snap in (OLD, NEW) for s, r in ranks[snap].items()},
           "summary": {}, "comparisons": {}}
    for name, truth in truths.items():
        mrows = metric_rows(truth, ranks)
        out["summary"][name] = summarize(mrows)
        out["comparisons"][name] = TB.compare_systems(mrows, pairs, TESTED, topics=sorted({t.topic_id for t in truth}))
    Path(args.out).write_bytes((json.dumps(out, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    print("targets", out["targets"], "topics", out["topics"])
    print("judged@10", out["judged@10_T1T2T3"])
    for name in truths:
        print(f"== {name}")
        for s in SYSTEMS:
            a, b = out["summary"][name][f"{s}@{OLD[-8:]}"], out["summary"][name][f"{s}@{NEW[-8:]}"]
            print(f"  {s:14s} " + "  ".join(f"{m} {a[m]:.3f}→{b[m]:.3f}" for m in TESTED))
        for k, v in out["comparisons"][name].items():
            print(f"    {k}: Δ {v['delta']:+.4f} [{v['ci_lo']:+.4f}; {v['ci_hi']:+.4f}] p_holm {v['p_holm']}")


if __name__ == "__main__":
    main()
