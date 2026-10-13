"""Score the search experiments of 13.10.2026 (PREREGISTRATION_EXPERIMENTS_2026-10-13.md): E6 (model paraphrases),
E7 (graph stages G1, G5) and E11 (late stage flags) on topic_v1 and retrieval_v1. Output holds IDs and numbers only.

Frozen code wherever it exists: run lines → ``score.load_runs`` (topic_v1), rankings → ``topic_bench.ranking_from_hits``,
truth P → ``score_t3.merged_labels`` + ``score_t1.p_topics``, judged@10 → ``score_t1.judged_pages`` +
``score_t2.judged_at``, per-query topic metrics → ``topic_bench.query_metrics``, tests → ``topic_bench.compare_systems``
(topic means, sign flip and bootstrap 10 000, seed 20260928, Holm over the family of one call); retrieval_v1 metrics →
``retrieval_lab.metrics`` (ordinary form) and ``graph_search_v1/scripts/run.py:dedup_metrics`` (copy-group form),
tests → ``metrics.paired_randomization`` / ``bootstrap_ci`` (seed 20260928).

Three forms of a page ranking (§3 of the pre-registration):

* ``plain``    — a hit counts with its collapsed duplicates (``score.ranking_of``; G1 copies ignored);
* ``ordinary`` — a hit counts with its duplicates and its G1 copies (GRAPH_SEARCH_V1 §6 for topic_v1);
* ``groups``   — copy groups: the union-find of every (page, duplicate) and (page, copy) pair the server reported in
  any run of the snapshot given (``--group-runs``); topic targets of one group merge into one target, a hit counts
  with every member of its groups; retrieval_v1: a page whose group was shown above gains nothing, a group's grade is
  its best judged grade, the ideal ranking counts a group once (GRAPH_SEARCH_V1 §6).

usage: PYTHONPATH=src python benchmarks/topic_v1/scripts/score_experiments_20261013.py e6e7 \
           --topic-runs <e6e7_topic.jsonl> --retrieval-runs <e7_retrieval.jsonl> [--group-runs <more.jsonl> …] \
           --out benchmarks/topic_v1/results_experiments_20261013.json
       … e11 --topic-runs <e11.jsonl> --retrieval-runs <e11.jsonl> [--group-runs …] --out <json>
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pool_t1 as PT  # noqa: E402  (also puts <repo>/src on sys.path)
import score_t1 as ST  # noqa: E402
import score_t2 as S2  # noqa: E402
import score_t3 as S3  # noqa: E402

from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab import metrics as M  # noqa: E402
from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402

REPO, BENCH = PT.REPO, PT.BENCH
SNAPSHOT = "snap-20261007T103222Z-2d71e9e8"
TOPIC_METRICS = ("page_recall@50", "page_recall@20", "mrr@50", "success@10")
SUMMARY_METRICS = ("page_recall@10", "page_recall@20", "page_recall@50", "mrr@50", "success@10", "source_recall@10")
E6_SETS = {"NAME": "-0", "PARA1": "-1"}
E6_SYSTEMS = {"base": "g0", "human3": "human3", "llm3": "llm3"}
E6_PAIRS = (("llm3", "base"), ("human3", "base"), ("llm3", "human3"))
E7_SYSTEMS = ("g0", "g1", "g15")
E7_FAMILY = (("g1", "g0"), ("g15", "g1"))
E11_SYSTEMS = ("late_default", "late_rrf", "late_bt", "late_both")
JUDGED_MIN = 0.85
RETRIEVAL_DIR = REPO / "benchmarks" / "retrieval_v0"
POOLED_FILES = (REPO / "benchmarks" / "retrieval_v1" / "qrels_v1_pooled.tsv",
                REPO / "benchmarks" / "retrieval_v2" / "qrels_v2_pooled.tsv",
                REPO / "benchmarks" / "graph_search_v1" / "qrels_gs_pooled.tsv")
R_METRICS = ("ndcg@10", "recall@50", "mrr@10", "judged@10")
EMPTY_INDEX = TB.SectionIndex.from_outlines({})


# ------------------------------------------------------------------------------------------------ runs and groups
def load_runs(paths: list[str]) -> tuple[dict[tuple[str, str], dict], list[dict], list[dict]]:
    score = PT.load_module("topic_v1_score", BENCH / "scripts" / "score.py")
    runs, _outlines, meta, files = score.load_runs(paths)
    snaps = sorted({m["canonical_snapshot_id"] for m in meta if m.get("canonical_snapshot_id")})
    if snaps and snaps != [SNAPSHOT]:
        raise SystemExit(f"runs on snapshots {snaps}, expected {SNAPSHOT}")
    return runs, meta, files


class Groups:
    """Union-find over the (page, duplicate) and (page, copy) pairs the server reported."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def add_runs(self, runs: dict) -> None:
        for line in runs.values():
            for h in line.get("hits") or []:
                for other in (h.get("duplicates") or []) + (h.get("copies") or []):
                    self.union(h["page_id"], other)

    def members(self) -> dict[str, frozenset[str]]:
        acc: dict[str, set[str]] = defaultdict(set)
        for p in list(self.parent):
            acc[self.find(p)].add(p)
        return {r: frozenset(v) for r, v in acc.items()}

    def group_of(self) -> dict[str, str]:
        return {p: self.find(p) for p in list(self.parent)}


