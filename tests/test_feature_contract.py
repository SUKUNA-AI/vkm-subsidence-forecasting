from __future__ import annotations

from pathlib import Path

import pytest

from skru1.data_contracts import ContractViolation, load_canonical_bundle
from skru1.leakage import (
    LeakageViolation,
    assert_estimator_feature_safety,
    assert_feature_table_has_no_forbidden_fields,
    forbidden_field_reason,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bundle():
    return load_canonical_bundle(ROOT)


def test_formal_feature_contract_covers_canonical_table(bundle) -> None:
    bundle.feature_contract.assert_covers_feature_table(bundle.features.columns)
    assert set(bundle.feature_contract.allowed_features).issubset(bundle.features.columns)


def test_estimator_uses_exact_allowlist(bundle) -> None:
    allowed = bundle.feature_contract.allowed_features
    assert_estimator_feature_safety(allowed, bundle.feature_contract)
    with pytest.raises(ContractViolation):
        bundle.feature_contract.assert_exact_estimator_columns([*allowed, "point_id"])
    with pytest.raises(ContractViolation):
        bundle.feature_contract.assert_exact_estimator_columns(allowed[:-1])


def test_identifiers_and_campaign_ids_are_not_allowed(bundle) -> None:
    allowed = set(bundle.feature_contract.allowed_features)
    for field in ("sample_id", "point_id", "profile_id", "current_campaign_id", "target_campaign_id"):
        assert field not in allowed
        assert forbidden_field_reason(field) is not None


def test_hidden_private_and_generator_fields_are_absent(bundle) -> None:
    assert_feature_table_has_no_forbidden_fields(bundle.features, bundle.feature_contract)
    for field in (
        "true_velocity_mm_y",
        "hidden_true_rate_mm_y",
        "event_onset_date",
        "process_family",
        "regime_stage",
        "generator_seed",
    ):
        assert field not in bundle.features.columns
        assert forbidden_field_reason(field) is not None


def test_only_frozen_plan_future_fields_are_allowed(bundle) -> None:
    allowed = set(bundle.feature_contract.allowed_features)
    assert "target_campaign_type" in allowed
    assert "forecast_horizon_days" in allowed
    assert forbidden_field_reason("target_campaign_type") is None
    assert forbidden_field_reason("forecast_horizon_days") is None
    assert forbidden_field_reason("target_observed_settlement_mm") is not None
    assert forbidden_field_reason("next_observation_uncertainty_mm") is not None


def test_historical_next_cycle_files_are_not_canonical(bundle) -> None:
    canonical = set(bundle.paths.values())
    assert all(path not in canonical for path in bundle.historical_paths.values())
    assert all("next_cycle" in path.name for path in bundle.historical_paths.values())
