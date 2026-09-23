from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from skru1.imm_kalman import TwoRegimeIMMRate
from skru1.scenario_adapter import load_scenario_model_bundle
from skru1.scenario_benchmark import finite_sample_higher
from skru1.scenario_simulation import REQUIRED_FEATURES, _temporal_factor, build_scenario_catalog


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data/scenario_simulation_v1"
RESULT_ROOT = ROOT / "artifacts/model_selection/scenario_b1_imm_v1"


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def assert_manifest_files(manifest_path: Path) -> None:
    manifest = read_json(manifest_path)
    for section in ("inputs", "outputs"):
        assert manifest[section]
        for item in manifest[section]:
            path = (
                ROOT / item["path"]
                if section == "inputs"
                else manifest_path.parent / item["path"]
            )
            assert path.is_file(), item["path"]
            assert path.stat().st_size == item["size_bytes"]
            assert file_hash(path) == item["sha256"]


def test_scenario_catalog_is_full_factorial_with_heldout_mechanisms() -> None:
    config = read_json(ROOT / "configs/scenario_simulation_v1.json")
    catalog = build_scenario_catalog(config)
    assert len(catalog) == 60
    assert catalog["scenario_id"].is_unique
    assert set(catalog["dynamic_mechanism"]) == {
        "uniform",
        "creep_decay",
        "saturating_acceleration",
        "reactivation",
        "moving_spatial_focus",
    }
    combinations = catalog.groupby(
        ["dynamic_mechanism", "missingness_mechanism", "measurement_error_mechanism"]
    ).size()
    assert len(combinations) == 5 * 4 * 3
    assert combinations.eq(1).all()
    heldout = set(
        catalog.loc[catalog["experiment_role"].eq("heldout_mechanism"), "dynamic_mechanism"]
    )
    assert heldout == {"reactivation", "moving_spatial_focus"}


def test_static_temporal_mechanisms_are_distinct_monotone_and_normalized() -> None:
    config = read_json(ROOT / "configs/scenario_simulation_v1.json")
    times = np.linspace(0.0, 1.0, 101)
    curves = {}
    for item in config["dynamic_mechanisms"]:
        if item["mechanism"] == "moving_spatial_focus":
            continue
        curve = _temporal_factor(times, item["mechanism"], item["parameters"])
        assert np.isclose(curve[0], 0.0, atol=1e-6)
        assert np.isclose(curve[-1], 1.0, atol=1e-12)
        assert np.diff(curve).min() >= -1e-12
        curves[item["mechanism"]] = curve
    for first, first_curve in curves.items():
        for second, second_curve in curves.items():
            if first < second:
                assert not np.allclose(first_curve, second_curve, rtol=0, atol=1e-4)


def test_generated_release_preserves_truth_observation_and_next_plan_boundaries() -> None:
    validation = read_json(DATA_ROOT / "validation_report.json")
    assert validation["status"] == "PASS"
    assert validation["scenario_count"] == 60
    assert validation["point_count"] == 42
    assert validation["profile_count"] == 14
    assert validation["campaign_count"] == 29
    assert validation["model_role_counts"] == {
        "calibration": 2691,
        "evaluation": 17209,
        "excluded": 1586,
        "train": 9648,
    }
    assert all(validation["checks"].values())
    assert validation["settlement_sign_convention"] == "positive_down_mm"
    catalog = pd.read_csv(DATA_ROOT / "scenario_catalog.csv")
    observations = pd.read_csv(DATA_ROOT / "campaign_observations.csv.gz")
    samples = pd.read_csv(DATA_ROOT / "next_planned_samples.csv.gz", low_memory=False)
    assert len(catalog) == 60
    assert len(observations) == validation["campaign_observation_rows"]
    assert len(samples) == validation["sample_rows"]
    observed = observations.loc[observations["observed"]]
    np.testing.assert_allclose(
        observed["observed_settlement_mm"] - observed["latent_settlement_mm"],
        observed["ordinary_noise_mm"]
        + observed["gross_error_mm"]
        + observed["reference_datum_error_mm"],
        rtol=0,
        atol=2e-9,
    )
    assert observations.loc[~observations["observed"], "observed_settlement_mm"].isna().all()
    assert (
        samples["model_role"].eq("evaluation") & ~samples["target_observed_available"]
    ).any()
    assert samples.loc[samples["model_role"].eq("evaluation"), "latent_rate_mm_y"].notna().all()
    assert_manifest_files(DATA_ROOT / "manifest.json")


