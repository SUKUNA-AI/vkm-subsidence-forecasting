"""Score experiment E6c (PREREGISTRATION_E6C_2026-10-13.md): E6b on the PARA1 set with three independent model
generations of two Russian paraphrases. Output holds IDs and numbers only.

Everything that measures is the frozen code of the E6 / E6b scorers (``score_experiments_20261013.py``): run lines →
``score.load_runs`` (snapshot check), rankings in the ``plain`` form → ``topic_rankings``, truth V / P → ``truths``
(catalogue; catalogue + T1–T3 labels ≥ 2), judged@10 and the unjudged top-10 pairs → ``judged_block``, per-query
metrics → ``metric_rows``, tests → ``topic_bench.compare_systems`` (topic means, sign flip and bootstrap 10 000,
seed 20260928, Holm over the family of one call).

New in E6c (§4–§5 of the addendum):

* ``llm3ru_mean`` — a derived system: per query (PARA1 has one query per topic) every metric is the mean of the three
  generations ``llm3ru_g1..g3``; its topic mean is the mean of the three generations' topic means. It is never sent to
  the API.
* Primary family per truth (V, P): 16 tests — (``llm3ru_g1``, ``llm3ru_g2``, ``llm3ru_g3``, ``llm3ru_mean``) − ``base``
  × 4 metrics; Holm over the 16.
* The +0.05 threshold is checked on the exact (unrounded) difference of topic means (E6b deviation 2), reported next to
  the rounded one.
* Spread between generations (descriptive): min / max / SD of the three differences, the per-topic SD of R@50 over the
  three generations, per-topic sign agreement against ``base``, and the pairwise generation comparisons as a separate
  descriptive family (3 pairs × 4 metrics per truth).

usage: PYTHONPATH=src python benchmarks/topic_v1/scripts/score_e6c_20261013.py --runs <e6c.jsonl> \
           [--e6b-runs <e6b.jsonl of image 1b20e03>] --out benchmarks/topic_v1/results_e6c_20261013.json
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median, stdev

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


SE = _load("score_experiments_20261013", HERE / "score_experiments_20261013.py")
PL = _load("plans_e6c_20261013", HERE / "plans_e6c_20261013.py")
TB = SE.TB

SET_NAME, SUFFIX = "PARA1", SE.E6_SETS["PARA1"]           # "-1"
GENS = PL.GENERATIONS                                      # llm3ru_g1, llm3ru_g2, llm3ru_g3
SYSTEMS = PL.E6C_SYSTEMS                                   # base + GENS
MEAN = "llm3ru_mean"
PRIMARY_PAIRS = tuple((g, "base") for g in GENS) + ((MEAN, "base"),)
SPREAD_PAIRS = (("llm3ru_g2", "llm3ru_g1"), ("llm3ru_g3", "llm3ru_g1"), ("llm3ru_g3", "llm3ru_g2"))
METRICS = SE.TOPIC_METRICS                                 # R@50 (primary), R@20, MRR@50, S@10
PRIMARY = "page_recall@50"
JUDGED_MIN = SE.JUDGED_MIN                                 # 0.85
GAIN_MIN = 0.05
T975_DF2 = 4.302652729911275                               # Student t, 0.975 quantile, 2 degrees of freedom
E6B_NAMES = {"base": "base", "llm3ru_g1": "llm3ru"}       # system names of the same requests in the E6b run


def L(system: str) -> str:
    return f"{SET_NAME}:{system}"


# ------------------------------------------------------------------------------------------------ derived system
def mean_rows(rows: list[dict]) -> list[dict]:
    """Rows of ``llm3ru_mean``: per query, every metric is the mean over the three generations (all three required)."""
    by: dict[str, dict[str, dict]] = {}
    for r in rows:
        by.setdefault(r["query_id"], {})[r["system"]] = r
    out = []
    for qid, d in sorted(by.items()):
        gs = [d.get(L(g)) for g in GENS]
        if any(x is None for x in gs):
            continue
        keys = [k for k in gs[0] if k not in ("query_id", "topic_id", "system") and isinstance(gs[0][k], (int, float))]
        out.append({"query_id": qid, "topic_id": gs[0]["topic_id"], "system": L(MEAN),
                    **{k: sum(x[k] for x in gs) / len(gs) for k in keys}})
    return out


def exact_delta(rows: list[dict], a: str, b: str, metric: str) -> float:
    ta, tb = TB.topic_means(rows, L(a), metric), TB.topic_means(rows, L(b), metric)
    ids = sorted(set(ta) & set(tb))
    return sum(ta[t] - tb[t] for t in ids) / len(ids)


def topic_mean_value(rows: list[dict], system: str, metric: str) -> float:
    tm = TB.topic_means(rows, L(system), metric)
    return sum(tm.values()) / len(tm)


# ------------------------------------------------------------------------------------------------ rules
def p_ok(judged: dict, systems: tuple[str, ...]) -> bool:
    return all(judged[L(s)]["judged@10"] >= JUDGED_MIN for s in systems)


def p_systems(a: str, b: str) -> tuple[str, ...]:
    """Systems whose judged@10 must be ≥ 0.85 for a P comparison: the two compared, the three generations for the mean."""
    return tuple(dict.fromkeys([*(GENS if a == MEAN else (a,)), *(GENS if b == MEAN else (b,))]))


def harm(c: dict, judged: dict, a: str, b: str) -> tuple[list[str], bool]:
    key = f"{L(a)} - {L(b)}"
    out = [f"V {key} | {m}" for m in ("mrr@50", "success@10")
           if c["V"][f"{key} | {m}"]["delta"] < 0 and c["V"][f"{key} | {m}"]["p_holm"] < 0.05]
    checked = p_ok(judged, p_systems(a, b))
    if checked:
        out += [f"P {key} | {m}" for m in ("mrr@50", "success@10")
                if c["P"][f"{key} | {m}"]["delta"] < 0 and c["P"][f"{key} | {m}"]["p_holm"] < 0.05]
    return out, checked


def decide_e6c(res: dict, exact: dict) -> dict:
    """Per generation (E6b-R1 applied to each, descriptive) and the summary rule E6c-R1 (§5 of the addendum)."""
    c, judged = res["comparisons"], res["judged"]
    per_gen = {}
    for g in (*GENS, MEAN):
        key = f"{L(g)} - {L('base')} | {PRIMARY}"
        v = c["V"][key]
        d = exact["V"][key]
        hs, checked = harm(c, judged, g, "base")
        gain = d >= GAIN_MIN and v["p_holm"] < 0.05
        per_gen[g] = {"delta_R50_V_exact": d, "delta_R50_V": v["delta"], "ci_R50_V": [v["ci_lo"], v["ci_hi"]],
                      "p_R50_V": v["p_value"], "p_holm_R50_V": v["p_holm"], "gain": gain, "harm": hs,
                      "P_harm_checked": checked, "E6b_R1_like_passes": bool(gain and not hs)}
    m = per_gen[MEAN]
    cond1 = m["delta_R50_V_exact"] >= GAIN_MIN and m["p_holm_R50_V"] < 0.05
    gen_deltas = [per_gen[g]["delta_R50_V_exact"] for g in GENS]
    cond2 = min(gen_deltas) >= 0
    cond3 = not m["harm"]
    failed = [name for name, ok in (("1: среднее ≥ +0,05 и p_Холм < 0,05", cond1),
                                    ("2: ни одна генерация не < 0", cond2), ("3: нет вреда", cond3)) if not ok]
    verdict = "ПРИНЯТО на PARA1" if not failed else "НЕ ПРИНЯТО на PARA1"
    p_flags = {f"{L(a)} - {L(b)}": p_ok(judged, p_systems(a, b)) for a, b in PRIMARY_PAIRS + SPREAD_PAIRS}
    return {"per_generation": per_gen,
            "summary_rule": {"cond1_mean_gain": cond1, "cond2_no_generation_negative": cond2, "cond3_no_harm": cond3,
                             "mean_delta_R50_V_exact": m["delta_R50_V_exact"], "p_holm_mean": m["p_holm_R50_V"],
                             "min_generation_delta_exact": min(gen_deltas), "failed": failed},
            "verdict": verdict, "P_interpretable": p_flags}


# ------------------------------------------------------------------------------------------------ spread
def _sd(xs: list[float]) -> float | None:
    return stdev(xs) if len(xs) > 1 else None


def spread(rows: list[dict], exact_v: dict) -> dict:
    """Between-generation spread on R@50 (V): the three differences against base, per-topic SD over generations,
    per-topic sign agreement against base, SD over topics of each generation's per-topic difference."""
    deltas = {g: exact_v[f"{L(g)} - {L('base')} | {PRIMARY}"] for g in GENS}
    xs = list(deltas.values())
    sd3 = _sd(xs)
    tm = {s: TB.topic_means(rows, L(s), PRIMARY) for s in SYSTEMS}
    topics = sorted(set.intersection(*(set(v) for v in tm.values())))
    per_topic_sd = [stdev([tm[g][t] for g in GENS]) for t in topics]
    signs = Counter()
    for t in topics:
        d = [tm[g][t] - tm["base"][t] for g in GENS]
        pos, neg = sum(x > 1e-12 for x in d), sum(x < -1e-12 for x in d)
        signs["all_equal_base" if pos == neg == 0 else "all_above" if pos == 3 else "all_below" if neg == 3 else
              "mixed_signs" if pos and neg else "above_or_equal" if pos else "below_or_equal"] += 1
    return {"delta_R50_V_exact": deltas, "min": min(xs), "max": max(xs), "range": max(xs) - min(xs),
            "sd_of_three_deltas": sd3, "mean_of_three": mean(xs),
            "t_interval_95_of_generation_mean_descriptive": ([mean(xs) - T975_DF2 * sd3 / 3 ** 0.5,
                                                              mean(xs) + T975_DF2 * sd3 / 3 ** 0.5] if sd3 else None),
            "R50_V_topic_mean": {s: sum(tm[s].values()) / len(tm[s]) for s in SYSTEMS},
            "per_topic_sd_over_generations": {"n_topics": len(topics), "mean": mean(per_topic_sd),
                                              "median": median(per_topic_sd), "max": max(per_topic_sd),
                                              "topics_all_three_equal": sum(1 for x in per_topic_sd if x < 1e-12)},
            "per_topic_sign_against_base": dict(signs),
            "sd_over_topics_of_difference": {g: stdev([tm[g][t] - tm["base"][t] for t in topics]) for g in GENS}}