def ranking(line: dict, form: str, groups: Groups | None = None, members: dict | None = None) -> TB.Ranking:
    """The page ranking of a run line in one of the three forms."""
    hits = []
    for h in line.get("hits") or []:
        al = list(h.get("duplicates") or [])
        if form in ("ordinary", "groups"):
            al += list(h.get("copies") or [])
        if form == "groups":
            assert groups is not None and members is not None
            extra: list[str] = []
            for p in [h["page_id"], *al]:
                extra += sorted(members.get(groups.find(p), ()))
            al += extra
        hits.append({"page_id": h["page_id"], "duplicates": [p for p in dict.fromkeys(al) if p != h["page_id"]]})
    return TB.ranking_from_hits(hits, EMPTY_INDEX, error=line.get("error"))


def group_topics(topics: list[TB.Topic], groups: Groups, members: dict) -> list[TB.Topic]:
    """Targets of one copy group merge into one target (first target id and page; pages = all members)."""
    out = []
    for t in topics:
        merged: dict[str, list[TB.Target]] = {}
        for tg in t.targets:
            merged.setdefault(groups.find(tg.page_id), []).append(tg)
        new = []
        for key, tgs in merged.items():
            pages: set[str] = set()
            for tg in tgs:
                for p in tg.pages:
                    pages |= {p} | set(members.get(groups.find(p), ()))
            first = tgs[0]
            new.append(TB.Target(first.target_id, first.page_id, first.source_id,
                                 tuple(sorted(pages - {first.page_id})), max(x.grade for x in tgs), first.mapping))
        out.append(dataclasses.replace(t, targets=tuple(new)))
    return out


# ------------------------------------------------------------------------------------------------ topic_v1
def truths(topics: list[TB.Topic]) -> tuple[dict[str, list[TB.Topic]], dict, dict[str, frozenset[str]]]:
    _r12, _g12, rows, groups, info = S3.merged_labels()
    judged = ST.judged_pages(topics, rows, groups)
    return {"V": topics, "P": ST.p_topics(topics, rows, groups, 2)}, info, judged


def metric_rows(truth: list[TB.Topic], rankings: dict[str, dict[str, TB.Ranking]]) -> list[dict]:
    qmap = {q.query_id: t for t in truth for q in t.queries}
    rows = []
    for system, rs in rankings.items():
        for qid, r in rs.items():
            if qid in qmap:
                rows.append({"query_id": qid, "topic_id": qmap[qid].topic_id, "system": system,
                             **TB.query_metrics(qmap[qid], r)})
    return rows


def summarize(rows: list[dict]) -> dict[str, dict]:
    out = {}
    for s in sorted({r["system"] for r in rows}):
        xs = [r for r in rows if r["system"] == s]
        out[s] = {"n_queries": len(xs), **{m: round(mean(x[m] for x in xs), 4) for m in SUMMARY_METRICS}}
    return out


