from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from skru1.adaptive_kalman import AdaptiveKalmanRate, prepare_kalman_history
from skru1.evaluation import causal_feature_history, derived_dataset
from skru1.gate_b2 import adaptive_parameters, load_gate_b2_config
from skru1.leakage import LeakageViolation
from skru1.splits import combine_development_datasets, load_split_dataset
from skru1.transition_validation import (
    classify_transition_proxy,
    fit_transition_thresholds,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def b2_context():
    _, config = load_gate_b2_config(ROOT)
    train = load_split_dataset("t1", "train", root=ROOT)
    validation = load_split_dataset("t1", "validation", root=ROOT)
    development = combine_development_datasets([train, validation])
    history = causal_feature_history(development)
    parameters = adaptive_parameters(config, q_base=25.0, acceleration_gain=0.25)
    return config, train, validation, development, history, parameters


def test_adaptive_kalman_fits_train_and_returns_finite_distribution(b2_context) -> None:
    _, train, validation, _, history, parameters = b2_context
    model = AdaptiveKalmanRate("B6_adaptive_kalman", parameters).fit(train)
    mean, sigma, diagnostics = model.predict_distribution(
        validation,
        history_frame=prepare_kalman_history(history),
    )
    assert mean.shape == sigma.shape == (len(validation.frame),)
    assert np.isfinite(mean).all()
    assert np.isfinite(sigma).all() and (sigma > 0).all()
    assert np.isfinite(diagnostics["adaptive_q"]).all()
    assert diagnostics["process_scale"].between(1.0, 10.0).all()
    state = model.state_dict()
    assert state["train_sample_ids_sha256"] == train.provenance.sample_ids_sha256


def test_adaptive_kalman_rejects_validation_fit(b2_context) -> None:
    _, _, validation, _, _, parameters = b2_context
    with pytest.raises(LeakageViolation):
        AdaptiveKalmanRate("B6_adaptive_kalman", parameters).fit(validation)


def test_adaptive_kalman_is_invariant_to_future_feature_history(b2_context) -> None:
    _, train, validation, development, history, parameters = b2_context
    sample = derived_dataset(
        development,
        validation.sample_ids[:8],
        split="validation",
        label="b2_future_history_test",
    )
    model = AdaptiveKalmanRate("B6_adaptive_kalman", parameters).fit(train)
    original = model.predict(sample, history_frame=history)
    future = history.iloc[[0]].copy()
    future["sample_id"] = "future-injected-b2"
    future["point_id"] = sample.frame.iloc[0]["point_id"]
    future["current_date"] = pd.to_datetime(sample.frame["current_date"]).max() + pd.Timedelta(
        days=3650
    )
    future["last_settlement_mm"] = 1_000_000_000.0
    future["last_rate_mm_y"] = 1_000_000_000.0
    future["recent_acceleration_mm_y2"] = 1_000_000_000.0
    changed = model.predict(
        sample,
        history_frame=pd.concat([history, future], ignore_index=True),
    )
    np.testing.assert_allclose(original, changed, rtol=0, atol=1e-12)


def test_transition_proxy_is_origin_only_mutually_exclusive_and_nonempty(b2_context) -> None:
    config, train, validation, _, _, _ = b2_context
    policy = config["transition_validation"]
    thresholds = fit_transition_thresholds(
        train.frame,
        acceleration_quantile=float(policy["acceleration_absolute_quantile"]),
        volatility_quantile=float(policy["volatility_quantile"]),
        missing_campaigns_threshold=int(policy["missing_campaigns_threshold"]),
    )
    classified = classify_transition_proxy(validation.frame, thresholds)
    assert len(classified) == len(validation.frame)
    assert set(classified["transition_segment"]) == {
        "stable",
        "accelerating",
        "decelerating",
        "volatile_or_gap",
    }
    flags = classified[
        [
            "transition_acceleration_flag",
            "transition_deceleration_flag",
            "transition_volatility_or_gap_flag",
        ]
    ].sum(axis=1)
    assert flags.le(1).all()
    assert classified["is_transition"].eq(classified["transition_segment"].ne("stable")).all()
