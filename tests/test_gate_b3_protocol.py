from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pandas as pd

from skru1.adaptive_kalman import prepare_kalman_history
from skru1.data_contracts import sha256_file
from skru1.evaluation import causal_feature_history
from skru1.gate_b3 import (
    capture_protected_predecessor_snapshot,
    find_test_loader_calls,
    imm_parameter_grid,
    load_gate_b3_config,
    tune_imm_parameters,
)
from skru1.gate_b3_audit import frames_equivalent_with_absent_group_normalization
from skru1.splits import load_split_dataset


ROOT = Path(__file__).resolve().parents[1]


def test_gate_b3_protocol_is_predeclared_and_narrow() -> None:
    _, config = load_gate_b3_config(ROOT)
    assert len(imm_parameter_grid(config)) == 16
    assert config["imm_model"]["regimes"] == ["stable", "transition"]
    assert config["development_policy"]["tuning_objective"] == {
        "overall_normalized_mae_weight": 0.5,
        "problem_transition_normalized_mae_weight": 0.5,
        "problem_transition_segments": ["accelerating", "volatile_or_gap"],
        "minimum_problem_transition_rows": 5,
    }
    acceptance = config["acceptance"]
    assert acceptance["problem_transition_improvement_vs_b1_percent_min"] == 10.0
    assert acceptance["problem_transition_improvement_vs_b6_percent_min"] == 10.0
    assert acceptance["leave_zone_mae_degradation_vs_temporal_percent_max"] == 5.0


def test_gate_b3_has_no_current_test_loading_path() -> None:
    _, config = load_gate_b3_config(ROOT)
    assert config["test_policy"]["model_selection_access"] == "prohibited"
    assert config["test_policy"]["final_acceptance_access"] == "prohibited"
    paths = [
        ROOT / "src" / "skru1" / "imm_kalman.py",
        ROOT / "src" / "skru1" / "gate_b3.py",
        ROOT / "scripts" / "run_gate_b3.py",
    ]
    assert find_test_loader_calls(paths) == []
    runner = (ROOT / "scripts" / "run_gate_b3.py").read_text(encoding="utf-8")
    assert '"final-test"' not in runner and "'final-test'" not in runner


def test_gate_b3_predecessor_hashes_match() -> None:
    _, config = load_gate_b3_config(ROOT)
    snapshot = capture_protected_predecessor_snapshot(ROOT, config)
    assert snapshot["all_match"] is True
    assert len(snapshot["protected_files"]) == 12
    assert all(row["matches"] for row in snapshot["protected_files"])
    roles = pd.Series(row["role"] for row in snapshot["protected_files"]).value_counts()
    assert roles.to_dict() == {"frozen_comparator": 7, "disclosed_test": 5}


def test_reduced_inner_tuning_is_forward_only_and_selects_minimum_score() -> None:
    _, base_config = load_gate_b3_config(ROOT)
    config = deepcopy(base_config)
    config["imm_model"]["q_stable_grid"] = [0.5]
    config["imm_model"]["q_transition_grid"] = [50.0, 200.0]
    config["imm_model"]["p_stable_stay_grid"] = [0.97]
    config["imm_model"]["p_transition_stay_grid"] = [0.75]
    config["development_policy"]["inner_rolling_origin_folds"] = 2
    train = load_split_dataset("t1", "train", root=ROOT)
    history = prepare_kalman_history(causal_feature_history(train))
    selected, tuning = tune_imm_parameters(
        train,
        prepared_history=history,
        config=config,
        context="unit",
    )
    assert selected["q_transition"] in {50.0, 200.0}
    assert tuning["candidate_key"].nunique() == 2
    assert (
        pd.to_datetime(tuning["train_target_date_max"])
        < pd.to_datetime(tuning["validation_target_date_min"])
    ).all()
    selected_rows = tuning.loc[tuning["selected"]]
    assert selected_rows["candidate_key"].nunique() == 1
    assert selected_rows["candidate_tuning_score"].iloc[0] == tuning[
        "candidate_tuning_score"
    ].min()


def test_published_gate_b3_candidate_and_inventory_are_consistent_if_present() -> None:
    artifact_root = ROOT / "artifacts" / "model_selection" / "t1_b3_v1"
    candidate_path = artifact_root / "development_candidate.json"
    inventory_path = artifact_root / "artifact_inventory.csv"
    if not candidate_path.is_file() or not inventory_path.is_file():
        return
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert candidate["current_t1_test_used"] is False
    assert candidate["current_t1_test_authorized"] is False
    assert candidate["eligible_for_final_claim"] is False
    inventory = pd.read_csv(inventory_path)
    assert inventory["relative_path"].is_unique
    for row in inventory.itertuples(index=False):
        path = ROOT / row.relative_path
        assert path.is_file()
        assert path.stat().st_size == row.size_bytes
        assert sha256_file(path) == row.sha256


def test_authoritative_audit_normalizes_only_absent_string_cells() -> None:
    left = pd.DataFrame(
        {"fold_id": ["a"], "held_out_group": [""], "mae": [1.0]}
    )
    right = pd.DataFrame(
        {"fold_id": ["a"], "held_out_group": [pd.NA], "mae": [1.0]}
    )
    assert frames_equivalent_with_absent_group_normalization(left, right)
    changed = right.copy()
    changed["mae"] = 1.1
    assert not frames_equivalent_with_absent_group_normalization(left, changed)