# ------------------------------------------------------------------------------------------------ descriptive
def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))], 1)


def new_pages(rk: dict, topics: list, k: int) -> dict:
    """Mean number of first-k pages a system adds against ``base`` of the same query and how many of them are V targets
    of the topic (a hit with its collapsed duplicates); the union of the three generations as well."""
    tmap = {q.query_id: t for t in topics for q in t.queries}
    out = {}
    union_new, union_t = [], []
    acc: dict[str, tuple[list, list]] = {g: ([], []) for g in GENS}
    for qid, b in rk[L("base")].items():
        base_pages = set(b.pages[:k])
        tpages = set().union(*(t.pages for t in tmap[qid].targets)) if qid in tmap else set()
        u: dict[str, frozenset] = {}
        for g in GENS:
            r = rk[L(g)].get(qid)
            if r is None:
                continue
            new = [(p, al) for p, al in list(zip(r.pages, r.aliases))[:k] if p not in base_pages]
            acc[g][0].append(len(new))
            acc[g][1].append(sum(1 for _p, al in new if al & tpages))
            for p, al in new:
                u[p] = al
        union_new.append(len(u))
        union_t.append(sum(1 for al in u.values() if al & tpages))
    for g, (a, t) in acc.items():
        out[L(g)] = {"mean_new": round(mean(a), 2), "mean_new_targets_V": round(mean(t), 3)}
    out["union_of_generations"] = {"mean_new": round(mean(union_new), 2),
                                   "mean_new_targets_V": round(mean(union_t), 3)}
    return out


