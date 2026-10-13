"""Score experiment E12 (PREREGISTRATION_E12_2026-10-13.md): the opt-in ``long_query`` mode (``relax``, ``strip``,
``relax_strip``) against ``strict`` on NL117 (main), topic_v1 351, query_types_v1 70 and retrieval_v1 189. Output holds
IDs and numbers only.

Everything that measures is frozen code:

* topic sets (NL117, topic_v1) — ``score_experiments_20261013.py`` (the E6 / E6c scorer): run lines →
  ``score.load_runs`` (snapshot check), ``plain`` rankings → ``topic_rankings``, truth V / P → ``truths``, judged@10
  and the unjudged top-10 pairs → ``judged_block``, per-query metrics → ``metric_rows``, tests →
  ``topic_bench.compare_systems`` (topic means, sign flip and bootstrap 10 000, seed 20260928, Holm over the family of
  one call). NL117 is scored on the truth of the topic of its PARA2 query (same ``query_id``).
* query_types_v1 — ``benchmarks/query_types_v1/scripts/score.py`` (E10): ``ordinary`` rankings, the primary metric of
  each type (A/B R@50 on V, C S@10, D source capped R@10, E capped R@50); its ``SYSTEMS`` constant is set to the four
  modes (the functions read it at call time); every query is its own unit.
* retrieval_v1 — ``score_experiments_20261013.retrieval_block`` (``ordinary`` and copy-group forms, V = VERIFIED,
  P = VERIFIED + pools V1, V2, GS; ``metrics.paired_randomization`` / ``bootstrap_ci``).

New in E12 (§4–§6 of the pre-registration): exact (unrounded) differences of means for every threshold, the primary
family on NL117 (3 modes × R@50 on V, Holm over 3), the decision rule ``decide_e12`` (gain, head, A_NL direction,
no harm on the three other sets, code check), the code check of short queries (``code_check``, with the repeat
protocol ``classify_repeats``) and the trace statistics (``trace_stats``).

usage: PYTHONPATH=src python benchmarks/topic_v1/scripts/score_e12_20261013.py \
           --nl-runs <e12_nl.jsonl> --topic-runs <e12_topic.jsonl> --qt-runs <e12_qt.jsonl> --rv-runs <e12_rv.jsonl> \
           [--repeat-runs <e12_repeat.jsonl>] [--e6c-runs <e6c.jsonl>] [--rename <set>:<old>=<new>[,<new>…] …] \
           --out benchmarks/topic_v1/results_e12_20261013.json
       (``--rename`` copies the lines of a system under other names: used only to check the code on old answers)
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
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
PL = _load("plans_e12_20261013", HERE / "plans_e12_20261013.py")
QS = _load("query_types_v1_score", SE.REPO / "benchmarks" / "query_types_v1" / "scripts" / "score.py")
TB = SE.TB

MODES = PL.MODES                                          # strict, relax, strip, relax_strip
VARIANTS = MODES[1:]                                      # relax, strip, relax_strip — the order of simplicity
PAIRS = tuple((m, "strict") for m in VARIANTS)
PARA2 = "para2_strict"                                    # the PARA2 query itself, strict (topic run), NL117 only
QF_PAIRS = (("strict", PARA2),) + tuple((m, PARA2) for m in VARIANTS)
METRICS = SE.TOPIC_METRICS                                # R@50 (primary), R@20, MRR@50, S@10
PRIMARY = "page_recall@50"
GAIN_MIN = 0.05                                           # NL117 R@50 (V), exact difference
HEAD_TOL = 0.02                                           # NL117 MRR@50 and S@10 (V): not lower by more
HARM_TOL = 0.01                                           # main metric of topic_v1 / query_types_v1 / retrieval_v1
TYPE_TOL = 0.05                                           # any query type of query_types_v1
TIE = 0.01                                                # passing modes closer than this in ΔR@50: the simpler one
JUDGED_MIN = SE.JUDGED_MIN                                # 0.85
QT_TYPES = QS.TYPES
NL_SUFFIX = "-2"


def sysname(set_: str, mode: str) -> str:
    return PL.nl_system(mode) if set_ == "nl" else mode


# ------------------------------------------------------------------------------------------------ helpers
def exact_delta(rows: list[dict], a: str, b: str, metric: str) -> float:
    """Unrounded difference of topic means (``topic_bench.topic_means``) over the topics both systems answered."""
    ta, tb = TB.topic_means(rows, a, metric), TB.topic_means(rows, b, metric)
    ids = sorted(set(ta) & set(tb))
    return sum(ta[t] - tb[t] for t in ids) / len(ids)


def top(line: dict | None, k: int = 50) -> list[str]:
    return [h["page_id"] for h in (line or {}).get("hits") or []][:k]


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))], 1)


def rename(runs: dict, spec: dict[str, list[str]]) -> dict:
    """Copies of the lines of system ``old`` under each new name (code checks on old answers only)."""
    if not spec:
        return runs
    out = {}
    for (q, s), line in runs.items():
        if s in spec:
            for new in spec[s]:
                out[(q, new)] = {**line, "system": new}
        else:
            out[(q, s)] = line
    return out


# ------------------------------------------------------------------------------------------------ NL117
def nl_block(nl_runs: dict, topic_runs: dict, topics: list, tr: dict, judged: dict) -> dict:
    qtopic = {q.query_id: t.topic_id for t in topics for q in t.queries}
    ids = {q for q in qtopic if q.endswith(NL_SUFFIX)}
    rk = SE.topic_rankings(nl_runs, {m: sysname("nl", m) for m in MODES}, ids, "plain")
    rk[PARA2] = SE.topic_rankings(topic_runs, {PARA2: "strict"}, ids, "plain")[PARA2]
    res: dict = {"n_queries": {s: len(r) for s, r in rk.items()}, "judged": SE.judged_block(rk, qtopic, judged),
                 "summary": {}, "primary": {}, "secondary": {}, "exact": {}, "question_form_descriptive": {},
                 "per_question_R50": {}}
    for tname, truth in tr.items():
        rows = SE.metric_rows(truth, rk)
        res["summary"][tname] = SE.summarize(rows)
        res["primary"][tname] = TB.compare_systems(rows, PAIRS, (PRIMARY,))             # family of 3 (main on V)
        res["secondary"][tname] = TB.compare_systems(rows, PAIRS, METRICS[1:])          # family of 9, descriptive
        res["question_form_descriptive"][tname] = TB.compare_systems(rows, QF_PAIRS, METRICS)
        res["exact"][tname] = {f"{a} - {b} | {m}": exact_delta(rows, a, b, m)
                               for a, b in PAIRS + QF_PAIRS for m in METRICS}
        by = {(r["query_id"], r["system"]): r[PRIMARY] for r in rows}
        per = {}
        for m in VARIANTS:
            d = sorted(((by[(q, m)] - by[(q, "strict")], q) for q in sorted(ids)
                        if (q, m) in by and (q, "strict") in by), key=lambda x: (x[0], x[1]))

            def item(x: float, q: str, m: str = m) -> dict:
                return {"query_id": q, "delta": round(x, 4), "strict": round(by[(q, "strict")], 4),
                        "mode": round(by[(q, m)], 4)}

            per[m] = {"n_up": sum(1 for x, _q in d if x > 1e-12), "n_down": sum(1 for x, _q in d if x < -1e-12),
                      "n_equal": sum(1 for x, _q in d if abs(x) <= 1e-12),
                      "top_gain": [item(x, q) for x, q in sorted(d, key=lambda y: (-y[0], y[1]))[:5]],
                      "top_loss": [item(x, q) for x, q in d[:5]]}
        res["per_question_R50"][tname] = per
    res["P_interpretable"] = {f"{a} - {b}": all(res["judged"][s]["judged@10"] >= JUDGED_MIN for s in (a, b))
                              for a, b in PAIRS + QF_PAIRS}
    return res


# ------------------------------------------------------------------------------------------------ topic_v1 351
def topic_block(topic_runs: dict, topics: list, tr: dict, judged: dict, long_ids: set[str]) -> dict:
    qtopic = {q.query_id: t.topic_id for t in topics for q in t.queries}
    rk = SE.topic_rankings(topic_runs, {m: m for m in MODES}, set(qtopic), "plain")
    res: dict = {"n_queries": {s: len(r) for s, r in rk.items()}, "judged": SE.judged_block(rk, qtopic, judged),
                 "summary": {}, "comparisons": {}, "exact": {}, "by_variant_exact_R50": {}, "long_only": {}}
    for tname, truth in tr.items():
        rows = SE.metric_rows(truth, rk)
        res["summary"][tname] = SE.summarize(rows)
        res["comparisons"][tname] = TB.compare_systems(rows, PAIRS, METRICS)            # family of 12, descriptive
        res["exact"][tname] = {f"{a} - {b} | {m}": exact_delta(rows, a, b, m) for a, b in PAIRS for m in METRICS}
        res["by_variant_exact_R50"][tname] = {
            v: {f"{a} - {b}": exact_delta([r for r in rows if r["query_id"].endswith(sfx)], a, b, PRIMARY)
                for a, b in PAIRS} for v, sfx in (("NAME", "-0"), ("PARA1", "-1"), ("PARA2", "-2"))}
        lrows = [r for r in rows if r["query_id"] in long_ids]
        res["long_only"][tname] = {"n_queries": len({r["query_id"] for r in lrows}),
                                   "exact_R50": {f"{a} - {b}": exact_delta(lrows, a, b, PRIMARY) for a, b in PAIRS},
                                   "comparisons": TB.compare_systems(lrows, PAIRS, (PRIMARY,))}
    res["P_interpretable"] = {f"{a} - {b}": all(res["judged"][s]["judged@10"] >= JUDGED_MIN for s in (a, b))
                              for a, b in PAIRS}
    return res


# ------------------------------------------------------------------------------------------------ query_types_v1
def qt_primary_rows(rows: list[dict]) -> list[dict]:
    """One row per query and system: ``value`` = the primary metric of its type (truth V for A/B, AUTO otherwise)."""
    out = []
    for r in rows:
        if r["truth"] == ("V" if r["type"] in ("A_NL", "B_EN") else "AUTO"):
            out.append({"query_id": r["query_id"], "topic_id": r["query_id"], "type": r["type"],
                        "system": r["system"], "value": r[QS.PRIMARY[r["type"]]]})
    return out


def qt_block(qt_runs: dict, tr: dict, judged: dict) -> dict:
    QS.SYSTEMS = MODES                                   # the frozen functions read the module constant
    qs = QS.load_set()
    tv = {t.topic_id: t for t in tr["V"]}
    tp = {t.topic_id: t for t in tr["P"]}
    rows = QS.score(qs, qt_runs, tv, tp)
    prim = qt_primary_rows(rows)
    res: dict = {"n_queries": dict(Counter(q["type"] for q in qs)), "primary_metric": dict(QS.PRIMARY),
                 "summary": QS.summary(rows), "exact": {}, "means": {}, "comparisons_by_type": {},
                 "comparisons_overall": {}}
    for scope in ("overall", *QT_TYPES):
        sub = prim if scope == "overall" else [r for r in prim if r["type"] == scope]
        res["means"][scope] = {s: mean(r["value"] for r in sub if r["system"] == s) for s in MODES}
        res["exact"][scope] = {f"{a} - {b}": exact_delta(sub, a, b, "value") for a, b in PAIRS}
    pvals, out = {}, {}
    for t in QT_TYPES:
        for k, v in TB.compare_systems([r for r in prim if r["type"] == t], PAIRS, ("value",)).items():
            out[f"{t} | {k}"] = v
            pvals[f"{t} | {k}"] = v["p_value"]
    adj = TB.holm(pvals)
    res["comparisons_by_type"] = {k: {**v, "p_holm": round(adj[k], 5)} for k, v in out.items()}   # family of 15
    res["comparisons_overall"] = TB.compare_systems(prim, PAIRS, ("value",))                       # family of 3
    qtopic = {q["query_id"]: q["topic_id"] for q in qs if q["truth"]["kind"] == "TOPIC"}
    res["judged_A_B_P"] = {s: {"judged@10": SE.S2.judged_at({q: SE.ranking(line, "ordinary") for (q, sy), line in
                                                             qt_runs.items() if sy == s and q in qtopic},
                                                            qtopic, judged)} for s in MODES}
    res["per_query_primary"] = {r["query_id"]: {} for r in prim}
    for r in prim:
        res["per_query_primary"][r["query_id"]][r["system"]] = round(r["value"], 4)
    return res


# ------------------------------------------------------------------------------------------------ retrieval_v1
def rv_block(rv_runs: dict) -> dict:
    groups = SE.Groups()
    groups.add_runs(rv_runs)
    retr = SE.retrieval_block(rv_runs, MODES, list(PAIRS), groups)
    _bench, judged, hn, _info, tracks = SE.retrieval_truth()
    exact: dict = {}
    for tname, js in judged.items():
        for track, qids in tracks.items():
            per = {s: SE.retrieval_eval(rv_runs, s, qids, js, hn, "ordinary", None, None) for s in MODES}
            for a, b in PAIRS:
                for m in ("ndcg@10", "recall@50"):
                    common = sorted(set(per[a]) & set(per[b]))
                    exact[f"ordinary|{tname}|{track}|{a} - {b}|{m}"] = (
                        sum(per[a][q][m] - per[b][q][m] for q in common) / len(common) if common else None)
    p_ok = {f"{track}|{a} - {b}": all((retr["summary"]["ordinary"]["P"][track][s].get("judged@10") or 0.0)
                                      >= JUDGED_MIN for s in (a, b))
            for track in tracks for a, b in PAIRS}
    return {**retr, "exact": exact, "P_interpretable": p_ok}


# ------------------------------------------------------------------------------------------------ code check
REQUIRED = ("short_plain", "unchanged_plain")            # code check categories where identity is required


def code_check(sets: dict[str, dict], offline: dict, repeats: dict | None = None) -> dict:
    """Lines whose BM25 the mode leaves as it is must answer as ``strict``: the first 50 page ids identical, in order.
    Categories by the trace of the mode's line (``stages.long_query``):

    * ``short_plain`` — ``status = SHORT`` (fewer than 6 content terms) and no expansion (translation) ``APPLIED``;
    * ``unchanged_plain`` — ``status = UNCHANGED`` (long, but ``strip`` found no frame or nothing would be left) and no
      expansion ``APPLIED``;
    * ``with_long_expansion`` — ``SHORT`` / ``UNCHANGED``, but a translation is long and its BM25 leg is changed by
      design: identity is only described.

    Differences in the two required categories go to the repeat protocol (``classify_repeats``): ``CODE`` fails the
    check, ``NONDETERMINISM`` does not. Also: the offline long / short of ``plans_e12.offline`` against the server's
    flag, and that ``strict`` lines carry no ``long_query`` stage."""
    out: dict = {}
    for set_, runs in sets.items():
        per = offline["sets"][set_]["per_query"]
        block: dict = {"strict_lines_with_long_query_stage": sum(
            1 for (q, s), line in runs.items() if s == sysname(set_, "strict") and line.get("long_query") is not None),
            "offline_vs_server_long_mismatch": [], "modes": {}}
        for m in VARIANTS:
            cats: dict = {c: {"n": 0, "identical": 0, "differ": []}
                          for c in (*REQUIRED, "with_long_expansion")}
            for qid in sorted(per):
                line = runs.get((qid, sysname(set_, m)))
                ref = runs.get((qid, sysname(set_, "strict")))
                if line is None or ref is None:
                    continue
                lq = line.get("long_query") or {}
                if lq.get("long") is not None and bool(lq.get("long")) != bool(per[qid]["long"]) and m == "relax":
                    block["offline_vs_server_long_mismatch"].append(qid)
                if lq.get("status") not in ("SHORT", "UNCHANGED"):
                    continue
                exp_long = any(x.get("status") == "APPLIED" for x in lq.get("expansions") or [])
                c = cats["with_long_expansion" if exp_long else
                         "short_plain" if lq.get("status") == "SHORT" else "unchanged_plain"]
                c["n"] += 1
                if top(line) == top(ref):
                    c["identical"] += 1
                else:
                    c["differ"].append(qid)
            for c in cats.values():
                c["differ"] = sorted(c["differ"])
            block["modes"][m] = cats
        out[set_] = block
    verdict = {}
    for m in VARIANTS:
        diffs = [(s, q) for s, b in out.items() for c in REQUIRED for q in b["modes"][m][c]["differ"]]
        classes = {f"{s}|{q}": classify_repeats(sets[s], repeats, s, q, m) for s, q in diffs}
        verdict[m] = {"required_differ": len(diffs), "classes": classes,
                      "ok": all(c == "NONDETERMINISM" for c in classes.values())}
    out["verdict"] = verdict
    return out


def classify_repeats(runs: dict, repeats: dict | None, set_: str, qid: str, mode: str) -> str:
    """Repeat protocol (§6 of the pre-registration): the main answers and two repeats of ``strict`` and of the mode.
    ``CODE`` — the three strict answers are identical, the three mode answers are identical, and they differ;
    ``NONDETERMINISM`` — answers of one request vary; ``NOT_REPEATED`` — no repeat answers (counts as a failure)."""
    if not repeats:
        return "NOT_REPEATED"
    s_main, m_main = top(runs.get((qid, sysname(set_, "strict")))), top(runs.get((qid, sysname(set_, mode))))
    s_rep = [top(repeats.get((qid, f"{sysname(set_, 'strict')}~r{i}"))) for i in (1, 2)]
    m_rep = [top(repeats.get((qid, f"{sysname(set_, mode)}~r{i}"))) for i in (1, 2)]
    if not all(s_rep) or not all(m_rep):
        return "NOT_REPEATED"
    if s_rep[0] == s_rep[1] == s_main and m_rep[0] == m_rep[1] == m_main and s_main != m_main:
        return "CODE"
    return "NONDETERMINISM"


# ------------------------------------------------------------------------------------------------ descriptive
def trace_stats(set_: str, runs: dict, offline: dict) -> dict:
    per = offline["sets"][set_]["per_query"]
    long_ids = {q for q, x in per.items() if x["long"]}
    out: dict = {}
    strict = {q: line for (q, s), line in runs.items() if s == sysname(set_, "strict")}
    for m in MODES:
        lines = {q: line for (q, s), line in runs.items() if s == sysname(set_, m)}
        ms = [x["api_ms"] for x in lines.values() if x.get("api_ms") is not None]
        lq = [x.get("long_query") or {} for x in lines.values()]
        bm_long = [((x.get("totals_page") or {}).get("bm25")) for q, x in lines.items() if q in long_ids]
        bm_long = [b for b in bm_long if b is not None]
        cand = next((x.get("candidates") for x in lines.values() if x.get("candidates")), 100)
        overlap = [len(set(top(x)) & set(top(strict.get(q)))) for q, x in lines.items() if q in long_ids]
        same50 = sum(1 for q, x in lines.items() if q in long_ids and top(x) == top(strict.get(q)))
        out[m] = {
            "n": len(lines), "errors": sum(1 for x in lines.values() if x.get("error")),
            "api_ms_p50": _pct(ms, 0.5), "api_ms_p90": _pct(ms, 0.9),
            "long_query_status": dict(Counter(str(x.get("status")) for x in lq)),
            "long_query_strip": dict(Counter(str(x.get("strip")) for x in lq if x.get("status") is not None)),
            "applied_share": (sum(1 for x in lq if x.get("status") == "APPLIED") / len(lq)) if lq else None,
            "lines_with_expansions": sum(1 for x in lq if x.get("expansions")),
            "expansion_status": dict(Counter(str(e.get("status")) for x in lq for e in x.get("expansions") or [])),
            "translation": dict(Counter(str(x.get("translation_status")) for x in lines.values())),
            "translation_built_from": dict(Counter(str(x.get("translation_built_from")) for x in lines.values())),
            "bm25_total_long": {"n": len(bm_long), "median": median(bm_long) if bm_long else None,
                                "p10": _pct(bm_long, 0.1), "p90": _pct(bm_long, 0.9),
                                "below_candidates": sum(1 for b in bm_long if b < cand), "candidates": cand},
            "long_top50_overlap_with_strict_mean": round(mean(overlap), 2) if overlap else None,
            "long_top50_identical_to_strict": same50,
            "warnings": dict(Counter(w for x in lines.values() for w in x.get("warnings") or [])),
            "author_route": dict(Counter(str((x.get("author_route") or {}).get("status")) for x in lines.values())),
            "graph": dict(Counter(str((x.get("graph") or {}).get("status")) if isinstance(x.get("graph"), dict)
                                  else str(x.get("graph")) for x in lines.values())),
        }
    return out


def vs_e6c(topic_runs: dict, e6c_runs: dict) -> dict:
    """``strict`` PARA1 of E12 (image 5c7ad8d) against ``base`` of E6c (image e15f9b3) — the same requests: queries
    whose first 50 page ids are identical (descriptive: the strict path is unchanged by the long_query deploy)."""
    same, differ, n = 0, [], 0
    for (q, s), line in topic_runs.items():
        if s != "strict" or (q, "base") not in e6c_runs:
            continue
        n += 1
        if top(line) == top(e6c_runs[(q, "base")]):
            same += 1
        else:
            differ.append(q)
    return {"n": n, "identical_top50": same, "differ": sorted(differ)}


# ------------------------------------------------------------------------------------------------ rule
def decide_e12(nl: dict, topic: dict, qt: dict, rv: dict, code: dict) -> dict:
    """The rule of §5: per mode G1 gain, G2 head, G3 A_NL direction, H1–H3 no harm, C code check; then the choice."""
    per: dict = {}
    for m in VARIANTS:
        key = f"{m} - strict"
        d50 = nl["exact"]["V"][f"{key} | {PRIMARY}"]
        p_h = nl["primary"]["V"][f"{key} | {PRIMARY}"]["p_holm"]
        d_mrr, d_s10 = nl["exact"]["V"][f"{key} | mrr@50"], nl["exact"]["V"][f"{key} | success@10"]
        d_anl = qt["exact"]["A_NL"][key]
        d_topic = topic["exact"]["V"][f"{key} | {PRIMARY}"]
        d_qt = qt["exact"]["overall"][key]
        qt_types = {t: qt["exact"][t][key] for t in QT_TYPES}
        rv_truth = "P" if rv["P_interpretable"][f"text|{key}"] else "V"
        d_rv = rv["exact"][f"ordinary|{rv_truth}|text|{key}|ndcg@10"]
        conds = {"G1_gain": d50 >= GAIN_MIN and p_h < 0.05,
                 "G2_head": d_mrr >= -HEAD_TOL and d_s10 >= -HEAD_TOL,
                 "G3_A_NL_direction": d_anl >= 0,
                 "H1_topic_v1": d_topic >= -HARM_TOL,
                 "H2_query_types_v1": d_qt >= -HARM_TOL and all(v >= -TYPE_TOL for v in qt_types.values()),
                 "H3_retrieval_v1": d_rv >= -HARM_TOL,
                 "C_code_check": code["verdict"][m]["ok"]}
        per[m] = {"values": {"nl_R50_V_exact": d50, "nl_R50_V_rounded": round(d50, 4), "nl_p_holm": p_h,
                             "nl_mrr50_V_exact": d_mrr, "nl_s10_V_exact": d_s10, "A_NL_R50_V_exact": d_anl,
                             "topic_v1_R50_V_exact": d_topic, "query_types_v1_overall_exact": d_qt,
                             "query_types_v1_by_type_exact": qt_types, "retrieval_v1_truth": rv_truth,
                             "retrieval_v1_text_ndcg10_exact": d_rv},
                  "conditions": conds, "failed": [k for k, ok in conds.items() if not ok],
                  "passes": all(conds.values())}
    passing = [m for m in VARIANTS if per[m]["passes"]]
    chosen = None
    if passing:
        best = max(per[m]["values"]["nl_R50_V_exact"] for m in passing)
        near = [m for m in passing if best - per[m]["values"]["nl_R50_V_exact"] < TIE]
        chosen = next(m for m in VARIANTS if m in near)                 # the simplest of the near-best
    verdict = (f"КАНДИДАТ В УМОЛЧАНИЕ: {chosen}" if chosen else "НИ ОДИН РЕЖИМ НЕ ПРИНЯТ: умолчание остаётся strict")
    return {"per_mode": per, "passing": passing, "chosen": chosen, "verdict": verdict}


# ------------------------------------------------------------------------------------------------ main
def parse_rename(specs: list[str]) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    for s in specs:
        set_, rest = s.split(":", 1)
        old, new = rest.split("=", 1)
        out.setdefault(set_, {})[old] = new.split(",")
    return out


def meta_of(meta: list[dict]) -> list[dict]:
    return [{k: m.get(k) for k in ("kind", "harness_version", "label", "systems", "n_lines", "canonical_snapshot_id",
                                   "started_at", "finished_at", "calls", "errors", "retries", "requests")
             if m.get(k) is not None} for m in meta]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nl-runs", action="append", required=True)
    ap.add_argument("--topic-runs", action="append", required=True)
    ap.add_argument("--qt-runs", action="append", required=True)
    ap.add_argument("--rv-runs", action="append", required=True)
    ap.add_argument("--repeat-runs", action="append", default=[])
    ap.add_argument("--e6c-runs", action="append", default=[], help="E6c answers (image e15f9b3), descriptive")
    ap.add_argument("--rename", action="append", default=[])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    ren = parse_rename(args.rename)
    topics = SE.PT.load_topics()
    tr, label_info, judged = SE.truths(topics)
    offline = PL.offline()
    loaded, files, metas = {}, {}, {}
    for set_, paths in (("nl", args.nl_runs), ("topic", args.topic_runs), ("qt", args.qt_runs),
                        ("rv", args.rv_runs)):
        runs, meta, fs = SE.load_runs(paths)
        loaded[set_], files[set_], metas[set_] = rename(runs, ren.get(set_, {})), fs, meta_of(meta)
    repeats = None
    if args.repeat_runs:
        repeats, rmeta, rfiles = SE.load_runs(args.repeat_runs)
        files["repeat"], metas["repeat"] = rfiles, meta_of(rmeta)
    long_topic = {q for q, x in offline["sets"]["topic"]["per_query"].items() if x["long"]}
    nl = nl_block(loaded["nl"], loaded["topic"], topics, tr, judged)
    topic = topic_block(loaded["topic"], topics, tr, judged, long_topic)
    qt = qt_block(loaded["qt"], tr, judged)
    rv = rv_block(loaded["rv"])
    code = code_check(loaded, offline, repeats)
    out = {"benchmark": "TOPIC_BENCHMARK_V1 + QUERY_TYPES_V1 + RETRIEVAL_BENCHMARK_V1",
           "kind": "pre-registered experiment E12 (PREREGISTRATION_E12_2026-10-13.md)",
           "snapshot_id": SE.SNAPSHOT, "labels": label_info,
           "inputs": {PL.NL_FILE[0]: PL.NL_FILE[1], PL.TASK_FILE[0]: {"crlf": PL.TASK_FILE[1], "lf": PL.TASK_FILE[2]}},
           "renamed_for_code_check": ren or None,
           "runs": {"files": files, "meta": metas},
           "errors": {s: dict(Counter(sy for (q, sy), line in r.items() if line.get("error")))
                      for s, r in loaded.items()},
           "offline_long": {s: {k: v for k, v in x.items() if k != "per_query"} for s, x in offline["sets"].items()},
           "NL117": nl, "topic_v1": topic, "query_types_v1": qt, "retrieval_v1": rv, "code_check": code,
           "trace": {s: trace_stats(s, r, offline) for s, r in loaded.items()},
           "E12_decision": decide_e12(nl, topic, qt, rv, code)}
    if args.e6c_runs:
        e6c_runs, _m, e6c_files = SE.load_runs(args.e6c_runs)
        out["strict_vs_e6c_base_image_e15f9b3"] = {"files": e6c_files, **vs_e6c(loaded["topic"], e6c_runs)}
    text = json.dumps(out, ensure_ascii=False, indent=1) + "\n"
    Path(args.out).write_bytes(text.encode("utf-8"))
    print(f"wrote {args.out} sha256 {hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}")
    print(json.dumps(out["E12_decision"], ensure_ascii=False, indent=1)[:3000])


if __name__ == "__main__":
    main()
