from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from skru1.b5_splits import build_benchmark_folds
from skru1.benchmarking import (
    DYNAMIC_CORE_17,
    FitContext,
    PredictionBundle,
    assert_structural_keys_not_in_x,
    assert_train_only_worker_job,
    feature_view_columns,
)
from skru1.data_contracts import load_canonical_bundle
from skru1.gate_b5 import load_gate_b5_config
from skru1.leakage import LeakageViolation
from skru1.splits import load_split_dataset


ROOT = Path(__file__).resolve().parents[1]


def _benchmark():
    _, config = load_gate_b5_config(ROOT)
    bundle = load_canonical_bundle(ROOT)
    train = load_split_dataset("t1", "train", root=ROOT)
    source, outer, inner, contracts = build_benchmark_folds(train, bundle, config)
    return config, bundle, source, outer, inner, contracts


def test_gate_b5_exact_outer_and_inner_geometry() -> None:
    _, _, _, outer, inner, contracts = _benchmark()
    outer_counts = (
        contracts.loc[contracts["level"].eq("outer")]
        .groupby("design")["fold_id"]
        .nunique()
        .to_dict()
    )
    assert outer_counts == {
        "rolling_origin": 11,
        "spatiotemporal_leave_profile_out": 42,
        "spatiotemporal_leave_zone_out": 12,
    }
    assert len(outer) == 65
    assert len(inner) == 195
    assert contracts.loc[contracts["level"].eq("inner")].groupby("parent_fold_id").size().eq(3).all()


def test_gate_b5_first_rolling_fold_and_dates_are_predeclared() -> None:
    _, _, _, outer, _, contracts = _benchmark()
    rolling = [fold for fold in outer if fold.design == "rolling_origin"]
    assert [fold.validation_target_date for fold in rolling] == [
        "2021-05-18",
        "2021-07-13",
        "2021-11-02",
        "2022-03-01",
        "2022-05-17",
        "2022-07-19",
        "2022-10-18",
        "2023-01-17",
        "2023-05-16",
        "2023-07-25",
        "2023-11-07",
    ]
    first = contracts.loc[contracts["fold_id"].eq("rolling_origin_2021-05-18")].iloc[0]
    assert int(first["train_rows"]) == 316
    assert first["train_target_date_max"] == "2021-01-26"
    assert first["validation_target_date_min"] == "2021-05-18"


def test_gate_b5_all_outer_and_inner_folds_are_forward_only_and_group_safe() -> None:
    _, _, source, outer, inner, contracts = _benchmark()
    assert contracts["forward_only"].all()
    assert contracts["held_group_absent_from_train"].all()
    indexed = source.frame.set_index("sample_id", drop=False)
    for fold in [*outer, *inner]:
        assert set(fold.train_sample_ids).isdisjoint(fold.validation_sample_ids)
        if fold.held_out_key:
            train_groups = set(indexed.loc[list(fold.train_sample_ids), fold.held_out_key].astype(str))
            assert fold.held_out_group not in train_groups
            if fold.level == "inner":
                validation_groups = set(
                    indexed.loc[list(fold.validation_sample_ids), fold.held_out_key].astype(str)
                )
                assert fold.held_out_group not in validation_groups


def test_focused_campaigns_are_rolling_only() -> None:
    _, _, _, outer, _, _ = _benchmark()
    spatial_dates = {
        fold.validation_target_date
        for fold in outer
        if fold.design != "rolling_origin"
    }
    assert "2023-01-17" not in spatial_dates
    assert "2023-07-25" not in spatial_dates


