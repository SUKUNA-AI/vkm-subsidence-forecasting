from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from skru1.adaptive_kalman import prepare_kalman_history
from skru1.evaluation import causal_feature_history, derived_dataset
from skru1.gate_b3 import imm_parameters, load_gate_b3_config
from skru1.imm_kalman import TwoRegimeIMMRate
from skru1.leakage import LeakageViolation
from skru1.splits import combine_development_datasets, load_split_dataset


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def b3_context():
    _, config = load_gate_b3_config(ROOT)
    train = load_split_dataset("t1", "train", root=ROOT)
    validation = load_split_dataset("t1", "validation", root=ROOT)
    development = combine_development_datasets([train, validation])
    history = causal_feature_history(development)
    parameters = imm_parameters(
        config,
        q_stable=0.5,
        q_transition=50.0,
        p_stable_stay=0.97,
        p_transition_stay=0.75,
    )
    return config, train, validation, development, history, parameters


def test_imm_returns_finite_mixture_distribution(b3_context) -> None:
    _, train, validation, development, history, parameters = b3_context
    sample = derived_dataset(
        development,
        validation.sample_ids[:24],
        split="validation",
        label="b3_distribution_test",
    )
    model = TwoRegimeIMMRate("B7_two_regime_imm", parameters).fit(train)
    mean, sigma, diagnostics = model.predict_distribution(
        sample,
        history_frame=prepare_kalman_history(history),
    )
    assert mean.shape == sigma.shape == (len(sample.frame),)
    assert np.isfinite(mean).all()
    assert np.isfinite(sigma).all() and (sigma > 0).all()
    assert diagnostics["stable_probability"].between(0.0, 1.0).all()
    assert diagnostics["transition_probability"].between(0.0, 1.0).all()
    np.testing.assert_allclose(
        diagnostics["stable_probability"] + diagnostics["transition_probability"],
        1.0,
        rtol=0,
        atol=1e-12,
    )
    assert diagnostics["causal_history_rows"].ge(2).all()
    assert not diagnostics["numerical_fallback_used"].any()
    state = model.state_dict()
    assert state["train_sample_ids_sha256"] == train.provenance.sample_ids_sha256
    assert state["state_dimension"] == 3 and state["regime_count"] == 2


def test_imm_rejects_validation_fit(b3_context) -> None:
    _, _, validation, _, _, parameters = b3_context
    with pytest.raises(LeakageViolation):
        TwoRegimeIMMRate("B7_two_regime_imm", parameters).fit(validation)


def test_imm_is_invariant_to_future_feature_history(b3_context) -> None:
    _, train, validation, development, history, parameters = b3_context
    sample = derived_dataset(
        development,
        validation.sample_ids[:8],
        split="validation",
        label="b3_future_history_test",
    )
    model = TwoRegimeIMMRate("B7_two_regime_imm", parameters).fit(train)
    original_mean, original_sigma, original_diagnostics = model.predict_distribution(
        sample,
        history_frame=history,
    )
    future = history.iloc[[0]].copy()
    future["sample_id"] = "future-injected-b3"
    future["point_id"] = sample.frame.iloc[0]["point_id"]
    future["current_date"] = pd.to_datetime(sample.frame["current_date"]).max() + pd.Timedelta(
        days=3650
    )
    future["last_settlement_mm"] = 1_000_000_000.0
    future["last_rate_mm_y"] = 1_000_000_000.0
    future["recent_acceleration_mm_y2"] = 1_000_000_000.0
    changed_mean, changed_sigma, changed_diagnostics = model.predict_distribution(
        sample,
        history_frame=pd.concat([history, future], ignore_index=True),
    )
    np.testing.assert_allclose(original_mean, changed_mean, rtol=0, atol=1e-12)
    np.testing.assert_allclose(original_sigma, changed_sigma, rtol=0, atol=1e-12)
    np.testing.assert_allclose(
        original_diagnostics["transition_probability"],
        changed_diagnostics["transition_probability"],
        rtol=0,
        atol=1e-12,
    )


def test_imm_parameter_contract_requires_distinct_regimes(b3_context) -> None:
    _, train, _, _, _, parameters = b3_context
    invalid = dict(parameters)
    invalid["q_transition"] = invalid["q_stable"]
    with pytest.raises(ValueError, match="q_transition must exceed q_stable"):
        TwoRegimeIMMRate("B7_two_regime_imm", invalid).fit(train)
