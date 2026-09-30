"""TERM_DICTIONARY_V1 stage C: the dossier's page lists replayed through ``DossierBuilder._retrieve`` (the production
fusion code) over the stage-B searches, the metrics, paired tests and the preregistered decision (§5–§6).

Environment: ``TD_WORK`` (``stage_a.json``, ``stage_b.json``, ``root/``). Writes
``benchmarks/term_dictionary_v1/results_v1.json`` (IDs and numbers only) and prints the summary. ``TD_VARIANT=nolate``
evaluates the exploratory RRF-only searches (``stage_b_nolate.json`` → ``results_v1_nolate.json``); the blocks
``exploratory`` (R@100, R@20, R@10 and the relevant pages that enter or leave the top 100) were added after the
preregistered results were read and take no part in the decision.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from vkm_corpus.api import topic as T  # noqa: E402
from vkm_corpus.api.canon import CanonStore  # noqa: E402
from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab import metrics as M  # noqa: E402
from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402

WORK = Path(os.environ["TD_WORK"])
VARIANT = os.environ.get("TD_VARIANT", "")          # "" = preregistered; "nolate" = exploratory RRF-only
OUT = REPO / "benchmarks" / "term_dictionary_v1" / (f"results_v1_{VARIANT}.json" if VARIANT else "results_v1.json")
SEED, N_PERM = 20260929, 10_000
Q1_METRICS = ("page_recall@50", "page_recall@20", "mrr@50", "success@10")
Q2_METRICS = ("ndcg@10", "recall@50", "mrr@10")
EXPLORATORY_METRICS = ("recall@100", "recall@20", "recall@10")   # added after the preregistered results were read
EPS = 0.005


class Replay:
    def __init__(self, searches: dict[str, list[str]]) -> None:
        self.searches = searches

    def search(self, query, *, source_ids=None, limit=50):
        pages = self.searches[("CORE" if source_ids else "ALL") + "\t" + query][:limit]
        return {"engine": "P-E", "units": [{"page_id": p, "source_id": p.split(":")[0], "rank": i,
                                            "unit_id": None, "object_ids": []} for i, p in enumerate(pages, 1)]}


class ReplayBuilder(T.DossierBuilder):
    def _formulations(self, st):
        return st.recorded


def compare(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    t = M.paired_randomization(a, b, n=N_PERM, seed=SEED)
    ci = M.bootstrap_ci(a, b, n=N_PERM, seed=SEED)
    return {"n": t["n"], "delta": round(t["delta"], 6), "p_value": round(t["p_value"], 6),
            "ci_lo": round(ci["lo"], 6), "ci_hi": round(ci["hi"], 6)}


def mcompare(ea, eb, metric: str, ids: list[str] | None = None) -> dict:
    """Paired comparison of two lab evaluations on ``metric`` with the preregistered seed (§5)."""
    sel = set(ids) if ids is not None else None
    a = {q: m[metric] for q, m in ea.per_query.items() if metric in m and (sel is None or q in sel)}
    b = {q: m[metric] for q, m in eb.per_query.items() if metric in m and (sel is None or q in sel)}
    return {"metric": metric, **compare(a, b)}


def not_worse(c: dict[str, float]) -> bool:
    return c["delta"] >= -EPS and not (c["delta"] < 0 and c["p_value"] < 0.05)


def source_languages(canon_path: Path) -> dict[str, str]:
    import duckdb

    con = duckdb.connect(str(canon_path), read_only=True)
    rows = con.execute("SELECT l.source_id, w.languages FROM canonical.source_work_links l JOIN canonical.works w "
                       "ON w.work_id = l.work_id WHERE l.is_primary").fetchall()
    con.close()
    return {s: (langs or ["?"])[0] for s, langs in rows}


def q1(a: dict, b: dict) -> dict:
    root = WORK / "root"
    snap = a["snapshot_id"]
    canon = CanonStore(root / "duckdb" / "vkm_corpus.duckdb", current_snapshot=lambda: snap)
    builder = ReplayBuilder(canon, None, None, Replay(b["dossier"]), cache={})
    topics = {t.topic_id: t for t in TB.load_set(REPO / "benchmarks/topic_v1/topic_set_v1.jsonl")}
    core = set(a["dossier"]["core_sources"])
    per_query: dict[str, dict[str, dict[str, float]]] = {"D0": {}, "D1": {}}
    applied = set()
    for qid, q in sorted(a["dossier"]["queries"].items()):
        t = topics[q["topic_id"]]
        text = next(x.text for x in t.queries if x.query_id == qid)
        for arm in ("D0", "D1"):
            st = T._State(req=T.TopicRequest(query=text, translate=(arm == "D1")), stems=T.query_stems(text))
            st.core = set(core)
            st.recorded = q[arm]["formulations"]
            builder._retrieve(st)
            pages = builder._page_list(st)
            r = TB.Ranking()
            for p in pages["core"] + pages["rest"]:
                r.add_page(p["page_id"])
            per_query[arm][qid] = TB.query_metrics(t, r)
        if any(f["kind"] == "translation" for f in q["D1"]["formulations"]):
            applied.add(qid)
    langs = source_languages(root / "duckdb" / "vkm_corpus.duckdb")
    mixed = {tid for tid, t in topics.items() if any(langs.get(s) == "en" for s in t.sources)}
    by_topic = {arm: defaultdict(dict) for arm in per_query}
    for arm, rows in per_query.items():
        for qid, m in rows.items():
            by_topic[arm][a["dossier"]["queries"][qid]["topic_id"]][qid] = m

    def topic_means(arm: str, metric: str, only: set[str] | None = None) -> dict[str, float]:
        return {tid: sum(m[metric] for m in qs.values()) / len(qs) for tid, qs in by_topic[arm].items()
                if only is None or tid in only}

    res: dict = {"n_topics": len(by_topic["D0"]), "n_queries": len(per_query["D0"]),
                 "translation_applied_queries": len(applied), "mixed_topics": len(mixed), "overall": {}, "mixed": {},
                 "applied_queries": {}}
    for metric in Q1_METRICS:
        d0, d1 = topic_means("D0", metric), topic_means("D1", metric)
        res["overall"][metric] = {"D0": round(sum(d0.values()) / len(d0), 6), "D1": round(sum(d1.values()) / len(d1), 6),
                                  **compare(d1, d0)}
        m0, m1 = topic_means("D0", metric, mixed), topic_means("D1", metric, mixed)
        if m0:
            res["mixed"][metric] = {"D0": round(sum(m0.values()) / len(m0), 6),
                                    "D1": round(sum(m1.values()) / len(m1), 6), **compare(m1, m0)}
        q0 = {q: per_query["D0"][q][metric] for q in applied}
        q1_ = {q: per_query["D1"][q][metric] for q in applied}
        if q0:
            res["applied_queries"][metric] = {"D0": round(sum(q0.values()) / len(q0), 6),
                                              "D1": round(sum(q1_.values()) / len(q1_), 6), **compare(q1_, q0)}
    ok = not_worse(res["overall"]["page_recall@50"]) and not_worse(res["overall"]["mrr@50"])
    res["decision"] = {"rule": "D1 kept if Δ page_recall@50 and Δ mrr@50 are both >= -0.005 and not significantly "
                               "negative (PREREGISTRATION §6)", "translate_default": ok}
    res["per_query"] = {arm: {q: {k: round(v, 4) for k, v in m.items() if k in Q1_METRICS} for q, m in rows.items()}
                        for arm, rows in per_query.items()}
    return res


def exploratory(ev: dict, runs: dict, judged: dict, applied: list[str]) -> dict:
    """Post hoc (not preregistered): deeper and shallower recall, and where the relevant pages that enter or leave
    the top 100 of the translated queries stood."""
    out: dict = {"note": "added after the preregistered results were read; not part of the decision"}
    for metric in EXPLORATORY_METRICS:
        out[metric] = {name: mcompare(ev["H1"], ev["H0"], metric, ids) for name, ids in (("all", None),
                                                                                         ("applied", applied))}
    lost: Counter = Counter()
    gained: Counter = Counter()
    for q in applied:
        rel = {p for p, g in judged.get(q, {}).items() if g >= 2}
        h0, h1 = runs["H0"][q][:100], runs["H1"][q][:100]
        for p in rel & (set(h0) - set(h1)):
            lost["1-50" if h0.index(p) < 50 else "51-100"] += 1
        for p in rel & (set(h1) - set(h0)):
            gained["1-50" if h1.index(p) < 50 else "51-100"] += 1
    out["top100_relevant_pages_of_applied_queries"] = {
        "left_top100_rank_in_H0": dict(sorted(lost.items())), "entered_top100_rank_in_H1": dict(sorted(gained.items()))}
    return out


def q2(a: dict, b: dict) -> dict:
    bench = B.load_benchmark(REPO / "benchmarks/retrieval_v0")
    covered = json.load(open(Path(os.environ["J_V1"]) / "prepare.json", encoding="utf-8"))["covered_queries"]
    qs = [q for q in bench.queries if q.track == "text" and q.query_id in covered]
    qids = [q.query_id for q in qs]
    verified = bench.judgments(level="PAGE")
    pooled_rows = B.load_pooled_qrels(REPO / "benchmarks/retrieval_v1/qrels_v1_pooled.tsv") + \
        B.load_pooled_qrels(REPO / "benchmarks/retrieval_v2/qrels_v2_pooled.tsv")
    pooled = B.merge_judgments(verified, B.pooled_judgments(pooled_rows))
    hn = bench.hard_negative_ids("PAGE")
    rt = a["retrieval_translations"]
    applied = [q for q in qids if rt.get(q, {}).get("translation")]
    xling = [q for q in qids if q.startswith(("T-RUEN-", "T-ENRU-"))]
    res: dict = {"n_queries": len(qids), "translation_applied": len(applied), "cross_lingual": len(xling), "sets": {}}
    ok = True
    for label, judged in (("V", verified), ("P", pooled)):
        ev = {s: M.evaluate({q: b["hybrid"][s][q] for q in qids}, {q: judged.get(q, {}) for q in qids},
                            hard_negatives=hn, query_ids=qids) for s in ("H0", "H1")}
        block = {"H0": {k: round(v, 6) for k, v in ev["H0"].mean().items()},
                 "H1": {k: round(v, 6) for k, v in ev["H1"].mean().items()}, "compare": {}, "applied": {},
                 "cross_lingual": {}}
        for metric in Q2_METRICS:
            block["compare"][metric] = mcompare(ev["H1"], ev["H0"], metric)
            for name, ids in (("applied", applied), ("cross_lingual", xling)):
                if ids:
                    block[name][metric] = mcompare(ev["H1"], ev["H0"], metric, ids)
        for metric in ("ndcg@10", "recall@50"):
            ok &= not_worse({"delta": block["compare"][metric]["delta"],
                             "p_value": block["compare"][metric]["p_value"]})
        block["exploratory"] = exploratory(ev, b["hybrid"], judged, applied)
        block["per_query"] = {s: {q: [round(m["ndcg@10"], 4), round(m["recall@50"], 4), round(m["mrr@10"], 4)]
                                  for q, m in ev[s].per_query.items()} for s in ("H0", "H1")}
        res["sets"][label] = block
    res["control_v1_E_rrf_lab_late100"] = {"V_ndcg@10": 0.328, "P_ndcg@10": 0.722,
                                           "note": "V1 results_v1.json, reference for the proxy only"}
    res["decision"] = {"rule": "translate flag on by default only if Δ nDCG@10 and Δ R@50 on V and P are all >= -0.005 "
                               "and not significantly negative (PREREGISTRATION §6)", "translate_default_on": ok}
    return res


def main() -> None:
    a = json.load(open(WORK / "stage_a.json", encoding="utf-8"))
    b = json.load(open(WORK / (f"stage_b_{VARIANT}.json" if VARIANT else "stage_b.json"), encoding="utf-8"))
    pre = (REPO / "benchmarks/term_dictionary_v1/PREREGISTRATION.sha256").read_text(encoding="utf-8").split()[0]
    results = {"benchmark": "TERM_DICTIONARY_V1", "preregistration_sha256": pre,
               "status": ("EXPLORATORY: " + VARIANT + " (after the preregistered results; its 'decision' fields are "
                          "not the decision)") if VARIANT else "PREREGISTERED", "snapshot_id": a["snapshot_id"],
               "dictionary": a["dictionary"], "engine": b["engine"],
               "control_dense_query_vs_v2": b["control_dense_query_vs_v2"],
               "q1_dossier": q1(a, b), "q2_hybrid": q2(a, b)}
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    short = {"q1": {k: v for k, v in results["q1_dossier"].items() if k != "per_query"},
             "q2": {k: ({s: {kk: vv for kk, vv in blk.items() if kk != "per_query"} for s, blk in v.items()}
                        if k == "sets" else v) for k, v in results["q2_hybrid"].items()}}
    print(json.dumps(short, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