def unjudged(rankings: dict[str, TB.Ranking], qtopic: dict[str, str], judged: dict[str, frozenset[str]],
             k: int = 10) -> dict:
    """First-k slots without any T1–T3 / catalogue judgment of the topic: count and the distinct (topic, page) pairs."""
    slots, pairs = 0, set()
    for qid, r in rankings.items():
        js = judged.get(qtopic.get(qid, ""), frozenset())
        for p, al in list(zip(r.pages, r.aliases))[:k]:
            if p not in js and not (al & js):
                slots += 1
                pairs.add((qtopic.get(qid), p))
    return {"slots": slots, "pairs": len(pairs), "pair_ids": sorted([list(x) for x in pairs])}


def topic_rankings(runs: dict, systems: dict[str, str], qids: set[str] | None, form: str,
                   groups: Groups | None = None, members: dict | None = None) -> dict[str, dict[str, TB.Ranking]]:
    """label → query → ranking for ``systems`` = {label: system name in the runs}."""
    out: dict[str, dict[str, TB.Ranking]] = {}
    for label, system in systems.items():
        out[label] = {q: ranking(line, form, groups, members) for (q, s), line in runs.items()
                      if s == system and (qids is None or q in qids)}
    return out


def judged_block(rk: dict[str, dict[str, TB.Ranking]], qtopic: dict[str, str], judged: dict) -> dict:
    return {s: {"judged@10": S2.judged_at(r, qtopic, judged), "unjudged_top10": unjudged(r, qtopic, judged)}
            for s, r in rk.items()}


# ------------------------------------------------------------------------------------------------ retrieval_v1
def retrieval_truth() -> tuple[B.Benchmark, dict, dict, dict, dict]:
    bench = B.load_benchmark(RETRIEVAL_DIR)
    verified = bench.judgments(level="PAGE")
    rows: list[dict[str, str]] = []
    for p in POOLED_FILES:
        rows += B.load_pooled_qrels(p)
    pooled = B.merge_judgments(verified, B.pooled_judgments(rows))
    info = {p.name: PT.sha256_file(p) for p in POOLED_FILES}
    return bench, {"V": verified, "P": pooled}, bench.hard_negative_ids("PAGE"), info, {
        "text": [q.query_id for q in bench.select(track="text")],
        "visual": [q.query_id for q in bench.select(track="visual")]}


def retrieval_eval(runs: dict, system: str, qids: list[str], judged: dict, hn: dict, form: str,
                   group_of: dict[str, str] | None, dedup) -> dict[str, dict[str, float]]:
    order = {q: [h["page_id"] for h in (runs.get((q, system)) or {}).get("hits") or []] for q in qids
             if (q, system) in runs}
    if form == "ordinary":
        return M.evaluate(order, judged, hard_negatives=hn, query_ids=list(order)).per_query
    out = {}
    for q, o in order.items():
        j = judged.get(q, {})
        if any(g >= 2 for g in j.values()):
            out[q] = dedup(o, j, group_of)
    return out


def r_mean(per_query: dict, metric: str) -> float | None:
    vals = [m[metric] for m in per_query.values() if metric in m]
    return round(sum(vals) / len(vals), 4) if vals else None


def r_compare(a: dict, b: dict, metric: str) -> dict:
    x = {q: m[metric] for q, m in a.items() if metric in m}
    y = {q: m[metric] for q, m in b.items() if metric in m}
    t = M.paired_randomization(x, y)
    ci = M.bootstrap_ci(x, y)
    return {"n": t["n"], "delta": round(t["delta"], 4), "ci_lo": round(ci["lo"], 4), "ci_hi": round(ci["hi"], 4),
            "p_value": round(t["p_value"], 5)}


