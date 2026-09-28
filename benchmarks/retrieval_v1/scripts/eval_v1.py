"""V1 evaluation: page-level metrics of every system on VERIFIED qrels and on VERIFIED + pooled labels
(``qrels_v1_pooled.tsv``, label source ``LLM_AGENT_V1``), slices, categories, test split, paired randomization tests
(vs the production BM25 and vs the production hybrid scheme), Holm correction, reranker statistics and latency.

Environment: ``J_V1`` (``out/rankings.json``, ``out/rerank.json``, ``out/api.json``, ``prepare.json``). Writes
``$J_V1/out/results.json`` (full, git-ignored) and, with ``--export``, the public ``results_v1.json`` (IDs and numbers
only) to ``benchmarks/retrieval_v1/``.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from vkm_corpus.retrieval_lab import bench as B
from vkm_corpus.retrieval_lab import metrics

V1 = Path(os.environ["J_V1"])
OUT = V1 / "out"
BENCH_DIR = Path("benchmarks/retrieval_v0")
V1_DIR = Path("benchmarks/retrieval_v1")
BASE_BM25 = "A|bm25-os"
BASE_PROD = "C|rrf-prod"
SIG_METRICS = ("ndcg@10", "recall@50", "mrr@10")


def load_pooled(path: Path, bench) -> dict[str, dict[str, int]]:
    if not path.is_file():
        return {}
    rows = B.load_pooled_qrels(path)
    problems = B.validate_pooled_qrels(rows, bench)
    if problems:
        raise SystemExit("pooled labels invalid:\n" + "\n".join(problems[:20]))
    return B.pooled_judgments(rows)


API_SYSTEMS = {"hybrid_late": "E|hybrid-late-api", "bm25_recheck": "A|bm25-os@recheck"}

# Runs that did not finish because a backend went away: reported with the reason and never evaluated (a partial run
# would be compared on a different query set). Empty since the rerun after the outage below.
NOT_RUN: dict[tuple[str, str], str] = {}
INCIDENTS = [
    {"what": "EDGE text rerank backend unavailable: gateway /v1/rerank/text returned HTTP 503",
     "window_utc": "2026-09-28T17:40:22Z .. 2026-09-28T17:44:36Z (last call OK 17:40:18Z; the container was restarted "
                   "by the user at 17:44:36Z)",
     "client": "the rerank job retried every 10 s (26 attempts, all HTTP 503) and was stopped at 17:44:40Z; J-V1 did "
               "not restore or work around the backend",
     "affected": "R|E|rrf-prod>late@100>rerank@12 / visual: 20 of 42 queries were done before the outage",
     "rerun": "after the restart: 2 of the 20 earlier queries were re-run first (V-FIELD-001, V-FIELD-002: identical "
              "order), then the other 22 queries (calls 17:50:11Z .. 17:54:08Z); the run is complete (42 of 42)",
     "not_affected": "rerank@8 text and visual (finished by 17:36:17Z); rerank@12 text (finished before the visual run)",
     "transient": "ConnectError at 17:29:36Z and 17:31:56Z, both retried successfully"},
]


def load_rankings() -> tuple[dict[str, dict[str, list[str]]], dict, dict]:
    first = json.load(open(OUT / "rankings.json", encoding="utf-8"))
    rankings = {n: {q: [p for p, _ in lst] for q, lst in per.items()} for n, per in first["rankings"].items()}
    # API systems collected after the ``first`` stage (served results, read as is; the first stage is not re-run so
    # that the pooled rankings stay byte-identical)
    if (OUT / "api.json").is_file():
        api = json.load(open(OUT / "api.json", encoding="utf-8"))["systems"]
        for key, name in API_SYSTEMS.items():
            if key in api and not rankings.get(name):
                rankings[name] = {q: [h["id"] for h in v["hits"]] for q, v in api[key].items()}
    meta = {}
    for extra in ("rerank.json", "visual.json"):
        if (OUT / extra).is_file():
            rr = json.load(open(OUT / extra, encoding="utf-8"))
            for n, per in rr["rankings"].items():
                rankings[n] = {q: [p for p, _ in lst] for q, lst in per.items()}
            meta.update(rr["meta"])
    return rankings, meta, first


def rerank_stats(meta: dict, qids: list[str]) -> dict:
    ok = [m for q, m in meta.items() if q in qids and m.get("status") == 200]
    lat = [m["latency_s"] for m in ok]
    return {"calls_ok": len(ok), "errors": sum(1 for q, m in meta.items() if q in qids and m.get("status") != 200),
            "fail_open": sum(1 for q, m in meta.items() if q in qids and m.get("fail_open")),
            "latency_s_p50": float(np.percentile(lat, 50)) if lat else None,
            "latency_s_p95": float(np.percentile(lat, 95)) if lat else None,
            "fallback_413": sum(1 for m in ok if m.get("fallback_from")),
            "sent_total": sum(m.get("n_sent", 0) for m in ok), "rejected_total": sum(m.get("rejected", 0) for m in ok),
            "truncated_total": sum(m.get("truncated", 0) for m in ok),
            "rejected_codes": sorted({c for m in ok for c in m.get("rejected_codes", [])})}


def holm(pvals: dict[str, float]) -> dict[str, float]:
    ps = sorted(pvals.items(), key=lambda x: x[1])
    m, out, running = len(ps), {}, 0.0
    for i, (n, p) in enumerate(ps):
        running = max(running, min(1.0, (m - i) * p))
        out[n] = running
    return out


PAIRS = [
    ("E|rrf-prod>late@100", "L|late-full@final"),
    ("E|rrf-prod>late@100", "E|rrf-prod>late@50"),
    ("E|rrf-prod>late@200", "E|rrf-prod>late@100"),
    ("E|rrf-prod>late@100", "E|rrf-prod>late@30"),
    ("E|rrf-prod>late@100", "E|bm25-os>late@100"),
    ("E|rrf-prod>late@100@final_bib", "E|rrf-prod>late@100"),
    ("E|rrf-prod>late@100@dense", "E|rrf-prod>late@100"),
    ("L|late-full@final_bib", "L|late-full@final"),
    ("L|late-full@dense", "L|late-full@final"),
    ("A|bm25-lab@final_bib", "A|bm25-lab@final"),
    ("F3|rrf(bm25-os,dense,late-full)", "E|rrf-prod>late@100"),
    ("C|hybrid-api", "C|rrf-prod"),
    ("C|rrf-prod", "B|dense"),
    ("A|bm25-lab@final", "A|bm25-os"),
    ("E|rrf-lab>late@100", "E|rrf-prod>late@100"),
    ("E|rrf-unit-lab>late@dense", "E|rrf-prod>late@100"),
    ("R|E|rrf-prod>late@100>rerank@8", "E|rrf-prod>late@100"),
    ("R|E|rrf-prod>late@100>rerank@12", "E|rrf-prod>late@100"),
    ("R|E|rrf-prod>late@100>rerank@12", "R|E|rrf-prod>late@100>rerank@8"),
    ("R|C|rrf-prod>rerank@12", "C|rrf-prod"),
    ("R|C|rrf-prod>rerank@12", "E|rrf-prod>late@100"),
    ("E|hybrid-late-api", "E|rrf-prod>late@100"),
    ("E|hybrid-late-api", "E|rrf-prod>late@100@final_bib"),
    ("E|hybrid-late-api", "C|hybrid-api"),
    ("A|bm25-os@recheck", "A|bm25-os"),
]


def evaluate_set(label: str, judged_all: dict, rankings: dict, meta: dict, bench, covered: set, hn: dict) -> dict:
    res = {}
    for track in ("text", "visual"):
        queries = [q for q in bench.queries if q.track == track and q.query_id in covered]
        qids = [q.query_id for q in queries]
        test_ids = [q for q in qids if bench.splits.get(q) == "test"]
        slices = metrics.group_queries(queries, "slices")
        cats = metrics.group_queries(queries, "category")
        judged = {q: judged_all.get(q, {}) for q in qids}
        tr = {"n_queries": len(qids), "n_test": len(test_ids), "systems": {}, "pairs": {}}
        evals = {}
        for name, per_q in rankings.items():
            rq = {q: per_q[q] for q in qids if q in per_q}
            if not rq:
                continue
            if (name, track) in NOT_RUN:
                tr["systems"][name] = {"status": "NOT_RUN", "n": len(rq), "reason": NOT_RUN[(name, track)]}
                continue
            if len(rq) < len(qids):
                tr["systems"][name] = {"status": "PARTIAL", "n": len(rq)}
                continue
            ev = metrics.evaluate(rq, judged, hard_negatives=hn, query_ids=qids)
            ev1 = metrics.evaluate(rq, judged, threshold=1, query_ids=qids)
            evals[name] = ev
            r = {"status": "OK", "overall": ev.mean(), "lenient_grade1": ev1.mean(), "test": ev.mean(test_ids),
                 "slices": ev.by_group(slices), "categories": ev.by_group(cats), "excluded": ev.excluded,
                 "per_query": {q: {k: round(v, 4) for k, v in m.items()} for q, m in ev.per_query.items()}}
            if name in meta:
                r["rerank"] = rerank_stats(meta[name], qids)
            tr["systems"][name] = r
        for base_key, base in (("vs_bm25", BASE_BM25), ("vs_prod", BASE_PROD)):
            if base not in evals:
                continue
            for name, ev in evals.items():
                if name == base:
                    continue
                sig = {}
                for m in SIG_METRICS:
                    sig[m] = metrics.compare(ev, evals[base], m)
                    sig[m + "|test"] = metrics.compare(ev, evals[base], m, test_ids)
                tr["systems"][name][base_key] = sig
        for name in list(evals):
            if name.startswith("R|"):
                src = name[2:].rsplit(">rerank@", 1)[0]
                if src in evals:
                    tr["systems"][name]["vs_first_stage"] = {m: metrics.compare(evals[name], evals[src], m)
                                                            for m in ("ndcg@10", "recall@10", "mrr@10")}
        for a, b in PAIRS:
            if a in evals and b in evals:
                tr["pairs"][f"{a} ~ {b}"] = {m: metrics.compare(evals[a], evals[b], m) for m in SIG_METRICS}
        ps = {n: r["vs_bm25"]["ndcg@10"]["p_value"] for n, r in tr["systems"].items()
              if r.get("status") == "OK" and "vs_bm25" in r and not n.startswith("R|")}
        tr["holm_vs_bm25_ndcg10"] = holm(ps)
        res[track] = tr
        rows = sorted(((n, r["overall"]) for n, r in tr["systems"].items() if r.get("status") == "OK"),
                      key=lambda x: -x[1]["ndcg@10"])
        print(f"== {label} / {track}: {len(qids)} queries ({len(test_ids)} test)")
        for n, o in rows:
            v = tr["systems"][n].get("vs_bm25", {}).get("ndcg@10", {})
            w = tr["systems"][n].get("vs_prod", {}).get("ndcg@10", {})
            print(f"{n:42s} nDCG@10 {o['ndcg@10']:.3f} R@10 {o['recall@10']:.3f} R@50 {o['recall@50']:.3f} "
                  f"MRR {o['mrr@10']:.3f} j@10 {o['judged@10']:.2f} dBM25 {v.get('delta', 0):+.3f} p {v.get('p_value', 1):.4f}"
                  f" dPROD {w.get('delta', 0):+.3f} p {w.get('p_value', 1):.4f}")
    return res


def latency(first: dict, meta: dict) -> dict:
    out = {"offline": first.get("timing", {})}
    q = first.get("qmeta", {})
    for k in ("dense_ms", "late_ms", "wall_ms"):
        vals = [m[k] for m in q.values() if m.get(k) is not None]
        if vals:
            out[f"encode_{k}"] = {"p50": float(np.percentile(vals, 50)), "p95": float(np.percentile(vals, 95))}
    api_path = OUT / "api.json"
    if api_path.is_file():
        api = json.load(open(api_path, encoding="utf-8"))
        out["api_status"] = api.get("meta", {}).get("status")
        for name, per in api["systems"].items():
            first_calls = [c["calls"][0] for c in per.values() if c.get("calls") and c["calls"][0].get("status") == 200]
            walls = [c["wall_ms"][0] for c in per.values() if c.get("wall_ms")]
            d = {"wall_ms_p50": float(np.percentile(walls, 50)) if walls else None,
                 "wall_ms_p95": float(np.percentile(walls, 95)) if walls else None,
                 "elapsed_ms_p50": float(np.percentile([c["elapsed_ms"] for c in first_calls if c.get("elapsed_ms")], 50))
                 if first_calls else None}
            tms = defaultdict(list)
            for c in first_calls:
                for k, v in (c.get("timings_ms") or {}).items():
                    if isinstance(v, (int, float)):
                        tms[k].append(v)
            d["timings_ms"] = {k: {"p50": float(np.percentile(v, 50)), "p95": float(np.percentile(v, 95))}
                               for k, v in tms.items()}
            out[f"api|{name}"] = d
    for name, m in meta.items():
        lat = [x["latency_s"] for x in m.values() if x.get("status") == 200]
        if lat:
            out[f"rerank|{name}"] = {"p50_s": float(np.percentile(lat, 50)), "p95_s": float(np.percentile(lat, 95)),
                                     "n": len(lat)}
    return out


def export(results: dict, path: Path) -> None:
    def rnd(x):
        if isinstance(x, float):
            return round(x, 4)
        if isinstance(x, dict):
            return {k: rnd(v) for k, v in x.items()}
        if isinstance(x, list):
            return [rnd(v) for v in x]
        return x

    out = {k: v for k, v in results.items() if k != "sets"}
    out["sets"] = {}
    for label, tracks in results["sets"].items():
        out["sets"][label] = {}
        for track, tr in tracks.items():
            systems = {}
            for n, r in tr["systems"].items():
                if r.get("status") != "OK":
                    systems[n] = r
                    continue
                systems[n] = {"overall": r["overall"], "test": r["test"], "lenient_grade1": r["lenient_grade1"],
                              "slices": {g: {"n": m.get("n_queries"), "ndcg@10": m.get("ndcg@10"),
                                             "recall@50": m.get("recall@50")} for g, m in r["slices"].items()},
                              "categories": {g: {"n": m.get("n_queries"), "ndcg@10": m.get("ndcg@10")}
                                             for g, m in r["categories"].items()},
                              "vs_bm25": r.get("vs_bm25"), "vs_prod": r.get("vs_prod"),
                              "vs_first_stage": r.get("vs_first_stage"), "rerank": r.get("rerank"),
                              "per_query": {q: [m.get("ndcg@10"), m.get("recall@50"), m.get("mrr@10")]
                                            for q, m in r["per_query"].items()}}
            out["sets"][label][track] = {"n_queries": tr["n_queries"], "n_test": tr["n_test"],
                                         "per_query_columns": ["ndcg@10", "recall@50", "mrr@10"],
                                         "systems": systems, "pairs": tr["pairs"],
                                         "holm_vs_bm25_ndcg10": tr["holm_vs_bm25_ndcg10"]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rnd(out), f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def main() -> None:
    rankings, meta, first = load_rankings()
    bench = B.load_benchmark(BENCH_DIR)
    prep = json.load(open(V1 / "prepare.json", encoding="utf-8"))
    covered = set(prep["covered_queries"])
    verified = bench.judgments(level="PAGE")
    pooled = load_pooled(V1_DIR / "qrels_v1_pooled.tsv", bench)
    hn = bench.hard_negative_ids("PAGE")
    results = {"benchmark": "RETRIEVAL_BENCHMARK_V1", "snapshot": prep["snapshot"],
               "dense_units_snapshot": prep["dense_units_snapshot"], "units": prep["units_final"],
               "units_dense": prep["units_dense"], "diff_final_vs_dense": prep["diff_final_vs_dense"],
               "n_pages": prep["pages"], "coverage": prep["coverage"], "not_covered": sorted(prep["not_covered"]),
               "pooled_labels": sum(len(v) for v in pooled.values()), "latency": latency(first, meta),
               "not_run": [{"system": s, "track": t, "reason": r} for (s, t), r in NOT_RUN.items()],
               "incidents": INCIDENTS, "sets": {}}
    results["sets"]["verified"] = evaluate_set("VERIFIED", verified, rankings, meta, bench, covered, hn)
    if pooled:
        results["sets"]["verified+pooled"] = evaluate_set("VERIFIED+POOLED", B.merge_judgments(verified, pooled),
                                                          rankings, meta, bench, covered, hn)
    json.dump(results, open(OUT / "results.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False,
              default=float)
    if "--export" in sys.argv:
        export(results, V1_DIR / "results_v1.json")
        print("exported", V1_DIR / "results_v1.json")


if __name__ == "__main__":
    main()
