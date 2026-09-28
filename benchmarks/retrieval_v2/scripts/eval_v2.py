"""V2 evaluation (PREREGISTRATION §5–§6): check 0 (the recomputed E reproduces V1), page-level metrics of every V2
system on V (VERIFIED) and P (VERIFIED + LLM_AGENT_V1 + LLM_AGENT_V2), judged@10, paired randomization tests, Holm
over the pre-registered families and the decision rule (a recommendation; the switch is the user's decision).

Environment: ``J_V1`` (``prepare.json``), ``V2_WORK`` (``out/rankings_v2.json``, ``pool/pool.json``,
``out/serving_v2.json`` if present, ``vec/*/meta.json``, ``vis/*/meta.json``). Writes ``$V2_WORK/out/results_v2_full.json``
and, with ``--export``, ``benchmarks/retrieval_v2/results_v2.json`` (IDs and numbers only).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab import metrics  # noqa: E402

V1 = Path(os.environ["J_V1"])
WORK = Path(os.environ["V2_WORK"])
V1_DIR = REPO / "benchmarks/retrieval_v1"
V2_DIR = REPO / "benchmarks/retrieval_v2"
CFG = json.loads((V2_DIR / "configs/models_v2.json").read_text(encoding="utf-8"))
BASE = "E:nano-rx580"
F1 = [m["key"] for m in CFG["text"] if m["role"] == "candidate"]
F2 = [m["key"] for m in CFG["visual"]]
LICENSE = {m["key"]: m["license"] for m in CFG["text"] + CFG["visual"]}
LICENSE["nano-rx580"] = "CC-BY-NC-4.0"
SIG = ("ndcg@10", "recall@50", "mrr@10")
ALPHA = 0.05


def holm(pvals: dict[str, float]) -> dict[str, float]:
    ps = sorted(pvals.items(), key=lambda x: x[1])
    m, out, running = len(ps), {}, 0.0
    for i, (n, p) in enumerate(ps):
        running = max(running, min(1.0, (m - i) * p))
        out[n] = running
    return out


def load_pooled(bench) -> tuple[dict, dict, int, int]:
    v1 = B.load_pooled_qrels(V1_DIR / "qrels_v1_pooled.tsv")
    probs = B.validate_pooled_qrels(v1, bench)
    rows = list(v1)
    n2 = 0
    if (V2_DIR / "qrels_v2_pooled.tsv").is_file():
        v2 = B.load_pooled_qrels(V2_DIR / "qrels_v2_pooled.tsv")
        probs += B.validate_pooled_qrels(v2, bench)
        probs += B.pooled_round_overlaps(v1, v2)
        rows += v2
        n2 = len(v2)
    if probs:
        raise SystemExit("pooled labels invalid:\n" + "\n".join(probs[:20]))
    return B.pooled_judgments(rows), {}, len(v1), n2


def check0(rankings: dict, bench, covered: set, verified: dict, pooled_v1_only: dict) -> dict:
    """Recomputed V1 systems must give V1's per-query metrics exactly (rounded as in results_v1.json)."""
    r1 = json.loads((V1_DIR / "results_v1.json").read_text(encoding="utf-8"))
    pairs = {"E:nano-rx580": "E|rrf-prod>late@100", "B:nano-rx580": "B|dense", "A|bm25-os": "A|bm25-os"}
    hn = bench.hard_negative_ids("PAGE")
    out = {"ok": True, "systems": {}}
    for label, judged in (("verified", verified), ("verified+pooled", B.merge_judgments(verified, pooled_v1_only))):
        for track in ("text", "visual"):
            qids = [q.query_id for q in bench.queries if q.track == track and q.query_id in covered]
            for mine, theirs in pairs.items():
                ev = metrics.evaluate({q: rankings[mine][q] for q in qids}, {q: judged.get(q, {}) for q in qids},
                                      hard_negatives=hn, query_ids=qids)
                ref = r1["sets"][label][track]["systems"][theirs]["per_query"]
                diff = []
                for q, m in ev.per_query.items():
                    got = [round(m["ndcg@10"], 4), round(m["recall@50"], 4), round(m["mrr@10"], 4)]
                    if got != ref.get(q):
                        diff.append(q)
                missing = sorted(set(ref) ^ set(ev.per_query))
                ok = not diff and not missing
                out["systems"][f"{label}/{track}/{mine}"] = {"queries": len(ev.per_query), "differ": diff[:10],
                                                             "n_differ": len(diff), "missing": missing, "ok": ok}
                out["ok"] &= ok
    return out


