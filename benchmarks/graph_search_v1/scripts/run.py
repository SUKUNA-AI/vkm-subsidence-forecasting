"""GRAPH_SEARCH_V1 stages B and C (retrieval lab venv): every system on P-E, the dev choice, the combination, the
evaluation and the preregistered decision (PREREGISTRATION §4–§7).

    variants  — E and every grid variant of G1–G5 for every query of both sets → ``$GS_WORK/runs.jsonl``
    select    — the dev choice per stage (topic_v1 dev topics, page_recall@50) and GC's composition
                → ``$GS_WORK/selection.json``
    combo     — GC (when composed) → ``$GS_WORK/runs_gc.jsonl``
    evaluate  — test decision + full-set description → ``benchmarks/graph_search_v1/results_v1.json`` (IDs and
                numbers only); needs the top-up labels of ``pool.py`` for retrieval_v1

The stages run through ``search.graph_stages.GraphRun`` — the hooks ``search.hybrid`` calls — over the local copy of E
(``engine.py``); the NAV lookup is ``NavGraphSignals`` over the local NAV root, G3's wordings come from stage A
(``prepare.py``, the same ``expand_query``). Environment: as ``engine.py`` plus ``GS_NAV_ROOT``.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab import metrics as M  # noqa: E402
from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402
from vkm_corpus.search import graph_stages as G  # noqa: E402

OUT_DIR = REPO / "benchmarks" / "graph_search_v1"
SEED, N_PERM = 20260929, 10_000
MIN_GAIN = 0.02
DEPTH_OUT = 100
TIE = 0.002                                   # dev: a smaller difference keeps the earlier (more cautious) variant

VARIANTS: dict[str, tuple[tuple[str, ...], dict[str, Any]]] = {
    "E": ((), {}),
    "G1": (("collapse",), {}),
    "G2@w0.5": (("cohesion",), {"cohesion_weight": 0.5}),
    "G2@w1.0": (("cohesion",), {"cohesion_weight": 1.0}),
    "G3@post-plain": (("concepts",), {"concepts_mode": "post", "concepts_narrower": False}),
    "G3@post-narrower": (("concepts",), {"concepts_mode": "post", "concepts_narrower": True}),
    "G3@window-plain": (("concepts",), {"concepts_mode": "window", "concepts_narrower": False}),
    "G3@window-narrower": (("concepts",), {"concepts_mode": "window", "concepts_narrower": True}),
    "G4@post": (("cites",), {"cites_mode": "post"}),
    "G4@window": (("cites",), {"cites_mode": "window"}),
    "G5@w0.5": (("topics",), {"topics_weight": 0.5}),
    "G5@w1.0": (("topics",), {"topics_weight": 1.0}),
}
GRID: dict[str, list[str]] = {"G1": ["G1"], "G2": ["G2@w0.5", "G2@w1.0"],
                              "G3": ["G3@post-plain", "G3@post-narrower", "G3@window-plain", "G3@window-narrower"],
                              "G4": ["G4@post", "G4@window"], "G5": ["G5@w0.5", "G5@w1.0"]}
STAGE_OF = {"G1": "collapse", "G2": "cohesion", "G3": "concepts", "G4": "cites", "G5": "topics"}


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def work() -> Path:
    return Path(os.environ["GS_WORK"])


# ---------------------------------------------------------------------------------------------------- sets
def load_sets() -> dict[str, Any]:
    splits = json.loads((OUT_DIR / "splits.json").read_text(encoding="utf-8"))
    bench = B.load_benchmark(REPO / "benchmarks/retrieval_v0")
    v1q = [q for q in bench.queries if q.query_id in splits["retrieval_v1"]]
    topics = TB.load_set(REPO / "benchmarks/topic_v1/topic_set_v1.jsonl")
    return {"splits": splits, "bench": bench, "v1": v1q, "topics": topics,
            "texts": {**{q.query_id: q.text for q in v1q}, **{q.query_id: q.text for t in topics for q in t.queries}}}


def params_of(overrides: dict[str, Any]) -> G.GraphParams:
    return replace(G.GraphParams(), **overrides).validate()


# ---------------------------------------------------------------------------------------------------- stage B
def run_system(eng: Any, maps: G.GraphMaps, st: Any, query: str, stages: tuple[str, ...], params: G.GraphParams,
               expansions: dict[str, Any] | None) -> tuple[list[str], G.GraphRun, float]:
    """E's state + the graph stages, exactly in the order ``search.hybrid`` runs them (PAGE kind, late on)."""
    from engine import WINDOW

    t0 = time.perf_counter()
    run = G.GraphRun(stages, params, G.StaticSignals(maps, {query: expansions or {}}), bm25=eng.bm25_pages)
    run.gate(page_kind=True, late=True)
    if run.on("concepts"):
        texts = run.wordings(query)
        run.concepts_leg(texts, [run.bm25(x, None, params.concepts_depth) for x in texts])
    srcs = run.seed([p for p, _s in st.fused])
    if srcs:
        run.cites_leg(run.bm25(query, srcs, params.cites_depth))
    window = run.window(st.window)
    if len(window) > len(st.window):
        added = set(window) - set(st.window)
        late = eng.late_scores(query, window)
        order = eng.late_order(window, late) + [p for p, _s in st.fused[WINDOW:] if p not in added]
    else:
        order = list(st.order)
    if any(run.on(s) for s in G.PAGE_STAGES):
        order = run.after_late(order)
    if run.on("collapse"):
        order = run.finish(order, {})
    return order, run, (time.perf_counter() - t0) * 1000


