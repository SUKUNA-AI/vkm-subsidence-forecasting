"""Plan of experiment E6b (PREREGISTRATION_E6B_2026-10-13.md): two Russian model paraphrases per query.

Four systems on the 117 NAME and the 117 PARA1 queries of topic_v1, every request with ``graph: []`` and both registry
routes off (``author_route: false, entity_route: false``; image 1b20e03 turns them on by default):

    base     the query alone
    human3   + the topic's two other human formulations, in set order (as E6 / 05.10)
    llm3     + f1, f2 of the E6 model file of the query's variant (Russian + English pair; llm_name / llm_para1)
    llm3ru   + f1, f2 of the E6b model file of the query's variant (two Russian; llm_ru_name / llm_ru_para1)

All four are re-run on the current image (the E6 answers of image 4cefa90 are not reused). Lines are interleaved by
query (every system of a query, then the next query). Every request: kinds PAGE, limit 50, candidates 100 (the common
fields of ``plans_20261013.py``), the server defaults for everything not named. Plans hold query texts and stay in
git-ignored ``work/``; run with ``run_plan.sh`` (``harness_plan.py`` in the API container, read-only, concurrency 1).

usage: python benchmarks/topic_v1/scripts/plans_e6b_20261013.py --out <plan.jsonl>
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


P = _load("plans_20261013", HERE / "plans_20261013.py")

LLM_RU_FILES = {"NAME": ("llm_ru_name.jsonl", "52a0c3410e4c4aa4d62cd29c65e208a0ef8161a852c5a28c671fcbf70ccf2036"),
                "PARA1": ("llm_ru_para1.jsonl", "9bdef5bcfde229e47eb82f26d7f042872124c39144849aa69ecea6384f605c00")}
E6B_VARIANTS = ("NAME", "PARA1")
E6B_SYSTEMS = ("base", "human3", "llm3", "llm3ru")
COMMON = {"graph": [], **P.NO_ROUTES}


def load_llm_ru(variant: str, directory: Path = P.LLM_DIR) -> dict[str, list[str]]:
    """query_id → [f1, f2] of the E6b (two Russian) model file of ``variant``; sha256 checked (LF-normalised)."""
    name, sha = LLM_RU_FILES[variant]
    path = directory / name
    if P.sha256_lf(path) != sha:
        raise SystemExit(f"{name}: sha256 differs from the pre-registered {sha}")
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            out[d["query_id"]] = [d["f1"], d["f2"]]
    return out


def plan_e6b(topics: list[dict], llm: dict[str, dict[str, list[str]]], llm_ru: dict[str, dict[str, list[str]]]
             ) -> list[dict]:
    out = []
    for t in topics:
        for q in t["queries"]:
            if q["variant"] not in E6B_VARIANTS:
                continue
            others = [x["text"] for x in t["queries"] if x["query_id"] != q["query_id"]]
            forms = {"base": None, "human3": others, "llm3": list(llm[q["variant"]][q["query_id"]]),
                     "llm3ru": list(llm_ru[q["variant"]][q["query_id"]])}
            for system in E6B_SYSTEMS:
                extra = dict(COMMON)
                if forms[system] is not None:
                    extra["formulations"] = forms[system]
                out.append(P.line(q["query_id"], t["topic_id"], system, P.hybrid(q["text"], **extra)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    plan = plan_e6b(P.load_topics(), {v: P.load_llm(v) for v in E6B_VARIANTS},
                    {v: load_llm_ru(v) for v in E6B_VARIANTS})
    P.write(plan, args.out)


if __name__ == "__main__":
    main()
