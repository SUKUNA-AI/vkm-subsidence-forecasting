"""TOPIC_BENCHMARK_V1 — every system on truth V (catalogue only: the frozen set) and on truth P (catalogue + pooled
labels ``LLM_AGENT_T1`` with grade ≥ 2), P3 (grade 3 only, sensitivity) and judged@k (POOL_LABELING_V1.md §7).

usage: PYTHONPATH=src python benchmarks/topic_v1/scripts/score_t1.py [--out benchmarks/topic_v1/results_pool_t1.json]
                                                                      [--tables]
Environment: ``TOPIC_RUNS`` and ``TD_WORK`` as for pool_t1.py (the rankings are rebuilt from the frozen raw answers).
The metric code is the frozen ``vkm_corpus.retrieval_lab.topic_bench``; ``metrics_spec_v1.json`` and ``results_v1.json``
are not touched. The output holds IDs and numbers only. ``p_topics`` builds truth P for any other scorer (agent GS).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BENCH = REPO / "benchmarks" / "topic_v1"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402

import pool_t1 as PT  # noqa: E402

POOLED = ("hybrid_late", "hybrid_nolate", "bm25", "nav", "D1")
PARTIAL = ("hybrid_late_pool", "hybrid_late_kinds", "hybrid_late_drill", "D0")
SYSTEMS = POOLED + PARTIAL
JUDGED_K = (10, 20, 50)
SECONDARY = (("D1", "hybrid_late"),)
MAIN_METRICS = ("page_recall@10", "page_recall@20", "page_recall@50", "mrr@50", "source_recall@10", "success@10",
                "section_hit@10", "section_pages@10")


def p_topics(topics: list[TB.Topic], rows: list[dict[str, str]], groups: list[dict], min_grade: int = 2
             ) -> list[TB.Topic]:
    """Truth P (``min_grade`` 2) or P3 (3): the catalogue targets plus one target per pooled label unit with a grade
    ≥ ``min_grade`` — its first page, the unit's other pages and duplicate aliases as alternates.

    ``rows`` — ``bench.load_pooled_qrels(qrels_topic_v1_pooled.tsv)``; ``groups`` —
    ``qrels_topic_v1_pooled_groups.json["groups"]`` (units with several pages or with aliases)."""
    unit_of: dict[tuple[str, str], tuple[str, tuple[str, ...], tuple[str, ...]]] = {}
    for g in groups:
        for p in g["pages"]:
            unit_of[(g["topic_id"], p)] = (g["pages"][0], tuple(g["pages"]), tuple(g["aliases"]))
    by_topic: dict[str, dict[str, tuple[int, tuple[str, ...], tuple[str, ...]]]] = defaultdict(dict)
    for r in rows:
        tid, p = r["query_id"], r["doc_id"]
        first, pages, aliases = unit_of.get((tid, p), (p, (p,), ()))
        by_topic[tid][first] = (int(r["grade"]), pages, aliases)
    out = []
    for t in topics:
        extra = []
        for first, (grade, pages, aliases) in sorted(by_topic.get(t.topic_id, {}).items()):
            if grade < min_grade:
                continue
            alts = tuple(dict.fromkeys(x for x in (*pages, *aliases) if x != first))
            extra.append(TB.Target(f"{t.topic_id}/P{len(extra) + 1:03d}", first, first.split(":")[0], alts,
                                   2 if grade == 3 else 1, "POOL_T1"))
        out.append(TB.Topic(t.topic_id, t.track, t.group, t.title, t.queries, t.targets + tuple(extra)))
    return out


def judged_pages(topics: list[TB.Topic], rows: list[dict[str, str]] | None, groups: list[dict]
                 ) -> dict[str, frozenset[str]]:
    """Pages that carry a judgment for each topic: catalogue target pages (V); with ``rows`` also every pooled page
    of any grade and the aliases of its unit (P)."""
    out = {t.topic_id: set(PT.target_pages(t)) for t in topics}
    if rows is not None:
        alias = defaultdict(set)
        for g in groups:
            for p in g["pages"]:
                alias[(g["topic_id"], p)] |= set(g["pages"]) | set(g["aliases"])
        for r in rows:
            out[r["query_id"]] |= {r["doc_id"]} | alias.get((r["query_id"], r["doc_id"]), set())
    return {k: frozenset(v) for k, v in out.items()}


def judged_at(r: TB.Ranking, judged: frozenset[str], k: int) -> float | None:
    n = min(k, len(r.pages))
    if n == 0:
        return None
    return sum(1 for al in r.aliases[:k] if al & judged) / n


def score_rows(topics: list[TB.Topic], rankings: dict[str, dict[str, TB.Ranking]],
               judged: dict[str, frozenset[str]]) -> list[dict]:
    rows = []
    for t in topics:
        for q in t.queries:
            for s in SYSTEMS:
                r = rankings.get(s, {}).get(q.query_id)
                if r is None:
                    continue
                row = {"query_id": q.query_id, "topic_id": t.topic_id, "system": s, "track": t.track,
                       "group": t.group, "variant": q.variant, "n_targets": len(t.targets), **TB.query_metrics(t, r)}
                for k in JUDGED_K:
                    row[f"judged@{k}"] = judged_at(r, judged[t.topic_id], k)
                rows.append(row)
    return rows


def judged_means(rows: list[dict], keys: tuple[str, ...] = ("system",)) -> dict[str, dict[str, float | None]]:
    acc: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        g = "|".join(str(r[k]) for k in keys)
        for k in JUDGED_K:
            if r[f"judged@{k}"] is not None:
                acc[g][f"judged@{k}"].append(r[f"judged@{k}"])
    return {g: {m: round(sum(v) / len(v), 4) if v else None for m, v in ms.items()} for g, ms in sorted(acc.items())}


def kendall_tau(a: list[str], b: list[str]) -> float:
    pos = {s: i for i, s in enumerate(b)}
    items = [s for s in a if s in pos]
    conc = disc = 0
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            d = pos[items[i]] - pos[items[j]]
            conc += d < 0
            disc += d > 0
    n = conc + disc
    return round((conc - disc) / n, 4) if n else float("nan")


def order(summary: dict, systems: tuple[str, ...], metric: str) -> list[str]:
    return sorted(systems, key=lambda s: (-(summary[s][metric] or 0.0), s))


def label_stats(rows: list[dict[str, str]], groups: list[dict], topics: list[TB.Topic]) -> dict:
    track = {t.topic_id: t.track for t in topics}
    later = {(g["topic_id"], p) for g in groups for p in g["pages"][1:]}
    units = [r for r in rows if (r["query_id"], r["doc_id"]) not in later]
    sweep = PT.SWEEP_SOURCES

    def origin(r: dict[str, str]) -> str:
        return PT.origin({x.split("@")[0]: int(x.split("@")[1]) for x in r["pooled_from"].split(",")})

    out = {"rows": len(rows), "units": len(units),
           "by_grade": dict(sorted(Counter(int(r["grade"]) for r in units).items())),
           "by_code": dict(sorted(Counter(r["rationale"] for r in units).items())),
           "by_track_grade": {tr: dict(sorted(Counter(int(r["grade"]) for r in units if track[r["query_id"]] == tr
                                                      ).items())) for tr in TB.TRACKS},
           "by_sources_grade": {name: dict(sorted(Counter(int(r["grade"]) for r in units
                                                          if (r["doc_id"].split(":")[0] in sweep) == inside).items()))
                                for name, inside in (("catalogued_39", True), ("other_sources", False))},
           "by_origin_grade": {o: dict(sorted(Counter(int(r["grade"]) for r in units if origin(r) == o).items()))
                               for o in ("nav_only", "one_other", "several")},
           "evidence_units_by_system": {}, "evidence_share_by_system": {}}
    by_sys = defaultdict(list)
    for r in units:
        for x in r["pooled_from"].split(","):
            by_sys[x.split("@")[0]].append(int(r["grade"]))
    for s, gs in sorted(by_sys.items()):
        out["evidence_units_by_system"][s] = sum(1 for g in gs if g >= 2)
        out["evidence_share_by_system"][s] = round(sum(1 for g in gs if g >= 2) / len(gs), 4)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(BENCH / "results_pool_t1.json"))
    ap.add_argument("--tables", action="store_true", help="print the markdown tables of RESULTS_POOL_T1.md")
    args = ap.parse_args()
    spec = json.loads((BENCH / "metrics_spec_v1.json").read_text(encoding="utf-8"))
    topics = PT.load_topics()
    rankings, index, info = PT.all_rankings(topics)
    ctl = PT.control(topics, rankings)
    if ctl["mismatches"]:
        raise SystemExit("recomputed rankings do not reproduce the published per-query metrics")
    for arm in PT.DOSSIER_ARMS:                     # page systems: nodes = the deepest sections of the first 10 pages
        for r in rankings[arm].values():
            TB._nodes_from_pages(r, index)
    qrels = B.load_pooled_qrels(PT.OUT_TSV)
    problems = PT.validate_rows(qrels, topics)
    if problems:
        raise SystemExit("\n".join(problems[:20]))
    groups = json.load(open(PT.OUT_GROUPS, encoding="utf-8"))["groups"]
    truths = {"V": topics, "P": p_topics(topics, qrels, groups, 2), "P3": p_topics(topics, qrels, groups, 3)}
    judged = {"V": judged_pages(topics, None, groups), "P": judged_pages(topics, qrels, groups)}
    judged["P3"] = judged["P"]
    levels = spec["acceptance_levels"]["levels"]
    out: dict = {
        "benchmark": "TOPIC_BENCHMARK_V1", "addition": "pooled labels T1 (POOL_LABELING_V1.md)",
        "preregistration_sha256": PT.sha256_file(BENCH / "POOL_LABELING_V1.md", lf=True),
        "spec_sha256": PT.sha256_file(BENCH / "metrics_spec_v1.json", lf=True), "set_sha256": PT.SET_SHA256,
        "qrels_sha256": PT.sha256_file(PT.OUT_TSV), "groups_sha256": PT.sha256_file(PT.OUT_GROUPS),
        "canonical_snapshot_id": spec["canonical_snapshot_id"], "inputs": info, "control": ctl,
        "systems": {"pooled": list(POOLED), "partial_pool": list(PARTIAL)},
        "labels": label_stats(qrels, groups, topics),
        "truths": {}, "summary": {}, "by_track": {}, "judged": {}, "judged_by_track": {}, "acceptance": {},
        "comparisons_primary": {}, "comparisons_secondary": {}, "order": {}}
    rows_by_truth = {}
    for name, ts in truths.items():
        n_t = sum(len(t.targets) for t in ts)
        new = [x for t in ts for x in t.targets if x.mapping == "POOL_T1"]
        out["truths"][name] = {"targets": n_t, "new_targets": len(new),
                               "new_targets_by_track": dict(Counter(t.track for t in ts for x in t.targets
                                                                    if x.mapping == "POOL_T1")),
                               "new_targets_outside_catalogued_sources": sum(
                                   1 for x in new if x.source_id not in PT.SWEEP_SOURCES),
                               "target_sources": len({x.source_id for t in ts for x in t.targets})}
        rows = score_rows(ts, rankings, judged[name])
        rows_by_truth[name] = rows
        summ = TB.aggregate(rows, ["system"])
        out["summary"][name] = summ
        out["by_track"][name] = TB.aggregate(rows, ["system", "track"])
        out["judged"][name] = judged_means(rows)
        out["judged_by_track"][name] = judged_means(rows, ("system", "track"))
        out["acceptance"][name] = {s: TB.acceptance(summ[s], levels) for s in SYSTEMS if s in summ}
        if name in ("V", "P"):
            pooled_rows = [r for r in rows if r["system"] in POOLED]
            out["comparisons_primary"][name] = TB.compare_systems(pooled_rows, TB.PRIMARY_COMPARISONS)
            out["comparisons_secondary"][name] = TB.compare_systems(pooled_rows, SECONDARY)
            out["order"][name] = {m: order(summ, POOLED, m) for m in ("page_recall@20", "mrr@50", "page_recall@50")}
    out["order"]["kendall_tau_V_P"] = {m: kendall_tau(out["order"]["V"][m], out["order"]["P"][m])
                                       for m in ("page_recall@20", "mrr@50", "page_recall@50")}
    cols = ["query_id", "system", "page_recall@10", "page_recall@20", "page_recall@50", "mrr@50", "success@10",
            "source_recall@10", "judged@10"]
    per_query = [[(round(r[c], 4) if isinstance(r[c], float) else r[c]) for c in cols] for r in rows_by_truth["P"]]
    head = json.dumps(out, ensure_ascii=False, indent=1)
    text = (head[:-2] + ',\n "per_query_P": {\n  "columns": ' + json.dumps(cols) + ',\n  "rows": [\n'
            + ",\n".join("   " + json.dumps(r, ensure_ascii=False) for r in per_query) + "\n  ]\n }\n}\n")
    json.loads(text)
    Path(args.out).write_bytes(text.encode("utf-8"))
    print("written", args.out)
    if args.tables:
        tables(out)


def fmt(x: float | None, nd: int = 3) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:.{nd}f}".replace(".", ",")


def tables(out: dict) -> None:
    print("\n### V и P по системам\n")
    print("| Система | R@10 V | R@10 P | R@20 V | R@20 P | R@50 V | R@50 P | MRR V | MRR P | S@10 V | S@10 P | "
          "Src R@10 V | Src R@10 P | judged@10 V | judged@10 P |")
    print("|---|" + "---|" * 14)
    for s in SYSTEMS:
        v, p = out["summary"]["V"][s], out["summary"]["P"][s]
        jv, jp = out["judged"]["V"][s], out["judged"]["P"][s]
        name = f"`{s}`" + (" *(PARTIAL_POOL)*" if s in PARTIAL else "")
        print(f"| {name} | " + " | ".join(fmt(x) for x in (
            v["page_recall@10"], p["page_recall@10"], v["page_recall@20"], p["page_recall@20"], v["page_recall@50"],
            p["page_recall@50"], v["mrr@50"], p["mrr@50"], v["success@10"], p["success@10"], v["source_recall@10"],
            p["source_recall@10"], jv["judged@10"], jp["judged@10"])) + " |")
    print("\n### P3 (только оценка 3) и judged@20/50\n")
    print("| Система | R@20 P3 | R@50 P3 | MRR P3 | S@10 P3 | judged@20 V | judged@20 P | judged@50 V | judged@50 P |")
    print("|---|" + "---|" * 8)
    for s in SYSTEMS:
        p3 = out["summary"]["P3"][s]
        jv, jp = out["judged"]["V"][s], out["judged"]["P"][s]
        print(f"| `{s}` | " + " | ".join(fmt(x) for x in (
            p3["page_recall@20"], p3["page_recall@50"], p3["mrr@50"], p3["success@10"], jv["judged@20"],
            jp["judged@20"], jv["judged@50"], jp["judged@50"])) + " |")
    print("\n### Проверки\n")
    for name in ("V", "P"):
        for k, v in {**out["comparisons_primary"][name], **out["comparisons_secondary"][name]}.items():
            print(f"{name} | {k} | Δ {v['delta']:+.4f} [{v['ci_lo']:+.4f}; {v['ci_hi']:+.4f}] p {v['p_value']} "
                  f"p_holm {v['p_holm']}")
    print("\n### Порядок\n")
    print(json.dumps(out["order"], ensure_ascii=False))
    print("\n### По трекам (P)\n")
    for g, m in out["by_track"]["P"].items():
        print(g, {k: m[k] for k in ("page_recall@20", "page_recall@50", "mrr@50", "success@10")})
    for g, m in out["by_track"]["V"].items():
        print("V", g, {k: m[k] for k in ("page_recall@20", "page_recall@50", "mrr@50", "success@10")})
    print("\n### Приёмка\n")
    for name in ("V", "P", "P3"):
        print(name, {s: (a["pass"], {m: a["levels"][m]["value"] for m in a["levels"]}) for s, a in
                     out["acceptance"][name].items()})
    print(json.dumps(out["labels"], ensure_ascii=False))
    print(json.dumps(out["truths"], ensure_ascii=False))


if __name__ == "__main__":
    main()
