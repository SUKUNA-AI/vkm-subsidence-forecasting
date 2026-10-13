"""Score experiment E6b (PREREGISTRATION_E6B_2026-10-13.md): two Russian model paraphrases per query against the
query alone, the E6 Russian + English pair and the two human formulations. Output holds IDs and numbers only.

Everything that measures is the frozen code of the E6 scorer (``score_experiments_20261013.py``): run lines →
``score.load_runs`` (snapshot check), rankings in the ``plain`` form → ``topic_rankings``, truth V / P → ``truths``
(catalogue; catalogue + T1–T3 labels ≥ 2), judged@10 and the unjudged top-10 pairs → ``judged_block``, per-query
metrics → ``metric_rows``, tests → ``topic_bench.compare_systems`` (topic means, sign flip and bootstrap 10 000,
seed 20260928, Holm over the family of one call).

Families (§4 of the addendum): per truth (V, P) the primary family of 24 tests — 2 sets × (``llm3ru − base``,
``llm3ru − llm3``, ``llm3ru − human3``) × 4 metrics; the E6 comparisons re-run on the current image (``llm3 − base``,
``human3 − base``, ``llm3 − human3``) are a separate, descriptive family of 24.

usage: PYTHONPATH=src python benchmarks/topic_v1/scripts/score_e6b_20261013.py --runs <e6b.jsonl> \
           [--e6-runs <e6e7_topic.jsonl of image 4cefa90>] --out benchmarks/topic_v1/results_e6b_20261013.json
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
from statistics import mean, median

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


SE = _load("score_experiments_20261013", HERE / "score_experiments_20261013.py")
PL = _load("plans_e6b_20261013", HERE / "plans_e6b_20261013.py")
TB = SE.TB

E6B_SETS = SE.E6_SETS                                  # NAME → "-0", PARA1 → "-1"
E6B_SYSTEMS = PL.E6B_SYSTEMS                           # base, human3, llm3, llm3ru
PRIMARY_PAIRS = (("llm3ru", "base"), ("llm3ru", "llm3"), ("llm3ru", "human3"))
REPLICATION_PAIRS = (("llm3", "base"), ("human3", "base"), ("llm3", "human3"))
METRICS = SE.TOPIC_METRICS                             # R@50 (primary), R@20, MRR@50, S@10
JUDGED_MIN = SE.JUDGED_MIN                             # 0.85
GAIN_MIN = 0.05
E6_NAMES = {"base": "g0", "human3": "human3", "llm3": "llm3"}   # system names of the same requests in the E6 run


def p_interpretable(judged: dict, set_name: str, a: str, b: str) -> bool:
    """A comparison on P is interpreted only when judged@10 of both its systems is ≥ 0.85 (§2 of the addendum)."""
    return all(judged[f"{set_name}:{s}"]["judged@10"] >= JUDGED_MIN for s in (a, b))


def decide_e6b(res: dict) -> dict:
    """Rules E6b-R1 (llm3ru − base), E6b-R2 (llm3ru − human3, the E6-R2 grades), E6b-R3 (llm3ru − llm3)."""
    c = res["comparisons"]
    per_set = {}
    for s in E6B_SETS:
        key = f"{s}:llm3ru - {s}:base"
        v = c["V"][f"{key} | page_recall@50"]
        gain = v["delta"] >= GAIN_MIN and v["p_holm"] < 0.05
        harm = [f"V {key} | {m}" for m in ("mrr@50", "success@10")
                if c["V"][f"{key} | {m}"]["delta"] < 0 and c["V"][f"{key} | {m}"]["p_holm"] < 0.05]
        p_ok = p_interpretable(res["judged"], s, "llm3ru", "base")
        if p_ok:
            harm += [f"P {key} | {m}" for m in ("mrr@50", "success@10")
                     if c["P"][f"{key} | {m}"]["delta"] < 0 and c["P"][f"{key} | {m}"]["p_holm"] < 0.05]
        h = c["V"][f"{s}:llm3ru - {s}:human3 | page_recall@50"]
        r2 = ("лучше ручных" if h["delta"] > 0 and h["p_holm"] < 0.05 else
              "хуже ручных" if h["delta"] < 0 and h["p_holm"] < 0.05 and h["ci_lo"] < -0.05 else
              "не хуже ручных (граница 0,05)" if h["ci_lo"] >= -0.05 else "не определено")
        e = c["V"][f"{s}:llm3ru - {s}:llm3 | page_recall@50"]
        r3 = ("две русские лучше пары рус+англ" if e["delta"] > 0 and e["p_holm"] < 0.05 else
              "две русские хуже пары рус+англ" if e["delta"] < 0 and e["p_holm"] < 0.05 else
              "разница не установлена")
        per_set[s] = {"R1_gain": gain, "R1_harm": harm, "R1_passes": bool(gain and not harm),
                      "R1_P_harm_checked": p_ok, "delta_R50_V": v["delta"], "ci_R50_V": [v["ci_lo"], v["ci_hi"]],
                      "p_holm_R50_V": v["p_holm"], "R2_llm3ru_vs_human3": r2, "R3_llm3ru_vs_llm3": r3}
    n = sum(1 for x in per_set.values() if x["R1_passes"])
    verdict = ("ПРИНЯТО на обоих наборах" if n == 2 else
               "ЧАСТИЧНО: принято на одном наборе" if n == 1 else "НЕ ПРИНЯТО")
    p_flags = {f"{s}:{a} - {s}:{b}": p_interpretable(res["judged"], s, a, b)
               for s in E6B_SETS for a, b in PRIMARY_PAIRS + REPLICATION_PAIRS}
    return {"per_set": per_set, "verdict": verdict, "P_interpretable": p_flags}


# ------------------------------------------------------------------------------------------------ descriptive
def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))], 1)


def new_pages(runs: dict, rk: dict, topics: list, k: int) -> dict:
    """Mean number of first-k pages a system adds against ``base`` of the same query (page ids of the hits), and how
    many of them are V targets of the topic (a hit with its collapsed duplicates)."""
    tmap = {q.query_id: t for t in topics for q in t.queries}
    out = {}
    for s_name in E6B_SETS:
        for system in E6B_SYSTEMS[1:]:
            added, added_t = [], []
            for qid, r in rk[f"{s_name}:{system}"].items():
                b = rk[f"{s_name}:base"].get(qid)
                if b is None:
                    continue
                base_pages = set(b.pages[:k])
                tpages = set().union(*(t.pages for t in tmap[qid].targets)) if qid in tmap else set()
                new = [(p, al) for p, al in list(zip(r.pages, r.aliases))[:k] if p not in base_pages]
                added.append(len(new))
                added_t.append(sum(1 for _p, al in new if al & tpages))
            out[f"{s_name}:{system}"] = {"mean_new": round(mean(added), 2) if added else None,
                                         "mean_new_targets_V": round(mean(added_t), 3) if added_t else None}
    return out


def diagnostics(runs: dict) -> dict:
    """Latency, formulations stage and route statuses per system (ids / counters only)."""
    out: dict = {}
    for system in E6B_SYSTEMS:
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


def formulation_stats() -> dict:
    """Words per formulation, Latin and digits (descriptive; the files are used as they are)."""
    out = {}
    for v in E6B_SETS:
        name, _sha = PL.LLM_RU_FILES[v]
        rows = PL.load_llm_ru(v)
        fs = [f for pair in rows.values() for f in pair]
        wc = [len(f.split()) for f in fs]
        out[name] = {"rows": len(rows), "formulations": len(fs), "words_min": min(wc), "words_median": median(wc),
                     "words_max": max(wc),
                     "with_latin": sorted(q for q, pair in rows.items() if any(LATIN.search(f) for f in pair)),
                     "with_digits": sorted(q for q, pair in rows.items() if any(DIGIT.search(f) for f in pair))}
    return out


def vs_e6(runs: dict, e6_runs: dict) -> dict:
    """Image 1b20e03 (routes off) against the E6 answers of image 4cefa90 for the same requests: queries whose first 50
    page ids are identical (descriptive; the E6 answers are not used in any comparison)."""
    out = {}
    for system, old in E6_NAMES.items():
        same = n = 0
        for (q, s), x in runs.items():
            if s != system or (q, old) not in e6_runs:
                continue
            n += 1
            a = [h["page_id"] for h in x.get("hits") or []][:50]
            b = [h["page_id"] for h in e6_runs[(q, old)].get("hits") or []][:50]
            same += a == b
        out[system] = {"n": n, "identical_top50": same}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", action="append", required=True)
    ap.add_argument("--e6-runs", action="append", default=[])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    topics = SE.PT.load_topics()
    runs, meta, files = SE.load_runs(args.runs)
    qtopic = {q.query_id: t.topic_id for t in topics for q in t.queries}
    tr, label_info, judged = SE.truths(topics)
    rk: dict = {}
    sets = {}
    for set_name, suffix in E6B_SETS.items():
        qids = {q for q in qtopic if q.endswith(suffix)}
        for label, ranks in SE.topic_rankings(runs, {s: s for s in E6B_SYSTEMS}, qids, "plain").items():
            rk[f"{set_name}:{label}"] = ranks
        sets[set_name] = {"n_queries": len(qids), **{s: len(rk[f"{set_name}:{s}"]) for s in E6B_SYSTEMS}}
    prim = [(f"{s}:{a}", f"{s}:{b}") for s in E6B_SETS for a, b in PRIMARY_PAIRS]
    repl = [(f"{s}:{a}", f"{s}:{b}") for s in E6B_SETS for a, b in REPLICATION_PAIRS]
    res: dict = {"systems": list(E6B_SYSTEMS), "sets": sets, "judged": SE.judged_block(rk, qtopic, judged),
                 "summary": {}, "comparisons": {}, "replication_descriptive": {}}
    for tname, truth in tr.items():
        rows = SE.metric_rows(truth, rk)
        res["summary"][tname] = SE.summarize(rows)
        res["comparisons"][tname] = TB.compare_systems(rows, prim, METRICS)
        res["replication_descriptive"][tname] = TB.compare_systems(rows, repl, METRICS)
    out = {"benchmark": "TOPIC_BENCHMARK_V1", "kind": "pre-registered experiment E6b (PREREGISTRATION_E6B_2026-10-13.md)",
           "snapshot_id": SE.SNAPSHOT, "labels": label_info,
           "inputs": {name: sha for name, sha in PL.LLM_RU_FILES.values()},
           "runs": {"files": files,
                    "meta": [{k: m.get(k) for k in ("kind", "harness_version", "label", "systems", "n_lines",
                                                     "canonical_snapshot_id", "started_at", "finished_at", "calls",
                                                     "errors", "retries", "requests") if m.get(k) is not None}
                             for m in meta]},
           "errors": dict(Counter(s for (q, s), line in runs.items() if line.get("error"))),
           "E6b": res, "E6b_decision": decide_e6b(res),
           "descriptive": {"new_pages_top10": new_pages(runs, rk, tr["V"], 10),
                           "new_pages_top50": new_pages(runs, rk, tr["V"], 50),
                           "diagnostics": diagnostics(runs), "formulations": formulation_stats()}}
    if args.e6_runs:
        e6_runs, _m, e6_files = SE.load_runs(args.e6_runs)
        out["descriptive"]["vs_e6_image_4cefa90"] = {"files": e6_files, **vs_e6(runs, e6_runs)}
    text = json.dumps(out, ensure_ascii=False, indent=1) + "\n"
    Path(args.out).write_bytes(text.encode("utf-8"))
    print(f"wrote {args.out} sha256 {hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}")


if __name__ == "__main__":
    main()