def evaluate_set(label: str, judged_all: dict, rankings: dict, bench, covered: set, partial: set) -> dict:
    hn = bench.hard_negative_ids("PAGE")
    res = {}
    for track in ("text", "visual"):
        queries = [q for q in bench.queries if q.track == track and q.query_id in covered]
        qids = [q.query_id for q in queries]
        test_ids = [q for q in qids if bench.splits.get(q) == "test"]
        judged = {q: judged_all.get(q, {}) for q in qids}
        evals, systems = {}, {}
        for name, per_q in rankings.items():
            rq = {q: per_q[q] for q in qids if q in per_q}
            if len(rq) < len(qids):
                systems[name] = {"status": "PARTIAL", "n": len(rq)}
                continue
            ev = metrics.evaluate(rq, judged, hard_negatives=hn, query_ids=qids)
            evals[name] = ev
            flag = "PARTIAL_POOL" if (label == "verified+pooled" and name in partial.get(track, set())) else "OK"
            systems[name] = {"status": flag, "overall": ev.mean(), "test": ev.mean(test_ids),
                             "slices": ev.by_group(metrics.group_queries(queries, "slices")),
                             "categories": ev.by_group(metrics.group_queries(queries, "category")),
                             "per_query": {q: {k: round(v, 4) for k, v in m.items()} for q, m in ev.per_query.items()}}
        pairs = {}
        base_ok = BASE in evals

        def cmp(a: str, b: str) -> None:
            if a in evals and b in evals and systems[a]["status"] == "OK" and systems[b]["status"] == "OK":
                pairs[f"{a} ~ {b}"] = {m: metrics.compare(evals[a], evals[b], m) for m in SIG}
                pairs[f"{a} ~ {b}"]["ndcg@10|test"] = metrics.compare(evals[a], evals[b], "ndcg@10", test_ids)

        for name in evals:
            kind, _, key = name.partition(":")
            if not key or name == BASE:
                continue
            if "~" in key:                                   # serving parity variant vs the same model with GPU queries
                cmp(name, f"{kind}:{key.split('~')[0]}")
                continue
            if kind in ("E", "E+VIS", "E3") and base_ok:
                cmp(name, BASE)
            if kind == "E" and key not in ("nano-gpu", "nano-rx580"):
                cmp(name, "E:nano-gpu")
            if kind == "B" and key != "nano-rx580":
                cmp(name, "B:nano-rx580")
            if kind == "VIS":
                cmp(name, "B:nano-rx580")
                cmp(name, "A|bm25-os")
        cmp("B:nano-rx580", "A|bm25-os")
        res[track] = {"n_queries": len(qids), "n_test": len(test_ids), "systems": systems, "pairs": pairs}
    return res


