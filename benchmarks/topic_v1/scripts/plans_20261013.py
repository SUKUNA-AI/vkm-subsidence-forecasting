"""Plans of the search experiments of 13.10.2026 (PREREGISTRATION_EXPERIMENTS_2026-10-13.md): the exact requests each
system sends, written as plan files for ``harness_plan.py`` (run with ``run_plan.sh``; plans hold query texts and stay
in git-ignored ``work/``).

    e6e7   topic_v1 (351 queries): E7 systems g0 (graph []), g1 (["collapse"]), g15 (["collapse", "topics"]) on every
           query; E6 systems human3 (+ the topic's two other human formulations) and llm3 (+ f1, f2 of the model file
           of the query's variant) on the NAME and PARA1 queries (graph []). E6 ``base`` is g0 (the same request).
    e7r    retrieval_v1 (the 189 queries of benchmarks/retrieval_v0): g0, g1, g15 (harm check of E7)
    e10    query_types_v1: bm25 (/v1/search), hybrid (server defaults: no graph field), hybrid_g0 (graph [])
    e11    topic_v1 (graph []) and retrieval_v1 (graph []): late_default, late_rrf, late_bt, late_both — the field
           names and values of the late-stage flags are arguments (fixed by the deploy message; see §E11)

Lines are interleaved by query (every system of a query, then the next query), so drift over the run hits every system
alike. Every hybrid request: kinds PAGE, limit 50, candidates 100 (``harness_core.py``'s ``hybrid_late``), the server
defaults for everything not named.

usage: python benchmarks/topic_v1/scripts/plans_20261013.py {e6e7,e7r,e10,e11} --out <plan.jsonl> [e11 options]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BENCH = REPO / "benchmarks" / "topic_v1"
SET_PATH = BENCH / "topic_set_v1.jsonl"
SET_SHA256 = "4dbc595723cbb9ed3e59d1a71cbe84301c9554ecedc41b7f3abf654258bac2b1"            # LF-normalised
LLM_DIR = BENCH / "llm_formulations_v1"
LLM_FILES = {"NAME": ("llm_name.jsonl", "61e88098befa1854f9955e6391a3adaf1e7e780bf424fb58e7665642fedd62a9"),
             "PARA1": ("llm_para1.jsonl", "58cb8f1028eb9705ef80d1198c1767e80a97d861d106cb6bc7044637bbe7d0a2")}
RETRIEVAL_QUERIES = REPO / "benchmarks" / "retrieval_v0" / "queries.jsonl"
QT_SET = REPO / "benchmarks" / "query_types_v1" / "query_types_v1.jsonl"
HYBRID = "/v1/search/hybrid"
BM25 = "/v1/search"
BASE_REQUEST = {"kinds": ["PAGE"], "limit": 50, "candidates": 100}
GRAPHS = {"g0": [], "g1": ["collapse"], "g15": ["collapse", "topics"]}
E6_VARIANTS = ("NAME", "PARA1")


def sha256_lf(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_topics() -> list[dict]:
    if sha256_lf(SET_PATH) != SET_SHA256:
        raise SystemExit("topic_set_v1.jsonl does not match the pre-registered sha256")
    return [json.loads(line) for line in SET_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_llm(variant: str, directory: Path = LLM_DIR) -> dict[str, list[str]]:
    """query_id → [f1, f2] of the model file of ``variant`` (LF-normalised sha256 checked against the
    pre-registration; the files were written with LF, so it equals the sha256 of the original bytes)."""
    name, sha = LLM_FILES[variant]
    path = directory / name
    if sha256_lf(path) != sha:
        raise SystemExit(f"{name}: sha256 differs from the pre-registered {sha}")
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            out[d["query_id"]] = [d["f1"], d["f2"]]
    return out


def hybrid(text: str, **extra) -> dict:
    return {"route": HYBRID, "request": {"query": text, **BASE_REQUEST, **extra}}


def line(query_id: str, topic_id: str | None, system: str, call: dict) -> dict:
    return {"query_id": query_id, "topic_id": topic_id, "system": system, **call}


def plan_e6e7(topics: list[dict], llm: dict[str, dict[str, list[str]]]) -> list[dict]:
    out = []
    for t in topics:
        for q in t["queries"]:
            for system, graph in GRAPHS.items():
                out.append(line(q["query_id"], t["topic_id"], system, hybrid(q["text"], graph=graph)))
            if q["variant"] in E6_VARIANTS:
                others = [x["text"] for x in t["queries"] if x["query_id"] != q["query_id"]]
                out.append(line(q["query_id"], t["topic_id"], "human3", hybrid(q["text"], graph=[], formulations=others)))
                out.append(line(q["query_id"], t["topic_id"], "llm3",
                                hybrid(q["text"], graph=[], formulations=list(llm[q["variant"]][q["query_id"]]))))
    return out


def retrieval_queries() -> list[dict]:
    return [json.loads(x) for x in RETRIEVAL_QUERIES.read_text(encoding="utf-8").splitlines() if x.strip()]


def plan_e7r() -> list[dict]:
    return [line(q["query_id"], None, system, hybrid(q["text"], graph=graph))
            for q in retrieval_queries() for system, graph in GRAPHS.items()]


NO_ROUTES = {"author_route": False, "entity_route": False}     # registry author / entity index routes (685a099)


def plan_e10(path: Path = QT_SET, with_noroutes: bool = False) -> list[dict]:
    out = []
    for x in path.read_text(encoding="utf-8").splitlines():
        if not x.strip():
            continue
        q = json.loads(x)
        out.append(line(q["query_id"], q.get("topic_id"), "bm25",
                        {"route": BM25, "request": {"query": q["text"], "kinds": ["PAGE"], "limit": 50}}))
        out.append(line(q["query_id"], q.get("topic_id"), "hybrid", hybrid(q["text"])))
        out.append(line(q["query_id"], q.get("topic_id"), "hybrid_g0", hybrid(q["text"], graph=[])))
        if with_noroutes:
            out.append(line(q["query_id"], q.get("topic_id"), "hybrid_noroutes", hybrid(q["text"], **NO_ROUTES)))
    return out


def plan_routes_r() -> list[dict]:
    """Descriptive (after 685a099): retrieval_v1 with the server defaults and with both registry routes off."""
    return [line(q["query_id"], None, system, hybrid(q["text"], **extra))
            for q in retrieval_queries() for system, extra in (("rv_default", {}), ("rv_noroutes", NO_ROUTES))]


def e11_systems(fusion_field: str, rrf_value: str, query_field: str, bt_value: str) -> dict[str, dict]:
    return {"late_default": {}, "late_rrf": {fusion_field: rrf_value}, "late_bt": {query_field: bt_value},
            "late_both": {fusion_field: rrf_value, query_field: bt_value}}


def plan_e11(topics: list[dict], systems: dict[str, dict], retrieval_extra: dict | None = None) -> list[dict]:
    out = []
    for t in topics:
        for q in t["queries"]:
            for system, extra in systems.items():
                out.append(line(q["query_id"], t["topic_id"], system, hybrid(q["text"], graph=[], **extra)))
    for q in retrieval_queries():
        for system, extra in systems.items():
            out.append(line(q["query_id"], None, system,
                            hybrid(q["text"], graph=[], **(retrieval_extra or {}), **extra)))
    return out


def write(plan: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n" for p in plan)
    if '"""' in text:
        raise SystemExit("a plan text contains a triple quote")
    out.write_bytes(text.encode("utf-8"))
    systems: dict[str, int] = {}
    for p in plan:
        systems[p["system"]] = systems.get(p["system"], 0) + 1
    print(f"{out.name}: {len(plan)} lines {systems} sha256 {hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=["e6e7", "e7r", "e10", "e11", "routes_r"])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--fusion-field", default="late_fusion")
    ap.add_argument("--rrf-value", default="rrf")
    ap.add_argument("--query-field", default="late_query")
    ap.add_argument("--bt-value", default="best_translation")
    ap.add_argument("--retrieval-no-routes", action="store_true",
                    help="e11: author_route / entity_route false on every retrieval_v1 line (deviation, 685a099)")
    ap.add_argument("--with-noroutes", action="store_true", help="e10: add hybrid_noroutes (both routes off)")
    args = ap.parse_args()
    if args.which == "e6e7":
        plan = plan_e6e7(load_topics(), {v: load_llm(v) for v in E6_VARIANTS})
    elif args.which == "e7r":
        plan = plan_e7r()
    elif args.which == "e10":
        plan = plan_e10(with_noroutes=args.with_noroutes)
    elif args.which == "routes_r":
        plan = plan_routes_r()
    else:
        plan = plan_e11(load_topics(), e11_systems(args.fusion_field, args.rrf_value, args.query_field, args.bt_value),
                        NO_ROUTES if args.retrieval_no_routes else None)
    write(plan, args.out)


if __name__ == "__main__":
    main()
