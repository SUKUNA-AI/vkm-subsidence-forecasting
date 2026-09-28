"""Render markdown tables for RESULTS_V0.md from $J_V0/out/results.json (IDs and metrics only)."""
import json
import os
import sys
from pathlib import Path

V0 = Path(os.environ["J_V0"])
R = json.load(open(V0 / "out" / "results.json", encoding="utf-8"))
MODEL = {"bgem3mv": "BGE-M3 multi-vector fp32", "bgem3": "BGE-M3 fp32", "granite311": "granite-311m-r2", "jnano": "jina-v5-nano", "qwen3": "Qwen3-Emb-0.6B",
         "mlateon": "mLateOn", "jcolbert": "jina-colbert-v2"}


def pretty(name: str) -> str:
    def m(x):
        for k, v in MODEL.items():
            x = x.replace(k, v)
        return x

    if name.startswith("R|"):
        return pretty(name[2:]) + " → rerank v3.5 (top-24)"
    if name.startswith("V|"):
        return pretty(name[2:]) + " → m0 (top-8 images)"
    kind, rest = name.split("|", 1)
    if kind == "A":
        return "A · BM25"
    if kind == "B":
        return "B · dense " + m(rest.split("=", 1)[1])
    if kind == "L":
        return "L · late " + m(rest.split("=", 1)[1]) + " (full MaxSim)"
    if kind == "S":
        return "S · sparse BGE-M3 fp32 (lexical weights)"
    if kind == "U":
        return "U · BGE-M3 fp32 unified (0.4 dense + 0.2 sparse + 0.4 multi-vector)"
    if kind == "C":
        return "C · RRF(BM25, " + m(rest.split("+", 1)[1]) + ")"
    if kind == "E":
        d, l = rest.split(">late=")
        if "+" not in d:
            return "E · BM25 → " + m(l) + " re-score top-100"
        return "E · RRF(BM25, " + m(d.split("+", 1)[1]) + ") → " + m(l) + " re-score top-100"
    if kind == "F3":
        return "F3 · RRF(BM25, " + m(rest[4:-1].split(",", 1)[1]).replace(",", ", ") + ")"
    return name


def fp(p: float) -> str:
    return "<0.001" if p < 0.001 else f"={p:.3f}"


def fmt_sig(s: dict | None) -> str:
    if not s:
        return "—"
    star = " ✓" if s.get("worth_it") else ""
    return f"{s['delta']:+.3f} [{s['ci_lo']:+.3f}; {s['ci_hi']:+.3f}] p{fp(s['p_value'])}{star}"


def headline(track: str, names: list[str] | None = None) -> str:
    tr = R["tracks"][track]
    rows = [(n, r) for n, r in tr["systems"].items() if r.get("status") == "OK" and (names is None or n in names)]
    rows.sort(key=lambda x: -x[1]["overall"]["ndcg@10"])
    out = [f"| # | Система | nDCG@10 | R@10 | R@50 | MRR@10 | judged@10 | ΔnDCG@10 vs BM25 [95% CI], p | ΔR@50 vs BM25, p |",
           "|---|---|---|---|---|---|---|---|---|"]
    for i, (n, r) in enumerate(rows, 1):
        o = r["overall"]
        v = r.get("vs_bm25", {})
        r50 = v.get("recall@50")
        r50s = f"{r50['delta']:+.3f}, p{fp(r50['p_value'])}" if r50 else "—"
        out.append(f"| {i} | {pretty(n)} | {o['ndcg@10']:.3f} | {o['recall@10']:.3f} | {o['recall@50']:.3f} | "
                   f"{o['mrr@10']:.3f} | {o['judged@10']:.2f} | {fmt_sig(v.get('ndcg@10'))} | {r50s} |")
    return "\n".join(out)


def test_split(track: str, names: list[str]) -> str:
    tr = R["tracks"][track]
    out = ["| Система | n | nDCG@10 (test) | ΔnDCG@10 vs BM25 (test) | R@50 (test) |", "|---|---|---|---|---|"]
    for n in names:
        r = tr["systems"].get(n)
        if not r or r.get("status") != "OK":
            continue
        t = r["test"]
        out.append(f"| {pretty(n)} | {int(t['n_queries'])} | {t['ndcg@10']:.3f} | "
                   f"{fmt_sig(r.get('vs_bm25', {}).get('ndcg@10|test'))} | {t['recall@50']:.3f} |")
    return "\n".join(out)


