"""QUERY_TYPES_V1 (experiment E10 of PREREGISTRATION_EXPERIMENTS_2026-10-13.md): metrics by query type of bm25, hybrid
(server defaults) and hybrid_g0 (graph []) on the frozen set ``query_types_v1.jsonl``. Output holds IDs and numbers.

Rankings: the ``ordinary`` form of ``score_experiments_20261013.py`` (a hit counts with its collapsed duplicates and its
G1 copies). Page metrics: frozen ``topic_bench.query_metrics`` over the query's targets (A/B: the topic's V and P
targets; C/E: the automatic targets) plus ``precision@10`` (share of the first 10 slots that hit a target). D: source
metrics over the truth sources. Tests: ``topic_bench.compare_systems`` with every query its own unit (sign flip and
bootstrap 10 000, seed 20260928), Holm over the family of the pre-registered descriptive comparisons.

usage: PYTHONPATH=src python benchmarks/query_types_v1/scripts/score.py --runs <e10.jsonl>
           [--out benchmarks/query_types_v1/results_v1.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "benchmarks" / "topic_v1" / "scripts"))
import score_experiments_20261013 as SE  # noqa: E402  (pool_t1 puts <repo>/src on sys.path)

from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402

SET_FILE = REPO / "benchmarks" / "query_types_v1" / "query_types_v1.jsonl"
SYSTEMS = ("bm25", "hybrid", "hybrid_g0")
TYPES = ("A_NL", "B_EN", "C_OBJECT", "D_AUTHOR", "E_PARAM")
PAGE_METRICS = ("page_recall@10", "page_recall@50", "capped_recall@10", "capped_recall@50", "mrr@50", "success@10",
                "precision@10")
SOURCE_METRICS = ("source_capped_recall@10", "source_recall@50", "precision@10", "mrr@50", "success@10")
PRIMARY = {"A_NL": "page_recall@50", "B_EN": "page_recall@50", "C_OBJECT": "success@10",
           "D_AUTHOR": "source_capped_recall@10", "E_PARAM": "capped_recall@50"}
PAIRS = (("hybrid", "bm25"), ("hybrid", "hybrid_g0"))


def load_set(path: Path = SET_FILE) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def pages_topic(qid: str, targets: list[dict]) -> TB.Topic:
    tg = tuple(TB.Target(f"{qid}/T{i:03d}", t["page_id"], t["page_id"].split(":")[0], tuple(t.get("alt_page_ids") or ()))
               for i, t in enumerate(targets, 1))
    return TB.Topic(qid, "QUERY_TYPES", "", "", (), tg)


def precision_at(r: TB.Ranking, pages: frozenset[str], k: int = 10) -> float:
    return sum(1 for al in r.aliases[:k] if al & pages) / k


def page_metrics(topic: TB.Topic, r: TB.Ranking) -> dict[str, float]:
    m = TB.query_metrics(topic, r)
    allp = frozenset(p for t in topic.targets for p in t.pages)
    out = {k: m[k] for k in PAGE_METRICS if k in m}
    out["precision@10"] = precision_at(r, allp)
    return out


def source_metrics(r: TB.Ranking, sources: set[str]) -> dict[str, float]:
    """Source-level metrics: a hit belongs to the sources of its page and of its aliases (duplicates, copies)."""
    hit_src = [{p.split(":")[0] for p in al} for al in r.aliases[:TB.MAX_PAGES]]
    got10 = set().union(*[s & sources for s in hit_src[:10]]) if hit_src else set()
    got50 = set().union(*[s & sources for s in hit_src[:50]]) if hit_src else set()
    first = next((i + 1 for i, s in enumerate(hit_src[:50]) if s & sources), None)
    return {"source_capped_recall@10": len(got10) / min(10, len(sources)),
            "source_recall@50": len(got50) / len(sources),
            "precision@10": sum(1 for s in hit_src[:10] if s & sources) / 10,
            "mrr@50": 1.0 / first if first else 0.0,
            "success@10": 1.0 if first and first <= 10 else 0.0}


def score(qs: list[dict], runs: dict, topics_v: dict[str, TB.Topic], topics_p: dict[str, TB.Topic]) -> list[dict]:
    rows = []
    for q in qs:
        for s in SYSTEMS:
            line = runs.get((q["query_id"], s))
            if line is None:
                continue
            r = SE.ranking(line, "ordinary")
            base = {"query_id": q["query_id"], "topic_id": q["query_id"], "type": q["type"], "system": s,
                    "error": line.get("error")}
            truth = q["truth"]
            if truth["kind"] == "TOPIC":
                for tname, tmap in (("V", topics_v), ("P", topics_p)):
                    rows.append({**base, "truth": tname, **page_metrics(tmap[truth["topic_id"]], r)})
            elif truth["kind"] == "PAGES":
                rows.append({**base, "truth": "AUTO", **page_metrics(pages_topic(q["query_id"], truth["targets"]), r)})
            else:
                rows.append({**base, "truth": "AUTO", **source_metrics(r, set(truth["source_ids"]))})
    return rows


def summary(rows: list[dict]) -> dict:
    out: dict = {}
    for t in TYPES:
        for tname in sorted({r["truth"] for r in rows if r["type"] == t}):
            for s in SYSTEMS:
                xs = [r for r in rows if r["type"] == t and r["truth"] == tname and r["system"] == s]
                if not xs:
                    continue
                names = SOURCE_METRICS if t == "D_AUTHOR" else PAGE_METRICS
                out.setdefault(t, {}).setdefault(tname, {})[s] = {
                    "n_queries": len(xs), **{m: round(mean(x[m] for x in xs), 4) for m in names}}
    return out


def comparisons(rows: list[dict]) -> dict:
    """hybrid − bm25 and hybrid − hybrid_g0 on each type's primary metric (truth V for A/B), Holm over the family."""
    out, pvals = {}, {}
    for t in TYPES:
        tname = "V" if t in ("A_NL", "B_EN") else "AUTO"
        sub = [{**r, "system": r["system"]} for r in rows if r["type"] == t and r["truth"] == tname]
        res = TB.compare_systems(sub, list(PAIRS), (PRIMARY[t],))
        for k, v in res.items():
            out[f"{t} | {k}"] = v
            pvals[f"{t} | {k}"] = v["p_value"]
    adj = TB.holm(pvals)
    for k in out:
        out[k] = {**out[k], "p_holm": round(adj[k], 5)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", action="append", required=True)
    ap.add_argument("--out", default=str(REPO / "benchmarks" / "query_types_v1" / "results_v1.json"))
    args = ap.parse_args()
    qs = load_set()
    runs, meta, files = SE.load_runs(args.runs)
    topics = SE.PT.load_topics()
    tr, label_info, judged = SE.truths(topics)
    tv = {t.topic_id: t for t in tr["V"]}
    tp = {t.topic_id: t for t in tr["P"]}
    rows = score(qs, runs, tv, tp)
    qtopic = {q["query_id"]: q["topic_id"] for q in qs if q["truth"]["kind"] == "TOPIC"}
    judged_ab = {}
    for s in SYSTEMS:
        rk = {q: SE.ranking(line, "ordinary") for (q, sy), line in runs.items() if sy == s and q in qtopic}
        judged_ab[s] = {"judged@10": SE.S2.judged_at(rk, qtopic, judged),
                        "unjudged_top10": SE.unjudged(rk, qtopic, judged)}
    out = {"benchmark": "QUERY_TYPES_V1", "kind": "experiment E10 (baseline before the 13.10 search changes)",
           "set_sha256": hashlib.sha256(SET_FILE.read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
           "snapshot_id": SE.SNAPSHOT, "labels": label_info,
           "runs": {"files": files, "meta": [{k: m.get(k) for k in ("kind", "harness_version", "label", "n_lines",
                                                                     "canonical_snapshot_id", "started_at",
                                                                     "finished_at", "calls", "errors", "retries")
                                               if m.get(k) is not None} for m in meta]},
           "errors": dict(Counter(s for (q, s), line in runs.items() if line.get("error"))),
           "n_queries": dict(Counter(q["type"] for q in qs)), "summary": summary(rows),
           "comparisons": comparisons(rows), "judged_A_B_P": judged_ab,
           "per_query": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in rows]}
    text = json.dumps(out, ensure_ascii=False, indent=1) + "\n"
    Path(args.out).write_bytes(text.encode("utf-8"))
    print(f"wrote {args.out} sha256 {hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}")


if __name__ == "__main__":
    main()
