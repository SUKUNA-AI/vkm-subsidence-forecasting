from __future__ import annotations

from pathlib import Path

import pytest

from skru1.data_contracts import ContractViolation, load_canonical_bundle
from skru1.gate_c import load_gate_c_config
from skru1.sequences import (
    SequenceFitContext,
    TrainOnlySequencePreprocessor,
    assert_early_stopping_scope,
    assert_gate_c_data_boundary,
    assert_network_feature_columns,
    build_sequence_bundle,
)
from skru1.splits import sample_id_list_sha256


ROOT = Path(__file__).resolve().parents[1]


def test_gate_c_data_boundary_rejects_validation_test_and_aliases() -> None:
    assert_gate_c_data_boundary("t1_v1/train")
    for source in ("t1_v1/validation", "t1_v1/test", "development", "synthetic_holdout"):
        with pytest.raises(ContractViolation):
            assert_gate_c_data_boundary(source)


def test_gate_c_early_stopping_means_inner_train_only_validation() -> None:
    _, config = load_gate_c_config(ROOT)
    expected = "inner_rolling_validation_within_t1_v1_train"
    assert_early_stopping_scope(expected, config)
    for unsafe in ("validation", "t1_v1/validation", "outer_validation", "test"):
        with pytest.raises(ContractViolation):
            assert_early_stopping_scope(unsafe, config)


def test_gate_c_identifier_and_private_fields_are_rejected() -> None:
    _, config = load_gate_c_config(ROOT)
    canonical = load_canonical_bundle(ROOT)
    safe = config["sequence_contract"]["sequence_feature_channels"]
    assert_network_feature_columns(safe, config=config, feature_contract=canonical.feature_contract)
    for forbidden in (
        "sample_id",
        "point_id",
        "profile_id",
        "zone_id",
        "campaign_id",
        "true_velocity_mm_y",
        "hidden_event",
        "process_family",
        "regime_stage",
    ):
        with pytest.raises(ContractViolation):
            assert_network_feature_columns(
                [*safe, forbidden],
                config=config,
                feature_contract=canonical.feature_contract,
            )


def test_sequence_preprocessor_fits_only_train_role() -> None:
    _, config = load_gate_c_config(ROOT)
    canonical = load_canonical_bundle(ROOT)
    sequence = build_sequence_bundle(ROOT, config, canonical)
    selected_ids = tuple(sequence.manifest["sample_id"].astype(str).head(8))
    rows = sequence.rows.loc[sequence.rows["sample_id"].astype(str).isin(selected_ids)].copy()
    context = SequenceFitContext(
        fold_id="fixture_inner_train",
        split="t1_v1/train",
        role="train",
        sample_ids_sha256=sample_id_list_sha256(selected_ids),
        feature_contract_sha256=canonical.feature_contract.source_sha256,
        seed=42117,
    )
    preprocessor = TrainOnlySequencePreprocessor(
        numeric_fields=(
            "last_settlement_mm",
            "last_rate_mm_y",
            "current_standard_uncertainty_mm",
            "days_since_previous_observation",
            "missing_campaigns_since_previous",
        ),
        categorical_fields=("current_campaign_type",),
    ).fit(rows, context=context, config=config, feature_contract=canonical.feature_contract)
    transformed = preprocessor.transform(rows)
    assert transformed.shape == (len(rows), 6)
    assert transformed.notna().all().all()
    assert preprocessor.state_dict()["fit_context"]["role"] == "train"

    validation_context = SequenceFitContext(
        fold_id="fixture_validation",
        split="t1_v1/train",
        role="validation",
        sample_ids_sha256=sample_id_list_sha256(selected_ids),
        feature_contract_sha256=canonical.feature_contract.source_sha256,
        seed=42117,
    )
    with pytest.raises(ContractViolation):
        TrainOnlySequencePreprocessor(
            numeric_fields=("last_settlement_mm",),
            categorical_fields=(),
        ).fit(rows, context=validation_context, config=config, feature_contract=canonical.feature_contract)


def test_gate_c_runner_has_no_manifest_cli_escape_hatch() -> None:
    runner = (ROOT / "scripts" / "run_gate_c.py").read_text(encoding="utf-8")
    assert "--manifest" not in runner
    assert "--validation" not in runner
    assert "--test" not in runner
    assert 'load_split_dataset("t1", "validation"' not in runner
    assert 'load_split_dataset("t1", "test"' not in runner


def test_gate_c_registry_and_lock_are_local_train_from_scratch() -> None:
    _, config = load_gate_c_config(ROOT)
    specs = list(config["architecture_registry"])
    lock = (ROOT / config["environment"]["lock"]).read_text(encoding="utf-8").lower()
    dependency_lines = [line.strip() for line in lock.splitlines() if line.strip() and not line.startswith("#")]
    assert all(str(spec["model_id"]).startswith("C") for spec in specs)
    assert all("checkpoint" not in spec and "weights" not in spec for spec in specs)
    assert all("http://" not in line and "https://" not in line for line in dependency_lines)
    assert config["environment"]["external_pretrained_models_allowed"] is False