def record_of(qid: str, system: str, order: list[str], run: G.GraphRun, ms: float) -> dict[str, Any]:
    top = order[:DEPTH_OUT]
    keep = set(top)
    return {"qid": qid, "system": system, "order": top,
            "copies": {p: [c["page_id"] for c in v if "page_id" in c] for p, v in run.copies.items() if p in keep},
            "status": {s: v for s, v in run.status.items() if v != "OFF"},
            "legs": {k: len(v) for k, v in run.legs.items()},
            "leg_pages_top50": {name: sum(1 for p in top[:50] if p in r) for name, r in run.leg_ranks.items()},
            "window_added": len(run.window_added),
            "info": _small_info(run.info), "ms": round(ms, 3)}


def _small_info(info: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "cohesion" in info:
        out["cohesion_sections"] = len(info["cohesion"].get("sections") or [])
    if "topics" in info:
        out["topics"] = len(info["topics"].get("topics") or [])
    if "concepts" in info:
        out["expansions"] = [x.get("kind") for x in info["concepts"].get("expansions") or []]
    if "cites" in info:
        out["neighbour_sources"] = info["cites"].get("neighbour_sources")
    if "collapse" in info:
        out["collapsed"] = info["collapse"].get("collapsed")
    return out


def load_maps() -> G.GraphMaps:
    from vkm_corpus.navigation import store as nav_store

    root = Path(os.environ["GS_NAV_ROOT"])
    nav = nav_store.NavStore(root, canonical_db=root / "duckdb" / "vkm_corpus.duckdb")
    return G.NavGraphSignals(nav).view()


def stage_variants(systems: dict[str, tuple[tuple[str, ...], dict[str, Any]]], out_name: str) -> None:
    from engine import Engine

    sets = load_sets()
    a = json.loads((work() / "stage_a.json").read_text(encoding="utf-8"))
    maps = load_maps()
    log("maps", maps.parts, maps.build_ms)
    eng = Engine(encoders=False)
    texts = sets["texts"]
    eng.encode(texts.values())
    path = work() / out_name
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for qid, text in sorted(texts.items()):
            st = eng.run_e(text)
            for system, (stages, overrides) in systems.items():
                params = params_of(overrides)
                exp = a["expansions"]["narrower" if params.concepts_narrower else "plain"].get(text)
                order, run, ms = run_system(eng, maps, st, text, stages, params, exp)
                f.write(json.dumps(record_of(qid, system, order, run, ms), ensure_ascii=False) + "\n")
            n += 1
            if n % 100 == 0:
                log("queries", n, "/", len(texts))
    log("done", n, "queries", len(systems), "systems ->", path.name)


def read_runs(*names: str) -> dict[str, dict[str, dict[str, Any]]]:
    runs: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for name in names:
        p = work() / name
        if not p.exists():
            continue
        for line in open(p, encoding="utf-8"):
            r = json.loads(line)
            runs[r["system"]][r["qid"]] = r
    return runs


# ---------------------------------------------------------------------------------------------------- topic metrics
def topic_rows(runs: dict[str, dict[str, dict[str, Any]]], topics: list[TB.Topic], systems: list[str]
               ) -> list[dict[str, Any]]:
    evidence_sources = {x.source_id for t in topics for x in t.targets}
    rows = []
    for system in systems:
        for t in topics:
            for q in t.queries:
                r = runs[system][q.query_id]
                ranking = TB.Ranking()
                for p in r["order"][:TB.MAX_PAGES]:
                    ranking.add_page(p, r["copies"].get(p, ()))
                m = TB.query_metrics(t, ranking)
                top = r["order"][:10]
                m["evidence_source_share@10"] = (sum(1 for p in top if p.split(":")[0] in evidence_sources) /
                                                 len(top)) if top else 0.0
                rows.append({"system": system, "topic_id": t.topic_id, "query_id": q.query_id, "track": t.track,
                             "variant": q.variant, **m})
    return rows


TOPIC_METRICS = ("page_recall@50", "page_recall@20", "mrr@50", "source_recall@10", "success@10",
                 "evidence_source_share@10")


def topic_means(rows: list[dict[str, Any]], system: str, metric: str, topics: set[str]) -> dict[str, float]:
    acc: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["system"] == system and r["topic_id"] in topics:
            acc[r["topic_id"]].append(r[metric])
    return {t: sum(v) / len(v) for t, v in acc.items()}


def compare(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    t = M.paired_randomization(a, b, n=N_PERM, seed=SEED)
    ci = M.bootstrap_ci(a, b, n=N_PERM, seed=SEED)
    return {"n": t["n"], "delta": round(t["delta"], 6), "p_value": round(t["p_value"], 6),
            "ci_lo": round(ci["lo"], 6), "ci_hi": round(ci["hi"], 6)}


# ---------------------------------------------------------------------------------------------------- stage C1
def stage_select() -> None:
    sets = load_sets()
    runs = read_runs("runs.jsonl")
    dev = {t for t, s in sets["splits"]["topic_v1"].items() if s == "dev"}
    rows = topic_rows(runs, sets["topics"], list(VARIANTS))
    mean = {s: statistics.fmean(topic_means(rows, s, "page_recall@50", dev).values()) for s in VARIANTS}
    base = mean["E"]
    chosen, table = {}, {}
    for stage, variants in GRID.items():
        best = variants[0]
        for v in variants[1:]:
            if mean[v] > mean[best] + TIE:
                best = v
        chosen[stage] = best
        table[stage] = {v: {"dev_page_recall@50": round(mean[v], 6), "delta_vs_E": round(mean[v] - base, 6)}
                        for v in variants}
    combo = [s for s in GRID if mean[chosen[s]] - base > 0]
    out = {"rule": "dev topics (topic_v1, 39): the variant with the highest mean page_recall@50; a difference < "
                   f"{TIE} keeps the earlier variant; GC = the stages whose chosen variant has delta > 0 vs E "
                   "(PREREGISTRATION §4)",
           "dev_topics": len(dev), "E_dev_page_recall@50": round(base, 6), "variants": table, "chosen": chosen,
           "gc_stages": combo if len(combo) >= 2 else [], "gc": "RUN" if len(combo) >= 2 else "NOT_RUN (< 2 stages)"}
    (work() / "selection.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))


def gc_system() -> dict[str, tuple[tuple[str, ...], dict[str, Any]]]:
    sel = json.loads((work() / "selection.json").read_text(encoding="utf-8"))
    if not sel["gc_stages"]:
        return {}
    stages, overrides = [], {}
    for s in sel["gc_stages"]:
        st, ov = VARIANTS[sel["chosen"][s]]
        stages += list(st)
        overrides.update(ov)
    return {"GC": (tuple(x for x in G.STAGES if x in stages), overrides)}


def final_systems() -> dict[str, str]:
    """Final system name → the variant it is (E, G1…G5 with the chosen variant, GC)."""
    sel = json.loads((work() / "selection.json").read_text(encoding="utf-8"))
    out = {"E": "E", **{s: sel["chosen"][s] for s in GRID}}
    if sel["gc_stages"]:
        out["GC"] = "GC"
    return out


# ---------------------------------------------------------------------------------------------------- V1 metrics
def copy_groups(maps: G.GraphMaps, min_coverage: float) -> dict[str, str]:
    """Pages linked by the G1 relation (coverage ≥ ``min_coverage`` either way) → one group id (union-find)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p, cov in maps.coverage.items():
        for q, share in cov.items():
            if share >= min_coverage:
                a, b = find(p), find(q)
                if a != b:
                    parent[max(a, b)] = min(a, b)
    return {p: find(p) for p in parent}


def dedup_metrics(ranked: list[str], judged: dict[str, int], group_of: dict[str, str], threshold: int = 2
                  ) -> dict[str, float]:
    """Metrics over copy groups: a page whose group was already shown takes a slot and gains nothing; a group's grade
    is the best grade of its judged pages; the ideal ranking counts every group once (PREREGISTRATION §6)."""
    grade: dict[str, int] = {}
    for d, g in judged.items():
        k = group_of.get(d, d)
        grade[k] = max(grade.get(k, 0), int(g))
    relevant = {k for k, g in grade.items() if g >= threshold}
    seen: set[str] = set()
    gains, rel_hit, judged_slot = [], [], []
    for p in ranked:
        k = group_of.get(p, p)
        new = k not in seen
        seen.add(k)
        gains.append(float(grade.get(k, 0)) if new else 0.0)
        rel_hit.append(k if new and k in relevant else None)
        judged_slot.append(k in grade)
    ideal = sorted((float(g) for g in grade.values() if g > 0), reverse=True)
    m: dict[str, float] = {}
    idcg = M.dcg(ideal[:10])
    m["ndcg@10"] = M.dcg(gains[:10]) / idcg if idcg > 0 else 0.0
    for k in (10, 50, 100):
        m[f"recall@{k}"] = len({x for x in rel_hit[:k] if x}) / len(relevant) if relevant else 0.0
    m["mrr@10"] = next((1.0 / (i + 1) for i, x in enumerate(rel_hit[:10]) if x), 0.0)
    m["judged@10"] = sum(judged_slot[:10]) / len(judged_slot[:10]) if judged_slot[:10] else 1.0
    return m


def v1_labels(sets: dict[str, Any]) -> dict[str, Any]:
    bench = sets["bench"]
    verified = bench.judgments(level="PAGE")
    rows = B.load_pooled_qrels(REPO / "benchmarks/retrieval_v1/qrels_v1_pooled.tsv") + \
        B.load_pooled_qrels(REPO / "benchmarks/retrieval_v2/qrels_v2_pooled.tsv")
    gs = OUT_DIR / "qrels_gs_pooled.tsv"
    n_gs = 0
    if gs.exists():
        gs_rows = B.load_pooled_qrels(gs)
        n_gs = len(gs_rows)
        rows += gs_rows
    pooled = B.merge_judgments(verified, B.pooled_judgments(rows))
    return {"V": verified, "P": pooled, "hn": bench.hard_negative_ids("PAGE"), "n_gs": n_gs}


V1_METRICS = ("ndcg@10", "recall@50", "mrr@10", "judged@10", "recall@100")


def v1_eval(runs: dict[str, dict[str, dict[str, Any]]], system: str, qids: list[str], judged: dict[str, dict[str, int]],
            hn: dict[str, set[str]], groups: dict[str, str] | None) -> dict[str, dict[str, float]]:
    """Per query metrics (queries with a relevant judged page only, as ``metrics.evaluate``)."""
    out = {}
    if groups is None:
        ev = M.evaluate({q: runs[system][q]["order"] for q in qids}, {q: judged.get(q, {}) for q in qids},
                        hard_negatives=hn, query_ids=qids)
        return ev.per_query
    for q in qids:
        j = judged.get(q, {})
        if not any(g >= 2 for g in j.values()):
            continue
        out[q] = dedup_metrics(runs[system][q]["order"], j, groups)
    return out


def mean_of(per_query: dict[str, dict[str, float]], metric: str, qids: list[str] | None = None) -> float | None:
    vals = [m[metric] for q, m in per_query.items() if metric in m and (qids is None or q in qids)]
    return round(sum(vals) / len(vals), 6) if vals else None


# ---------------------------------------------------------------------------------------------------- stage C2
def stage_evaluate() -> None:
    sets = load_sets()
    runs = read_runs("runs.jsonl", "runs_gc.jsonl")
    sel = json.loads((work() / "selection.json").read_text(encoding="utf-8"))
    finals = final_systems()
    for name, variant in finals.items():
        if name not in runs:
            runs[name] = runs[variant]
    maps = load_maps()
    groups = copy_groups(maps, G.GraphParams().copy_min_coverage)
    labels = v1_labels(sets)
    split_t = sets["splits"]["topic_v1"]
    split_v = sets["splits"]["retrieval_v1"]
    test_topics = {t for t, s in split_t.items() if s == "test"}
    all_topics = set(split_t)
    names = list(finals)
    rows = topic_rows(runs, sets["topics"], names)
    res: dict[str, Any] = {"benchmark": "GRAPH_SEARCH_V1", "status": "PREREGISTERED",
                           "preregistration_sha256": (OUT_DIR / "PREREGISTRATION.sha256").read_text(
                               encoding="utf-8").split()[0],
                           "engine": "P-E (local copy of scheme E; TERM_DICTIONARY_V1 §3)",
                           "selection": sel, "systems": finals,
                           "labels": {"gs_top_up": labels["n_gs"]}, "topic": {}, "retrieval_v1": {}, "decision": {}}
    # ---- topic: means (test, all) and paired comparisons vs E
    for scope, tset in (("test", test_topics), ("all", all_topics)):
        block: dict[str, Any] = {"n_topics": len(tset), "means": {}, "vs_E": {}}
        for s in names:
            block["means"][s] = {m: round(statistics.fmean(topic_means(rows, s, m, tset).values()), 6)
                                 for m in TOPIC_METRICS}
        for s in names:
            if s == "E":
                continue
            block["vs_E"][s] = {m: compare(topic_means(rows, s, m, tset), topic_means(rows, "E", m, tset))
                                for m in TOPIC_METRICS}
        res["topic"][scope] = block
    # ---- retrieval_v1: V and P, standard and copy-group forms
    tracks = {"text": [q.query_id for q in sets["v1"] if q.track == "text"],
              "visual": [q.query_id for q in sets["v1"] if q.track == "visual"]}
    per: dict[tuple[str, str, str, str], dict[str, dict[str, float]]] = {}
    for form in ("standard", "groups"):
        for lab in ("V", "P"):
            for track, qids in tracks.items():
                for s in names:
                    per[(form, lab, track, s)] = v1_eval(runs, s, qids, labels[lab], labels["hn"],
                                                         groups if form == "groups" else None)
    for scope in ("test", "all"):
        block = {}
        for form in ("standard", "groups"):
            for lab in ("V", "P"):
                for track, qids in tracks.items():
                    ids = [q for q in qids if scope == "all" or split_v.get(q) == "test"]
                    key = f"{form}|{lab}|{track}"
                    b = {"n_queries": len(ids), "means": {}, "vs_E": {}}
                    for s in names:
                        b["means"][s] = {m: mean_of(per[(form, lab, track, s)], m, ids) for m in V1_METRICS}
                    for s in names:
                        if s == "E":
                            continue
                        b["vs_E"][s] = {}
                        for m in ("ndcg@10", "recall@50", "mrr@10"):
                            pa = {q: v[m] for q, v in per[(form, lab, track, s)].items() if q in ids}
                            pb = {q: v[m] for q, v in per[(form, lab, track, "E")].items() if q in ids}
                            b["vs_E"][s][m] = compare(pa, pb)
                    block[key] = b
        res["retrieval_v1"][scope] = block
    # ---- decision (test only)
    prim = {s: res["topic"]["test"]["vs_E"][s]["page_recall@50"] for s in names if s != "E"}
    holm = TB.holm({s: v["p_value"] for s, v in prim.items()})
    for s in names:
        if s == "E":
            continue
        has_g1 = "collapse" in (VARIANTS.get(finals[s]) or gc_system().get(s, ((), {})))[0]
        form = "groups" if has_g1 else "standard"
        harm = {}
        for key, metric in ((f"{form}|P|text", "ndcg@10"), (f"{form}|P|text", "recall@50"),
                            (f"{form}|P|visual", "ndcg@10")):
            c = res["retrieval_v1"]["test"][key]["vs_E"][s][metric]
            harm[f"{key}|{metric}"] = {**c, "harm": c["delta"] < 0 and c["p_value"] < 0.05}
        p = prim[s]
        gain_ok = p["delta"] >= MIN_GAIN and p["ci_lo"] > 0 and holm[s] < 0.05
        res["decision"][s] = {"variant": finals[s], "primary": {**p, "p_holm": round(holm[s], 6)},
                              "gain_ok": gain_ok, "harm_checks": harm,
                              "no_harm": not any(v["harm"] for v in harm.values()),
                              "default_on": gain_ok and not any(v["harm"] for v in harm.values()),
                              "v1_form": form}
    res["rule"] = ("default-on only if on the test topics delta page_recall@50 >= 0.02, CI low > 0 and Holm p < 0.05, "
                   "and no significant harm (delta < 0 with p < 0.05) on retrieval_v1 test: text nDCG@10 (P), text "
                   "R@50 (P), visual nDCG@10 (P); copy-group form for systems with G1 (PREREGISTRATION §7)")
    # ---- behaviour and lab timings
    beh: dict[str, Any] = {}
    for s in names:
        recs = list(runs[s].values())
        st = defaultdict(int)
        for r in recs:
            for k, v in r["status"].items():
                st[f"{k}:{v}"] += 1
        ms = [r["ms"] for r in recs]
        beh[s] = {"status_counts": dict(sorted(st.items())),
                  "window_added_mean": round(statistics.fmean(r["window_added"] for r in recs), 3),
                  "leg_pages_in_top50_mean": {k: round(statistics.fmean(r["leg_pages_top50"].get(k, 0) for r in recs),
                                                       3) for k in ("cohesion", "concepts", "cites", "topics")},
                  "copies_top100_mean": round(statistics.fmean(sum(len(v) for v in r["copies"].values())
                                                               for r in recs), 3),
                  "lab_stage_ms_p50": round(statistics.median(ms), 3)}
    res["behaviour"] = beh
    a = json.loads((work() / "stage_a.json").read_text(encoding="utf-8"))
    res["latency"] = {"nav_view_build_ms": a["view_build_ms"], "expand_query": a["latency_ms"],
                      "lab_note": "lab_stage_ms_p50 in behaviour: the stage work of the lab engine (lab BM25 legs, "
                                  "late MaxSim of widened windows) — not CORE timings"}
    if (work() / "latency.json").exists():
        lat = json.loads((work() / "latency.json").read_text(encoding="utf-8"))
        res["latency"]["served_code"] = lat["systems"]
    res["per_query"] = {"topic": {s: {r["query_id"]: round(r["page_recall@50"], 4) for r in rows if r["system"] == s}
                                  for s in names},
                        "retrieval_v1_P_text_ndcg@10": {s: {q: round(v["ndcg@10"], 4) for q, v in
                                                            per[("standard", "P", "text", s)].items()} for s in names}}
    (OUT_DIR / "results_v1.json").write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                                             newline="\n")
    short = {"decision": {s: {k: v for k, v in d.items() if k in ("variant", "primary", "gain_ok", "no_harm",
                                                                  "default_on")} for s, d in res["decision"].items()},
             "topic_test_means": res["topic"]["test"]["means"]}
    print(json.dumps(short, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "variants":
        stage_variants(VARIANTS, "runs.jsonl")
    elif cmd == "select":
        stage_select()
    elif cmd == "combo":
        systems = gc_system()
        if systems:
            stage_variants(systems, "runs_gc.jsonl")
        else:
            log("GC not composed (fewer than two stages with a dev gain)")
    elif cmd == "evaluate":
        stage_evaluate()
    else:
        raise SystemExit(f"unknown command {cmd}")
