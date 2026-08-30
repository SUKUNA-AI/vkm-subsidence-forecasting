from __future__ import annotations

import math

import numpy as np
import pandas as pd

from skru1.benchmark_metrics import (
    apply_scaled_conformal,
    fit_scaled_conformal,
    interval_score,
    normal_crps,
    point_metrics,
    weighted_interval_score,
)


def test_point_metric_formulas_on_manual_fixture() -> None:
    truth = [1.0, 2.0, 4.0]
    prediction = [2.0, 2.0, 1.0]
    metrics = point_metrics(
        truth,
        prediction,
        sample_weight=[1.0, 2.0, 1.0],
        b1_prediction=[1.0, 1.0, 1.0],
        mase_denominator=2.0,
        last_rate=[0.0, 3.0, 2.0],
        neutral_zone=[0.0, 0.0, 0.0],
    )
    assert math.isclose(metrics["mae"], 4.0 / 3.0)
    assert math.isclose(metrics["median_absolute_error"], 1.0)
    assert math.isclose(metrics["rmse"], math.sqrt(10.0 / 3.0))
    assert math.isclose(metrics["bias"], -2.0 / 3.0)
    assert math.isclose(metrics["precision_weighted_mae"], 1.0)
    assert math.isclose(metrics["mase"], 2.0 / 3.0)
    assert math.isclose(metrics["b1_skill"], 0.0)
    assert math.isclose(metrics["direction_accuracy"], 2.0 / 3.0)


def test_interval_score_and_wis_manual_fixture() -> None:
    truth = np.asarray([0.0, 2.0, 5.0])
    lower = np.asarray([-1.0, 0.0, 0.0])
    upper = np.asarray([1.0, 1.0, 4.0])
    score = interval_score(truth, lower, upper, alpha=0.2)
    assert np.allclose(score, [2.0, 11.0, 14.0])
    wis = weighted_interval_score(truth, [0.0, 0.5, 2.0], {0.8: (lower, upper)})
    expected = (0.5 * np.asarray([0.0, 1.5, 3.0]) + 0.1 * score) / 1.5
    assert np.allclose(wis, expected)


def test_normal_crps_is_nonnegative_and_minimized_near_truth() -> None:
    truth = [0.0]
    at_truth = normal_crps(truth, [0.0], [1.0])[0]
    far = normal_crps(truth, [3.0], [1.0])[0]
    assert at_truth >= 0
    assert at_truth < far


def test_scaled_conformal_uses_only_inner_validation_oof_and_is_reproducible() -> None:
    inner = pd.DataFrame(
        {
            "sample_id": [f"s{index}" for index in range(10)],
            "y_true": np.arange(10, dtype=float),
            "y_pred": np.arange(10, dtype=float) + np.linspace(-2, 2, 10),
            "forecast_horizon_days": [90, 120] * 5,
            "current_standard_uncertainty_mm": [1.0, 2.0] * 5,
            "provenance_role": "inner_validation",
        }
    )
    first = fit_scaled_conformal(inner)
    second = fit_scaled_conformal(inner)
    assert first == second
    outer = inner.iloc[:3].drop(columns="provenance_role").copy()
    output = apply_scaled_conformal(outer, first)
    for tag in ("50", "80", "95"):
        assert (output[f"conformal_lower_{tag}"] <= output["y_pred"]).all()
        assert (output[f"conformal_upper_{tag}"] >= output["y_pred"]).all()
