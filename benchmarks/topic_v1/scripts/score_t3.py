"""TOPIC_BENCHMARK_V1 — re-scoring on the OCR v2 snapshot after the T3 delta labels (POOL_LABELING_T3.md).

usage: PYTHONPATH=src python benchmarks/topic_v1/scripts/score_t3.py [--out benchmarks/topic_v1/results_pool_t3.json]
Environment: ``T3_RUNS`` / ``T3_EXPERIMENTS`` as for pool_t3.py (raw answers, IDs only, outside git).
Rankings, truths and metrics are the frozen code (``score.ranking_of``, ``score_t1.p_topics``,
``topic_bench.query_metrics`` / ``compare_systems``), as in score_t2.py. Truths: V (catalogue), P_T1T2 (catalogue + T1 + T2
labels ≥ 2: the P of 574daaac), P (+ T3) and P3 (grade 3 only). A T2 or T3 ``t1_alias_links`` page becomes an alias of
the earlier unit it links to.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pool_t1 as PT  # noqa: E402  (also puts <repo>/src on sys.path)
import pool_t3 as P3  # noqa: E402
import score_t1 as ST  # noqa: E402
import score_t2 as S2  # noqa: E402  (T1 ∪ T2 labels; its pool_t2 module keeps the T2 constants)

from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402

BENCH = PT.BENCH
METRICS = S2.METRICS
TESTED = S2.TESTED
REFERENCE = S2.REFERENCE


def add_links(groups: list[dict], links: list[dict]) -> None:
    """A ``t1_alias_links`` page becomes an alias of the earlier unit whose pages it matched (a new group if none)."""
    for link in links:
        hit = next((g for g in groups if g["topic_id"] == link["topic_id"]
                    and set(link["t1_pages"]) & set(g["pages"])), None)
        if hit is None:
            hit = {"topic_id": link["topic_id"], "pages": list(link["t1_pages"]), "aliases": []}
            groups.append(hit)
        if link["page"] not in hit["aliases"]:
            hit["aliases"].append(link["page"])


def merged_labels() -> tuple[list[dict[str, str]], list[dict], list[dict[str, str]], list[dict], dict]:
    """(T1 ∪ T2 rows, groups) and (T1 ∪ T2 ∪ T3 rows, groups); no pair may carry two labels."""
    rows12, groups12, info = S2.merged_labels()
    rows3 = B.load_pooled_qrels(P3.P2.OUT_TSV)
    doc3 = json.loads(P3.P2.OUT_GROUPS.read_text(encoding="utf-8"))
    groups = [dict(g, pages=list(g["pages"]), aliases=list(g["aliases"])) for g in groups12]
    add_links(groups, doc3["t1_alias_links"])
    groups += doc3["groups"]
    seen = {(r["query_id"], r["doc_id"]) for r in rows12}
    both = [k for k in ((r["query_id"], r["doc_id"]) for r in rows3) if k in seen]
    if both:
        raise SystemExit(f"{len(both)} pairs are labelled in T3 and in T1/T2, e.g. {both[:3]}")
    info = {**info, "t3_rows": len(rows3), "t3_qrels_sha256": PT.sha256_file(P3.P2.OUT_TSV),
            "t3_groups_sha256": PT.sha256_file(P3.P2.OUT_GROUPS), "t3_alias_links": len(doc3["t1_alias_links"])}
    return rows12, groups12, rows12 + rows3, groups, info


def experiment_rankings(exp_path: Path) -> dict[str, dict[str, TB.Ranking]]:
    score = PT.load_module("topic_v1_score", BENCH / "scripts" / "score.py")
    runs, outlines, meta, _files = score.load_runs([str(exp_path)])
    snaps = sorted({m[k] for m in meta for k in ("canonical_snapshot_id", "nav_snapshot_id") if m.get(k)})
    if snaps != [P3.SNAPSHOT]:
        raise SystemExit(f"{exp_path.name}: snapshots {snaps}, expected {P3.SNAPSHOT}")
    index = TB.SectionIndex.from_outlines(outlines)
    out: dict[str, dict[str, TB.Ranking]] = {}
    for (qid, system), line in runs.items():
        out.setdefault(system, {})[qid] = score.ranking_of(line, index)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(BENCH / "results_pool_t3.json"))
    args = ap.parse_args()
    topics = PT.load_topics()
    runs_path, exp_path = P3.run_paths()
    rk, run_info = P3.P2.load_rankings(runs_path, exp_path, P3.P2.RUN_SYSTEMS)
    exp = experiment_rankings(exp_path)
    systems = {**{s: rk[s] for s in P3.P2.RUN_SYSTEMS}, **exp}
    rows1, groups1 = S2.P2.load_t1()
    rows12, groups12, rows, groups, label_info = merged_labels()
    truths = {"V": topics, "P_T1T2": ST.p_topics(topics, rows12, groups12, 2), "P": ST.p_topics(topics, rows, groups, 2),
              "P3": ST.p_topics(topics, rows, groups, 3)}
    judged = {"T1": ST.judged_pages(topics, rows1, groups1), "T1+T2": ST.judged_pages(topics, rows12, groups12),
              "T1+T2+T3": ST.judged_pages(topics, rows, groups)}
    qtopic = {q.query_id: t.topic_id for t in topics for q in t.queries}
    out = {"benchmark": "TOPIC_BENCHMARK_V1", "kind": "re-scoring after the T3 delta labels",
           "snapshot_id": P3.SNAPSHOT, "labels": label_info, "runs": run_info,
           "experiments_file": {"file": exp_path.name, "sha256": P3.P2.sha256_big(exp_path)},
           "pooled_in_t3": list(P3.P2.SYSTEMS), "targets": {}, "summary": {}, "judged@10": {}, "comparisons_P": {}}
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
        out["judged@10"][jname] = {s: S2.judged_at(r, qtopic, js) for s, r in systems.items()}
    Path(args.out).write_bytes((json.dumps(out, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    for tname in truths:
        print(f"== {tname} (targets {out['targets'][tname]})")
        for s, m in out["summary"][tname].items():
            print(f"  {s:20s} R@10 {m['page_recall@10']:.3f} R@20 {m['page_recall@20']:.3f} "
                  f"R@50 {m['page_recall@50']:.3f} MRR {m['mrr@50']:.3f} S@10 {m['success@10']:.3f}")
    for jname, d in out["judged@10"].items():
        print(f"judged@10 {jname}: " + ", ".join(f"{s} {v:.3f}" for s, v in d.items()))
    print("comparisons_P", json.dumps(out["comparisons_P"], ensure_ascii=False)[:800])


if __name__ == "__main__":
    main()