def decide(sets: dict, serving: dict) -> dict:
    """Pre-registered rule §6 (recommendation)."""
    V, P = sets["verified"], sets.get("verified+pooled", {})

    def pair(s, track, a, b=BASE, m="ndcg@10"):
        return (s.get(track, {}).get("pairs", {}).get(f"{a} ~ {b}") or {}).get(m)

    # F1: Holm over the 10 pre-registered candidates (not run → p = 1)
    p_f1 = {}
    for k in F1:
        d = pair(V, "text", f"E:{k}")
        p_f1[k] = d["p_value"] if d else 1.0
    h1 = holm(p_f1)
    dense = {}
    for k in F1:
        dv, dp = pair(V, "text", f"E:{k}"), pair(P, "text", f"E:{k}") if P else None
        vv, vp = pair(V, "visual", f"E:{k}"), pair(P, "visual", f"E:{k}") if P else None
        crit = {
            "a_sig_V_text": bool(dv and dv["delta"] > 0 and h1[k] < ALPHA),
            "b_P_text_not_negative": bool(dp and dp["delta"] >= 0),
            "c_visual_guard": not any(d and d["delta"] < 0 and d["p_value"] < ALPHA for d in (vv, vp)),
            "d_serving": (serving.get(k) or {}).get("feasible"),
        }
        dense[k] = {"run": dv is not None, "license": LICENSE.get(k), "holm_p": h1[k],
                    "delta_V_text": dv["delta"] if dv else None, "p_V_text": dv["p_value"] if dv else None,
                    "ci_V_text": [dv["ci_lo"], dv["ci_hi"]] if dv else None,
                    "useful_V_text": dv["worth_it"] if dv else None,
                    "delta_P_text": dp["delta"] if dp else None, "p_P_text": dp["p_value"] if dp else None,
                    "delta_V_visual": vv["delta"] if vv else None, "p_V_visual": vv["p_value"] if vv else None,
                    "delta_P_visual": vp["delta"] if vp else None, "p_P_visual": vp["p_value"] if vp else None,
                    "criteria": crit, "passes_quality": crit["a_sig_V_text"] and crit["b_P_text_not_negative"]
                    and crit["c_visual_guard"]}
        dense[k]["recommended_candidate"] = bool(dense[k]["passes_quality"] and crit["d_serving"])
    passing = [k for k in F1 if dense[k]["recommended_candidate"]]
    choice = None
    if passing:
        passing.sort(key=lambda k: -(dense[k]["delta_P_text"] or 0))
        choice = passing[0]
    p_f2 = {}
    for k in F2:
        d = pair(V, "visual", f"E+VIS:{k}")
        p_f2[k] = d["p_value"] if d else 1.0
    h2 = holm(p_f2)
    vis = {}
    for k in F2:
        dv, dp = pair(V, "visual", f"E+VIS:{k}"), pair(P, "visual", f"E+VIS:{k}") if P else None
        tv, tp = pair(V, "text", f"E+VIS:{k}"), pair(P, "text", f"E+VIS:{k}") if P else None
        ok = bool(dv and dv["delta"] > 0 and h2[k] < ALPHA and dp and dp["delta"] >= 0)
        glob = ok and bool(tp and tp["delta"] >= 0) and not (tv and tv["delta"] < 0 and tv["p_value"] < ALPHA)
        vis[k] = {"run": dv is not None, "license": LICENSE.get(k), "holm_p": h2[k],
                  "delta_V_visual": dv["delta"] if dv else None, "p_V_visual": dv["p_value"] if dv else None,
                  "delta_P_visual": dp["delta"] if dp else None, "p_P_visual": dp["p_value"] if dp else None,
                  "delta_V_text": tv["delta"] if tv else None, "p_V_text": tv["p_value"] if tv else None,
                  "delta_P_text": tp["delta"] if tp else None, "p_P_text": tp["p_value"] if tp else None,
                  "passes_quality": ok, "scope": ("all queries" if glob else "visual route only") if ok else None,
                  "serving": (serving.get(k) or {}).get("feasible")}
        vis[k]["recommended"] = bool(ok and vis[k]["serving"])
    return {"rule": "PREREGISTRATION.md §6 (recommendation; the switch is the user's decision)",
            "dense": dense, "dense_choice": choice, "visual": vis,
            "visual_choice": next((k for k in sorted(F2, key=lambda x: -(vis[x]["delta_P_visual"] or 0))
                                   if vis[k]["recommended"]), None)}


