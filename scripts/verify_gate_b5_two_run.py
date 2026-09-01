#!/usr/bin/env python
"""Run frozen Gate B5 validation twice and prove durable outputs do not mutate.

The authoritative B5 freeze predates governed B6/C successors, so rerunning
``--phase all`` against the repository would correctly trip the immutable
manifest guard when preregistered source hashes have evolved.  This audit
therefore executes the independent validation phase twice and compares both
artifact roots before, between, and after the runs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skru1.artifact_io import write_json_atomic
from skru1.data_contracts import ContractViolation, discover_project_root, sha256_file


def snapshot(root: Path) -> dict[str, str]:
    roots = (
        root / "artifacts" / "splits" / "t1_train_benchmark_v1",
        root / "artifacts" / "model_selection" / "t1_b5_evidence_v1",
    )
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for artifact_root in roots
        for path in sorted(artifact_root.rglob("*"))
        if path.is_file()
    }


def run_once(root: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(root / ".venv" / "Scripts" / "python.exe"),
            str(root / "scripts" / "run_gate_b5.py"),
            "--phase",
            "validate",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=7200,
        check=False,
    )
    if completed.returncode != 0:
        raise ContractViolation(f"Gate B5 two-run execution failed: {completed.stderr[-2000:]}")
    return {
        "returncode": completed.returncode,
        "phase": "validate",
        "stdout_tail": completed.stdout[-1000:],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="work/gate_b5_two_run/determinism_report.json",
        help="Repository-relative path for the current non-mutation audit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = discover_project_root()
    baseline = snapshot(root)
    executions = []
    executions.append(run_once(root))
    first = snapshot(root)
    executions.append(run_once(root))
    second = snapshot(root)
    all_paths = set(baseline) | set(first) | set(second)
    changed = sorted(
        path
        for path in all_paths
        if not (baseline.get(path) == first.get(path) == second.get(path))
    )
    report = {
        "schema_version": 1,
        "gate": "B5_EVIDENCE_AND_BENCHMARK_PROTOCOL",
        "mode": "frozen_validation_non_mutation",
        "status": "PASS" if not changed and bool(baseline) else "FAIL",
        "runs": 2,
        "files_compared": len(all_paths),
        "baseline_equals_first": baseline == first,
        "first_equals_second": first == second,
        "byte_identical_sha256_maps": baseline == first == second,
        "changed_or_missing_files": changed,
        "run_records": executions,
        "authoritative_historical_report": (
            "artifacts/model_selection/t1_b6_expanded_v1/"
            "b5_two_run_determinism_report.json"
        ),
        "historical_validation_loaded": False,
        "current_test_loaded": False,
    }
    output = root / Path(args.output)
    write_json_atomic(root, output, report, work_scope="gate_b5_two_run")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "PASS":
        raise ContractViolation(f"Gate B5 durable outputs changed: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
