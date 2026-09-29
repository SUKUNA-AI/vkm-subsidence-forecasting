"""Nightly TOPIC_BENCHMARK_V1 scoring (agent OPS, ``infra/core/nightly/topic_score.py``): the raw hybrid_late answers
of the frozen harness → the pre-registered metrics (``vkm_corpus.retrieval_lab.topic_bench``) as a regression signal.
Synthetic answers built from the frozen set's own targets; no API."""
from __future__ import annotations

import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "benchmarks" / "topic_v1"
SPEC = importlib.util.spec_from_file_location("topic_score", ROOT / "infra" / "core" / "nightly" / "topic_score.py")
TS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TS)


def runs_file(tmp_path: Path, hit_first_target: bool = True, errors: int = 0) -> Path:
    lines = [{"kind": "meta", "harness_version": "topic_v1-harness-1", "canonical_snapshot_id": "snap-x"}]
    n = 0
    for raw in BENCH.joinpath("topic_set_v1.jsonl").read_text(encoding="utf-8").splitlines():
        t = json.loads(raw)
        target = t["targets"][0]["page_id"]
        for q in t["queries"]:
            hits = [{"page_id": target, "source_id": target.split(":")[0], "duplicates": []}] if hit_first_target \
                else []
            err = "HTTP503:DEPENDENCY_UNAVAILABLE" if n < errors else None
            lines.append({"kind": "run", "query_id": q["query_id"], "topic_id": t["topic_id"],
                          "system": "hybrid_late", "hits": hits, "error": err, "api_ms": 150.0 + n % 7})
            n += 1
    lines.append({"kind": "end", "nav_snapshot_id": "snap-x", "calls": n, "errors": errors})
    path = tmp_path / "runs.jsonl"
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    return path


def score(tmp_path, **kw) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert TS.main(["--runs", str(runs_file(tmp_path, **kw)), "--set", str(BENCH / "topic_set_v1.jsonl"),
                        "--spec", str(BENCH / "metrics_spec_v1.json"),
                        "--registered", str(BENCH / "results_v1.json")]) == 0
    return json.loads(buf.getvalue())


def test_scores_all_351_queries_with_the_registered_metrics(tmp_path):
    out = score(tmp_path)
    assert out["n_queries"] == out["n_expected"] == 351 and out["errors"] == 0
    s = out["summary"]
    assert s["success@10"] == 1.0 and s["mrr@50"] == 1.0 and 0 < s["page_recall@50"] < 1
    assert set(out["by_track"]) == {"MODEL_FAMILY", "PROCESS"}
    assert out["acceptance"]["levels"]["success@10"]["pass"] is True
    assert out["registered"]["page_recall@50"] == pytest.approx(0.3031)
    assert out["latency"]["p50_ms"] >= 150 and out["canonical_snapshot_id"] == "snap-x"


def test_errors_and_empty_answers_count(tmp_path):
    out = score(tmp_path, hit_first_target=False, errors=5)
    assert out["errors"] == 5 and out["summary"]["page_recall@50"] == 0.0 and out["summary"]["success@10"] == 0.0


def test_a_changed_set_is_refused(tmp_path):
    changed = tmp_path / "topic_set_v1.jsonl"
    changed.write_text(BENCH.joinpath("topic_set_v1.jsonl").read_text(encoding="utf-8") + "\n{}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="pre-registered"):
        TS.main(["--runs", str(runs_file(tmp_path)), "--set", str(changed), "--spec",
                 str(BENCH / "metrics_spec_v1.json")])