def model_meta() -> dict:
    out = {}
    for sub in ("vec", "vis"):
        d = WORK / sub
        if not d.is_dir():
            continue
        for m in sorted(d.iterdir()):
            f = m / "meta.json"
            if f.is_file():
                meta = json.loads(f.read_text(encoding="utf-8"))
                out[m.name] = {k: meta.get(k) for k in ("model_id", "revision", "license", "precision", "n_units",
                                                        "n_pages", "dim", "units_per_s", "tokens_per_s", "pages_per_s",
                                                        "encode_s", "load_s", "queries_s", "peak_vram_mib", "tokens",
                                                        "doc_prefix", "query_prefix", "doc_prompt", "query_prompt")
                               if meta.get(k) is not None}
                out[m.name]["env"] = meta.get("env")
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
                if "overall" not in r:
                    systems[n] = r
                    continue
                systems[n] = {"status": r["status"], "overall": r["overall"], "test": r["test"],
                              "slices": {g: {"n": m.get("n_queries"), "ndcg@10": m.get("ndcg@10"),
                                             "recall@50": m.get("recall@50")} for g, m in r["slices"].items()},
                              "per_query": {q: [m.get("ndcg@10"), m.get("recall@50"), m.get("mrr@10")]
                                            for q, m in r["per_query"].items()}}
            out["sets"][label][track] = {"n_queries": tr["n_queries"], "n_test": tr["n_test"],
                                         "per_query_columns": ["ndcg@10", "recall@50", "mrr@10"],
                                         "systems": systems, "pairs": tr["pairs"]}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rnd(out), f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def main() -> None:
    bench = B.load_benchmark(REPO / "benchmarks/retrieval_v0")
    prep = json.load(open(V1 / "prepare.json", encoding="utf-8"))
    covered = set(prep["covered_queries"])
    data = json.load(open(WORK / "out" / "rankings_v2.json", encoding="utf-8"))
    rankings = data["rankings"]
    verified = bench.judgments(level="PAGE")
    pooled_v1_only = B.pooled_judgments(B.load_pooled_qrels(V1_DIR / "qrels_v1_pooled.tsv"))
    c0 = check0(rankings, bench, covered, verified, pooled_v1_only)
    print("check 0:", "OK" if c0["ok"] else "FAILED",
          json.dumps({k: v["n_differ"] for k, v in c0["systems"].items() if not v["ok"]}))
    if not c0["ok"] or "--check0" in sys.argv:
        (WORK / "out").mkdir(exist_ok=True)
        (WORK / "out" / "check0.json").write_text(json.dumps(c0, indent=1), encoding="utf-8")
        if not c0["ok"]:
            raise SystemExit("check 0 failed: the recomputed E does not reproduce V1")
        return
    pooled, _, n1, n2 = load_pooled(bench)
    pool_stats = {}
    partial: dict[str, set] = {}
    if (WORK / "pool" / "pool.json").is_file():
        pool_stats = json.load(open(WORK / "pool" / "pool.json", encoding="utf-8"))["stats"]
        if pool_stats.get("only_p1") or pool_stats.get("p2_depth", 10) < 10:
            # P2 judged to depth 5 only: its systems are flagged on P (text track: all P2 systems; visual track: all
            # but VIS, which is P1 there)
            p2 = set(pool_stats.get("p2_systems", []))
            partial = {"text": p2, "visual": {s for s in p2 if not s.startswith("VIS:")}}
    serving = {}
    if (WORK / "out" / "serving_v2.json").is_file():
        serving = json.load(open(WORK / "out" / "serving_v2.json", encoding="utf-8")).get("feasibility", {})
    results = {"benchmark": "RETRIEVAL_BENCHMARK_V2", "snapshot": prep["snapshot"],
               "dense_units_snapshot": prep["dense_units_snapshot"], "preregistration_sha256":
               (V2_DIR / "PREREGISTRATION.sha256").read_text(encoding="utf-8").split()[0],
               "coverage": prep["coverage"], "check0": {"ok": c0["ok"], "systems": {k: {"ok": v["ok"],
                                                                                      "queries": v["queries"]}
                                                                                  for k, v in c0["systems"].items()}},
               "pooled_labels": {"LLM_AGENT_V1": n1, "LLM_AGENT_V2": n2}, "pool": pool_stats,
               "models": model_meta(), "sets": {}}
    results["sets"]["verified"] = evaluate_set("verified", verified, rankings, bench, covered, partial)
    results["sets"]["verified+pooled"] = evaluate_set("verified+pooled", B.merge_judgments(verified, pooled), rankings,
                                                      bench, covered, partial)
    results["decision"] = decide(results["sets"], serving)
    (WORK / "out").mkdir(exist_ok=True)
    with open(WORK / "out" / "results_v2_full.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, default=float)
    for label in ("verified", "verified+pooled"):
        for track in ("text", "visual"):
            tr = results["sets"][label][track]
            rows = sorted(((n, r) for n, r in tr["systems"].items() if "overall" in r),
                          key=lambda x: -x[1]["overall"]["ndcg@10"])
            print(f"== {label} / {track}: {tr['n_queries']} queries")
            for n, r in rows:
                o = r["overall"]
                d = tr["pairs"].get(f"{n} ~ {BASE}", {}).get("ndcg@10") or {}
                print(f"{n:28s} {r['status']:12s} nDCG@10 {o['ndcg@10']:.3f} R@50 {o['recall@50']:.3f} "
                      f"MRR {o['mrr@10']:.3f} j@10 {o['judged@10']:.2f}"
                      + (f"  dE {d['delta']:+.3f} p {d['p_value']:.4f}" if d else ""))
    print(json.dumps({k: v for k, v in results["decision"].items() if k in ("dense_choice", "visual_choice")}))
    if "--export" in sys.argv:
        export(results, V2_DIR / "results_v2.json")
        print("exported results_v2.json")


if __name__ == "__main__":
    main()
