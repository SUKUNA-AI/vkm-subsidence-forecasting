"""Target-separated, allowlisted model representation of the frozen v2 release.

No evaluator import or loader is used here. Even integrity checking is restricted
to model payloads; full-release verification belongs to acceptance/scorer QA.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
import json

import numpy as np
import pandas as pd

from .scenario_boundary_v2_1 import (
    DATASET, DATA_SHA, DATA_DIRECTORY, CONSTRAINTS_SHA, model_worker_scope,
    verified_manifest, verified_payload,
)
from .splits import ManifestDataset, SplitProvenance, sample_id_list_sha256

FEATURES = (
    "n_history", "last_settlement_mm", "last_rate_mm_y", "mean_last_3_rates_mm_y",
    "std_last_3_rates_mm_y", "recent_acceleration_mm_y2", "current_standard_uncertainty_mm",
    "days_since_previous_observation", "forecast_horizon_days", "current_campaign_type",
    "missing_campaigns_since_previous", "profile_mean_settlement_mm", "profile_mean_rate_mm_y",
    "profile_rate_std_mm_y", "profile_n_observed", "target_campaign_type",
)
METADATA = (
    "sample_id", "scenario_id", "point_id", "base_point_id", "profile_id", "base_profile_id",
    "current_campaign_id", "current_date", "target_campaign_id", "target_date",
)
HISTORY_COLUMNS = (
    "history_id", "scenario_id", "point_id", "base_point_id", "profile_id", "base_profile_id",
    "campaign_id", "current_date", "last_settlement_mm", "last_rate_mm_y",
    "current_standard_uncertainty_mm", "recent_acceleration_mm_y2", "std_last_3_rates_mm_y",
    "missing_campaigns_since_previous", "n_history", "provenance",
)
COUNTS = {"train": 122047, "calibration": 35148, "evaluation": 133833, "excluded": 30738}
TARGET = "observed_rate_mm_y"


def exact_columns(frame, columns):
    if tuple(frame.columns) != tuple(columns):
        raise ValueError("Columns/order differ from the v2 allowlist")


def estimator_matrix(dataset: ManifestDataset) -> pd.DataFrame:
    if tuple(dataset.feature_columns) != FEATURES:
        raise ValueError("Estimator feature allowlist must equal the frozen 16 features")
    allowed = set(METADATA + FEATURES)
    if dataset.provenance.split == "train":
        allowed.add(TARGET)
    if set(dataset.frame) - allowed:
        raise ValueError("Unexpected metadata/target in model frame")
    return dataset.frame.loc[:, FEATURES].copy()


def make_dataset(frame, role, manifest_hash, *, candidate_id=None):
    ids = tuple(frame.sample_id)
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate origins")
    return ManifestDataset(frame.reset_index(drop=True), FEATURES, SplitProvenance(
        task="scenario_v2_t1", split=role, version=DATASET,
        manifest_path=Path(DATA_DIRECTORY) / "split_assignments.csv.gz",
        manifest_file_sha256=manifest_hash, sample_ids_sha256=sample_id_list_sha256(ids),
        row_count=len(ids), test_authorized=False, candidate_id=candidate_id,
    ))


@dataclass(frozen=True)
class CausalHistory:
    """Separate lookup object. for_origins only returns own-point past observations."""
    frame: pd.DataFrame

    def for_origins(self, origins: pd.DataFrame) -> pd.DataFrame:
        cutoffs = origins.groupby("point_id", sort=False).current_date.max()
        maximum = self.frame.point_id.map(cutoffs)
        return self.frame.loc[self.frame.current_date.le(maximum)].copy()

    def for_origin(self, origin) -> pd.DataFrame:
        return self.frame.loc[self.frame.point_id.eq(origin.point_id)
                              & self.frame.current_date.le(origin.current_date)].copy()


@dataclass(frozen=True)
class ModelDataBundle:
    frames: pd.DataFrame  # all origins, always target-free
    train: ManifestDataset
    calibration: ManifestDataset
    evaluation: ManifestDataset
    history: CausalHistory
    windows: pd.DataFrame
    roles: pd.Series
    provenance: object  # sanitized identity only, not a generator manifest/catalog


@dataclass(frozen=True)
class CalibrationTargetStore:
    targets: pd.DataFrame

    def for_ids(self, sample_ids) -> pd.DataFrame:
        ids = list(sample_ids)
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate calibration IDs")
        return self.targets.set_index("sample_id").loc[ids].reset_index()


def _read(root, manifest, name, **kwargs):
    return pd.read_csv(verified_payload(root, manifest, name), **kwargs)


def _checked_targets(targets, expected_ids):
    exact_columns(targets, ("sample_id", TARGET))
    if targets.sample_id.duplicated().any() or set(targets.sample_id) != set(expected_ids):
        raise ValueError("Observed target IDs do not exactly match their outer role")
    if not np.isfinite(targets[TARGET].to_numpy(float)).all():
        raise ValueError("Missing/nonfinite observed target")


def load_model_data(root: str | Path) -> ModelDataBundle:
    root = Path(root).resolve()
    with model_worker_scope(root):
        manifest = verified_manifest(root)
        contract_path = verified_payload(root, manifest, "feature_contract.json")
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        if tuple(contract["features"]) != FEATURES or tuple(contract["metadata"]) != METADATA:
            raise ValueError("Feature contract mismatch")
        frame = _read(root, manifest, "model_features.csv.gz", low_memory=False)
        exact_columns(frame, METADATA + FEATURES)
        if len(frame) != sum(COUNTS.values()) or frame.sample_id.duplicated().any():
            raise ValueError("Invalid origin cardinality")
        for column in ("current_date", "target_date"):
            frame[column] = pd.to_datetime(frame[column], errors="raise")
        if not frame.target_date.gt(frame.current_date).all():
            raise ValueError("Nonpositive target horizon")
        assignments = _read(root, manifest, "split_assignments.csv.gz",
                            usecols=["sample_id", "model_role"])
        if not assignments.sample_id.equals(frame.sample_id):
            raise ValueError("Origin/assignment keys/order mismatch")
        if assignments.model_role.value_counts().to_dict() != COUNTS:
            raise ValueError("Frozen outer roles changed")
        roles = assignments.set_index("sample_id").model_role
        train_targets = _read(root, manifest, "targets/train_observed.csv.gz")
        _checked_targets(train_targets, roles.index[roles.eq("train")])
        frames = {role: frame.loc[frame.sample_id.map(roles).eq(role)].copy()
                  for role in ("train", "calibration", "evaluation")}
        frames["train"] = frames["train"].merge(train_targets, on="sample_id",
                                                validate="one_to_one", sort=False)
        history = _read(root, manifest, "causal_history.csv.gz", low_memory=False)
        exact_columns(history, HISTORY_COLUMNS)
        if history.history_id.duplicated().any() or history.duplicated(["point_id", "current_date"]).any():
            raise ValueError("Duplicate history keys")
        history["current_date"] = pd.to_datetime(history.current_date, errors="raise")
        windows = _read(root, manifest, "sequence_windows.csv.gz")
        if not windows.sample_id.equals(frame.sample_id):
            raise ValueError("Window/origin identity mismatch")
        windows["current_date"] = pd.to_datetime(windows.current_date, errors="raise")
        outputs = {r["path"]: r["sha256"] for r in manifest["outputs"]}
        datasets = {role: make_dataset(rows, role, outputs["split_assignments.csv.gz"])
                    for role, rows in frames.items()}
        for dataset in datasets.values():
            estimator_matrix(dataset)
        return ModelDataBundle(
            frame, datasets["train"], datasets["calibration"], datasets["evaluation"],
            CausalHistory(history), windows, roles,
            MappingProxyType({"dataset_id": DATASET, "dataset_sha256": DATA_SHA,
                              "constraints_sha256": CONSTRAINTS_SHA,
                              "feature_schema_sha256": outputs["feature_contract.json"],
                              "split_sha256": outputs["split_assignments.csv.gz"]}),
        )


def load_calibration_targets(root: str | Path) -> CalibrationTargetStore:
    """Downstream calibration-only API. Denied inside model_worker_scope."""
    root = Path(root).resolve()
    manifest = verified_manifest(root)
    targets = _read(root, manifest, "targets/calibration_observed.csv.gz")
    roles = _read(root, manifest, "split_assignments.csv.gz", usecols=["sample_id", "model_role"])
    _checked_targets(targets, roles.loc[roles.model_role.eq("calibration"), "sample_id"])
    return CalibrationTargetStore(targets)
