"""Visual route on the V2 harness (agent VIS, 29.09.2026): the served scheme E for every query, E+VIS (RRF of E's
top-100 pages and the Qwen3-VL page-image leg's top-100) only for queries the visual intent detector marks.

Inputs are public and in git: the per-query metrics of V2 (``results_v2.json``: E = ``E:nano-rx580``, E+VIS with GPU
bf16 query vectors, with the F16 GGUF text tower ``~f16`` — the arithmetic the RX580 slot serves, reproduced
bit-for-bit by the service code path on CPU — and with Q8_0 ``~q8``), the benchmark queries
(``benchmarks/retrieval_v0/queries.jsonl``) and the detector (``vkm_corpus.search.intent.visual_intent``, committed
before this evaluation). Per query the route takes E+VIS's metrics when the detector fires and E's otherwise, so on a
query that is not routed the route equals E exactly (Δ = 0 by construction, reported per track).

Outputs ``benchmarks/retrieval_v2/visual_route_v2.json``: detector precision/recall against the track labels (all 189
queries and the 186 covered), per set (V = VERIFIED, P = VERIFIED + pooled) and track nDCG@10 / R@50 / MRR@10 of E,
the routes and the oracle route (E+VIS exactly on the visual track), paired randomization tests and bootstrap CIs
(``vkm_corpus.retrieval_lab.metrics``, 10 000 permutations, seed 20260928), and check 0 (E+VIS vs E recomputed from
the rounded per-query values equals V2's published Δ).

    python benchmarks/retrieval_v2/scripts/visual_route_v2.py [--out <json>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
from vkm_corpus.retrieval_lab.metrics import bootstrap_ci, paired_randomization  # noqa: E402
from vkm_corpus.search.intent import visual_intent  # noqa: E402

V2 = REPO / "benchmarks/retrieval_v2"
BASE = "E:nano-rx580"
VARIANTS = {"bf16": "E+VIS:qwen3-vl-2b", "f16": "E+VIS:qwen3-vl-2b~f16", "q8": "E+VIS:qwen3-vl-2b~q8"}
METRICS = ("ndcg@10", "recall@50", "mrr@10")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def per_query(system: dict, columns: list[str]) -> dict[str, dict[str, float]]:
    return {q: dict(zip(columns, vals)) for q, vals in system["per_query"].items()}


def compare(a: dict[str, dict], b: dict[str, dict], metric: str, ids: list[str] | None = None) -> dict:
    ids = sorted(set(a) & set(b) if ids is None else ids)
    x = {q: a[q][metric] for q in ids}
    y = {q: b[q][metric] for q in ids}
    t, ci = paired_randomization(x, y), bootstrap_ci(x, y)
    return {"n": t["n"], "delta": round(t["delta"], 4) if t["n"] else None,
            "p_value": round(t["p_value"], 4) if t["n"] else None, "ci_lo": round(ci["lo"], 4) if t["n"] else None,
            "ci_hi": round(ci["hi"], 4) if t["n"] else None,
            "wins": sum(1 for q in ids if a[q][metric] > b[q][metric] + 1e-12),
            "losses": sum(1 for q in ids if a[q][metric] < b[q][metric] - 1e-12)}


def mean(pq: dict[str, dict], metric: str) -> float:
    return round(sum(m[metric] for m in pq.values()) / max(1, len(pq)), 4)


def prf(pred: dict[str, bool], truth: dict[str, bool]) -> dict:
    tp = sum(1 for q in truth if pred[q] and truth[q])
    fp = sum(1 for q in truth if pred[q] and not truth[q])
    fn = sum(1 for q in truth if not pred[q] and truth[q])
    tn = len(truth) - tp - fp - fn
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"n": len(truth), "tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": round(p, 4), "recall": round(r, 4),
            "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(V2 / "visual_route_v2.json"))
    a = ap.parse_args()
    queries_path = REPO / "benchmarks/retrieval_v0/queries.jsonl"
    queries = [json.loads(line) for line in queries_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    res_path = V2 / "results_v2.json"
    results = json.loads(res_path.read_text(encoding="utf-8"))
    intents = {q["query_id"]: visual_intent(q["text"]) for q in queries}
    routed = {qid: it.visual for qid, it in intents.items()}
    truth = {q["query_id"]: q["track"] == "visual" for q in queries}
    covered = sorted(set().union(*(set(results["sets"]["verified"][t]["systems"][BASE]["per_query"])
                                   for t in ("text", "visual"))))
    detector = {"all_189": prf(routed, truth), "covered_186": prf({q: routed[q] for q in covered},
                                                                  {q: truth[q] for q in covered}),
                "false_positives": sorted(q for q in truth if routed[q] and not truth[q]),
                "false_negatives": sorted(q for q in truth if not routed[q] and truth[q]),
                "cues": {q: list(intents[q].cues) for q in sorted(intents) if intents[q].visual}}
    by_cat: dict[str, dict] = {}
    for q in queries:
        c = by_cat.setdefault(f"{q['track']}/{q['category']}", {"n": 0, "routed": 0})
        c["n"] += 1
        c["routed"] += int(routed[q["query_id"]])
    detector["by_category"] = dict(sorted(by_cat.items()))

    sets_out: dict[str, dict] = {}
    for label in ("verified", "verified+pooled"):
        sets_out[label] = {}
        for track in ("text", "visual"):
            s = results["sets"][label][track]
            cols = s["per_query_columns"]
            e = per_query(s["systems"][BASE], cols)
            ids = sorted(e)
            n_routed = sum(1 for q in ids if routed[q])
            out: dict[str, dict] = {"n_queries": len(ids), "n_routed": n_routed,
                                    "routed_ids": sorted(q for q in ids if routed[q]),
                                    "systems": {"E": {m: mean(e, m) for m in METRICS}}, "pairs": {}}
            for name, sysname in VARIANTS.items():
                ev = per_query(s["systems"][sysname], cols)
                route = {q: (ev[q] if routed[q] else e[q]) for q in ids}
                oracle = {q: (ev[q] if track == "visual" else e[q]) for q in ids}
                out["systems"][f"E+VIS~{name}"] = {m: mean(ev, m) for m in METRICS}
                out["systems"][f"ROUTE~{name}"] = {m: mean(route, m) for m in METRICS}
                out["systems"][f"ORACLE~{name}"] = {m: mean(oracle, m) for m in METRICS}
                for m in METRICS:
                    out["pairs"][f"ROUTE~{name} vs E|{m}"] = compare(route, e, m)
                out["pairs"][f"ROUTE~{name} vs E|ndcg@10|routed only"] = compare(route, e, "ndcg@10", [
                    q for q in ids if routed[q]]) if n_routed else {"n": 0}
                out["pairs"][f"ROUTE~{name} vs E|ndcg@10|not routed"] = compare(route, e, "ndcg@10", [
                    q for q in ids if not routed[q]])
                out["pairs"][f"E+VIS~{name} vs E|ndcg@10"] = compare(ev, e, "ndcg@10")
                out["pairs"][f"ROUTE~{name} vs ORACLE~{name}|ndcg@10"] = compare(route, oracle, "ndcg@10")
            test_ids = [q for q in ids if q in set(s["systems"][BASE].get("test_ids", []))]
            if test_ids:
                out["test_ids"] = len(test_ids)
            sets_out[label][track] = out
    # check 0: E+VIS vs E recomputed from the rounded per-query values reproduces V2's published deltas
    check0 = {}
    for label in ("verified", "verified+pooled"):
        for track in ("text", "visual"):
            pub = results["sets"][label][track]["pairs"][f"{VARIANTS['bf16']} ~ {BASE}"]["ndcg@10"]["delta"]
            mine = sets_out[label][track]["pairs"]["E+VIS~bf16 vs E|ndcg@10"]["delta"]
            check0[f"{label}/{track}"] = {"published": pub, "recomputed": mine, "ok": abs(pub - mine) <= 2e-4}
    out = {"schema": "vkm.visual_route_eval/1", "benchmark": "RETRIEVAL_BENCHMARK_V2 (visual route follow-up, agent VIS)",
           "inputs": {"results_v2.json": sha256(res_path), "queries.jsonl": sha256(queries_path),
                      "intent.py": sha256(REPO / "src/vkm_corpus/search/intent.py")},
           "route": "per query: E+VIS if visual_intent(query) else E; E = E:nano-rx580 (served scheme, late); "
                    "E+VIS = RRF(k=60) of E's top-100 pages and the Qwen3-VL page-image leg's top-100 (duplicates "
                    "collapsed)",
           "variants": {"bf16": "query vectors of the GPU bf16 run (V2's measured E+VIS)",
                        "f16": "query vectors of the F16 GGUF text tower (llama.cpp CPU; the service path reproduces "
                               "them, cos 1.0; the RX580 slot serves this GGUF)",
                        "q8": "Q8_0 GGUF text tower (failed V2's parity; reference only)"},
           "check0": check0, "check0_ok": all(v["ok"] for v in check0.values()), "detector": detector,
           "sets": sets_out}
    Path(a.out).write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    short = {"detector": {k: detector[k] for k in ("all_189", "covered_186", "false_positives", "false_negatives")},
             "check0_ok": out["check0_ok"]}
    for label, tracks in sets_out.items():
        for track, o in tracks.items():
            short[f"{label}/{track}"] = {"n_routed": o["n_routed"], "E": o["systems"]["E"]["ndcg@10"],
                                         **{k: o["systems"][k]["ndcg@10"] for k in o["systems"] if k != "E"},
                                         "route_f16_vs_E": o["pairs"]["ROUTE~f16 vs E|ndcg@10"],
                                         "route_bf16_vs_E": o["pairs"]["ROUTE~bf16 vs E|ndcg@10"]}
    print(json.dumps(short, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