def groups(track: str, names: list[str], key: str, metric: str = "ndcg@10") -> str:
    tr = R["tracks"][track]
    names = [n for n in names if tr["systems"].get(n, {}).get("status") == "OK"]
    gs = sorted({g for n in names for g in tr["systems"][n][key]})
    out = ["| " + {"slices": "срез", "categories": "категория"}[key] + " | n | " + " | ".join(pretty(n) for n in names) + " |",
           "|---|---|" + "---|" * len(names)]
    for g in gs:
        cells, nq = [], 0
        for n in names:
            m = tr["systems"][n][key].get(g) or {}
            nq = int(m.get("n_queries", 0)) or nq
            cells.append(f"{m[metric]:.3f}" if metric in m else "—")
        out.append(f"| {g} | {nq} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def rerank_table(track: str) -> str:
    tr = R["tracks"][track]
    out = ["| Реранк | вызовов OK | 413 → top-12 / top-7 | ошибок (fail-open) | латентность p50 / p95, с | кандидатов | отклонено | "
           "усечено | Δ nDCG@10 к своей 1-й стадии, p | Δ MRR@10, p |", "|---|---|---|---|---|---|---|---|---|---|"]
    for n, r in tr["systems"].items():
        if r.get("status") != "OK" or "rerank" not in r:
            continue
        rr = r["rerank"]
        vf = r.get("vs_first_stage", {})
        nd, mr = vf.get("ndcg@10"), vf.get("mrr@10")
        out.append(f"| {pretty(n)} | {rr['calls_ok']} | {rr.get('fallback_depth12', 0)} / {rr.get('fallback_depth7', 0)} | "
                   f"{rr['errors']} ({rr.get('fail_open', 0)}) | "
                   f"{rr['latency_s_p50']:.1f} / "
                   f"{rr['latency_s_p95']:.1f} | {rr['sent_total']} | {rr['rejected_total']} "
                   f"({', '.join(rr['rejected_codes']) or '—'}) | {rr['truncated_total']} | "
                   f"{nd['delta']:+.3f}, p{fp(nd['p_value'])} | {mr['delta']:+.3f}, p{fp(mr['p_value'])} |"
                   if nd and mr else f"| {pretty(n)} | {rr['calls_ok']} | — | {rr['errors']} | — | — | — | — | — | — |")
    return "\n".join(out)


def holm(track: str, metric: str = "ndcg@10", prefixes=("B|", "L|", "C|", "E|", "F3|", "S|", "U|")) -> dict[str, float]:
    tr = R["tracks"][track]["systems"]
    ps = sorted(((n, r["vs_bm25"][metric]["p_value"]) for n, r in tr.items()
                 if r.get("status") == "OK" and n.startswith(prefixes) and "vs_bm25" in r), key=lambda x: x[1])
    m, out, running = len(ps), {}, 0.0
    for i, (n, p) in enumerate(ps):
        running = max(running, min(1.0, (m - i) * p))
        out[n] = running
    return out


def export(path: str, extra: dict) -> None:
    """Compact public JSON: aggregates, significance, rerank stats, per-query nDCG@10 / R@50 / MRR@10 (IDs only)."""
    def rnd(x):
        if isinstance(x, float):
            return round(x, 4)
        if isinstance(x, dict):
            return {k: rnd(v) for k, v in x.items()}
        if isinstance(x, list):
            return [rnd(v) for v in x]
        return x

    out = {"benchmark": "RETRIEVAL_BENCHMARK_V0", "snapshot": R["snapshot"], "n_units": R["n_units"],
           "n_canon_pages": R["n_canon_pages"], "timing_cpu": R["timing"], "tracks": {}, **extra}
    for track, tr in R["tracks"].items():
        systems = {}
        for n, r in tr["systems"].items():
            if r.get("status") != "OK":
                systems[n] = r
                continue
            systems[n] = {"label": pretty(n), "overall": r["overall"], "test": r["test"],
                          "lenient_grade1": r["lenient_grade1"],
                          "slices": {g: {"n": m.get("n_queries"), "ndcg@10": m.get("ndcg@10"),
                                         "recall@50": m.get("recall@50")} for g, m in r["slices"].items()},
                          "categories": {g: {"n": m.get("n_queries"), "ndcg@10": m.get("ndcg@10")}
                                         for g, m in r["categories"].items()},
                          "vs_bm25": r.get("vs_bm25"), "vs_first_stage": r.get("vs_first_stage"),
                          "rerank": r.get("rerank"),
                          "per_query": {q: [m.get("ndcg@10"), m.get("recall@50"), m.get("mrr@10")]
                                        for q, m in r["per_query"].items()}}
        out["tracks"][track] = {k: v for k, v in tr.items() if k != "systems"}
        out["tracks"][track]["per_query_columns"] = ["ndcg@10", "recall@50", "mrr@10"]
        out["tracks"][track]["systems"] = systems
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rnd(out), f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def pairs(track: str, pair_list: list[tuple[str, str]], metrics_=("ndcg@10", "recall@50")) -> tuple[str, dict]:
    from vkm_corpus.retrieval_lab.metrics import Evaluation, compare

    tr = R["tracks"][track]["systems"]
    out = ["| A | B | метрика | Δ(A−B) [95% CI] | p |", "|---|---|---|---|---|"]
    raw = {}
    for a, b in pair_list:
        if tr.get(a, {}).get("status") != "OK" or tr.get(b, {}).get("status") != "OK":
            continue
        ea, eb = Evaluation(tr[a]["per_query"]), Evaluation(tr[b]["per_query"])
        for met in metrics_:
            c = compare(ea, eb, met)
            raw[f"{a} vs {b} | {met}"] = c
            out.append(f"| {pretty(a)} | {pretty(b)} | {met} | {c['delta']:+.3f} [{c['ci_lo']:+.3f}; "
                       f"{c['ci_hi']:+.3f}] | {fp(c['p_value']).lstrip('=')} |")
    return "\n".join(out), raw


if __name__ == "__main__":
    what = sys.argv[1]
    track = sys.argv[2] if len(sys.argv) > 2 else "text"
    names = sys.argv[3].split(";") if len(sys.argv) > 3 else None
    if what == "headline":
        print(headline(track, names))
    elif what == "test":
        print(test_split(track, names))
    elif what == "slices":
        print(groups(track, names, "slices"))
    elif what == "categories":
        print(groups(track, names, "categories"))
    elif what == "rerank":
        print(rerank_table(track))
    elif what == "export":
        export(sys.argv[2], json.loads(open(sys.argv[3], encoding="utf-8").read()) if len(sys.argv) > 3 else {})
    elif what == "pairs":
        pl = [tuple(x.split("~")) for x in sys.argv[3].split(";")]
        table, raw = pairs(track, pl)
        print(table)
        if len(sys.argv) > 4:
            json.dump(raw, open(sys.argv[4], "w", encoding="utf-8"), indent=1)
    elif what == "holm":
        for n, p in holm(track, names[0] if names else "ndcg@10").items():
            print(f"{pretty(n)}: Holm p = {p:.4f}")
