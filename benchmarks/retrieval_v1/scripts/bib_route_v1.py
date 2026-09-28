"""Bibliographic query route (agent L) evaluated on the V1 harness: the served scheme E stays for every query (CP-42:
BIB_ENTRY units never rank pages); a query that the rule detector ``vkm_corpus.search.intent.bibliographic_intent``
marks as bibliographic additionally searches the BIB_ENTRY units and the result is fused.

Routes (for detected queries only; everything else keeps E):
    late   — E's candidates, late page score over all units incl. BIB_ENTRY (= V1's ablation E+BIB)
    rrf    — RRF(E top-100, BIB channel top-100): the BIB channel ranks pages by their best BIB_ENTRY unit (late
             MaxSim over the BIB_ENTRY units; the service scans them in the token pack)
    cand   — the BIB channel (duplicate pages collapsed like the other legs) as a third RRF leg of the candidates,
             then the late stage over all units incl. BIB_ENTRY — the route implemented in ``vkm_corpus.search.hybrid``
    cand-nodup — as ``cand`` without collapsing duplicate pages in the BIB channel
    cand-cp42 — as ``cand`` but the late stage keeps CP-42 (separates the candidate effect from the scoring effect)
Every route is also computed without the detector (applied to all queries) to measure the cost of a false positive,
and on an approximation of the dense index after the stage-2 refresh (dense units that are still in the final
snapshot: the 5 147 pre-bibliography groups removed, the 1 138 new groups missing).

Reuses the V1 run (``run_v1``: units, the (units × queries) late matrix, the dense matrix, the query vectors, the
production BM25 top-100 read through the API) — nothing is re-encoded or re-queried. Checks that E recomputed here
equals V1's E per query. Metrics and paired randomization tests as in ``eval_v1`` (VERIFIED and VERIFIED + pooled).

Environment: ``J_V1`` (V1 work dir, read only), ``L_V1`` (this script's outputs). ``--export`` writes the public
``benchmarks/retrieval_v1/bib_route_v1.json`` (IDs and numbers only).
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_v1 as R  # noqa: E402  (J's V1 helpers: Units, page_ranking, page_scores, late_rescore, rrf_pages, …)
from eval_v1 import load_pooled  # noqa: E402

from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab import fusion, metrics  # noqa: E402
from vkm_corpus.search.intent import bibliographic_intent  # noqa: E402

J = Path(os.environ["J_V1"])
L = Path(os.environ["L_V1"])
L.mkdir(parents=True, exist_ok=True)
BENCH_DIR = Path("benchmarks/retrieval_v0")
V1_DIR = Path("benchmarks/retrieval_v1")
DEPTH = R.DEPTH
RRF_K = R.RRF_K
METRICS = ("ndcg@10", "recall@50", "mrr@10")
ROUTES = ("late", "rrf", "cand", "cand-nodup", "cand-cp42")


def pages_by_score(ps: dict[str, float], depth: int = DEPTH) -> list[tuple[str, float]]:
    return sorted(((p, s) for p, s in ps.items() if np.isfinite(s)), key=lambda x: (-x[1], x[0]))[:depth]


def collapse(pages: list[tuple[str, float]], dup: dict[str, str]) -> list[tuple[str, float]]:
    """First page of each duplicate group (the production BIB leg collapses like the BM25 and dense legs)."""
    seen, out = set(), []
    for p, s in pages:
        g = dup.get(p, p)
        if g not in seen:
            seen.add(g)
            out.append((p, s))
    return out


def main() -> None:
    bench = B.load_benchmark(BENCH_DIR)
    queries = list(bench.queries)
    qids = [q.query_id for q in queries]
    prep = json.load(open(J / "prepare.json", encoding="utf-8"))
    covered = set(prep["covered_queries"])
    units = R.Units()
    ids_run = json.load(open(J / "cache" / "units_ids.json", encoding="utf-8"))
    if ids_run != units.ids:
        raise SystemExit("unit order differs from the V1 late matrix")
    S_late = np.load(J / "cache" / "S_late.npy")
    if S_late.shape != (len(units.ids), len(qids)):
        raise SystemExit(f"late matrix {S_late.shape} != units × queries")
    fin, finb, didx = units.coll["final"], units.coll["final_bib"], units.coll["dense"]
    bib_idx = np.array(sorted(set(finb.tolist()) - set(fin.tolist())), dtype=np.int64)
    final_set = set(finb.tolist())
    didx_now = np.array([i for i in didx if i in final_set], dtype=np.int64)     # dense units still in the canon
    dq, _lq, _meta = R.load_queries_vectors()
    dmat = np.load(J / "cache" / "dense.npy")
    S_dense = dmat @ np.stack([dq[q] for q in qids]).T                           # (dense units, queries)
    del dmat
    dups = R.dup_groups()
    api = json.load(open(J / "out" / "api.json", encoding="utf-8"))["systems"]
    intent = {q.query_id: bibliographic_intent(q.text) for q in queries}
    runs: dict[str, dict[str, list[str]]] = defaultdict(dict)
    extra: dict[str, dict] = defaultdict(dict)
    for j, qid in enumerate(qids):
        if qid not in api.get("bm25", {}):
            continue
        a = [(h["id"], float(h.get("bm25_score") or 0.0)) for h in api["bm25"][qid]["hits"]][:DEPTH]
        col = np.full(len(units.ids), -np.inf, dtype=np.float32)
        col[didx] = S_dense[:, j]
        sl = np.asarray(S_late[:, j])
        ps_fin = R.page_scores(sl, fin[np.isfinite(sl[fin])], units)[0]
        ps_all = R.page_scores(sl, finb[np.isfinite(sl[finb])], units)[0]
        ps_bib, best_bib = R.page_scores(sl, bib_idx[np.isfinite(sl[bib_idx])], units)
        bibch_all = sorted(((p, s) for p, s in ps_bib.items() if np.isfinite(s)), key=lambda x: (-x[1], x[0]))
        bibch = collapse(bibch_all, dups)[:DEPTH]
        bibch_nodup = bibch_all[:DEPTH]
        for tag, dense_units in (("", didx), ("@refresh", didx_now)):
            b = R.page_ranking(col, dense_units, units, dup=dups)[0]
            fused = R.rrf_pages({"bm25": a, "dense": b[:DEPTH]})
            e = R.late_rescore(fused, ps_fin, DEPTH)[:DEPTH]
            cand = R.rrf_pages({"bm25": a, "dense": b[:DEPTH], "bib": bibch})
            cand_nodup = R.rrf_pages({"bm25": a, "dense": b[:DEPTH], "bib": bibch_nodup})
            routes = {"late": R.late_rescore(fused, ps_all, DEPTH)[:DEPTH],
                      "rrf": fusion.rrf({"E": e, "bib": bibch}, k=RRF_K)[:DEPTH],
                      "cand": R.late_rescore(cand, ps_all, DEPTH)[:DEPTH],
                      "cand-nodup": R.late_rescore(cand_nodup, ps_all, DEPTH)[:DEPTH],
                      "cand-cp42": R.late_rescore(cand, ps_fin, DEPTH)[:DEPTH]}
            runs[f"C{tag}"][qid] = [p for p, _ in fused[:DEPTH]]
            runs[f"E{tag}"][qid] = [p for p, _ in e]
            for name, lst in routes.items():
                pages = [p for p, _ in lst]
                runs[f"E{tag}+bib:{name}|ungated"][qid] = pages
                runs[f"E{tag}+bib:{name}"][qid] = pages if intent[qid].bibliographic else [p for p, _ in e]
        runs["BIB channel"][qid] = [p for p, _ in bibch]
        extra["bib_best_unit"][qid] = {p: best_bib[p] for p, _ in bibch[:10]}
    # verification: E recomputed here = V1's E (per query nDCG@10, both label sets)
    v1 = json.load(open(J / "out" / "results.json", encoding="utf-8"))
    verified = bench.judgments(level="PAGE")
    pooled = load_pooled(V1_DIR / "qrels_v1_pooled.tsv", bench)
    sets = {"verified": verified, "verified+pooled": B.merge_judgments(verified, pooled)}
    hn = bench.hard_negative_ids("PAGE")
    by_cat = {q.query_id: q.category for q in queries}
    track = {q.query_id: q.track for q in queries}
    subsets = {
        "bibliographic": [q for q in qids if q in covered and by_cat[q] == "bibliography"],
        "other_text": [q for q in qids if q in covered and track[q] == "text" and by_cat[q] != "bibliography"],
        "text": [q for q in qids if q in covered and track[q] == "text"],
        "visual": [q for q in qids if q in covered and track[q] == "visual"],
    }
    out: dict = {"benchmark": "RETRIEVAL_BENCHMARK_V1 / bibliographic route (agent L)",
                 "snapshot": prep["snapshot"]["snapshot_id"], "dense_units_snapshot": prep["dense_units_snapshot"],
                 "detector": {"fired": sorted(q for q in qids if intent[q].bibliographic),
                              "cues": {q: intent[q].as_dict() for q in qids if intent[q].bibliographic or intent[q].weak},
                              "recall_on_bibliographic": sum(intent[q].bibliographic for q in subsets["bibliographic"]),
                              "n_bibliographic": len(subsets["bibliographic"]),
                              "fired_outside_bibliographic": sorted(q for q in qids if intent[q].bibliographic
                                                                    and by_cat[q] != "bibliography")},
                 "subsets": {k: len(v) for k, v in subsets.items()}, "sets": {}, "checks": {}}
    for label, judged in sets.items():
        evals = {name: metrics.evaluate({q: r for q, r in per.items()}, judged, hard_negatives=hn,
                                        query_ids=[q for q in qids if q in covered])
                 for name, per in runs.items()}
        v1_e = v1["sets"][label]
        diffs = []
        for tr in ("text", "visual"):
            pq = v1_e[tr]["systems"]["E|rrf-prod>late@100"]["per_query"]
            for q, m in pq.items():
                diffs.append(abs(m["ndcg@10"] - evals["E"].per_query[q]["ndcg@10"]))
        out["checks"][f"E_equals_V1_E|{label}"] = {"max_abs_ndcg10_diff": float(max(diffs)), "n": len(diffs)}
        res = {}
        for sub, ids in subsets.items():
            rows = {}
            for name, ev in evals.items():
                row = {"overall": {m: round(v, 4) for m, v in ev.mean(ids).items()}}
                base = "E@refresh" if "@refresh" in name else "E"
                if name not in (base, "BIB channel") and not name.startswith("C"):
                    row["vs_E"] = {m: {k: (round(v, 4) if isinstance(v, float) else v)
                                       for k, v in metrics.compare(ev, evals[base], m, ids).items()} for m in METRICS}
                    ok = [q for q in ids if q in ev.per_query and q in evals[base].per_query]
                    a = {q: ev.per_query[q]["ndcg@10"] for q in ok}
                    b = {q: evals[base].per_query[q]["ndcg@10"] for q in ok}
                    row["wins_losses_ndcg10"] = [sum(a[q] > b[q] + 1e-9 for q in ok),
                                                 sum(a[q] < b[q] - 1e-9 for q in ok)]
                rows[name] = row
            res[sub] = rows
        out["sets"][label] = res
        print(f"== {label}")
        for sub, rows in res.items():
            print(f"  -- {sub} ({len(subsets[sub])})")
            for name, row in rows.items():
                o = row["overall"]
                vs = row.get("vs_E", {}).get("ndcg@10", {})
                print(f"    {name:34s} nDCG@10 {o['ndcg@10']:.3f} R@50 {o['recall@50']:.3f} MRR {o['mrr@10']:.3f} "
                      f"j@10 {o.get('judged@10', 0):.2f}"
                      + (f"  dE {vs['delta']:+.3f} p {vs['p_value']:.4f} w/l {row['wins_losses_ndcg10']}" if vs else ""))
    # unjudged pages in the top-10 of the routes on the detected queries (pool coverage of the new systems)
    judged_p = sets["verified+pooled"]
    unj = defaultdict(set)
    for name, per in runs.items():
        if "+bib:" in name and "|ungated" not in name:
            for q in out["detector"]["fired"]:
                for p in per.get(q, [])[:10]:
                    if p not in judged_p.get(q, {}):
                        unj[q].add(p)
    out["unjudged_top10_detected"] = {q: sorted(v) for q, v in sorted(unj.items())}
    out["unjudged_top10_detected_total"] = sum(len(v) for v in unj.values())
    json.dump({"out": out, "runs": {k: v for k, v in runs.items() if "|ungated" not in k}, "extra": extra},
              open(L / "bib_route.json", "w", encoding="utf-8"), ensure_ascii=False)
    print("checks", json.dumps(out["checks"]), "unjudged", out["unjudged_top10_detected_total"])
    if "--export" in sys.argv:
        with open(V1_DIR / "bib_route_v1.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
        print("exported", V1_DIR / "bib_route_v1.json")


if __name__ == "__main__":
    main()
