from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from skru1.baselines import (
    FixedKalmanRate,
    build_model,
    train_only_precision_weights,
)
from skru1.data_contracts import load_canonical_bundle
from skru1.evaluation import causal_feature_history, derived_dataset
from skru1.leakage import LeakageViolation
from skru1.model_selection import load_gate_b_config
from skru1.splits import combine_development_datasets, load_split_dataset


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def model_context():
    bundle = load_canonical_bundle(ROOT)
    _, config = load_gate_b_config(ROOT)
    train = load_split_dataset("t1", "train", root=ROOT)
    validation = load_split_dataset("t1", "validation", root=ROOT)
    development = combine_development_datasets([train, validation])
    return bundle, config, train, validation, development


def test_train_weights_are_recomputed_without_canonical_weight(model_context) -> None:
    _, _, train, _, _ = model_context
    original = train.frame.copy()
    altered = train.frame.copy()
    altered["training_weight"] = np.linspace(1.0, 1_000_000.0, len(altered))
    first = train_only_precision_weights(original)
    second = train_only_precision_weights(altered)
    np.testing.assert_allclose(first, second)
    assert np.isclose(first.mean(), 1.0)
    assert np.isfinite(first).all() and (first > 0).all()
    assert first.max() / first.min() <= 16.0 + 1e-12


@pytest.mark.parametrize(
    "model_id",
    [
        "B1_persistence_last_rate",
        "B3_profile_robust_trend",
        "B5_fixed_kalman",
        "M1_ridge",
        "M2_extra_trees",
    ],
)
def test_every_configured_model_fits_train_and_predicts_validation(
    model_context, model_id: str
) -> None:
    bundle, config, train, validation, development = model_context
    spec = deepcopy(next(item for item in config["models"] if item["model_id"] == model_id))
    if spec["family"] == "extra_trees":
        spec["parameters"]["n_estimators"] = 12
    model = build_model(
        spec,
        contract=bundle.feature_contract,
        random_seed=int(config["random_seed"]),
        weight_clip=(0.25, 4.0),
    )
    model.fit(train)
    prediction = model.predict(
        validation, history_frame=causal_feature_history(development)
    )
    assert prediction.shape == (len(validation.frame),)
    assert np.isfinite(prediction).all()
    state = model.state_dict()
    assert state["model_id"] == model_id
    assert state["parameter_count"] >= 0


def test_model_fit_rejects_validation_provenance(model_context) -> None:
    bundle, config, _, validation, _ = model_context
    spec = next(item for item in config["models"] if item["family"] == "ridge")
    model = build_model(
        spec,
        contract=bundle.feature_contract,
        random_seed=int(config["random_seed"]),
        weight_clip=(0.25, 4.0),
    )
    with pytest.raises(LeakageViolation):
        model.fit(validation)


def test_fixed_kalman_is_invariant_to_future_history_rows(model_context) -> None:
    _, config, train, validation, development = model_context
    spec = next(item for item in config["models"] if item["family"] == "fixed_kalman")
    model = FixedKalmanRate(
        model_id=spec["model_id"], parameters=spec["parameters"]
    ).fit(train)
    sample = derived_dataset(
        development,
        validation.sample_ids[:8],
        split="validation",
        label="kalman_causality_test",
    )
    history = causal_feature_history(development)
    original = model.predict(sample, history_frame=history)
    future = history.iloc[[0]].copy()
    future["sample_id"] = "future-injected"
    future["point_id"] = sample.frame.iloc[0]["point_id"]
    future["current_date"] = sample.frame["current_date"].max() + np.timedelta64(3650, "D")
    future["last_settlement_mm"] = 1_000_000_000.0
    changed = model.predict(
        sample, history_frame=pd.concat([history, future], ignore_index=True)
    )
    np.testing.assert_allclose(original, changed)


def test_learned_estimators_never_receive_identifier_columns(model_context) -> None:
    bundle, config, train, _, _ = model_context
    forbidden = {"sample_id", "point_id", "profile_id", "current_campaign_id", "target_campaign_id"}
    for family in ("ridge", "extra_trees"):
        spec = deepcopy(next(item for item in config["models"] if item["family"] == family))
        if family == "extra_trees":
            spec["parameters"]["n_estimators"] = 5
        model = build_model(
            spec,
            contract=bundle.feature_contract,
            random_seed=int(config["random_seed"]),
            weight_clip=(0.25, 4.0),
        ).fit(train)
        output_names = set(model.preprocessor_.feature_names_out_)
        assert forbidden.isdisjoint(output_names)
