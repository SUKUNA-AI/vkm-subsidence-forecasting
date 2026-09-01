from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from skru1.data_contracts import load_canonical_bundle, sha256_file
from skru1.gate_c import load_gate_c_config
from skru1.sequences import (
    assert_network_feature_columns,
    build_sequence_bundle,
    sequence_sha256_from_rows,
    validate_sequence_bundle,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def gate_c_sequence():
    _, config = load_gate_c_config(ROOT)
    canonical = load_canonical_bundle(ROOT)
    sequence = build_sequence_bundle(ROOT, config, canonical)
    return config, canonical, sequence


def test_gate_c_sequence_exact_origin_and_tensor_geometry(gate_c_sequence) -> None:
    config, canonical, sequence = gate_c_sequence
    proof = validate_sequence_bundle(sequence, config=config, canonical=canonical)
    assert proof["status"] == "PASS"
    assert proof["origins"] == 911
    assert proof["normalized_rows"] == 911 * 16
    assert proof["points"] == 98
    assert proof["profiles"] == 14
    assert proof["zones"] == 4
    assert proof["history_length_min"] == 3
    assert proof["history_length_max"] == 16


def test_gate_c_each_sequence_is_past_only_and_target_free(gate_c_sequence) -> None:
    _, _, sequence = gate_c_sequence
    manifest = sequence.manifest.set_index("sample_id", drop=False)
    for sample_id, rows in sequence.rows.groupby("sample_id", sort=False):
        actual = rows.loc[rows["padding_mask"].eq(0)].sort_values("sequence_position")
        current_date = pd.Timestamp(manifest.loc[sample_id, "current_date"])
        target_date = pd.Timestamp(manifest.loc[sample_id, "target_date"])
        observation_dates = pd.to_datetime(actual["observation_date"], errors="raise")
        target_observation = (
            str(manifest.loc[sample_id, "point_id"])
            + "::"
            + str(manifest.loc[sample_id, "target_campaign_id"])
        )
        assert observation_dates.max() == current_date
        assert observation_dates.le(current_date).all()
        assert current_date < target_date
        assert target_observation not in set(actual["observation_id"].astype(str))


def test_gate_c_padding_and_missing_masks_are_deterministic(gate_c_sequence) -> None:
    _, _, sequence = gate_c_sequence
    manifest = sequence.manifest.set_index("sample_id")
    for sample_id, rows in sequence.rows.groupby("sample_id", sort=False):
        rows = rows.sort_values("sequence_position")
        padding_count = int(manifest.loc[sample_id, "padding_count"])
        assert rows["padding_mask"].astype(int).tolist() == [1] * padding_count + [0] * (16 - padding_count)
        assert (rows["observation_mask"].astype(int) + rows["padding_mask"].astype(int)).eq(1).all()
        assert rows.loc[rows["padding_mask"].eq(1), "missing_campaign_mask"].eq(0).all()
        actual = rows.loc[rows["padding_mask"].eq(0)]
        expected_missing = actual["missing_campaigns_since_previous"].astype(int).gt(0).astype(int)
        assert actual["missing_campaign_mask"].astype(int).equals(expected_missing)


def test_gate_c_sequence_hashes_recompute(gate_c_sequence) -> None:
    config, _, sequence = gate_c_sequence
    fields = tuple(config["sequence_contract"]["sequence_feature_channels"])
    manifest = sequence.manifest.set_index("sample_id")
    for sample_id, rows in sequence.rows.groupby("sample_id", sort=False):
        assert sequence_sha256_from_rows(rows, fields) == manifest.loc[sample_id, "sequence_sha256"]


def test_gate_c_channels_are_formally_allowlisted(gate_c_sequence) -> None:
    config, canonical, _ = gate_c_sequence
    channels = assert_network_feature_columns(
        config["sequence_contract"]["sequence_feature_channels"],
        config=config,
        feature_contract=canonical.feature_contract,
    )
    assert channels == (
        "last_settlement_mm",
        "last_rate_mm_y",
        "current_standard_uncertainty_mm",
        "days_since_previous_observation",
        "missing_campaigns_since_previous",
        "current_campaign_type",
    )


def test_gate_c_frozen_contract_hashes_match_files() -> None:
    _, config = load_gate_c_config(ROOT)
    artifacts = config["artifacts"]
    contract_path = ROOT / artifacts["sequence_contract"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["sequence_manifest_sha256"] == sha256_file(ROOT / artifacts["sequence_manifest"])
    assert contract["sequence_rows_sha256"] == sha256_file(ROOT / artifacts["sequence_rows"])
    assert contract["fold_sequence_contracts_sha256"] == sha256_file(
        ROOT / artifacts["fold_sequence_contracts"]
    )
    assert contract["historical_validation_loaded"] is False
    assert contract["current_test_loaded"] is False
    assert contract["model_training_calls"] == 0
