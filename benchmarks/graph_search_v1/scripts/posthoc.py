"""GRAPH_SEARCH_V1 — analyses added AFTER the preregistered results were read (EXPLORATORY; no part of the decision).

1. G1 decomposition on topic_v1: E's own order with the NAV copies of every page listed as its duplicates (no page
   leaves) against E and G1 — how much of G1's gain is the copy credit and how much the freed places.
2. The combination G1 + G5 only (the two stages with a gain of their own) on the test split: the primary metric and
   the retrieval_v1 harm checks of the rule (copy-group form).
3. GC per track of topic_v1 (PROCESS, MODEL_FAMILY), test topics.
4. The mechanism of G3's retrieval_v1 R@50 loss: relevant (P, grade ≥ 2) pages that enter or leave the first 50 of the
   text queries.

Writes ``benchmarks/graph_search_v1/results_v1_posthoc.json``. Environment: as ``run.py``.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run as R  # noqa: E402
from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402
from vkm_corpus.search import graph_stages as G  # noqa: E402

OUT = R.OUT_DIR / "results_v1_posthoc.json"


def main() -> None:
    from engine import Engine

    sets = R.load_sets()
    runs = R.read_runs("runs.jsonl", "runs_gc.jsonl")
    finals = R.final_systems()
    for name, variant in finals.items():
        if name not in runs:
            runs[name] = runs[variant]
    maps = R.load_maps()
    P = G.GraphParams()
    split_t, split_v = sets["splits"]["topic_v1"], sets["splits"]["retrieval_v1"]
    test_topics = {t for t, s in split_t.items() if s == "test"}
    out: dict = {"status": "EXPLORATORY: added after the preregistered results were read; not part of the decision"}
    # ---- 1. copy credit without removal
    alias = {}
    for qid, r in runs["E"].items():
        copies = {}
        for p in r["order"]:
            cps = [q for q, share in (maps.coverage.get(p) or {}).items() if share >= P.copy_min_coverage]
            cps += [q for q, cov in maps.coverage.items() if (cov.get(p) or 0) >= P.copy_min_coverage]
            if cps:
                copies[p] = sorted(set(cps))
        alias[qid] = {**r, "copies": copies}
    runs["E+copies"] = alias
    rows = R.topic_rows(runs, sets["topics"], ["E", "E+copies", "G1"])
    blk = {}
    for s in ("E+copies", "G1"):
        blk[s] = {m: R.compare(R.topic_means(rows, s, m, test_topics), R.topic_means(rows, "E", m, test_topics))
                  for m in ("page_recall@50", "mrr@50")}
    blk["G1_vs_E+copies"] = {m: R.compare(R.topic_means(rows, "G1", m, test_topics),
                                          R.topic_means(rows, "E+copies", m, test_topics))
                             for m in ("page_recall@50", "mrr@50")}
    out["g1_decomposition_topic_test"] = blk
    # ---- 2. G1 + G5 only
    a = json.loads((R.work() / "stage_a.json").read_text(encoding="utf-8"))
    eng = Engine(encoders=False)
    eng.encode(sets["texts"].values())
    sel = json.loads((R.work() / "selection.json").read_text(encoding="utf-8"))
    ov = {**R.VARIANTS[sel["chosen"]["G1"]][1], **R.VARIANTS[sel["chosen"]["G5"]][1]}
    params = R.params_of(ov)
    g15 = {}
    for qid, text in sorted(sets["texts"].items()):
        st = eng.run_e(text)
        order, run, _ms = R.run_system(eng, maps, st, text, ("collapse", "topics"), params,
                                       a["expansions"]["plain"].get(text))
        g15[qid] = R.record_of(qid, "G1+G5", order, run, 0.0)
    runs["G1+G5"] = g15
    rows2 = R.topic_rows(runs, sets["topics"], ["E", "G1+G5"])
    prim = R.compare(R.topic_means(rows2, "G1+G5", "page_recall@50", test_topics),
                     R.topic_means(rows2, "E", "page_recall@50", test_topics))
    labels = R.v1_labels(sets)
    groups = R.copy_groups(maps, P.copy_min_coverage)
    harm = {}
    for track, metric in (("text", "ndcg@10"), ("text", "recall@50"), ("visual", "ndcg@10")):
        qids = [q.query_id for q in sets["v1"] if q.track == track and split_v.get(q.query_id) == "test"]
        pa = R.v1_eval(runs, "G1+G5", qids, labels["P"], labels["hn"], groups)
        pb = R.v1_eval(runs, "E", qids, labels["P"], labels["hn"], groups)
        c = R.compare({q: v[metric] for q, v in pa.items()}, {q: v[metric] for q, v in pb.items()})
        harm[f"groups|P|{track}|{metric}"] = {**c, "harm": c["delta"] < 0 and c["p_value"] < 0.05}
    top10_unlabelled = sum(1 for q in sets["v1"] for p in g15[q.query_id]["order"][:10]
                           if p not in labels["P"].get(q.query_id, {}))
    out["g1_g5_test"] = {"primary_page_recall@50": prim, "harm_checks": harm,
                         "unlabelled_in_top10": top10_unlabelled,
                         "means": {m: {s: round(statistics.fmean(R.topic_means(rows2, s, m, test_topics).values()), 6)
                                       for s in ("E", "G1+G5")} for m in ("page_recall@50", "page_recall@20",
                                                                           "mrr@50")}}
    # ---- 3. GC per track
    tracks = {t.topic_id: t.track for t in sets["topics"]}
    rows3 = R.topic_rows(runs, sets["topics"], ["E", "GC", "G1", "G5"])
    per_track = {}
    for track in ("PROCESS", "MODEL_FAMILY"):
        tt = {t for t in test_topics if tracks[t] == track}
        per_track[track] = {"n_topics": len(tt), **{s: R.compare(R.topic_means(rows3, s, "page_recall@50", tt),
                                                                 R.topic_means(rows3, "E", "page_recall@50", tt))
                                                    for s in ("GC", "G1", "G5")}}
    out["gc_per_track_test"] = per_track
    # ---- 4. G3 on retrieval_v1 R@50: relevant pages in / out of the first 50 (text, all queries)
    moved = Counter()
    for q in sets["v1"]:
        if q.track != "text":
            continue
        rel = {p for p, g in labels["P"].get(q.query_id, {}).items() if g >= 2}
        e50, g50 = runs["E"][q.query_id]["order"][:50], runs["G3"][q.query_id]["order"][:50]
        moved["left_top50"] += len(rel & (set(e50) - set(g50)))
        moved["entered_top50"] += len(rel & (set(g50) - set(e50)))
        moved["queries_changed"] += int(e50 != g50)
    out["g3_retrieval_v1_text_top50_relevant_moves"] = dict(moved)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