def test_feature_views_are_allowlisted_and_identifier_free() -> None:
    _, bundle, _, _, _, _ = _benchmark()
    assert feature_view_columns("DYNAMIC_CORE_17", bundle.feature_contract) == DYNAMIC_CORE_17
    assert len(feature_view_columns("SAFE_ALL", bundle.feature_contract)) == 50
    assert feature_view_columns("NATIVE_CATEGORICAL", bundle.feature_contract) == feature_view_columns(
        "SAFE_ALL", bundle.feature_contract
    )
    for view in ("SAFE_ALL", "DYNAMIC_CORE_17", "NATIVE_CATEGORICAL"):
        columns = feature_view_columns(view, bundle.feature_contract)
        assert not {"point_id", "profile_id", "zone_id", "current_campaign_id"} & set(columns)


def test_worker_job_cannot_receive_validation_or_test_manifest() -> None:
    assert_train_only_worker_job(
        {
            "source_split": "t1_v1/train",
            "train_manifest": "artifacts/splits/t1_train_benchmark_v1/outer_assignments.csv",
        }
    )
    with pytest.raises(LeakageViolation):
        assert_train_only_worker_job(
            {"source_split": "t1_v1/train", "validation_manifest": "artifacts/splits/t1_v1/validation.csv"}
        )
    with pytest.raises(LeakageViolation):
        assert_train_only_worker_job(
            {"source_split": "t1_v1/train", "input": "artifacts/splits/t1_v1/test.csv"}
        )


def test_gee_group_is_structural_and_never_part_of_x() -> None:
    columns = list(DYNAMIC_CORE_17)
    structural = {"point_id": ("P1", "P2"), "current_date": ("2020-01-01", "2020-02-01")}
    assert_structural_keys_not_in_x(columns, structural)
    with pytest.raises(LeakageViolation):
        assert_structural_keys_not_in_x([*columns, "point_id"], structural)


def test_prediction_bundle_requires_exact_schema_hash_and_no_duplicates() -> None:
    row = {
        "model_id": "demo",
        "family": "fixture",
        "environment_id": "b6_cpu",
        "feature_view": "DYNAMIC_CORE_17",
        "model_spec_sha256": "a" * 64,
        "benchmark_plan_sha256": "b" * 64,
        "fold_manifest_sha256": "c" * 64,
        "seed": 42117,
        "design": "rolling_origin",
        "fold_id": "fold",
        "sample_id": "sample",
        "point_id": "point",
        "profile_id": "profile",
        "zone_id": "zone",
        "current_date": "2020-01-01",
        "target_date": "2020-02-01",
        "forecast_horizon_days": 31,
        "last_rate_mm_y": 0.9,
        "current_standard_uncertainty_mm": 1.0,
        "sigma_rate_mm_y": 2.0,
        "n_history": 5,
        "missing_campaigns_since_previous": 0,
        "transition_segment": "stable",
        "is_transition": False,
        "y_true": 1.0,
        "y_pred": 1.1,
    }
    frame = pd.DataFrame([row])
    bundle = PredictionBundle.validate(
        frame,
        expected_sample_ids=["sample"],
        expected_environment_id="b6_cpu",
        expected_model_id="demo",
    )
    assert len(bundle.frame) == 1
    with pytest.raises(Exception):
        PredictionBundle.validate(pd.concat([frame, frame], ignore_index=True))


def test_fit_context_rejects_unregistered_structural_keys() -> None:
    FitContext(
        train_manifest_sha256="a" * 64,
        feature_contract_sha256="b" * 64,
        feature_view_sha256="c" * 64,
        seed=42117,
        structural_grouping_keys={"point_id": ("P1",)},
    )
    with pytest.raises(LeakageViolation):
        FitContext(
            train_manifest_sha256="a" * 64,
            feature_contract_sha256="b" * 64,
            feature_view_sha256="c" * 64,
            seed=42117,
            structural_grouping_keys={"campaign_id": ("C1",)},
        )


def test_b5_source_code_has_no_validation_or_test_loader() -> None:
    text = (ROOT / "src" / "skru1" / "gate_b5.py").read_text(encoding="utf-8")
    assert 'load_split_dataset("t1", "validation"' not in text
    assert 'load_split_dataset("t1", "test"' not in text
