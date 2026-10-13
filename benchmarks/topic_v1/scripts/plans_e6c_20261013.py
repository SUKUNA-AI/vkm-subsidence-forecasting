"""Plan of experiment E6c (PREREGISTRATION_E6C_2026-10-13.md): E6b on the PARA1 set with three independent model
generations of two Russian paraphrases.

Four systems on the 117 PARA1 queries (``TQ-…-1``) of topic_v1, every request with ``graph: []`` and both registry
routes off (``author_route: false, entity_route: false``), as in E6b:

    base        the query alone
    llm3ru_g1   + f1, f2 of generation 1 (``llm_ru_para1.jsonl``, the E6b input; re-run on the current image)
    llm3ru_g2   + f1, f2 of generation 2 (``llm_ru_para1_gen2.jsonl``)
    llm3ru_g3   + f1, f2 of generation 3 (``llm_ru_para1_gen3.jsonl``)

The requests of ``base`` and ``llm3ru_g1`` are the E6b requests ``base`` and ``llm3ru`` of the same PARA1 queries, field
for field. Lines are interleaved by query (every system of a query, then the next query). Every request: kinds PAGE,
limit 50, candidates 100 (the common fields of ``plans_20261013.py``), the server defaults for everything not named.
Plans hold query texts and stay in git-ignored ``work/``; run with ``run_plan.sh`` (``harness_plan.py`` in the API
container, read-only, concurrency 1).

usage: python benchmarks/topic_v1/scripts/plans_e6c_20261013.py --out <plan.jsonl>
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

GEN_FILES = {"llm3ru_g1": ("llm_ru_para1.jsonl", "9bdef5bcfde229e47eb82f26d7f042872124c39144849aa69ecea6384f605c00"),
             "llm3ru_g2": ("llm_ru_para1_gen2.jsonl",
                           "b9f2912d0576288715661863772b756645207217a451fbbfa2ffc38dcf3e2f63"),
             "llm3ru_g3": ("llm_ru_para1_gen3.jsonl",
                           "2729f3b6b329cc4b35f0b0ffc88f9f668ccdc1bc0cb0d748ad3eaf3daff4ec54")}
E6C_VARIANT = "PARA1"
GENERATIONS = tuple(GEN_FILES)
E6C_SYSTEMS = ("base", *GENERATIONS)
COMMON = {"graph": [], **P.NO_ROUTES}


def load_generation(system: str, directory: Path = P.LLM_DIR) -> dict[str, list[str]]:
    """query_id → [f1, f2] of one generation; sha256 checked (LF-normalised; the files are LF)."""
    name, sha = GEN_FILES[system]
    path = directory / name
    if P.sha256_lf(path) != sha:
        raise SystemExit(f"{name}: sha256 differs from the pre-registered {sha}")
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            out[d["query_id"]] = [d["f1"], d["f2"]]
    return out


def plan_e6c(topics: list[dict], gens: dict[str, dict[str, list[str]]]) -> list[dict]:
    out = []
    for t in topics:
        for q in t["queries"]:
            if q["variant"] != E6C_VARIANT:
                continue
            for system in E6C_SYSTEMS:
                extra = dict(COMMON)
                if system != "base":
                    extra["formulations"] = list(gens[system][q["query_id"]])
                out.append(P.line(q["query_id"], t["topic_id"], system, P.hybrid(q["text"], **extra)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    P.write(plan_e6c(P.load_topics(), {g: load_generation(g) for g in GENERATIONS}), args.out)


if __name__ == "__main__":
    main()
