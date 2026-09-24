from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from skru1.adaptive_kalman import prepare_kalman_history
from skru1.evaluation import causal_feature_history, derived_dataset
from skru1.leakage import LeakageViolation
from skru1.robust_imm import (
    RobustInnovationIMMRate,
    _student_t_scalar_update_with_log_likelihood,
)
from skru1.splits import load_split_dataset


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def b4_context():
    # Frozen numerical fixture; the retired B4 tuning pipeline is not needed.
    config = {}
    train = load_split_dataset("t1", "train", root=ROOT)
    history = causal_feature_history(train)
    parameters = {'q_stable': 0.5,
     'q_transition': 200.0,
     'p_stable_stay': 0.99,
     'p_transition_stay': 0.75,
     'stable_acceleration_retention_per_year': 0.2,
     'transition_acceleration_retention_per_year': 0.95,
     'initial_transition_probability': 0.1,
     'initial_position_variance': 4.0,
     'initial_velocity_variance': 400.0,
     'initial_acceleration_variance': 2500.0,
     'minimum_measurement_variance': 0.01,
     'minimum_rate_measurement_variance': 1.0,
     'rate_measurement_variance_multiplier': 4.0,
     'acceleration_scale_quantile': 0.8,
     'acceleration_measurement_scale_multiplier': 1.0,
     'minimum_acceleration_measurement_variance': 25.0,
     'acceleration_clip_ratio': 4.0,
     'raw_sigma_floor_mm_y': 1.0,
     'covariance_jitter': 1e-08,
     'likelihood_variance_floor': 1e-10,
     'minimum_robust_weight': 0.05,
     'student_t_df': 5.0}
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