def diagnostics(runs: dict) -> dict:
    out: dict = {}
    for system in SYSTEMS:
        lines = [x for (q, s), x in runs.items() if s == system]
        ms = [x["api_ms"] for x in lines if x.get("api_ms") is not None]
        out[system] = {
            "n": len(lines), "errors": sum(1 for x in lines if x.get("error")),
            "api_ms_p50": _pct(ms, 0.5), "api_ms_p90": _pct(ms, 0.9),
            "formulations_status": dict(Counter(str((x.get("formulations") or {}).get("status")) for x in lines)),
            "formulations_answered": dict(Counter(str((x.get("formulations") or {}).get("answered")) for x in lines)),
            "formulations_duplicates_merged_mean": round(mean([(x.get("formulations") or {}).get("duplicates_merged")
                                                               or 0 for x in lines]), 2) if lines else None,
            "author_route": dict(Counter(str((x.get("author_route") or {}).get("status")) for x in lines)),
            "entity_route": dict(Counter(str((x.get("object_ref") or {}).get("entity_route")) for x in lines)),
            "graph": dict(Counter(str((x.get("graph") or {}).get("status")) if isinstance(x.get("graph"), dict)
                                  else str(x.get("graph")) for x in lines)),
            "translation": dict(Counter(str(x.get("translation_status")) for x in lines)),
            "late": dict(Counter(str(x.get("late")) for x in lines)),
            "warnings": dict(Counter(w for x in lines for w in x.get("warnings") or [])),
        }
    return out


