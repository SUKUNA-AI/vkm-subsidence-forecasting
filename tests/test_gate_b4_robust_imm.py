from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from skru1.adaptive_kalman import prepare_kalman_history
from skru1.evaluation import causal_feature_history, derived_dataset
from skru1.gate_b4 import load_gate_b4_config, robust_parameters, tune_robust_df
from skru1.leakage import LeakageViolation
from skru1.robust_imm import (
    RobustInnovationIMMRate,
    _student_t_scalar_update_with_log_likelihood,
)
from skru1.splits import load_split_dataset


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def b4_context():
    _, config = load_gate_b4_config(ROOT)
    train = load_split_dataset("t1", "train", root=ROOT)
    history = causal_feature_history(train)
    parameters = robust_parameters(config, 5.0)
    return config, train, history, parameters


def test_robust_imm_returns_finite_distribution_and_influence_diagnostics(
    b4_context,
) -> None:
    _, train, history, parameters = b4_context
    sample_ids = tuple(train.frame.sort_values("target_date").tail(24)["sample_id"].astype(str))
    sample = derived_dataset(
        train,
        sample_ids,
        split="validation",
        label="b4_distribution_test",
    )
    model = RobustInnovationIMMRate(
        "B8_student_t_robust_imm", parameters
    ).fit(train)
    mean, sigma, diagnostics = model.predict_distribution(
        sample,
        history_frame=prepare_kalman_history(history),
    )
    assert mean.shape == sigma.shape == (len(sample.frame),)
    assert np.isfinite(mean).all()
    assert np.isfinite(sigma).all() and (sigma > 0).all()
    assert diagnostics["robust_weight_mean"].between(0.0, 1.0).all()
    assert diagnostics["robust_weight_min"].between(0.0, 1.0).all()
    assert diagnostics["robust_update_count"].gt(0).all()
    assert diagnostics["robust_downweighted_update_count"].sum() > 0
    state = model.state_dict()
    assert state["selected_parameter_count"] == 1
    assert state["observation_likelihood"] == "student_t"


def test_student_t_update_downweights_extreme_innovation() -> None:
    state = np.asarray([0.0, 0.0])
    covariance = np.eye(2)
    observation = np.asarray([1.0, 0.0])
    _, _, _, near_weight, _ = _student_t_scalar_update_with_log_likelihood(
        state,
        covariance,
        observation,
        0.1,
        1.0,
        degrees_of_freedom=5.0,
        minimum_weight=0.05,
        variance_floor=1e-10,
    )
    _, _, _, outlier_weight, z2 = _student_t_scalar_update_with_log_likelihood(
        state,
        covariance,
        observation,
        100.0,
        1.0,
        degrees_of_freedom=5.0,
        minimum_weight=0.05,
        variance_floor=1e-10,
    )
    assert near_weight == 1.0
    assert outlier_weight == 0.05
    assert z2 > 1000


def test_robust_imm_rejects_non_train_fit(b4_context) -> None:
    _, train, _, parameters = b4_context
    sample = derived_dataset(
        train,
        train.sample_ids[-8:],
        split="validation",
        label="b4_fit_guard",
    )
    with pytest.raises(LeakageViolation):
        RobustInnovationIMMRate("B8_student_t_robust_imm", parameters).fit(sample)


def test_robust_imm_is_invariant_to_future_history(b4_context) -> None:
    _, train, history, parameters = b4_context
    sample = derived_dataset(
        train,
        train.sample_ids[-8:],
        split="validation",
        label="b4_future_history",
    )
    model = RobustInnovationIMMRate("B8_student_t_robust_imm", parameters).fit(train)
    original = model.predict(sample, history_frame=history)
    future = history.iloc[[0]].copy()
    future["sample_id"] = "future-injected-b4"
    future["point_id"] = sample.frame.iloc[0]["point_id"]
    future["current_date"] = pd.to_datetime(sample.frame["current_date"]).max() + pd.Timedelta(days=3650)
    future["last_settlement_mm"] = 1_000_000_000.0
    future["last_rate_mm_y"] = 1_000_000_000.0
    future["recent_acceleration_mm_y2"] = 1_000_000_000.0
    changed = model.predict(
        sample,
        history_frame=pd.concat([history, future], ignore_index=True),
    )
    np.testing.assert_allclose(original, changed, rtol=0, atol=1e-12)


def test_reduced_nested_tuning_selects_minimum_train_only_score(b4_context) -> None:
    config, train, history, _ = b4_context
    reduced = deepcopy(config)
    reduced["robust_model"]["student_t_df_grid"] = [3.0, 10.0]
    reduced["resampling"]["inner_rolling_origin_folds"] = 2
    selected, tuning = tune_robust_df(
        train,
        prepared_history=prepare_kalman_history(history),
        config=reduced,
        context="unit",
    )
    assert selected["student_t_df"] in {3.0, 10.0}
    assert tuning["student_t_df"].nunique() == 2
    assert tuning["inner_folds"].eq(2).all()
    assert tuning["all_inner_folds_forward_only"].all()
    assert tuning["selected"].sum() == 1
    assert np.isclose(
        tuning.loc[tuning["selected"], "tuning_score"].iloc[0],
        tuning["tuning_score"].min(),
    )
