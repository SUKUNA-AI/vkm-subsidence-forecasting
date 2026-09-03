from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from skru1.data_contracts import ContractViolation
from skru1.gate_c1_interfaces import (
    C1FitContext,
    C1SequencePreprocessor,
    C1_SEEDS,
    SequencePredictionBundle,
    SequenceTargetScaler,
    ordered_sample_hash,
    target_values_sha256,
)


ROOT = Path(__file__).resolve().parents[1]


def _fixture() -> tuple[pd.DataFrame, tuple[str, ...], C1FitContext]:
    rows = pd.read_csv(ROOT / "artifacts/splits/t1_train_gate_c_v1/sequence_rows.csv")
    ids = tuple(rows["sample_id"].astype(str).drop_duplicates().iloc[:8])
    targets = np.arange(1, len(ids) + 1, dtype=float)
    context = C1FitContext(
        fold_id="fixture_inner",
        role="train",
        source_split="t1_v1/train",
        sample_ids_sha256=ordered_sample_hash(ids),
        sequence_pairs_sha256="1" * 64,
        target_sha256=target_values_sha256(ids, targets),
        seed=C1_SEEDS[0],
    )
    return rows, ids, context


def test_train_only_preprocessor_produces_zero_padding_and_no_identifiers() -> None:
    rows, ids, context = _fixture()
    preprocessor = C1SequencePreprocessor().fit(rows, sample_ids=ids, context=context)
    batch = preprocessor.transform(rows, sample_ids=ids)
    assert batch.x.shape[:2] == (8, 16)
    assert np.equal(batch.x[batch.padding_mask.astype(bool)], 0.0).all()
    assert not {
        "sample_id",
        "point_id",
        "profile_id",
        "zone_id",
        "campaign_id",
    } & set(batch.feature_names)
    assert batch.sample_ids == ids
    assert batch.lengths.min() >= 3


def test_unknown_campaign_category_uses_unknown_one_hot_bucket() -> None:
    rows, ids, context = _fixture()
    preprocessor = C1SequencePreprocessor().fit(rows, sample_ids=ids[:-1], context=C1FitContext(
        fold_id=context.fold_id,
        role="train",
        source_split=context.source_split,
        sample_ids_sha256=ordered_sample_hash(ids[:-1]),
        sequence_pairs_sha256=context.sequence_pairs_sha256,
        target_sha256=target_values_sha256(ids[:-1], np.arange(1, len(ids), dtype=float)),
        seed=context.seed,
    ))
    altered = rows.copy()
    mask = altered["sample_id"].astype(str).eq(ids[-1]) & altered["padding_mask"].eq(0)
    altered.loc[mask, "current_campaign_type"] = "NEVER_SEEN_IN_TRAIN"
    batch = preprocessor.transform(altered, sample_ids=(ids[-1],))
    valid = batch.observation_mask[0].astype(bool)
    unknown_index = batch.feature_names.index("current_campaign_type::<UNKNOWN>")
    assert np.equal(batch.x[0, valid, unknown_index], 1.0).all()


def test_preprocessor_and_target_scaler_fail_closed_on_non_train_provenance() -> None:
    rows, ids, context = _fixture()
    bad = C1FitContext(**{**context.__dict__, "role": "validation"})
    with pytest.raises(ContractViolation):
        C1SequencePreprocessor().fit(rows, sample_ids=ids, context=bad)
    with pytest.raises(ContractViolation):
        SequenceTargetScaler().fit(np.arange(len(ids), dtype=float), context=bad)


def test_target_scaler_roundtrip_distribution_scale_and_zero_variance_guard() -> None:
    _, ids, context = _fixture()
    values = np.linspace(-2.0, 5.0, len(ids))
    scaler = SequenceTargetScaler().fit(values, context=context)
    assert np.allclose(scaler.inverse_transform(scaler.transform(values)), values)
    assert np.allclose(scaler.inverse_scale(np.ones(3)), np.repeat(scaler.scale_, 3))
    with pytest.raises(ContractViolation):
        SequenceTargetScaler().fit(np.ones(len(ids)), context=context)


def test_worker_prediction_bundle_forbids_truth_and_requires_all_seeds() -> None:
    ids = ("a", "b")
    base = []
    for seed in C1_SEEDS:
        for sample_id in ids:
            base.append(
                {
                    "model_id": "C01_compact_gru",
                    "family": "gated_recurrent_unit",
                    "fold_id": "rolling_origin_fixture",
                    "seed": seed,
                    "sample_id": sample_id,
                    "y_pred": 1.0,
                    "environment_id": "gate_c_torch",
                    "model_spec_sha256": "1" * 64,
                    "config_sha256": "2" * 64,
                    "code_sha256": "3" * 64,
                    "environment_sha256": "4" * 64,
                    "expected_sample_ids_sha256": ordered_sample_hash(ids),
                    "selected_parameter_sha256": "5" * 64,
                    "selected_parameter_json": "{}",
                    "epoch_count": 10,
                    "parameter_count": 100,
                    "fit_seconds": 1.0,
                    "inference_seconds": 0.1,
                    "peak_ram_mb": 10.0,
                    "peak_vram_mb": 5.0,
                    "aggregation": "single_seed",
                }
            )
    frame = pd.DataFrame(base)
    SequencePredictionBundle.validate(
        frame,
        expected_sample_ids=ids,
        expected_model_id="C01_compact_gru",
        expected_fold_id="rolling_origin_fixture",
    )
    with pytest.raises(ContractViolation):
        SequencePredictionBundle.validate(
            frame.assign(y_true=1.0),
            expected_sample_ids=ids,
            expected_model_id="C01_compact_gru",
            expected_fold_id="rolling_origin_fixture",
        )
    with pytest.raises(ContractViolation):
        SequencePredictionBundle.validate(
            frame.loc[frame["seed"].ne(C1_SEEDS[-1])],
            expected_sample_ids=ids,
            expected_model_id="C01_compact_gru",
            expected_fold_id="rolling_origin_fixture",
        )