LATIN = re.compile(r"[A-Za-z]")
DIGIT = re.compile(r"\d")


def _toks(s: str) -> set[str]:
    return set(re.findall(r"\w+", s.lower()))


def formulation_stats() -> dict:
    """Words, Latin, digits per generation and the overlap between generations (identical formulations, word Jaccard
    of the pair of one query); descriptive, the files are used as they are."""
    gens = {g: PL.load_generation(g) for g in GENS}
    out: dict = {}
    for g, rows in gens.items():
        fs = [f for pair in rows.values() for f in pair]
        wc = [len(f.split()) for f in fs]
        out[g] = {"file": PL.GEN_FILES[g][0], "rows": len(rows), "formulations": len(fs), "words_min": min(wc),
                  "words_median": median(wc), "words_max": max(wc),
                  "with_latin": sorted(q for q, p in rows.items() if any(LATIN.search(f) for f in p)),
                  "with_digits": sorted(q for q, p in rows.items() if any(DIGIT.search(f) for f in p))}
    overlap = {}
    for a, b in (("llm3ru_g1", "llm3ru_g2"), ("llm3ru_g1", "llm3ru_g3"), ("llm3ru_g2", "llm3ru_g3")):
        qids = sorted(set(gens[a]) & set(gens[b]))
        ident = sum(1 for q in qids for f in gens[a][q] if f.strip().lower() in {x.strip().lower() for x in gens[b][q]})
        jac = []
        for q in qids:
            A = set().union(*(_toks(f) for f in gens[a][q]))
            B = set().union(*(_toks(f) for f in gens[b][q]))
            jac.append(len(A & B) / len(A | B))
        overlap[f"{a} ~ {b}"] = {"identical_formulations": ident, "of": 2 * len(qids),
                                 "word_jaccard_mean": round(mean(jac), 3), "word_jaccard_median": round(median(jac), 3)}
    out["overlap"] = overlap
    return out