def retrieval_block(runs: dict, systems: tuple[str, ...], pairs: list[tuple[str, str]], groups: Groups) -> dict:
    gs = PT.load_module("gs_run", REPO / "benchmarks" / "graph_search_v1" / "scripts" / "run.py")
    bench, judged, hn, info, tracks = retrieval_truth()
    group_of = groups.group_of()
    out: dict = {"labels": info, "n_queries": {k: len(v) for k, v in tracks.items()}, "summary": {},
                 "comparisons": {}}
    per: dict = {}
    for form in ("ordinary", "groups"):
        for tname, js in judged.items():
            for track, qids in tracks.items():
                for s in systems:
                    per[(form, tname, track, s)] = retrieval_eval(runs, s, qids, js, hn, form, group_of,
                                                                  gs.dedup_metrics)
                    out["summary"].setdefault(form, {}).setdefault(tname, {}).setdefault(track, {})[s] = {
                        "n_queries": len(per[(form, tname, track, s)]),
                        **{m: r_mean(per[(form, tname, track, s)], m) for m in R_METRICS}}
                for a, b in pairs:
                    for m in ("ndcg@10", "recall@50"):
                        key = f"{form}|{tname}|{track}|{a} - {b}|{m}"
                        out["comparisons"][key] = r_compare(per[(form, tname, track, a)],
                                                            per[(form, tname, track, b)], m)
    return out


# ------------------------------------------------------------------------------------------------ E6 / E7
def e6(runs: dict, topics: list[TB.Topic], tr: dict, judged: dict) -> dict:
    qtopic = {q.query_id: t.topic_id for t in topics for q in t.queries}
    out: dict = {"systems": E6_SYSTEMS, "sets": {}, "comparisons": {}, "judged": {}}
    rk: dict[str, dict[str, TB.Ranking]] = {}
    for set_name, suffix in E6_SETS.items():
        qids = {q for q in qtopic if q.endswith(suffix)}
        for label, ranks in topic_rankings(runs, E6_SYSTEMS, qids, "plain").items():
            rk[f"{set_name}:{label}"] = ranks
        out["sets"][set_name] = {"n_queries": len(qids)}
    pairs = [(f"{s}:{a}", f"{s}:{b}") for s in E6_SETS for a, b in E6_PAIRS]
    out["judged"] = judged_block(rk, qtopic, judged)
    out["summary"] = {}
    for tname, truth in tr.items():
        rows = metric_rows(truth, rk)
        out["summary"][tname] = summarize(rows)
        out["comparisons"][tname] = TB.compare_systems(rows, pairs, TOPIC_METRICS)
    return out


def e7(runs: dict, topics: list[TB.Topic], tr: dict, judged: dict, groups: Groups, members: dict) -> dict:
    qtopic = {q.query_id: t.topic_id for t in topics for q in t.queries}
    systems = {s: s for s in E7_SYSTEMS}
    out: dict = {"summary": {}, "comparisons": {}, "judged": {}}
    for form in ("plain", "ordinary", "groups"):
        rk = topic_rankings(runs, systems, None, form, groups, members)
        if form != "groups":
            out["judged"][form] = judged_block(rk, qtopic, judged)
        for tname, truth in tr.items():
            t2 = group_topics(truth, groups, members) if form == "groups" else truth
            rows = metric_rows(t2, rk)
            out["summary"].setdefault(form, {})[tname] = summarize(rows)
            out["comparisons"].setdefault(form, {})[tname] = TB.compare_systems(rows, list(E7_FAMILY), TOPIC_METRICS)
            out["comparisons"][form][tname + "_g15_vs_g0_descriptive"] = TB.compare_systems(
                rows, [("g15", "g0")], TOPIC_METRICS)
    out["targets"] = {**{k: sum(len(t.targets) for t in v) for k, v in tr.items()},
                      **{f"groups|{k}": sum(len(t.targets) for t in group_topics(v, groups, members))
                         for k, v in tr.items()}}
    return out


