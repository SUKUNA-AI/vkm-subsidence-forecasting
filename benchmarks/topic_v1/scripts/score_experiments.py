"""Score the TOPIC_BENCHMARK_V1 experiments (post hoc, not pre-registered): runs of ``harness_experiments.py``.

``score.py`` (frozen) scores only the pre-registered system names (``topic_bench.SYSTEMS``); its ``load_runs`` and
``ranking_of`` read the experiment lines unchanged (``ranking_of`` sends an unknown system to ``ranking_from_hits``,
the ``hybrid_late`` path). This adapter reuses them for every system name in the runs, compares each variant with a
reference system (paired over topics, sign-flip test and bootstrap CI, Holm over the family — ``topic_bench``) and
reports the dilution (most pages of one source in the first 50).

usage: PYTHONPATH=src python benchmarks/topic_v1/scripts/score_experiments.py --runs <experiments.jsonl>
           [--runs <runs with outlines, e.g. the frozen collector's>] [--reference baseline] [--out <result.json>]
Output holds IDs and numbers only.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from vkm_corpus.retrieval_lab import topic_bench as TB

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score import load_runs, ranking_of, sha256_lf  # noqa: E402

BENCH = Path(__file__).resolve().parents[1]
METRICS = ("page_recall@10", "page_recall@20", "page_recall@50", "mrr@50", "source_recall@10", "section_hit@10",
           "success@10")
TESTED = ("page_recall@20", "page_recall@50", "mrr@50")
POST_HOC_REFERENCE = "three_formulations_rrf_page_recall@50"     # failure_analysis_v1.json: RRF of the 3 wordings


def systems_of(runs: dict) -> list[str]:
    seen: dict[str, None] = {}
    for (_qid, system) in runs:
        seen.setdefault(system, None)
    return list(seen)


def score_rows(topics: list[TB.Topic], runs: dict, index: TB.SectionIndex, systems: list[str]) -> list[dict]:
    rows = []
    for t in topics:
        for q in t.queries:
            for system in systems:
                line = runs.get((q.query_id, system))
                if line is None:
                    continue
                r = ranking_of(line, index)
                rows.append({"query_id": q.query_id, "topic_id": t.topic_id, "system": system, "track": t.track,
                             "group": t.group, "variant": q.variant, "error": line.get("error"),
                             **TB.query_metrics(t, r)})
    return rows


def dilution(runs: dict, systems: list[str], k: int = 50) -> dict[str, float | None]:
    """Mean over queries of the most pages one source holds in the first ``k`` hits."""
    out = {}
    for s in systems:
        vals = [max(Counter(h["page_id"].split(":")[0] for h in (line.get("hits") or [])[:k]).values(), default=0)
                for (_q, sys_), line in runs.items() if sys_ == s]
        out[s] = round(sum(vals) / len(vals), 3) if vals else None
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", action="append", required=True)
    ap.add_argument("--reference", default="baseline", help="system the variants are compared with")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    spec = json.loads((BENCH / "metrics_spec_v1.json").read_text(encoding="utf-8"))
    set_path = BENCH / "topic_set_v1.jsonl"
    if sha256_lf(set_path) != spec["set"]["sha256"]:
        raise SystemExit("topic_set_v1.jsonl does not match the pre-registered sha256")
    topics = TB.load_set(set_path)
    runs, outlines, meta, files = load_runs(args.runs)
    index = TB.SectionIndex.from_outlines(outlines)
    systems = systems_of(runs)
    rows = score_rows(topics, runs, index, systems)
    summary = TB.aggregate(rows, ["system"])
    pairs = [(s, args.reference) for s in systems if s != args.reference]
    comparisons = TB.compare_systems(rows, pairs, TESTED) if args.reference in systems else {}
    errors = {s: sum(1 for k in runs if k[1] == s and runs[k].get("error")) for s in systems}
    statuses = {s: dict(Counter(str((runs[k].get("formulations") or {}).get("status")) for k in runs if k[1] == s))
                for s in systems}
    reference = None
    fa = BENCH / "failure_analysis_v1.json"
    if fa.exists():
        reference = (json.loads(fa.read_text(encoding="utf-8")).get("exploratory_gains") or {}).get(POST_HOC_REFERENCE)
    out = {"benchmark": "TOPIC_BENCHMARK_V1", "kind": "post-hoc experiments (not pre-registered)",
           "set_sha256": spec["set"]["sha256"], "runs": {"files": files, "outline_sources": len(outlines),
                                                         "meta": [{k: m.get(k) for k in (
                                                             "kind", "harness_version", "systems", "n_queries",
                                                             "canonical_snapshot_id", "nav_snapshot_id", "calls",
                                                             "errors", "retries") if m.get(k) is not None}
                                                                  for m in meta]},
           "systems": systems, "reference": args.reference,
           "summary": {s: {m: summary.get(s, {}).get(m) for m in ("n_queries", "n_topics", *METRICS)}
                       for s in systems},
           "comparisons": comparisons, "errors": errors, "formulation_status": statuses,
           "max_pages_of_one_source_in_top50_mean": dilution(runs, systems),
           "post_hoc_three_formulations_page_recall@50": reference}
    if args.out:
        Path(args.out).write_bytes((json.dumps(out, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    dil = out["max_pages_of_one_source_in_top50_mean"]

    def f(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.3f}"

    for s in systems:
        m = summary.get(s) or {}
        print(f"{s:20s} n={m.get('n_queries')} R@10 {f(m.get('page_recall@10'))} R@20 {f(m.get('page_recall@20'))} "
              f"R@50 {f(m.get('page_recall@50'))} MRR {f(m.get('mrr@50'))} SrcR@10 {f(m.get('source_recall@10'))} "
              f"maxsrc@50 {dil[s]} errors {errors[s]}")
    if reference is not None:
        print(f"post-hoc RRF of the three human formulations (failure_analysis_v1): R@50 {reference:.4f}")
    for k, v in comparisons.items():
        print(k, v)


if __name__ == "__main__":
    main()
