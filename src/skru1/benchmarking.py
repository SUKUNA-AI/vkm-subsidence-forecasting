"""Frozen interfaces shared by the Gate B5/B6 benchmark runners.

The objects in this module deliberately contain no estimator imports.  External
workers can therefore validate a job and emit a prediction shard without
making the aggregator depend on XGBoost, NGBoost, PyTorch, or TabPFN.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .data_contracts import ContractViolation, FeatureContract
from .leakage import FORBIDDEN_IDENTIFIER_FIELDS, LeakageViolation, assert_estimator_feature_safety
from .splits import ManifestDataset, sample_id_list_sha256


DYNAMIC_CORE_17 = (
    "n_history",
    "last_settlement_mm",
    "last_rate_mm_y",
    "mean_last_3_rates_mm_y",
    "std_last_3_rates_mm_y",
    "recent_acceleration_mm_y2",
    "current_standard_uncertainty_mm",
    "days_since_previous_observation",
    "forecast_horizon_days",
    "missing_campaigns_since_previous",
    "profile_mean_settlement_mm",
    "profile_mean_rate_mm_y",
    "profile_rate_std_mm_y",
    "profile_n_observed",
    "chainage_normalized_profile",
    "current_campaign_type",
    "target_campaign_type",
)

FEATURE_VIEW_NAMES = ("SAFE_ALL", "DYNAMIC_CORE_17", "NATIVE_CATEGORICAL")
STRUCTURAL_KEYS = ("point_id", "profile_id", "zone_id", "current_date", "target_date")


def canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ModelSpec:
    """Serializable, frozen description of one benchmark model."""

    model_id: str
    family: str
    environment_id: str
    feature_view: str
    seed_policy: Mapping[str, Any]
    parameter_grid: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    fixed_parameters: Mapping[str, Any] = field(default_factory=dict)
    probabilistic_capabilities: tuple[str, ...] = field(default_factory=tuple)
    fit_context_requirements: tuple[str, ...] = field(default_factory=tuple)
    status: str = "ELIGIBLE"

    def __post_init__(self) -> None:
        if not self.model_id or not self.family or not self.environment_id:
            raise ContractViolation("ModelSpec identifiers cannot be empty")
        if self.feature_view not in FEATURE_VIEW_NAMES:
            raise ContractViolation(f"Unknown feature view: {self.feature_view}")
        seeds = tuple(self.seed_policy.get("seeds", ()))
        if not seeds:
            raise ContractViolation(f"ModelSpec {self.model_id} has no frozen seed policy")
        if len(seeds) != len(set(map(int, seeds))):
            raise ContractViolation(f"ModelSpec {self.model_id} has duplicate seeds")

    @property
    def spec_sha256(self) -> str:
        return canonical_json_sha256(asdict(self))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["spec_sha256"] = self.spec_sha256
        return payload


@dataclass(frozen=True)
class FitContext:
    """Data provenance and structural keys supplied separately from X."""

    train_manifest_sha256: str
    feature_contract_sha256: str
    feature_view_sha256: str
    seed: int
    structural_grouping_keys: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, digest in (
            ("train_manifest_sha256", self.train_manifest_sha256),
            ("feature_contract_sha256", self.feature_contract_sha256),
            ("feature_view_sha256", self.feature_view_sha256),
        ):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ContractViolation(f"{name} is not a lowercase SHA-256 digest")
        unknown = set(self.structural_grouping_keys) - set(STRUCTURAL_KEYS)
        if unknown:
            raise LeakageViolation(f"Unknown structural grouping keys: {sorted(unknown)}")


PREDICTION_REQUIRED_COLUMNS = (
    "model_id",
    "family",
    "environment_id",
    "feature_view",
    "model_spec_sha256",
    "benchmark_plan_sha256",
    "fold_manifest_sha256",
    "seed",
    "design",
    "fold_id",
    "sample_id",
    "point_id",
    "profile_id",
    "zone_id",
    "current_date",
    "target_date",
    "forecast_horizon_days",
    "last_rate_mm_y",
    "current_standard_uncertainty_mm",
    "sigma_rate_mm_y",
    "n_history",
    "missing_campaigns_since_previous",
    "transition_segment",
    "is_transition",
    "y_true",
    "y_pred",
)

PREDICTION_OPTIONAL_COLUMNS = (
    "predictive_std",
    "distribution_family",
    "distribution_loc",
    "distribution_scale",
    "q025",
    "q10",
    "q25",
    "q50",
    "q75",
    "q90",
    "q975",
    "fit_seconds",
    "inference_seconds",
    "peak_ram_mb",
    "peak_vram_mb",
    "artifact_size_bytes",
    "parameter_count",
    "tree_count",
    "rule_count",
    "selected_parameter_sha256",
    "selected_parameter_json",
    "provenance_role",
    "effective_iterations",
    "ensemble_member_count",
    "aggregation",
)


@dataclass(frozen=True)
class PredictionBundle:
    """Schema-validated prediction table exchanged by isolated workers."""

    frame: pd.DataFrame

    @classmethod
    def validate(
        cls,
        frame: pd.DataFrame,
        *,
        expected_sample_ids: Sequence[str] | None = None,
        expected_environment_id: str | None = None,
        expected_model_id: str | None = None,
    ) -> "PredictionBundle":
        missing = set(PREDICTION_REQUIRED_COLUMNS) - set(frame.columns)
        if missing:
            raise ContractViolation(f"Prediction shard is missing columns: {sorted(missing)}")
        permitted = set(PREDICTION_REQUIRED_COLUMNS) | set(PREDICTION_OPTIONAL_COLUMNS)
        extra = set(frame.columns) - permitted
        if extra:
            raise ContractViolation(f"Prediction shard has unregistered columns: {sorted(extra)}")
        keys = ["model_id", "environment_id", "seed", "design", "fold_id", "sample_id"]
        if frame.duplicated(keys).any():
            raise ContractViolation("Prediction shard contains duplicate model/fold/sample rows")
        if frame["sample_id"].isna().any():
            raise ContractViolation("Prediction shard contains null sample_id")
        for column in ("y_true", "y_pred", "forecast_horizon_days"):
            values = pd.to_numeric(frame[column], errors="coerce")
            if not np.isfinite(values).all():
                raise ContractViolation(f"Prediction shard contains non-finite {column}")
        if pd.to_numeric(frame["forecast_horizon_days"]).le(0).any():
            raise ContractViolation("Prediction shard contains non-positive horizons")
        if expected_environment_id is not None and set(frame["environment_id"].astype(str)) != {
            expected_environment_id
        }:
            raise ContractViolation("Prediction shard environment ID mismatch")
        if expected_model_id is not None and set(frame["model_id"].astype(str)) != {
            expected_model_id
        }:
            raise ContractViolation("Prediction shard model ID mismatch")
        if expected_sample_ids is not None:
            actual = tuple(frame["sample_id"].astype(str))
            expected = tuple(map(str, expected_sample_ids))
            if len(actual) != len(expected) or set(actual) != set(expected):
                raise ContractViolation("Prediction shard does not cover exact expected sample IDs")
            if sample_id_list_sha256(sorted(actual)) != sample_id_list_sha256(sorted(expected)):
                raise ContractViolation("Prediction shard sample-ID hash mismatch")
        return cls(frame=frame.copy())


@dataclass(frozen=True)
class BenchmarkPlan:
    """Frozen outer/inner fold registry and expected validation hashes."""

    benchmark_version: str
    source_split: str
    source_sample_ids_sha256: str
    outer_fold_count: int
    inner_fold_count: int
    expected_outer_counts: Mapping[str, int]
    outer_validation_hashes: Mapping[str, str]
    feature_views_sha256: str
    config_sha256: str
    preregistered_source_hashes: Mapping[str, str]

    @property
    def plan_sha256(self) -> str:
        return canonical_json_sha256(asdict(self))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["plan_sha256"] = self.plan_sha256
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BenchmarkPlan":
        clean = {key: value for key, value in payload.items() if key != "plan_sha256"}
        plan = cls(**clean)
        supplied = payload.get("plan_sha256")
        if supplied is not None and supplied != plan.plan_sha256:
            raise ContractViolation("Benchmark plan hash does not match its content")
        return plan


@dataclass(frozen=True)
class MetricSuite:
    """Frozen declaration of point, group, transition and interval metrics."""

    headline: str = "mae"
    point_metrics: tuple[str, ...] = (
        "mae",
        "median_absolute_error",
        "rmse",
        "bias",
        "absolute_bias",
        "p90_absolute_error",
        "p95_absolute_error",
        "max_absolute_error",
        "r2_descriptive",
        "precision_weighted_mae",
        "precision_weighted_rmse",
        "b1_skill",
        "mase",
        "direction_accuracy",
    )
    interval_levels: tuple[float, ...] = (0.50, 0.80, 0.95)
    cluster_sensitivity_replicates: int = 2000
    cluster_sensitivity_seed: int = 42117

    @property
    def suite_sha256(self) -> str:
        return canonical_json_sha256(asdict(self))


def feature_view_columns(view_name: str, contract: FeatureContract) -> tuple[str, ...]:
    if view_name == "DYNAMIC_CORE_17":
        columns = DYNAMIC_CORE_17
    elif view_name in {"SAFE_ALL", "NATIVE_CATEGORICAL"}:
        columns = contract.allowed_features
    else:
        raise KeyError(f"Unknown feature view: {view_name}")
    missing = set(columns) - set(contract.allowed_features)
    if missing:
        raise ContractViolation(f"Feature view {view_name} is outside allowlist: {sorted(missing)}")
    forbidden_ids = set(columns) & set(FORBIDDEN_IDENTIFIER_FIELDS)
    if forbidden_ids:
        raise LeakageViolation(f"Feature view contains identifiers: {sorted(forbidden_ids)}")
    view_contract = feature_view_contract(view_name, contract)
    assert_estimator_feature_safety(columns, view_contract)
    return tuple(columns)


def feature_view_contract(view_name: str, contract: FeatureContract) -> FeatureContract:
    columns = DYNAMIC_CORE_17 if view_name == "DYNAMIC_CORE_17" else contract.allowed_features
    table = contract.table.loc[contract.table["field"].astype(str).isin(columns)].copy()
    table = table.set_index("field", drop=False).loc[list(columns)].reset_index(drop=True)
    return FeatureContract(
        table=table,
        source_path=contract.source_path,
        source_sha256=canonical_json_sha256(
            {"formal_feature_contract_sha256": contract.source_sha256, "view": view_name, "fields": list(columns)}
        ),
    )


def apply_feature_view(
    dataset: ManifestDataset,
    *,
    view_name: str,
    contract: FeatureContract,
) -> ManifestDataset:
    columns = feature_view_columns(view_name, contract)
    return ManifestDataset(
        frame=dataset.frame.copy(),
        feature_columns=columns,
        provenance=dataset.provenance,
    )


def assert_structural_keys_not_in_x(
    estimator_columns: Iterable[str],
    structural_keys: Mapping[str, Sequence[Any]],
) -> None:
    columns = set(map(str, estimator_columns))
    leaked = columns & set(structural_keys)
    if leaked:
        raise LeakageViolation(f"Structural keys leaked into estimator X: {sorted(leaked)}")
    unknown = set(structural_keys) - set(STRUCTURAL_KEYS)
    if unknown:
        raise LeakageViolation(f"Unregistered structural keys: {sorted(unknown)}")


FORBIDDEN_WORKER_KEYS = frozenset(
    {
        "validation_manifest",
        "test_manifest",
        "historical_validation_manifest",
        "candidate_record",
        "holdout_manifest",
    }
)


def assert_train_only_worker_job(payload: Mapping[str, Any]) -> None:
    """Reject any worker job that exposes canonical validation/test inputs."""

    found_keys: list[str] = []
    found_values: list[str] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key).lower()
                child_path = f"{path}.{key_text}" if path else key_text
                if key_text in FORBIDDEN_WORKER_KEYS:
                    found_keys.append(child_path)
                visit(child, child_path)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
        elif isinstance(value, (str, Path)):
            normalized = str(value).replace("\\", "/").lower()
            if "t1_v1/validation" in normalized or "t1_v1/test" in normalized:
                found_values.append(f"{path}={value}")

    visit(payload, "")
    if found_keys or found_values:
        raise LeakageViolation(
            f"B6 worker job exposes prohibited data: keys={found_keys}, values={found_values}"
        )
    if str(payload.get("source_split", "")) != "t1_v1/train":
        raise LeakageViolation("B6 worker job source_split must be exactly t1_v1/train")