def decide_e6(res: dict) -> dict:
    c = res["comparisons"]
    sets = {}
    for s in E6_SETS:
        v = c["V"][f"{s}:llm3 - {s}:base | page_recall@50"]
        harm = [k for k, x in c["V"].items() if k.startswith(f"{s}:llm3 - {s}:base | ")
                and k.split(" | ")[1] in ("mrr@50", "success@10") and x["delta"] < 0 and x["p_holm"] < 0.05]
        p_ok = all(res["judged"][f"{s}:{x}"]["judged@10"] >= JUDGED_MIN for x in E6_SYSTEMS)
        if p_ok:
            harm += [f"P {k}" for k, x in c["P"].items() if k.startswith(f"{s}:llm3 - {s}:base | ")
                     and k.split(" | ")[1] in ("mrr@50", "success@10") and x["delta"] < 0 and x["p_holm"] < 0.05]
        gain = v["delta"] >= 0.05 and v["p_holm"] < 0.05
        h = c["V"][f"{s}:llm3 - {s}:human3 | page_recall@50"]
        rel = ("лучше" if h["delta"] > 0 and h["p_holm"] < 0.05 else
               "хуже" if h["delta"] < 0 and h["p_holm"] < 0.05 and h["ci_lo"] < -0.05 else
               "не хуже ручных (граница 0,05)" if h["ci_lo"] >= -0.05 else "не определено")
        sets[s] = {"gain_rule": gain, "harm": harm, "P_reported": p_ok, "llm3_vs_human3": rel,
                   "delta_R50_V": v["delta"], "p_holm": v["p_holm"]}
    n = sum(1 for s in sets.values() if s["gain_rule"] and not s["harm"])
    verdict = ("НОРМА: вызывающий агент передаёт 2 перефразировки" if n == 2 else
               "ЧАСТИЧНО: рекомендация остаётся рекомендацией" if n == 1 else "НЕ ПРИНЯТО")
    return {"per_set": sets, "verdict": verdict}


def decide_e7(res: dict, retr: dict | None, judged_ok: bool) -> dict:
    c = res["comparisons"]["groups"]
    prim = c["V"]["g15 - g1 | page_recall@50"]
    gain = prim["delta"] >= 0.02 and prim["ci_lo"] > 0 and prim["p_holm"] < 0.05
    harm_g5, harm_g1 = [], []
    for tname in ("V", "P"):
        if tname == "P" and not judged_ok:
            continue
        for k, x in c[tname].items():
            metric = k.split(" | ")[1]
            if x["delta"] < 0 and x["p_value"] < 0.05:
                if k.startswith("g15 - g1") and (metric != "page_recall@50" or tname == "P"):
                    harm_g5.append(f"{tname} {k}")
                if k.startswith("g1 - g0"):
                    harm_g1.append(f"{tname} {k}")
    retr_stopped = []
    if retr is not None:
        for track, by_sys in retr["summary"]["ordinary"]["P"].items():
            if any((v.get("judged@10") or 0.0) < JUDGED_MIN for v in by_sys.values()):
                retr_stopped.append(track)
        for key, x in retr["comparisons"].items():
            form, tname, track, pair, metric = key.split("|")
            if form != "groups" or tname != "P" or (track, metric) == ("visual", "recall@50") or track in retr_stopped:
                continue
            if x["delta"] < 0 and x["p_value"] < 0.05:
                if pair == "g15 - g1":
                    harm_g5.append("retrieval_v1 " + key)
                if pair == "g1 - g0":
                    harm_g1.append("retrieval_v1 " + key)
    return {"G5": {"gain_rule": gain, "primary": prim, "harm": harm_g5, "P_reported": judged_ok,
                   "retrieval_tracks_stopped_judged": retr_stopped,
                   "default": bool(gain and not harm_g5 and judged_ok and not retr_stopped)},
            "G1": {"harm": harm_g1, "no_harm": not harm_g1, "retrieval_tracks_stopped_judged": retr_stopped}}


