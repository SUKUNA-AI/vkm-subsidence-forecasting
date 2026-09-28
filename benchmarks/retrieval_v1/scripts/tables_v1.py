"""Markdown tables for ``RESULTS_V1.md`` / ``MODEL_SELECTION_V1.md`` from ``$J_V1/out/results.json``.

Prints to stdout (IDs and numbers only). Sections: main tables per label set × track, CP-42 / late-depth / API pairs,
slices of the main systems, reranker statistics and latency.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

V1 = Path(os.environ["J_V1"])
MAIN = ["A|bm25-os", "A|bm25-lab@final", "B|dense", "C|rrf-prod", "C|hybrid-api", "E|rrf-prod>late@100",
        "E|hybrid-late-api", "L|late-full@final", "F3|rrf(bm25-os,dense,late-full)",
        "R|E|rrf-prod>late@100>rerank@8", "R|E|rrf-prod>late@100>rerank@12"]
SHORT = {"A|bm25-os": "A BM25 (OpenSearch, прод.)", "A|bm25-lab@final": "A BM25 lab (LocalBM25)",
         "B|dense": "B dense jina-v5-nano", "C|rrf-prod": "C RRF(BM25, dense) — прод. схема",
         "C|hybrid-api": "C /v1/search/hybrid (без late)", "E|rrf-prod>late@100": "E RRF → mLateOn@100",
         "E|hybrid-late-api": "E /v1/search/hybrid (late=on)", "L|late-full@final": "L полный mLateOn MaxSim",
         "F3|rrf(bm25-os,dense,late-full)": "F3 RRF(BM25, dense, late)",
         "R|E|rrf-prod>late@100>rerank@8": "R E → reranker v3.5 @8",
         "R|E|rrf-prod>late@100>rerank@12": "R E → reranker v3.5 @12"}


COL = {"A|bm25-os": "A", "A|bm25-lab@final": "A-lab", "B|dense": "B", "C|rrf-prod": "C", "C|hybrid-api": "C-api",
       "E|rrf-prod>late@100": "E", "E|hybrid-late-api": "E-api", "L|late-full@final": "L",
       "F3|rrf(bm25-os,dense,late-full)": "F3", "R|E|rrf-prod>late@100>rerank@8": "R8",
       "R|E|rrf-prod>late@100>rerank@12": "R12", "E|rrf-prod>late@100@final_bib": "E+BIB"}


def esc(name: str) -> str:
    """System names contain «|»; escape it inside markdown table cells."""
    return name.replace("|", "\\|")


def f3(x):
    return "—" if x is None or x != x else f"{x:.3f}"


def sig(d):
    if not d:
        return "—"
    p = d.get("p_value")
    star = "*" if d.get("worth_it") else ""
    return f"{d['delta']:+.3f} (p {p:.3f}){star}"


def main_table(sets: dict, label: str, track: str, names: list[str] | None = None) -> list[str]:
    tr = sets[label][track]
    rows = [(n, r) for n, r in tr["systems"].items() if r.get("status") == "OK"]
    rows.sort(key=lambda x: -x[1]["overall"]["ndcg@10"])
    if names:
        rows = [x for x in rows if x[0] in names]
    out = [f"**{label} / {track}** — {tr['n_queries']} запросов ({tr['n_test']} test)", "",
           "| система | nDCG@10 | R@10 | R@50 | MRR@10 | judged@10 | nDCG@10 test | Δ к BM25 | Δ к прод. |",
           "|---|---|---|---|---|---|---|---|---|"]
    for n, r in rows:
        o, t = r["overall"], r["test"]
        out.append(f"| {esc(SHORT.get(n, n))} | {f3(o['ndcg@10'])} | {f3(o['recall@10'])} | {f3(o['recall@50'])} | "
                   f"{f3(o['mrr@10'])} | {o['judged@10']:.2f} | {f3(t.get('ndcg@10'))} | "
                   f"{sig((r.get('vs_bm25') or {}).get('ndcg@10'))} | {sig((r.get('vs_prod') or {}).get('ndcg@10'))} |")
    return out + [""]


def pairs_table(sets: dict, label: str, track: str, keys: list[str]) -> list[str]:
    tr = sets[label][track]
    out = [f"**Пары ({label} / {track})**", "", "| A ~ B | Δ nDCG@10 [95% CI] | p | Δ R@50 | Δ MRR@10 |",
           "|---|---|---|---|---|"]
    for k in keys:
        d = tr["pairs"].get(k)
        if not d:
            continue
        n = d["ndcg@10"]
        out.append(f"| {esc(k)} | {n['delta']:+.3f} [{n['ci_lo']:+.3f}; {n['ci_hi']:+.3f}] | {n['p_value']:.4f} | "
                   f"{d['recall@50']['delta']:+.3f} | {d['mrr@10']['delta']:+.3f} |")
    return out + [""]


def slices_table(sets: dict, label: str, track: str, names: list[str]) -> list[str]:
    tr = sets[label][track]
    present = [n for n in names if (tr["systems"].get(n) or {}).get("status") == "OK"]
    groups = sorted({g for n in present for g in tr["systems"][n]["slices"]})
    out = [f"**Срезы nDCG@10 ({label} / {track})**", "",
           "| срез | n | " + " | ".join(COL.get(n, n) for n in present) + " |",
           "|---|---|" + "---|" * len(present)]
    for g in groups:
        nq = tr["systems"][present[0]]["slices"][g].get("n_queries", 0)
        vals = [f3(tr["systems"][n]["slices"][g].get("ndcg@10")) for n in present]
        out.append(f"| {g} | {int(nq)} | " + " | ".join(vals) + " |")
    return out + [""]


BIB_NAMES = ["E|rrf-prod>late@100", "E|rrf-prod>late@100@final_bib", "E|rrf-prod>late@100@dense", "L|late-full@final",
             "L|late-full@final_bib", "L|late-full@dense", "C|rrf-prod", "A|bm25-os", "A|bm25-lab@final",
             "A|bm25-lab@final_bib", "B|dense", "E|hybrid-late-api", "R|E|rrf-prod>late@100>rerank@8"]


def category_table(sets: dict, label: str, track: str, cat: str, names: list[str]) -> list[str]:
    tr = sets[label][track]
    out = [f"**Категория {cat} ({label} / {track})**", "", "| система | n | nDCG@10 | R@50 | judged@10 |",
           "|---|---|---|---|---|"]
    for n in names:
        r = tr["systems"].get(n) or {}
        c = (r.get("categories") or {}).get(cat) if r.get("status") == "OK" else None
        if c:
            out.append(f"| {esc(n)} | {int(c.get('n_queries', 0))} | {f3(c.get('ndcg@10'))} | {f3(c.get('recall@50'))} | "
                       f"{f3(c.get('judged@10'))} |")
    return out + [""]


def incidents(res: dict) -> list[str]:
    out = []
    if res.get("not_run"):
        out += ["**NOT_RUN**", ""] + [f"- {esc(x['system'])} / {x['track']}: {x['reason']}" for x in res["not_run"]] + [""]
    for inc in res.get("incidents", []):
        out += [f"**Инцидент:** {inc['what']}", ""] + [f"- {k}: {v}" for k, v in inc.items() if k != "what"] + [""]
    return out


def rerank_table(sets: dict, label: str) -> list[str]:
    out = ["**Реранкер (EDGE jina-reranker-v3.5 через /v1/rerank/text)**", "",
           "| система | трек | вызовов | ошибок | p50, с | p95, с | обрезано пассажей | отклонено | 413→7 |",
           "|---|---|---|---|---|---|---|---|---|"]
    for track, tr in sets[label].items():
        for n, r in tr["systems"].items():
            rr = r.get("rerank") if isinstance(r, dict) else None
            if rr:
                out.append(f"| {esc(COL.get(n, n))} | {track} | {rr['calls_ok']} | {rr['errors']} | {f3(rr['latency_s_p50'])} | "
                           f"{f3(rr['latency_s_p95'])} | {rr['truncated_total']} | {rr['rejected_total']} | "
                           f"{rr['fallback_413']} |")
    return out + [""]


def latency_table(lat: dict) -> list[str]:
    off = lat.get("offline", {})
    out = ["**Задержки**", "", "| этап | значение |", "|---|---|"]
    for k, v in sorted(off.items()):
        if v is not None:
            out.append(f"| offline: {esc(k)} | {v} |")
    for k in ("encode_dense_ms", "encode_late_ms", "encode_wall_ms"):
        if k in lat:
            out.append(f"| кодирование запроса ({k[7:]}) | p50 {lat[k]['p50']:.1f} мс, p95 {lat[k]['p95']:.1f} мс |")
    for k, v in lat.items():
        if k.startswith("api|"):
            t = ", ".join(f"{s} {x['p50']:.0f}" for s, x in sorted(v.get("timings_ms", {}).items()) if x["p50"] >= 0.5)
            ms = lambda x: "—" if x is None else f"{x:.0f}"  # noqa: E731
            out.append(f"| API {k[4:]}: клиент p50 / p95 | {ms(v.get('wall_ms_p50'))} / {ms(v.get('wall_ms_p95'))} мс"
                       + (f"; сервер p50, мс: {t}" if t else "") + " |")
        if k.startswith("rerank|"):
            out.append(f"| реранкер {esc(COL.get(k[7:], k[7:]))}: вызов | p50 {v['p50_s']:.1f} с, p95 {v['p95_s']:.1f} с "
                       f"(n={v['n']}) |")
    return out + [""]


def main() -> None:
    res = json.load(open(V1 / "out" / "results.json", encoding="utf-8"))
    sets = res["sets"]
    snap = res["snapshot"]
    snap_id = snap.get("snapshot_id") if isinstance(snap, dict) else snap
    lines = [f"Снимок: `{snap_id}`; dense-индекс: `{res['dense_units_snapshot']}`; пул-меток: "
             f"{res['pooled_labels']}.", ""]
    for label in sets:
        for track in ("text", "visual"):
            lines += main_table(sets, label, track)
    keys = ["E|rrf-prod>late@100 ~ L|late-full@final", "E|rrf-prod>late@100 ~ E|rrf-prod>late@50",
            "E|rrf-prod>late@200 ~ E|rrf-prod>late@100", "E|rrf-prod>late@100 ~ E|rrf-prod>late@30",
            "E|rrf-prod>late@100 ~ E|bm25-os>late@100", "E|rrf-prod>late@100@final_bib ~ E|rrf-prod>late@100",
            "E|rrf-prod>late@100@dense ~ E|rrf-prod>late@100", "L|late-full@final_bib ~ L|late-full@final",
            "L|late-full@dense ~ L|late-full@final", "A|bm25-lab@final_bib ~ A|bm25-lab@final",
            "F3|rrf(bm25-os,dense,late-full) ~ E|rrf-prod>late@100", "C|hybrid-api ~ C|rrf-prod",
            "C|rrf-prod ~ B|dense", "A|bm25-lab@final ~ A|bm25-os", "E|rrf-lab>late@100 ~ E|rrf-prod>late@100",
            "E|rrf-unit-lab>late@dense ~ E|rrf-prod>late@100",
            "R|E|rrf-prod>late@100>rerank@8 ~ E|rrf-prod>late@100",
            "R|E|rrf-prod>late@100>rerank@12 ~ E|rrf-prod>late@100",
            "R|E|rrf-prod>late@100>rerank@12 ~ R|E|rrf-prod>late@100>rerank@8",
            "E|hybrid-late-api ~ E|rrf-prod>late@100", "E|hybrid-late-api ~ E|rrf-prod>late@100@final_bib",
            "E|hybrid-late-api ~ C|hybrid-api", "A|bm25-os@recheck ~ A|bm25-os"]
    for label in sets:
        for track in ("text", "visual"):
            lines += pairs_table(sets, label, track, keys)
    slice_names = [n for n in MAIN if n not in ("C|hybrid-api", "E|hybrid-late-api")] + ["E|rrf-prod>late@100@final_bib"]
    for label in sets:
        lines += slices_table(sets, label, "text", slice_names)
        lines += slices_table(sets, label, "visual", slice_names)
    for label in sets:
        lines += category_table(sets, label, "text", "bibliography", BIB_NAMES)
    lines += rerank_table(sets, "verified")
    lines += latency_table(res.get("latency", {}))
    lines += incidents(res)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
