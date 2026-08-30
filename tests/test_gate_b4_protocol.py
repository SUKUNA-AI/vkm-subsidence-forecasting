from __future__ import annotations

from pathlib import Path

import pandas as pd

from skru1.data_contracts import load_canonical_bundle
from skru1.gate_b4 import (
    capture_protected_predecessor_snapshot,
    find_forbidden_split_loader_calls,
    load_gate_b4_config,
    robust_parameter_grid,
)
from skru1.splits import load_split_dataset
from skru1.train_research_splits import build_train_only_folds


ROOT = Path(__file__).resolve().parents[1]


def test_gate_b4_protocol_is_narrow_and_predeclared() -> None:
    _, config = load_gate_b4_config(ROOT)
    assert config["data_boundary"]["allowed_model_selection_data"] == [
        "t1_v1/train"
    ]
    assert len(robust_parameter_grid(config)) == 4
    assert [row["student_t_df"] for row in robust_parameter_grid(config)] == [
        3.0,
        5.0,
        10.0,
        30.0,
    ]
    assert config["robust_model"]["fixed_parameters"] == {
        "minimum_robust_weight": 0.05
    }


def test_gate_b4_train_only_fold_geometry() -> None:
    _, config = load_gate_b4_config(ROOT)
    bundle = load_canonical_bundle(ROOT)
    train = load_split_dataset("t1", "train", root=ROOT)
    source, folds, contracts = build_train_only_folds(train, bundle)
    assert len(source.frame) == 911
    assert len(folds) == 24
    assert contracts.groupby("design")["fold_id"].nunique().to_dict() == {
        "internal_temporal": 1,
        "train_leave_profile_out": 14,
        "train_leave_zone_out": 4,
        "train_rolling_origin": 5,
    }
    temporal = contracts.loc[contracts["design"].eq("internal_temporal")].iloc[0]
    assert temporal["train_rows"] == 823
    assert temporal["validation_rows"] == 88
    assert temporal["validation_target_date_min"] == "2023-11-07"
    assert (
        pd.to_datetime(contracts["train_target_date_max"])
        < pd.to_datetime(contracts["validation_target_date_min"])
    ).all()
    all_ids = set(source.frame["sample_id"].astype(str))
    assert all(set(fold.train_sample_ids) <= all_ids for fold in folds)
    assert all(set(fold.validation_sample_ids) <= all_ids for fold in folds)


def test_gate_b4_sources_have_no_validation_or_test_loader() -> None:
    _, config = load_gate_b4_config(ROOT)
    paths = [
        ROOT / relative
        for relative in config["source_files"]
        if str(relative).endswith(".py")
    ]
    assert find_forbidden_split_loader_calls(paths) == []
    runner = (ROOT / "scripts" / "run_gate_b4.py").read_text(encoding="utf-8")
    assert "final-test" not in runner
    assert "validation" not in config["data_boundary"]["allowed_model_selection_data"]


def test_gate_b4_protected_predecessors_match() -> None:
    _, config = load_gate_b4_config(ROOT)
    snapshot = capture_protected_predecessor_snapshot(ROOT, config)
    assert snapshot["all_match"] is True
    assert all(row["matches"] for row in snapshot["protected_files"])
