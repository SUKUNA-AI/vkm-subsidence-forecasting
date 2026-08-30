from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from skru1.adaptive_kalman import prepare_kalman_history
from skru1.evaluation import causal_feature_history
from skru1.gate_b2 import (
    capture_protected_test_snapshot,
    find_test_loader_calls,
    load_gate_b2_config,
    tune_adaptive_parameters,
)
from skru1.splits import load_split_dataset
from skru1.uncertainty import (
    apply_scaled_conformal_intervals,
    calibrate_scaled_conformal,
    finite_sample_conformal_quantile,
    interval_metrics,
)


ROOT = Path(__file__).resolve().parents[1]


def test_gate_b2_has_no_current_test_loading_path() -> None:
    _, config = load_gate_b2_config(ROOT)
    assert config["test_policy"]["model_selection_access"] == "prohibited"
    assert config["test_policy"]["final_acceptance_access"] == "prohibited"
    paths = [
        ROOT / "src" / "skru1" / "adaptive_kalman.py",
        ROOT / "src" / "skru1" / "uncertainty.py",
        ROOT / "src" / "skru1" / "transition_validation.py",
        ROOT / "src" / "skru1" / "gate_b2.py",
        ROOT / "scripts" / "run_gate_b2.py",
    ]
    assert find_test_loader_calls(paths) == []
    runner = (ROOT / "scripts" / "run_gate_b2.py").read_text(encoding="utf-8")
    assert '"final-test"' not in runner and "'final-test'" not in runner


def test_disclosed_test_artifacts_match_protected_hashes() -> None:
    _, config = load_gate_b2_config(ROOT)
    snapshot = capture_protected_test_snapshot(ROOT, config)
    assert snapshot["all_match"] is True
    assert len(snapshot["protected_files"]) == 5
    assert all(row["matches"] for row in snapshot["protected_files"])


def test_inner_tuning_is_forward_only_and_selects_one_candidate() -> None:
    _, base_config = load_gate_b2_config(ROOT)
    config = deepcopy(base_config)
    config["adaptive_model"]["q_base_grid"] = [2.0, 10.0]
    config["adaptive_model"]["acceleration_gain_grid"] = [0.0]
    config["development_policy"]["inner_rolling_origin_folds"] = 2
    train = load_split_dataset("t1", "train", root=ROOT)
    history = prepare_kalman_history(causal_feature_history(train))
    selected, tuning = tune_adaptive_parameters(
        train,
        prepared_history=history,
        config=config,
        context="unit",
    )
    assert selected["q_base"] in {2.0, 10.0}
    assert selected["acceleration_gain"] == 0.0
    assert (
        pd.to_datetime(tuning["train_target_date_max"])
        < pd.to_datetime(tuning["validation_target_date_min"])
    ).all()
    assert tuning.loc[tuning["selected"], "candidate_key"].nunique() == 1


def test_scaled_conformal_uses_finite_sample_higher_quantile() -> None:
    scores = [0.1, 0.2, 0.3, 0.4]
    qhat, probability = finite_sample_conformal_quantile(scores, coverage=0.8)
    assert probability == 1.0
    assert qhat == 0.4
    calibration_input = pd.DataFrame(
        {
            "sample_id": ["a", "b", "c", "d"],
            "y_true": [0.1, 0.2, 0.3, 0.4],
            "y_pred": [0.0, 0.0, 0.0, 0.0],
            "raw_sigma": [1.0, 1.0, 1.0, 1.0],
        }
    )
    _, calibration = calibrate_scaled_conformal(
        calibration_input,
        coverage_levels=[0.8],
        sigma_floor=1.0,
    )
    validation = pd.DataFrame(
        {
            "sample_id": ["v1", "v2"],
            "y_true": [0.2, 0.5],
            "y_pred": [0.0, 0.0],
            "raw_sigma": [1.0, 1.0],
        }
    )
    intervals = apply_scaled_conformal_intervals(
        validation, calibration, sigma_floor=1.0
    )
    metrics = interval_metrics(intervals, calibration).iloc[0]
    assert np.isclose(metrics["coverage_empirical"], 0.5)
    assert np.isclose(metrics["mean_width_mm_y"], 0.8)


def test_final_holdout_policy_excludes_disclosed_test() -> None:
    policy = yaml.safe_load(
        (ROOT / "configs" / "final_holdout_v2.yaml").read_text(encoding="utf-8")
    )
    assert policy["status"] == "PENDING_DATA"
    assert policy["excluded_evaluation_sets"][0]["split"] == "t1_v1/test"
    assert policy["access_protocol"]["tuning_after_access"] == "prohibited"
    assert policy["access_protocol"]["one_access_event_only"] is True


def test_published_gate_b2_candidate_and_inventory_are_consistent_if_present() -> None:
    artifact_root = ROOT / "artifacts" / "model_selection" / "t1_b2_v1"
    candidate_path = artifact_root / "development_candidate.json"
    inventory_path = artifact_root / "artifact_inventory.csv"
    if not candidate_path.is_file() or not inventory_path.is_file():
        return
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert candidate["current_t1_test_used"] is False
    assert candidate["current_t1_test_authorized"] is False
    inventory = pd.read_csv(inventory_path)
    assert inventory["relative_path"].is_unique
    from skru1.data_contracts import sha256_file

    for row in inventory.itertuples(index=False):
        path = ROOT / row.relative_path
        assert path.is_file()
        assert path.stat().st_size == row.size_bytes
        assert sha256_file(path) == row.sha256