def test_adapter_exposes_only_corrected_formal_features_and_causal_history() -> None:
    bundle = load_scenario_model_bundle(ROOT)
    assert bundle.train.feature_columns == REQUIRED_FEATURES
    assert len(bundle.train.frame) == 9648
    assert len(bundle.calibration.frame) == 2691
    assert len(bundle.evaluation.frame) == 17209
    assert bundle.train.provenance.split == "train"
    assert bundle.calibration.provenance.split == "calibration"
    assert bundle.evaluation.provenance.split == "evaluation"
    assert bundle.train.frame["target_observed_available"].all()
    assert bundle.calibration.frame["target_observed_available"].all()
    assert (~bundle.evaluation.frame["target_observed_available"]).any()
    assert not any(column.startswith("latent_") for column in bundle.causal_history)
    assert {
        "reactivation", "moving_spatial_focus"
    }.isdisjoint(bundle.train.frame["dynamic_mechanism"])


def test_imm_adapter_is_invariant_to_future_history() -> None:
    bundle = load_scenario_model_bundle(ROOT)
    config = read_json(ROOT / "configs/scenario_b1_imm_v1.json")
    model = TwoRegimeIMMRate(
        "B7_two_regime_imm", config["models"]["imm"]["parameters"]
    ).fit(bundle.train)
    sample_frame = bundle.evaluation.frame.iloc[:12].copy()
    sample = type(bundle.evaluation)(
        frame=sample_frame,
        feature_columns=bundle.evaluation.feature_columns,
        provenance=bundle.evaluation.provenance,
    )
    original, _, _ = model.predict_distribution(sample, history_frame=bundle.causal_history)
    future = bundle.causal_history.iloc[[0]].copy()
    future["point_id"] = sample_frame.iloc[0]["point_id"]
    future["current_date"] = sample_frame["current_date"].max() + pd.Timedelta(days=3650)
    future["last_settlement_mm"] = 1_000_000_000.0
    future["last_rate_mm_y"] = 1_000_000_000.0
    future["recent_acceleration_mm_y2"] = 1_000_000_000.0
    changed, _, _ = model.predict_distribution(
        sample,
        history_frame=pd.concat([bundle.causal_history, future], ignore_index=True),
    )
    np.testing.assert_allclose(original, changed, rtol=0, atol=1e-12)


def test_finite_sample_higher_uses_declared_rank() -> None:
    scores = np.arange(1.0, 101.0)
    assert finite_sample_higher(scores, 0.80) == 81.0
    assert finite_sample_higher(scores, 0.95) == 96.0


def test_b1_imm_execution_is_paired_and_does_not_select_a_model() -> None:
    validation = read_json(RESULT_ROOT / "validation_report.json")
    assert validation["status"] == "PASS_EXECUTED_NO_SELECTION"
    assert validation["train_rows"] == 9648
    assert validation["calibration_rows"] == 2691
    assert validation["evaluation_rows"] == 17209
    assert validation["imm_numerical_fallback_fraction"] == 0.0
    assert validation["legacy_test_rows_loaded"] == 0
    assert validation["external_holdout_rows_loaded"] == 0
    assert validation["selection_performed"] is False
    assert validation["field_accuracy_claim"] is False
    assert all(validation["checks"].values())
    predictions = pd.read_csv(RESULT_ROOT / "predictions.csv.gz", low_memory=False)
    assert len(predictions) == 2 * validation["evaluation_rows"]
    assert predictions.groupby("sample_id")["model_id"].nunique().eq(2).all()
    paired = pd.read_csv(RESULT_ROOT / "paired_comparison.csv")
    development = paired.loc[
        paired["group_name"].eq("evaluation_scope")
        & paired["group_value"].eq("development_future")
    ].iloc[0]
    reactivation = paired.loc[
        paired["group_name"].eq("dynamic_mechanism")
        & paired["group_value"].eq("reactivation")
    ].iloc[0]
    assert development["imm_skill_vs_b1_percent"] > 0
    assert reactivation["imm_skill_vs_b1_percent"] < 0
    manifest = read_json(RESULT_ROOT / "manifest.json")
    runtime = next(item for item in manifest["outputs"] if item["path"] == "runtime.csv")
    assert runtime["deterministic"] is False
    assert all(
        item["deterministic"] is True
        for item in manifest["outputs"]
        if item["path"] != "runtime.csv"
    )
    assert_manifest_files(RESULT_ROOT / "manifest.json")