def vs_e6b(runs: dict, e6b_runs: dict) -> dict:
    """Image e15f9b3 against the E6b answers of image 1b20e03 for the same requests: queries whose first 50 page ids
    are identical (descriptive; the E6b answers are not used in any comparison)."""
    out = {}
    for system, old in E6B_NAMES.items():
        same = n = 0
        differ = []
        for (q, s), x in runs.items():
            if s != system or (q, old) not in e6b_runs:
                continue
            n += 1
            a = [h["page_id"] for h in x.get("hits") or []][:50]
            b = [h["page_id"] for h in e6b_runs[(q, old)].get("hits") or []][:50]
            if a == b:
                same += 1
            else:
                differ.append(q)
        out[system] = {"n": n, "identical_top50": same, "differ": sorted(differ)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", action="append", required=True)
    ap.add_argument("--e6b-runs", action="append", default=[])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    topics = SE.PT.load_topics()
    runs, meta, files = SE.load_runs(args.runs)
    qtopic = {q.query_id: t.topic_id for t in topics for q in t.queries}
    tr, label_info, judged = SE.truths(topics)
    qids = {q for q in qtopic if q.endswith(SUFFIX)}
    rk = {L(label): ranks for label, ranks in
          SE.topic_rankings(runs, {s: s for s in SYSTEMS}, qids, "plain").items()}
    res: dict = {"systems": [*SYSTEMS, MEAN], "set": {"name": SET_NAME, "n_queries": len(qids),
                                                      **{s: len(rk[L(s)]) for s in SYSTEMS}},
                 "judged": SE.judged_block(rk, qtopic, judged), "summary": {}, "comparisons": {},
                 "comparisons_exact_delta": {}, "spread_pairs_descriptive": {}}
    prim = [(L(a), L(b)) for a, b in PRIMARY_PAIRS]
    pair = [(L(a), L(b)) for a, b in SPREAD_PAIRS]
    spreads = {}
    for tname, truth in tr.items():
        rows = SE.metric_rows(truth, rk)
        rows += mean_rows(rows)
        res["summary"][tname] = SE.summarize(rows)
        res["comparisons"][tname] = TB.compare_systems(rows, prim, METRICS)
        res["comparisons_exact_delta"][tname] = {f"{L(a)} - {L(b)} | {m}": exact_delta(rows, a, b, m)
                                                 for a, b in PRIMARY_PAIRS + SPREAD_PAIRS for m in METRICS}
        res["spread_pairs_descriptive"][tname] = TB.compare_systems(rows, pair, METRICS)
        spreads[tname] = spread(rows, res["comparisons_exact_delta"][tname])
    res["spread"] = spreads
    out = {"benchmark": "TOPIC_BENCHMARK_V1", "kind": "pre-registered experiment E6c (PREREGISTRATION_E6C_2026-10-13.md)",
           "snapshot_id": SE.SNAPSHOT, "labels": label_info,
           "inputs": {name: sha for name, sha in PL.GEN_FILES.values()},
           "runs": {"files": files,
                    "meta": [{k: m.get(k) for k in ("kind", "harness_version", "label", "systems", "n_lines",
                                                     "canonical_snapshot_id", "started_at", "finished_at", "calls",
                                                     "errors", "retries", "requests") if m.get(k) is not None}
                             for m in meta]},
           "errors": dict(Counter(s for (q, s), line in runs.items() if line.get("error"))),
           "E6c": res, "E6c_decision": decide_e6c(res, res["comparisons_exact_delta"]),
           "descriptive": {"new_pages_top10": new_pages(rk, tr["V"], 10), "new_pages_top50": new_pages(rk, tr["V"], 50),
                           "diagnostics": diagnostics(runs), "formulations": formulation_stats()}}
    if args.e6b_runs:
        e6b_runs, _m, e6b_files = SE.load_runs(args.e6b_runs)
        out["descriptive"]["vs_e6b_image_1b20e03"] = {"files": e6b_files, **vs_e6b(runs, e6b_runs)}
    text = json.dumps(out, ensure_ascii=False, indent=1) + "\n"
    Path(args.out).write_bytes(text.encode("utf-8"))
    print(f"wrote {args.out} sha256 {hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}")


if __name__ == "__main__":
    main()
