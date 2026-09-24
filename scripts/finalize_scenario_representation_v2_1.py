#!/usr/bin/env python3
"""Two-stage representation acceptance: core proof, then construction-only receipts."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
from skru1.scenario_boundary_v2_1 import DATA_SHA, CONSTRAINTS_SHA, file_sha256
from skru1.scenario_adapter_v2_1 import load_model_data
from skru1.scenario_splits_v2_1 import group_manifest, load_frozen_folds

WORK = ROOT / "work/scenario_representation_v2_1"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def tree_hashes(directory):
    return {p.relative_to(directory).as_posix():file_sha256(p) for p in sorted(directory.rglob("*")) if p.is_file()}


def test_receipt(name):
    xml = ET.parse(WORK/name)
    cases = list(xml.iter("testcase"))
    assert cases and not list(xml.iter("failure")) and not list(xml.iter("error"))
    return dict(passed=len(cases), failed=0, tests=[r.attrib["name"] for r in cases])


def core():
    correction = read(ROOT/"artifacts/data_quality/scenario_simulation_v2_1/correction_receipt.json")
    assert correction["status"] == "PASS" and correction["new_manifest_sha256"] == DATA_SHA
    independent = read(WORK/"independent01.json")
    assert independent["status"] == "PASS" and independent["origins_accepted"] == 321766
    assert independent["elapsed_day_mismatches"] == 0
    first, second = tree_hashes(WORK/"build01"), tree_hashes(WORK/"build02")
    assert first == second and "representation_manifest.json" in first
    manifest = read(WORK/"build01/representation_manifest.json")
    for row in manifest["sources"]:
        assert file_sha256(ROOT/row["path"]) == row["sha256"], row["path"]
    for row in manifest["outputs"]:
        assert first[row["path"]] == row["sha256"]
    bundle = load_model_data(ROOT)
    groups = group_manifest(ROOT, bundle)
    config = read(ROOT/"configs/scenario_adapter_v2_1.json")
    folds = load_frozen_folds(WORK/"build01", bundle, groups, config)
    assert len(folds) == 21
    # Validate every persisted preprocessing identity against its exact fold record.
    states = []
    for fold in folds:
        for kind in ("tabular", "sequence"):
            path = WORK/"build01/preprocessing"/f"{fold.fold_id}_{kind}.json"
            state = read(path)
            assert state["ordered_train_sample_sha256"] == fold.record()["fit_sample_ids_sha256"]
            assert state["fit_origins"] == len(fold.fit_ids)
            assert state["creation_identity"]["dataset_manifest_sha256"] == DATA_SHA
            assert state["parameters"] and "category_mappings" in state
            states.append(path.name)
    runtime = read(WORK/"build01/runtime_sequence_receipt.json")
    assert runtime["status"] == "PASS" and runtime["origins_checked"] == 321766
    assert runtime["final_token_mismatches"] == runtime["sequence_window_mismatches"] == 0
    new = test_receipt("pytest02.xml")
    regression = test_receipt("regression01.xml")
    assert new["passed"] == 28 and regression["passed"] == 49
    initial = read(ROOT/"work/scenario_v2_1_erratum/initial_inventory.json")
    changed = [r["path"] for r in initial if file_sha256(ROOT/r["path"]) != r["sha256"]]
    assert not changed, changed
    result = dict(status="PASS_REPRESENTATION_CORE", dataset_id="SKRU1_SCENARIO_SIMULATION_V2_1",
                  dataset_manifest_sha256=DATA_SHA, constraints_manifest_sha256=CONSTRAINTS_SHA,
                  origins_accepted=321766, elapsed_day_mismatches=0, runtime_sequence=runtime,
                  forward_world_folds=3, spatial_profile_folds=14, spatial_zone_folds=4,
                  embargo_days=30, grouping_valid=True, spatial_indirect_leakage_checks=True,
                  train_only_preprocessing=True, fitted_preprocessing_states=len(states),
                  worker_evaluator_boundary="Python audit hook; not an OS sandbox; child launch denied",
                  two_run=dict(status="PASS", deterministic_files=len(first), hashes=first),
                  new_representation_tests=new, existing_nonmodel_regression=regression,
                  independent_origin_checks=independent, initial_files_unchanged=len(initial),
                  old_frozen_v2_unchanged=True, models_fitted=0, models_executed=0,
                  model_scoring=0, legacy_holdout_labels_parsed=0)
    write(WORK/"core_receipt.json", result)
    print(json.dumps({"status":result["status"],"representation_sha256":first["representation_manifest.json"],"files":len(first)}))


def freeze():
    receipt = read(WORK/"core_receipt.json")
    assert receipt["status"] == "PASS_REPRESENTATION_CORE"
    baseline, c01 = read(WORK/"baseline_smoke.json"), read(WORK/"c01_smoke.json")
    assert baseline["status"] == "PASS" and c01["status"] == "PASS_ARCHITECTURE_CONSTRUCTION_ONLY"
    assert baseline["models_fitted"] == c01["models_fitted"] == c01["model_forward_calls"] == 0
    interface_tests = test_receipt("interface_test.xml")
    assert interface_tests["passed"] == 1
    destination = ROOT/"artifacts/splits/scenario_representation_v2_1"
    qa = ROOT/"artifacts/data_quality/scenario_representation_v2_1"
    if destination.exists() or qa.exists():
        raise FileExistsError("Final representation destinations must be absent")
    assert tree_hashes(WORK/"build01") == receipt["two_run"]["hashes"] == tree_hashes(WORK/"build02")
    shutil.copytree(WORK/"build01", destination)
    assert tree_hashes(destination) == receipt["two_run"]["hashes"]
    qa.mkdir(parents=True)
    write(qa/"independent_origin_validation.json", receipt["independent_origin_checks"])
    write(qa/"two_run_receipt.json", receipt["two_run"])
    write(qa/"test_execution_receipt.json", dict(new_representation_tests=receipt["new_representation_tests"],
          construction_test=interface_tests, existing_regression=receipt["existing_nonmodel_regression"],
          skipped_scope="Historical model fit/scoring workflows, old canonical-label fixtures, and historical BLOCKED v2 suite were not executed."))
    write(qa/"baseline_construction_receipt.json", baseline)
    write(qa/"c01_construction_receipt.json", c01)
    receipt.update(status="PASS_REPRESENTATION_FROZEN", baseline_construction=baseline,
                   C01_architecture_construction=c01, new_representation_tests_passed=29,
                   representation_manifest_sha256=file_sha256(destination/"representation_manifest.json"),
                   representation_directory=destination.relative_to(ROOT).as_posix(),
                   final_copy_byte_identical=True, benchmark_executed=False, commit_created=False, push_performed=False)
    # Receipt and inventory deliberately do not hash each other recursively.
    paths = sorted([p for p in destination.rglob("*") if p.is_file()] + [p for p in qa.iterdir() if p.is_file()])
    pd.DataFrame([dict(path=p.relative_to(ROOT).as_posix(),sha256=file_sha256(p),size_bytes=p.stat().st_size) for p in paths]).to_csv(qa/"artifact_inventory.csv",index=False,lineterminator="\n")
    receipt["artifact_inventory_sha256"] = file_sha256(qa/"artifact_inventory.csv")
    receipt["inventory_scope"] = "Frozen representation tree and supporting QA files; receipt and inventory excluded to avoid cyclic hashes"
    write(qa/"acceptance_receipt.json", receipt)
    print(json.dumps({"status":receipt["status"],"representation_manifest_sha256":receipt["representation_manifest_sha256"],
                      "acceptance_receipt_sha256":file_sha256(qa/"acceptance_receipt.json")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["core", "freeze"], required=True)
    args = parser.parse_args()
    (core if args.stage == "core" else freeze)()
