"""Representation acceptance only. No estimator fit, predict, or forward calls."""
from __future__ import annotations
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import pytest

from skru1.scenario_adapter_v2 import (
    FEATURES, METADATA, COUNTS, TARGET, load_model_data, load_calibration_targets,
    estimator_matrix, CausalHistory,
)
from skru1.scenario_boundary_v2 import (
    DATA_SHA, CONSTRAINTS_SHA, DATA_DIRECTORY, MODEL_FILES, file_sha256, model_worker_scope,
)
from skru1.scenario_scorer_v2 import load_evaluator_truth
from skru1.scenario_sequences_v2 import SequenceTensorizer, CHANNELS
from skru1.scenario_splits_v2 import (
    group_manifest, build_folds, validate_fold, fold_datasets, allowed_context, AGGREGATES,
)
from skru1.scenario_preprocessing_v2 import FoldPreprocessor
from skru1.scenario_worker_v2 import run_origin_worker, origin_inputs

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bundle():
    return load_model_data(ROOT)


@pytest.fixture(scope="module")
def groups(bundle):
    return group_manifest(ROOT, bundle)


@pytest.fixture(scope="module")
def folds(bundle, groups):
    config = json.loads((ROOT / "configs/scenario_adapter_v2.json").read_text(encoding="utf-8"))
    return build_folds(bundle, groups, config)


@pytest.fixture(scope="module")
def tensorizer(bundle):
    return SequenceTensorizer(bundle)


def test_frozen_identity_and_all_release_bytes():
    d = ROOT / DATA_DIRECTORY
    assert file_sha256(d / "manifest.json") == DATA_SHA
    assert file_sha256(ROOT / "artifacts/reconstruction/scenario_constraints_v2/manifest.json") == CONSTRAINTS_SHA
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    for record in manifest["outputs"]:
        assert file_sha256(d / record["path"]) == record["sha256"]


def test_model_adapter_reads_only_allowlisted_payloads(monkeypatch):
    # Install observer, not a replacement for the production audit guard.
    opened = []
    active = [True]
    def audit(event, args):
        if active[0] and event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.is_relative_to(ROOT / "data"):
                opened.append(path.relative_to(ROOT / DATA_DIRECTORY).as_posix())
    sys.addaudithook(audit)
    try:
        load_model_data(ROOT)
    finally:
        active[0] = False
    assert set(opened) == MODEL_FILES


@pytest.mark.parametrize("relative", [
    "data/scenario_simulation_v2/evaluator/next_planned_truth.csv.gz",
    "data/scenario_simulation_v2/evaluator/campaign_truth.csv.gz",
    "data/scenario_simulation_v2/scenario_catalog.csv",
    "data/scenario_simulation_v2/targets/calibration_observed.csv.gz",
    "inputs/holdout_candidates/t1_final_v3/never_open_labels.csv",
    "SKRU1_ACTUAL_DATA_TABLES_v1/never_open.csv",
    "data/scenario_simulation_v1/next_planned_samples.csv.gz",
])
def test_worker_denies_prohibited_file_opens(relative, bundle):
    # Includes os.open and a fresh Python thread, not merely Path.read_text.
    def attempt(inputs):
        assert TARGET not in inputs.origin.frame
        with pytest.raises(PermissionError):
            (ROOT / relative).open("rb")
        with pytest.raises(PermissionError):
            os.open(ROOT / relative, os.O_RDONLY)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(PermissionError):
                executor.submit((ROOT / relative).read_bytes).result()
    run_origin_worker(ROOT, bundle, bundle.frames.sample_id.iloc[0], attempt)


def test_scorer_and_calibration_loaders_denied_in_worker():
    with model_worker_scope(ROOT):
        with pytest.raises(PermissionError):
            load_evaluator_truth(ROOT)
        with pytest.raises(PermissionError):
            load_calibration_targets(ROOT)


def test_exact_features_roles_and_targets(bundle):
    assert bundle.roles.value_counts().to_dict() == COUNTS
    assert len(bundle.frames) == 321766
    assert tuple(bundle.frames) == METADATA + FEATURES
    assert bundle.frames.sample_id.is_unique
    assert set(bundle.__dict__) == {"frames", "train", "calibration", "evaluation", "history", "windows", "roles", "provenance"}
    for dataset in (bundle.train, bundle.calibration, bundle.evaluation):
        assert tuple(estimator_matrix(dataset)) == FEATURES
    for dataset in (bundle.calibration, bundle.evaluation):
        assert TARGET not in dataset.frame
    targets = pd.read_csv(ROOT / DATA_DIRECTORY / "targets/train_observed.csv.gz").set_index("sample_id")
    expected = targets.loc[list(bundle.train.sample_ids), TARGET]
    np.testing.assert_array_equal(bundle.train.frame[TARGET], expected)
    calibration = load_calibration_targets(ROOT)
    assert set(calibration.targets.sample_id) == set(bundle.calibration.sample_ids)
    assert calibration.targets[TARGET].notna().all()