def run_e6e7(args: argparse.Namespace) -> dict:
    topics = PT.load_topics()
    runs, meta, files = load_runs(args.topic_runs)
    rruns, rmeta, rfiles = load_runs(args.retrieval_runs) if args.retrieval_runs else ({}, [], [])
    groups = Groups()
    groups.add_runs(runs)
    groups.add_runs(rruns)
    extra_files = []
    for p in args.group_runs or []:
        gr, _gm, gf = load_runs([p])
        groups.add_runs(gr)
        extra_files += gf
    members = groups.members()
    tr, label_info, judged = truths(topics)
    res6 = e6(runs, topics, tr, judged)
    res7 = e7(runs, topics, tr, judged, groups, members)
    retr = retrieval_block(rruns, E7_SYSTEMS, list(E7_FAMILY) + [("g15", "g0")], groups) if rruns else None
    judged_ok = all(res7["judged"]["ordinary"][s]["judged@10"] >= JUDGED_MIN for s in E7_SYSTEMS)
    errors = Counter(s for (q, s), line in {**runs, **rruns}.items() if line.get("error"))
    return {"benchmark": "TOPIC_BENCHMARK_V1 + RETRIEVAL_BENCHMARK_V1",
            "kind": "pre-registered experiments E6, E7 (PREREGISTRATION_EXPERIMENTS_2026-10-13.md)",
            "snapshot_id": SNAPSHOT, "labels": label_info,
            "runs": {"topic": files, "retrieval": rfiles, "group_extra": extra_files,
                     "meta": [{k: m.get(k) for k in ("kind", "harness_version", "label", "systems", "n_lines",
                                                      "canonical_snapshot_id", "started_at", "finished_at", "calls",
                                                      "errors", "retries") if m.get(k) is not None}
                              for m in meta + rmeta]},
            "errors": dict(errors),
            "copy_groups": {"pages": len(groups.parent), "groups_with_2plus": sum(1 for v in members.values()
                                                                                  if len(v) > 1)},
            "E6": res6, "E6_decision": decide_e6(res6),
            "E7": res7, "E7_retrieval": retr, "E7_decision": decide_e7(res7, retr, judged_ok)}


# ------------------------------------------------------------------------------------------------ E11
def run_e11(args: argparse.Namespace) -> dict:
    topics = PT.load_topics()
    runs, meta, files = load_runs(args.topic_runs + [p for p in args.retrieval_runs if p not in args.topic_runs])
    qtopic = {q.query_id: t.topic_id for t in topics for q in t.queries}
    groups = Groups()
    groups.add_runs(runs)
    for p in args.group_runs or []:
        groups.add_runs(load_runs([p])[0])
    tr, label_info, judged = truths(topics)
    systems = {s: s for s in E11_SYSTEMS}
    rk = topic_rankings(runs, systems, set(qtopic), "plain")
    pairs = [(s, "late_default") for s in E11_SYSTEMS[1:]]
    res: dict = {"summary": {}, "comparisons": {}, "judged": judged_block(rk, qtopic, judged)}
    for tname, truth in tr.items():
        rows = metric_rows(truth, rk)
        res["summary"][tname] = summarize(rows)
        res["comparisons"][tname] = TB.compare_systems(rows, pairs, TOPIC_METRICS)
    retr = retrieval_block(runs, E11_SYSTEMS, pairs, groups)
    # primary family: topic R@50 (V) and retrieval text nDCG@10 (P, ordinary form) of the three variants (Holm, 6)
    fam = {}
    for a, b in pairs:
        x = TB.compare_systems(metric_rows(tr["V"], rk), [(a, b)], ("page_recall@50",))[f"{a} - {b} | page_recall@50"]
        fam[f"topic V | {a} - {b} | page_recall@50"] = x
        fam[f"retrieval P text | {a} - {b} | ndcg@10"] = retr["comparisons"][f"ordinary|P|text|{a} - {b}|ndcg@10"]
    adj = TB.holm({k: v["p_value"] for k, v in fam.items()})
    for k in fam:
        fam[k] = {**fam[k], "p_holm": round(adj[k], 5)}
    return {"benchmark": "TOPIC_BENCHMARK_V1 + RETRIEVAL_BENCHMARK_V1", "kind": "pre-registered experiment E11",
            "snapshot_id": SNAPSHOT, "labels": label_info, "runs": {"files": files, "meta": meta},
            "E11": res, "E11_retrieval": retr, "E11_primary_family": fam}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=["e6e7", "e11"])
    ap.add_argument("--topic-runs", action="append", required=True)
    ap.add_argument("--retrieval-runs", action="append", default=[])
    ap.add_argument("--group-runs", action="append", default=[])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = run_e6e7(args) if args.which == "e6e7" else run_e11(args)
    text = json.dumps(out, ensure_ascii=False, indent=1) + "\n"
    Path(args.out).write_bytes(text.encode("utf-8"))
    print(f"wrote {args.out} sha256 {hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}")


if __name__ == "__main__":
    main()
