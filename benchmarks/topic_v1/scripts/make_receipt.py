"""Public receipt of TOPIC_BENCHMARK_V1 (IDs, hashes and numbers only) from the benchmark outputs.

usage: python benchmarks/topic_v1/scripts/make_receipt.py [--commits c80eae1,a7dc73f] [--first-run 2026-...Z]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
ROOT = BENCH.parents[1]
OUT = ROOT / "docs" / "corpus_platform" / "receipts" / "topic_benchmark_v1.json"
MAIN = ("page_recall@10", "page_recall@20", "page_recall@50", "mrr@50", "source_recall@10", "section_hit@10",
        "section_pages@10", "success@10")


def sha_lf(p: Path) -> str:
    return hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commits", default="c80eae1,a7dc73f,3582350")
    ap.add_argument("--first-run", default="2026-09-28T18:04:50Z")
    args = ap.parse_args()
    res = json.loads((BENCH / "results_v1.json").read_text(encoding="utf-8"))
    fa = json.loads((BENCH / "failure_analysis_v1.json").read_text(encoding="utf-8"))
    pm = json.loads((BENCH / "page_mapping_v1.json").read_text(encoding="utf-8"))
    spec = json.loads((BENCH / "metrics_spec_v1.json").read_text(encoding="utf-8"))
    st = pm["status_totals"]
    receipt = {
        "receipt": "vkm.topic_benchmark/1",
        "benchmark": "TOPIC_BENCHMARK_V1",
        "agent": "Q",
        "date": "2026-09-28",
        "branch": "claude/agent-q-topic-benchmark-2026-09-28",
        "status": "RUN: 4 pre-registered systems + 3 post-hoc diagnostics; dossier adapter NOT_RUN (reconstruct_topic "
                  "not deployed yet)",
        "preregistration": {"commits_before_first_run": args.commits.split(","), "first_run_started": args.first_run,
                            "set_sha256": spec["set"]["sha256"], "queries_sha256": spec["set"]["queries_sha256"],
                            "page_mapping_sha256": spec["set"]["page_mapping_sha256"],
                            "spec_sha256": sha_lf(BENCH / "metrics_spec_v1.json")},
        "snapshots": {"canonical": res["canonical_snapshot_id"],
                      "nav": next((m.get("nav_snapshot_id") for m in res["runs"]["meta"] if m.get("nav_snapshot_id")),
                                  None)},
        "set": {"topics": spec["set"]["topics"], "queries": spec["set"]["queries"], "targets": spec["set"]["targets"],
                "sources": pm["set"]["sources"], "tracks": pm["set"]["topics_by_track"],
                "targets_by_mapping": pm["set"]["targets_by_mapping"]},
        "page_mapping": {"rule": pm["rule"], "records_checked": pm["records_checked"],
                         "same_page": st.get("MATCH0"), "anchored_adjacent": st.get("ANCHORED"),
                         "weak": st.get("WEAK"), "not_text_verifiable": st.get("NO_SCORE"),
                         "identity_share_of_verifiable": pm["identity_share_of_scoreable"]},
        "run": {"where": "CORE, inside the API container (docker exec), read-only API calls, token never printed",
                "concurrency": 1, "raw_runs": res["runs"]["files"],
                "raw_runs_location": "host-local work directory of WORKSTATION ($VKM_WORK/topic_v1), IDs only",
                "calls_and_errors": [{k: m.get(k) for k in ("calls", "errors", "retries")} for m in res["runs"]["meta"]
                                     if m.get("kind") == "end"],
                "errors_note": "5 errors = /v1/nav/sections answers 404 NOT_FOUND when nothing matches",
                "latency_ms": res["latency"]},
        "metrics_mean_over_351_queries": {s: {m: res["summary"][s][m] for m in MAIN} for s in res["systems"]},
        "acceptance": {s: {"pass": v["pass"], **{m: x["pass"] for m, x in v["levels"].items()}}
                       for s, v in res["acceptance"].items()},
        "comparisons_primary": res["comparisons_primary"],
        "post_hoc": {"note": res["post_hoc"]["note"], "comparisons": res["post_hoc"]["comparisons"],
                     "top10_share_outside_sweep_sources": res["post_hoc"]["top10_share_outside_sweep_sources"],
                     "exploratory_gains": fa["exploratory_gains"]},
        "failure_analysis": {"definition": "target missed = not within the first 50 pages for any of the 3 queries",
                             "deviation": fa["deviation"], "registry_methods": fa["registry_methods"],
                             **{s: {k: fa["systems"][s][k] for k in ("n_missed_all_variants", "missed_share",
                                                                     "by_cause", "by_cause_preregistered_rule",
                                                                     "queries_with_zero_page_recall@50",
                                                                     "topics_with_no_target_found", "worst_queries")}
                                for s in ("hybrid_late", "hybrid_late_pool", "nav") if s in fa["systems"]},
                             "diagnostics": fa["diagnostics"]},
        "files": {p: sha_lf(BENCH / p) for p in ("topic_set_v1.jsonl", "metrics_spec_v1.json", "results_v1.json",
                                                 "failure_analysis_v1.json", "RESULTS_V1.md")},
    }
    OUT.write_bytes((json.dumps(receipt, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    print(OUT.relative_to(ROOT), len(json.dumps(receipt)))


if __name__ == "__main__":
    main()