@pytest.mark.parametrize("name", ["scenario_id", "zone_id", "dynamic_mechanism", "latent_rate_mm_y", TARGET])
def test_features_cannot_expand(bundle, name):
    with pytest.raises(ValueError):
        estimator_matrix(replace(bundle.train, feature_columns=FEATURES+(name,)))


def test_origin_history_cutoff_object(bundle):
    for sid in bundle.frames.sample_id.iloc[::977]:
        inputs = origin_inputs(bundle, sid)
        origin = inputs.origin.frame.iloc[0]
        assert inputs.history.current_date.le(origin.current_date).all()
        assert inputs.history.point_id.eq(origin.point_id).all()
        last = inputs.history.sort_values("current_date").iloc[-1]
        assert last.last_settlement_mm == origin.last_settlement_mm


def test_all_runtime_windows_against_independent_token_gather(bundle, tensorizer):
    # Expected rows use serialized IDs directly, independent of tensorizer arrays.
    h = bundle.history.frame.sort_values(["point_id", "current_date"]).copy()
    h["days_since_previous_observation"] = h.groupby("point_id").current_date.diff().dt.days
    lookup = h.set_index("history_id")
    windows = bundle.windows.set_index("sample_id")
    origin_index = bundle.frames.set_index("sample_id")
    total = 0
    for batch in tensorizer.batches(bundle.frames.sample_id, batch_size=4096):
        selected = windows.loc[list(batch.sample_ids)]
        ids_per_window = [json.loads(value) for value in selected.history_ids_json]
        flattened = [item for ids in ids_per_window for item in ids]
        expected = lookup.loc[flattened, list(CHANNELS)].to_numpy(float)
        np.testing.assert_allclose(batch.values[batch.observation_mask], expected, rtol=0, atol=0, equal_nan=True)
        assert np.array_equal(batch.observation_mask, ~batch.padding_mask)
        assert np.array_equal(batch.lengths, selected.sequence_length)
        assert (batch.lengths <= 16).all()
        assert np.array_equal(batch.padding_mask.sum(axis=1), 16-batch.lengths)
        assert not batch.padding_mask[:, -1].any()
        assert np.all(np.diff(batch.padding_mask.astype(int), axis=1) <= 0)
        assert (batch.values[batch.padding_mask] == 0).all()
        assert np.array_equal(batch.value_valid_mask, np.isfinite(batch.values) & batch.observation_mask[:, :, None])
        assert np.array_equal(batch.missing_campaign_mask, (batch.values[:, :, 4] > 0) & batch.observation_mask)
        origins = origin_index.loc[list(batch.sample_ids)]
        np.testing.assert_array_equal(batch.values[:, -1, 0], origins.last_settlement_mm)
        np.testing.assert_array_equal(batch.values[:, -1, 3], origins.days_since_previous_observation)
        total += len(batch.sample_ids)
    assert total == 321766


def test_runtime_is_deterministic_and_future_invariant(bundle, tensorizer):
    ids = tuple(bundle.train.sample_ids[:128])
    before = tensorizer.raw(ids)
    after = tensorizer.raw(ids)
    assert sha256(before.values.tobytes()).digest() == sha256(after.values.tobytes()).digest()
    cutoff = bundle.frames.set_index("sample_id").loc[list(ids), "current_date"].max()
    changed = bundle.history.frame.copy()
    changed.loc[changed.current_date.gt(cutoff), "last_settlement_mm"] = 1e12
    after = SequenceTensorizer(replace(bundle, history=CausalHistory(changed))).raw(ids)
    np.testing.assert_array_equal(before.values, after.values)


def test_temporal_world_folds(bundle, groups, folds):
    assert len(folds) == 21
    assert [f.kind for f in folds[:3]] == ["temporal_world"]*3
    g = groups.set_index("sample_id")
    for fold in folds:
        validate_fold(fold, bundle, groups)
        assert bundle.roles.loc[list(fold.fit_ids)+list(fold.validation_ids)].eq("train").all()
    for fold in folds[:3]:
        assert not set(g.loc[list(fold.fit_ids), "latent_world_id"]) & set(g.loc[list(fold.validation_ids), "latent_world_id"])
    broken = replace(folds[0], fit_ids=folds[0].fit_ids+(bundle.calibration.sample_ids[0],))
    with pytest.raises(ValueError):
        validate_fold(broken, bundle, groups)


