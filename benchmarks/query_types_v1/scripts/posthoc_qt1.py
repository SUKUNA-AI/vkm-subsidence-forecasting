"""QUERY_TYPES_V1 / QT1 — post-hoc descriptive analyses after the labels (not pre-registered; protocol
``POOL_LABELING_QT1.md`` §7 names only the agreement counts with the automatic truth).

1. Circular automatic truth against the labels on the same pool and the same metric machinery (``pool_qt1``: copy
   groups, frozen ``metrics.ndcg_at`` / ``precision_at``, ideal from the pool): variant ``labels`` (grades 0–3, the
   pre-registered main metric), ``labels_binary`` (grade ≥ 2 → relevant) and ``auto_binary`` (pool pair relevant when its page
   is a borehole-catalogue locator of the query, alt pages included (C), or belongs to a registry source of the author
   (D)); a relevant pair of a binary variant gets gain 2 (nDCG is scale-free, P@10 counts gain ≥ 2).
   Δ = hybrid − hybrid_noroutes and hybrid − bm25 (sign-flip test and bootstrap as in §7, no Holm, no verdict).
2. Disagreements of the two truths on the pool by the systems that brought the pair (who gets credit the other truth
   denies).
3. Sensitivity of the C verdict to the pairs the labellers flagged (carry-over from a larger copy of the same scheme on
   another page; borderline map labels without a legend; an interpolated point): grades lowered against the route,
   the full Holm family of four recomputed, the §7 rule reapplied.

usage: PYTHONPATH=src python benchmarks/query_types_v1/scripts/posthoc_qt1.py [--out results_qt1_posthoc.json]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import pool_qt1 as Q

CARRY_OVER = (("QT-C-B5", "VKM-SRC-011:p0024"), ("QT-C-B7", "VKM-SRC-014:p0017"), ("QT-C-B8", "VKM-SRC-011:p0024"))
BORDERLINE = (("QT-C-B4", "VKM-SRC-012:p0067", 1), ("QT-C-B8", "VKM-SRC-012:p0028", 1),
              ("QT-C-B6", "VKM-SRC-012:p0028", 1), ("QT-C-B2", "VKM-SRC-011:p0131", 0))


def auto_relevant(q: dict, page: str) -> bool:
    if q["qt"] == "C":
        truth = {t["page_id"] for t in q["truth"]["targets"]} | {a for t in q["truth"]["targets"]
                                                                for a in t.get("alt_page_ids") or ()}
        return page in truth
    return Q.page_parts(page)[0] in set(q["truth"]["source_ids"])


def variant_qrels(queries: list[dict], pool: dict, labels: dict, kind: str) -> dict:
    qmap = {q["query_id"]: q for q in queries}
    out: dict = {}
    for x in pool["pairs"]:
        qid, p = x["query_id"], x["page_id"]
        g = labels[qid][p]
        if kind == "labels":
            v = g
        elif kind == "labels_binary":
            v = 2 if g != Q.UNKNOWN and g >= 2 else 0
        else:
            v = 2 if auto_relevant(qmap[qid], p) else 0
        out.setdefault(qid, {})[p] = v
    return out


def deltas(rows: list[dict], metric: str) -> dict:
    return {f"{t} | {a} - {b}": Q.compare(rows, t, a, b, metric) for t in ("C", "D") for a, b in Q.PRIMARY_PAIRS}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Q.BENCH / "results_qt1_posthoc.json"))
    args = ap.parse_args()
    w = Q.work_dir()
    pool = json.loads((w / "pool_qt1.json").read_text(encoding="utf-8"))
    queries = Q.load_queries()
    runs, _m, files = Q.load_runs([w / "runs" / Q.RUN_NAME])
    if [f["sha256"] for f in files] != [f["sha256"] for f in pool["runs"]["files"]]:
        raise SystemExit("runs differ from the pooled runs")
    labels = Q.read_qrels(Q.QRELS_OUT)
    gid = Q.group_map(pool["alias_groups"])
    res: dict = {"kind": "post-hoc, descriptive (not pre-registered)", "qrels_sha256":
                 Q.sha256_file(Q.QRELS_OUT), "pool_sha256": Q.sha256_file(w / "pool_qt1.json"), "truths": {}}
    for kind in ("labels", "labels_binary", "auto_binary"):
        qr = variant_qrels(queries, pool, labels, kind)
        rows, excl = Q.metric_rows(queries, runs, qr, gid, 0)
        res["truths"][kind] = {"excluded_queries": excl,
                               "mean": {"ndcg@10": Q.means(rows, "ndcg@10"), "p@10": Q.means(rows, "p@10")},
                               "ndcg@10": deltas(rows, "ndcg@10"), "p@10": deltas(rows, "p@10"),
                               "per_query_ndcg@10": {f"{r['query_id']} | {r['system']}": r["ndcg@10"]
                                                     for r in rows}}
    # 2. disagreements of the two truths, by the systems that brought the pair
    qmap = {q["query_id"]: q for q in queries}
    dis: dict = {}
    for x in pool["pairs"]:
        q = qmap[x["query_id"]]
        g = labels[x["query_id"]][x["page_id"]]
        hi = 2 if q["qt"] == "C" else 3       # C: data or substantive mention; D: page of the author's own work
        lab = "UNKNOWN" if g == Q.UNKNOWN else ("REL" if g >= hi else "NONREL")
        auto = "AUTO_REL" if auto_relevant(q, x["page_id"]) else "AUTO_NONREL"
        key = f"{auto} / LABEL_{lab}"
        d = dis.setdefault(q["qt"], {}).setdefault(key, {"n": 0, "grades": Counter(), "brought_by": Counter(),
                                                         "pairs": []})
        d["n"] += 1
        d["grades"][str(g)] += 1
        for s in x["systems"]:
            d["brought_by"][s] += 1
        if auto == "AUTO_REL" and lab != "REL" or auto == "AUTO_NONREL" and lab == "REL":
            d["pairs"].append({"query_id": x["query_id"], "page_id": x["page_id"], "grade": g,
                               "systems": x["systems"]})
    res["disagreements"] = {t: {k: {**v, "grades": dict(v["grades"]), "brought_by": dict(v["brought_by"])}
                                for k, v in sorted(d.items())} for t, d in dis.items()}
    # 3. sensitivity of the verdicts to the flagged C pairs (grades lowered against the route)
    n_type = Counter(x["type"] for x in pool["pairs"])
    unk = Counter(x["type"] for x in pool["pairs"] if labels[x["query_id"]][x["page_id"]] == Q.UNKNOWN)
    share = {t: unk[t] / n_type[t] for t in ("C", "D")}
    sens: dict = {}
    for name, carry, border in (("carry_over_to_0", True, False), ("borderline_lowered", False, True),
                                ("both", True, True)):
        qr = {qid: dict(v) for qid, v in labels.items()}
        changed = []
        if carry:
            for qid, p in CARRY_OVER:
                changed.append({"query_id": qid, "page_id": p, "from": qr[qid][p], "to": 0})
                qr[qid][p] = 0
        if border:
            for qid, p, to in BORDERLINE:
                changed.append({"query_id": qid, "page_id": p, "from": qr[qid][p], "to": to})
                qr[qid][p] = to
        rows, excl = Q.metric_rows(queries, runs, qr, gid, 0)
        fam = Q.family(rows, "ndcg@10")
        sens[name] = {"changed": changed, "excluded_queries": excl, "mean_ndcg@10": Q.means(rows, "ndcg@10"),
                      "primary_ndcg@10": fam,
                      "verdicts": {t: Q.VERDICT_RU[Q.verdict(fam[f"{t} | hybrid - hybrid_noroutes"], share[t])]
                                   for t in ("C", "D")}}
        for c in changed:
            c["systems"] = next(x["systems"] for x in pool["pairs"]
                                if x["query_id"] == c["query_id"] and x["page_id"] == c["page_id"])
    res["sensitivity_flagged_C"] = sens
    res = Q.rnd(res)
    sha = Q.write_json(Path(args.out), res)
    print(f"wrote {args.out} sha256 {sha}")


if __name__ == "__main__":
    main()
