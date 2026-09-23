#!/usr/bin/env python3
"""Data-only freeze recheck in a new work directory; never changes the release."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / "artifacts/data_quality/scenario_simulation_v2"
DATA = ROOT / "data/scenario_simulation_v2"
CONSTRAINTS = ROOT / "artifacts/reconstruction/scenario_constraints_v2"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def verify_manifest(path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for section in ("inputs", "outputs"):
        for spec in manifest[section]:
            p = (ROOT if section == "inputs" else path.parent) / spec["path"]
            assert p.stat().st_size == spec["size_bytes"] and sha(p) == spec["sha256"], spec["path"]


def compare(release, runs):
    entries = sorted(p.relative_to(release) for p in release.rglob("*") if p.is_file())
    for run in runs:
        assert entries == sorted(p.relative_to(run) for p in run.rglob("*") if p.is_file())
        for entry in entries:
            assert sha(release / entry) == sha(run / entry), str(entry)
    return dict(files=len(entries), all_runs_byte_identical_to_release=True,
                runs=[p.relative_to(ROOT).as_posix() for p in runs],
                manifest_sha256=sha(release / "manifest.json"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True, help="New directory below work/data_foundation_v2")
    ap.add_argument("--baseline", default="artifacts/data_quality/scenario_simulation_v2/reproducibility/baseline_inventory.json")
    args = ap.parse_args()
    out = (ROOT / args.work).resolve()
    assert out.is_relative_to(ROOT / "work/data_foundation_v2")
    if out.exists():
        raise FileExistsError("Choose a new scratch directory; existing receipts are never overwritten")
    out.mkdir(parents=True)
    for path in (DATA / "manifest.json", CONSTRAINTS / "manifest.json", QA / "manifest.json"):
        verify_manifest(path)
    final = json.loads((QA / "finalization_manifest.json").read_text(encoding="utf-8"))
    for spec in final["files"] + final["inputs"]:
        p = ROOT / spec["path"]
        assert sha(p) == spec["sha256"] and p.stat().st_size == spec["size_bytes"], spec["path"]
    runs = []

    def run(label, argv):
        print(label, flush=True)
        started = time.monotonic()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        with (out / (label + ".txt")).open("w", encoding="utf-8", newline="\n") as log:
            result = subprocess.run([sys.executable, *argv], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        runs.append(dict(label=label, command=[".venv/Scripts/python.exe", *argv],
                         exit_code=result.returncode, elapsed_seconds=round(time.monotonic()-started, 3),
                         log=label + ".txt"))
        write(out / "commands.json", runs)
        assert result.returncode == 0, label

    relative = out.relative_to(ROOT).as_posix()
    for kind, script in (("constraints", "build_scenario_constraints_v2.py"), ("dataset", "generate_scenario_v2.py")):
        for repeat in ("a", "b"):
            run(kind + "_" + repeat, ["scripts/" + script, "--output", relative + "/" + kind + "_" + repeat])
    comparisons = {
        "constraints": compare(CONSTRAINTS, [out / "constraints_a", out / "constraints_b"]),
        "dataset": compare(DATA, [out / "dataset_a", out / "dataset_b"]),
    }
    run("independent_validation", ["scripts/validate_scenario_v2.py", "--dataset", "data/scenario_simulation_v2",
        "--baseline", args.baseline, "--output", relative + "/independent_validation.json"])
    run("data_tests", ["-m", "pytest", "tests/test_data_foundation_v2.py", "tests/test_reconstruction_data.py",
        "tests/test_reconstruction_atlas.py", "-q", "--basetemp=" + relative + "/pytest_data"])
    run("v1_regression", ["-m", "pytest", "tests/test_scenario_simulation_v1.py", "-k",
        "catalog_is_full or static_temporal or generated_release or adapter_exposes", "-q", "--basetemp=" + relative + "/pytest_v1"])
    assert "36 passed" in (out / "data_tests.txt").read_text(encoding="utf-8")
    assert "4 passed, 3 deselected" in (out / "v1_regression.txt").read_text(encoding="utf-8")
    # These are newly generated, openly QA-accessible synthetic values, not legacy labels.
    cat = pd.read_csv(DATA / "scenario_catalog.csv", keep_default_na=False)
    obs = pd.read_csv(DATA / "evaluator/campaign_truth.csv.gz",
        usecols=["scenario_id", "base_point_id", "date", "latent_settlement_mm"])
    signatures = {}
    worlds = cat.drop_duplicates("latent_world_id").set_index("scenario_id").latent_world_id
    for sid, frame in obs.loc[obs.scenario_id.isin(worlds.index)].groupby("scenario_id", sort=True):
        matrix = frame.sort_values(["date", "base_point_id"]).latent_settlement_mm.to_numpy(dtype="<f8")
        signatures[worlds.loc[sid]] = hashlib.sha256(matrix.tobytes()).hexdigest()
    roles = pd.read_csv(DATA / "split_assignments.csv.gz", usecols=["model_role"]).model_role.value_counts().to_dict()
    features = pd.read_csv(DATA / "model_features.csv.gz", usecols=["sample_id"])
    windows = pd.read_csv(DATA / "sequence_windows.csv.gz", usecols=["sample_id"])
    actual = dict(scenarios=len(cat), latent_world_ids=len(signatures), distinct_full_latent_fields=len(set(signatures.values())),
        points=len(pd.read_csv(DATA / "point_roster.csv")), profiles=pd.read_csv(DATA / "point_roster.csv").profile_id.nunique(),
        campaigns=len(pd.read_csv(DATA / "campaign_catalog.csv")), origins=len(features), model_windows=len(windows),
        numerically_bounded_fraction=float(cat.numeric_scale_constrained.mean()),
        empirically_identified_temporal_law_fraction=float(cat.temporal_law_empirically_identified.mean()), roles=roles)
    expected = dict(scenarios=640, latent_world_ids=128, distinct_full_latent_fields=127, points=42, profiles=14,
        campaigns=29, origins=321766, model_windows=321766, numerically_bounded_fraction=0.984375,
        empirically_identified_temporal_law_fraction=0.0,
        roles={"train":122047,"calibration":35148,"evaluation":133833,"excluded":30738})
    assert actual == expected, actual
    assert features.sample_id.equals(windows.sample_id)
    write(out / "latent_field_signatures.json", signatures)
    independent = json.loads((out / "independent_validation.json").read_text(encoding="utf-8"))
    assert independent["status"] == "PASS"
    write(out / "recheck_report.json", dict(status="PASS", actual=actual, reproducibility=comparisons,
        tests_passed=40, baseline_files_verified=independent["preexisting_files_verified"],
        models_executed=0, legacy_holdout_labels_parsed=0, semantics_unchanged=True,
        source_script_sha256=sha(Path(__file__)), rerun_commands=runs,
        prior_finalization_manifest_sha256=sha(QA / "finalization_manifest.json"),
        verified_from_existing_receipt=["source PDF reader review", "10 PNG visual review", "old/new chart calculations"],
        rerun=["two constraints builds", "two dataset builds", "independent serialized validation", "36 data/reconstruction tests",
               "4 v1 data regression tests", "exact 127 full-field signatures", "all manifest hashes", "old-file integrity"]))
    print(json.dumps(dict(status="PASS", tests=40, comparisons=comparisons, actual=actual), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