@pytest.mark.parametrize("kind", ["profile", "zone"])
def test_spatial_context_and_replica_isolation(bundle, groups, folds, kind):
    g = groups.set_index("sample_id")
    for fold in [f for f in folds if f.kind == kind]:
        fit, val = fold_datasets(bundle, groups, fold)
        col = "base_profile_id" if kind == "profile" else "zone_id"
        assert not g.loc[list(fold.fit_ids), col].eq(fold.held_group).any()
        context = allowed_context(bundle, groups, fold)
        held_points = set(groups.loc[groups[col].eq(fold.held_group), "base_point_id"])
        assert not set(context.base_point_id) & held_points
        assert not set(bundle.history.for_origins(fit.frame).base_point_id) & held_points
        # Independent arithmetic for a deterministic selection from both sides.
        for frame in (fit.frame.iloc[::max(1, len(fit.frame)//19)], val.frame.iloc[::max(1, len(val.frame)//19)]):
            for row in frame.itertuples(index=False):
                c = context.loc[context.profile_id.eq(row.profile_id) & context.current_date.eq(row.current_date)]
                expected = [c.last_settlement_mm.mean(), c.last_rate_mm_y.mean(), c.last_rate_mm_y.std(ddof=0), len(c)]
                actual = [getattr(row, key) for key in AGGREGATES]
                np.testing.assert_allclose(actual, expected, equal_nan=True, atol=1e-8)
        # Fit/validation contain only metadata, allowlisted predictors, and fit target.
        estimator_matrix(fit)
        estimator_matrix(val)


def test_holdout_observations_cannot_affect_spatial_fit_aggregates(bundle, groups, folds):
    fold = next(f for f in folds if f.kind == "zone" and f.held_group == "GEO_NE")
    fit, val = fold_datasets(bundle, groups, fold)
    held = set(groups.loc[groups.zone_id.eq(fold.held_group), "base_point_id"])
    changed = bundle.history.frame.copy()
    changed.loc[changed.base_point_id.isin(held), ["last_settlement_mm", "last_rate_mm_y"]] = 1e12
    fit2, val2 = fold_datasets(replace(bundle, history=CausalHistory(changed)), groups, fold)
    pd.testing.assert_frame_equal(fit.frame, fit2.frame)
    pd.testing.assert_frame_equal(val.frame, val2.frame)


@pytest.mark.parametrize("kind", ["tabular", "sequence"])
def test_preprocessing_exact_fit_only(bundle, groups, folds, tensorizer, kind):
    fold = folds[0]
    pre = FoldPreprocessor(kind).fit(bundle, groups, fold, frozen_record=fold.record(), tensorizer=tensorizer)
    state = pre.state_dict()
    assert state["fold_id"] == fold.fold_id
    assert state["ordered_train_sample_sha256"] == fold.record()["fit_sample_ids_sha256"]
    assert len(state["feature_schema_sha256"]) == 64
    with pytest.raises(ValueError):
        pre.fit(bundle, groups, fold, frozen_record=fold.record(), tensorizer=tensorizer)
    with pytest.raises(ValueError):
        FoldPreprocessor(kind).fit(bundle, groups, fold, frozen_record=folds[1].record(), tensorizer=tensorizer)
    # Poison validation worlds and all future/calibration/evaluation values.
    train_worlds = set(groups.set_index("sample_id").loc[list(fold.fit_ids), "latent_world_id"])
    changed = bundle.history.frame.copy()
    worlds = changed.scenario_id.str.rsplit("-", n=1).str[0]
    max_fit_date = bundle.frames.set_index("sample_id").loc[list(fold.fit_ids), "current_date"].max()
    changed.loc[~worlds.isin(train_worlds) | changed.current_date.gt(max_fit_date), "last_settlement_mm"] = 1e12
    poisoned = replace(bundle, history=CausalHistory(changed))
    pre2 = FoldPreprocessor(kind).fit(poisoned, groups, fold, frozen_record=fold.record(), tensorizer=SequenceTensorizer(poisoned))
    assert pre2.state_dict() == state
    if kind == "sequence":
        raw = tensorizer.raw(fold.validation_ids[:32])
        result = pre.transform_sequence(raw)
        assert result.values.dtype == np.float32 and np.isfinite(result.values).all()
        assert (result.values[result.padding_mask] == 0).all()
    else:
        _, val = fold_datasets(bundle, groups, fold)
        result = pre.transform_tabular(val)
        assert result.shape == (len(fold.validation_ids), 16)
        assert result.dtype == np.float32 and np.isfinite(result).all()


def test_model_interface_construction_only(bundle):
    from skru1.baselines import PersistenceLastRate, FixedKalmanRate
    from skru1.adaptive_kalman import AdaptiveKalmanRate, prepare_kalman_history
    from skru1.imm_kalman import TwoRegimeIMMRate
    from skru1.robust_imm import RobustInnovationIMMRate
    import inspect
    for cls in (PersistenceLastRate, FixedKalmanRate, AdaptiveKalmanRate, TwoRegimeIMMRate, RobustInnovationIMMRate):
        model = cls(model_id="interface_only", parameters={})
        assert model.fallback_rate_ is None
        assert "history_frame" in inspect.signature(model.predict).parameters
    inputs = origin_inputs(bundle, bundle.frames.sample_id.iloc[0])
    prepared = prepare_kalman_history(inputs.history)
    assert prepared.source_rows == len(inputs.history)
    assert len(prepared.points) == 1
