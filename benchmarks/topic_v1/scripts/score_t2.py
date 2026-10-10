"""TOPIC_BENCHMARK_V1 on snapshot 574daaac after the T2 delta labels (POOL_LABELING_T2.md): the 05.10 re-run
(``bm25``, ``hybrid_late``, ``hybrid_nolate``, ``nav``) and the 05.10 experiments (``baseline``, ``human3``, …) on
truth V (catalogue), P_T1 (catalogue + T1 labels ≥ 2, as published on 05.10), P (catalogue + T1 and T2 labels ≥ 2) and
P3 (grade 3 only), plus judged@10 (share of first-10 pages that carry any judgment) under T1 and T1 ∪ T2.

usage: PYTHONPATH=src python benchmarks/topic_v1/scripts/score_t2.py [--out benchmarks/topic_v1/results_pool_t2.json]
Environment: ``T2_RUNS`` / ``T2_EXPERIMENTS`` as for pool_t2.py (raw answers, IDs only, outside git).
Rankings, truths and metrics are the frozen code: ``score.ranking_of`` (via pool_t2 / score.load_runs),
``score_t1.p_topics``, ``score_t1.judged_pages``, ``topic_bench.query_metrics``. Only ``human3`` among the experiment
variants was pooled in T2; the other variants are scored on P with an incomplete pool (lower bound). Output: IDs and
numbers only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean

REPO = Path(__file__).resolve().parents[3]
BENCH = REPO / "benchmarks" / "topic_v1"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402

import pool_t1 as PT  # noqa: E402
import pool_t2 as P2  # noqa: E402
import score_t1 as ST  # noqa: E402

METRICS = ("page_recall@10", "page_recall@20", "page_recall@50", "mrr@50", "success@10", "source_recall@10",
           "capped_recall@10")
TESTED = ("page_recall@20", "page_recall@50", "mrr@50")
REFERENCE = "baseline"


def merged_labels() -> tuple[list[dict[str, str]], list[dict], dict]:
    """T1 ∪ T2 label rows and unit groups. A T2 ``t1_alias_links`` page becomes an alias of its T1 unit."""
    rows1, groups1 = P2.load_t1()
    rows2 = B.load_pooled_qrels(P2.OUT_TSV)
    doc2 = json.loads(P2.OUT_GROUPS.read_text(encoding="utf-8"))
    groups = [dict(g, pages=list(g["pages"]), aliases=list(g["aliases"])) for g in groups1]
    for link in doc2["t1_alias_links"]:
        hit = next((g for g in groups if g["topic_id"] == link["topic_id"]
                    and set(link["t1_pages"]) & set(g["pages"])), None)
        if hit is None:
            hit = {"topic_id": link["topic_id"], "pages": list(link["t1_pages"]), "aliases": []}
            groups.append(hit)
        if link["page"] not in hit["aliases"]:
            hit["aliases"].append(link["page"])
    groups += doc2["groups"]
    seen1 = {(r["query_id"], r["doc_id"]) for r in rows1}
    both = [k for k in ((r["query_id"], r["doc_id"]) for r in rows2) if k in seen1]
    if both:
        raise SystemExit(f"{len(both)} pairs are labelled in both T1 and T2, e.g. {both[:3]}")
    info = {"t1_rows": len(rows1), "t2_rows": len(rows2), "t1_qrels_sha256": PT.sha256_file(PT.OUT_TSV),
            "t2_qrels_sha256": PT.sha256_file(P2.OUT_TSV), "t2_groups_sha256": PT.sha256_file(P2.OUT_GROUPS),
            "t1_alias_links": len(doc2["t1_alias_links"])}
    return rows1 + rows2, groups, info


def experiment_rankings(exp_path: Path) -> dict[str, dict[str, TB.Ranking]]:
    """Every variant of the experiment file, ranked as score_experiments.py does (``ranking_of``)."""
    score = PT.load_module("topic_v1_score", BENCH / "scripts" / "score.py")
    runs, outlines, meta, _files = score.load_runs([str(exp_path)])
    snaps = sorted({m[k] for m in meta for k in ("canonical_snapshot_id", "nav_snapshot_id") if m.get(k)})
    if snaps != [P2.SNAPSHOT]:
        raise SystemExit(f"{exp_path.name}: snapshots {snaps}, expected {P2.SNAPSHOT}")
    index = TB.SectionIndex.from_outlines(outlines)
    out: dict[str, dict[str, TB.Ranking]] = {}
    for (qid, system), line in runs.items():
        out.setdefault(system, {})[qid] = score.ranking_of(line, index)
    return out


def judged_at(rankings: dict[str, TB.Ranking], qtopic: dict[str, str], judged: dict[str, frozenset[str]],
              k: int = 10) -> float:
    """Mean over queries of the share of the first ``k`` pages that are judged (the page or one of its aliases)."""
    vals = []
    for qid, r in rankings.items():
        top = list(zip(r.pages, r.aliases))[:k]
        if top and qid in qtopic:
            js = judged.get(qtopic[qid], frozenset())
            vals.append(sum(1 for p, al in top if p in js or (al & js)) / len(top))
    return round(mean(vals), 4) if vals else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(BENCH / "results_pool_t2.json"))
    args = ap.parse_args()
    topics = PT.load_topics()
    runs_path, exp_path = P2.run_paths()
    rk, run_info = P2.load_rankings(runs_path, exp_path, P2.RUN_SYSTEMS)
    exp = experiment_rankings(exp_path)
    systems = {**{s: rk[s] for s in P2.RUN_SYSTEMS}, **exp}
    rows1, groups1 = P2.load_t1()
    rows, groups, label_info = merged_labels()
    truths = {"V": topics, "P_T1": ST.p_topics(topics, rows1, groups1, 2), "P": ST.p_topics(topics, rows, groups, 2),
              "P3": ST.p_topics(topics, rows, groups, 3)}
    judged = {"T1": ST.judged_pages(topics, rows1, groups1), "T1+T2": ST.judged_pages(topics, rows, groups)}
    qtopic = {q.query_id: t.topic_id for t in topics for q in t.queries}
    out = {"benchmark": "TOPIC_BENCHMARK_V1", "kind": "re-scoring after the T2 delta labels",
           "snapshot_id": P2.SNAPSHOT, "labels": label_info, "runs": run_info,
           "experiments_file": {"file": exp_path.name, "sha256": P2.sha256_big(exp_path)},
           "pooled_in_t2": list(P2.SYSTEMS), "targets": {}, "summary": {}, "judged@10": {}, "comparisons_P": {}}
    for tname, ts in truths.items():
        out["targets"][tname] = sum(len(t.targets) for t in ts)
        qmap = {q.query_id: t for t in ts for q in t.queries}
        out["summary"][tname] = {}
        metric_rows = []
        for system, rankings in systems.items():
            per_q = []
            for qid, r in rankings.items():
                if qid in qmap:
                    m = TB.query_metrics(qmap[qid], r)
                    per_q.append(m)
                    metric_rows.append({"query_id": qid, "topic_id": qmap[qid].topic_id, "system": system, **m})
            out["summary"][tname][system] = {"n_queries": len(per_q),
                                             **{m: round(mean(x[m] for x in per_q), 4) for m in METRICS}}
        if tname == "P":
            pairs = [(s, REFERENCE) for s in exp if s != REFERENCE]
            out["comparisons_P"] = TB.compare_systems(metric_rows, pairs, TESTED)
    for jname, js in judged.items():
        out["judged@10"][jname] = {s: judged_at(r, qtopic, js) for s, r in systems.items()}
    Path(args.out).write_bytes((json.dumps(out, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    for tname in truths:
        print(f"== {tname} (targets {out['targets'][tname]})")
        for s, m in out["summary"][tname].items():
            print(f"  {s:20s} R@10 {m['page_recall@10']:.3f} R@20 {m['page_recall@20']:.3f} "
                  f"R@50 {m['page_recall@50']:.3f} MRR {m['mrr@50']:.3f} S@10 {m['success@10']:.3f}")
    for jname, d in out["judged@10"].items():
        print(f"judged@10 {jname}: " + ", ".join(f"{s} {v:.3f}" for s, v in d.items()))


if __name__ == "__main__":
    main()
