"""Executable leakage guards used by Gate A1 and every model pipeline."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Iterable, Mapping, Sequence

import pandas as pd

from .data_contracts import ContractViolation, FeatureContract


FORBIDDEN_EXACT_FIELDS = frozenset(
    {
        "event_onset_date",
        "first_onset_date",
        "process_family",
        "regime_stage",
        "current_regime_stage",
        "base_rate_mm_y",
        "event_amplitude_mm_y",
        "decay_tau_y",
        "settlement_anchor_map_mm",
        "activity_180d",
        "onset_180d",
        "ongoing_acceleration_180d",
        "max_delta_rate_next_180d_mm_y",
        "max_acceleration_next_180d_mm_y2",
        "sustained_two_months",
        "target_observed_settlement_mm",
        "observed_increment_mm",
        "observed_rate_mm_y",
        "target_standard_uncertainty_mm",
        "sigma_increment_mm",
        "sigma_rate_mm_y",
        "training_weight",
        "target_available",
        "label_status",
        "missing_reason",
    }
)

FORBIDDEN_PREFIXES = ("true_", "hidden_", "generator_", "private_")
FORBIDDEN_IDENTIFIER_FIELDS = frozenset(
    {
        "sample_id",
        "point_id",
        "profile_id",
        "campaign_id",
        "current_campaign_id",
        "target_campaign_id",
        "scenario_id",
        "run_id",
        "accepted_run_id",
    }
)

# Plan information known at forecast issue time is explicitly allowed by the
# formal contract.  Any other target/future/next column must not reach X.
ALLOWED_PLANNED_ESTIMATOR_FIELDS = frozenset({"target_campaign_type", "forecast_horizon_days"})
FUTURE_NAME_PATTERN = re.compile(r"(?:^|_)(?:future|next|target)(?:_|$)", re.IGNORECASE)


class LeakageViolation(ContractViolation):
    """Raised when a feature, split, or time-order leakage rule is violated."""


def forbidden_field_reason(field: str) -> str | None:
    lowered = field.lower()
    if lowered in FORBIDDEN_IDENTIFIER_FIELDS or lowered.endswith("_campaign_id"):
        return "identifier or campaign ID cannot be an estimator feature"
    if lowered in FORBIDDEN_EXACT_FIELDS:
        return "target, private truth, or post-outcome field"
    if lowered.startswith(FORBIDDEN_PREFIXES):
        return "hidden/private/generator/true field prefix"
    if "event_onset_date" in lowered or "process_family" in lowered or "regime_stage" in lowered:
        return "explicitly forbidden future/private field"
    if FUTURE_NAME_PATTERN.search(lowered) and lowered not in ALLOWED_PLANNED_ESTIMATOR_FIELDS:
        return "future/next/target information is not in the planned-information exception"
    return None


def assert_estimator_feature_safety(columns: Iterable[str], contract: FeatureContract) -> None:
    """Require the exact allowlist and reject identifiers or future truth."""

    fields = tuple(str(column) for column in columns)
    contract.assert_exact_estimator_columns(fields)
    violations = {field: forbidden_field_reason(field) for field in fields}
    violations = {field: reason for field, reason in violations.items() if reason is not None}
    if violations:
        raise LeakageViolation(f"Forbidden estimator fields: {violations}")


def assert_feature_table_has_no_forbidden_fields(frame: pd.DataFrame, contract: FeatureContract) -> None:
    """Validate physical absence of private truth from the canonical feature table.

    Contract-declared metadata are allowed in the table for joins, while all
    estimator columns still have to pass :func:`assert_estimator_feature_safety`.
    """

    contract.assert_covers_feature_table(frame.columns)
    metadata = set(contract.metadata_fields)
    allowed = set(contract.allowed_features)
    suspicious: dict[str, str] = {}
    for field in frame.columns:
        if field in metadata or field in allowed:
            continue
        reason = forbidden_field_reason(field)
        if reason:
            suspicious[field] = reason
    if suspicious:
        raise LeakageViolation(f"Canonical feature table contains forbidden fields: {suspicious}")
    assert_estimator_feature_safety(contract.allowed_features, contract)


def assert_unique_sample_ids(frame: pd.DataFrame, name: str) -> None:
    if frame["sample_id"].isna().any():
        raise LeakageViolation(f"{name}: null sample_id values are forbidden")
    duplicates = int(frame["sample_id"].duplicated(keep=False).sum())
    if duplicates:
        raise LeakageViolation(f"{name}: {duplicates} rows participate in duplicate sample_id values")


def assert_expected_origin_grain(frame: pd.DataFrame, name: str) -> None:
    """Enforce one model origin per point and current observation epoch."""

    required = {"sample_id", "point_id", "current_campaign_id", "current_date"}
    missing = required - set(frame.columns)
    if missing:
        raise LeakageViolation(f"{name}: missing grain columns {sorted(missing)}")
    duplicate_origin = frame.duplicated(["point_id", "current_campaign_id"], keep=False)
    if duplicate_origin.any():
        raise LeakageViolation(
            f"{name}: {int(duplicate_origin.sum())} rows duplicate the expected point/current_campaign grain"
        )
    duplicate_date_origin = frame.duplicated(["point_id", "current_date"], keep=False)
    if duplicate_date_origin.any():
        raise LeakageViolation(
            f"{name}: {int(duplicate_date_origin.sum())} rows duplicate the expected point/current_date grain"
        )


def assert_disjoint_sample_sets(split_to_ids: Mapping[str, Iterable[str]]) -> None:
    normalized = {name: set(map(str, ids)) for name, ids in split_to_ids.items()}
    names = list(normalized)
    overlaps: list[str] = []
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            common = normalized[left] & normalized[right]
            if common:
                overlaps.append(f"{left}<->{right}:{len(common)}")
    if overlaps:
        raise LeakageViolation("Split sample IDs overlap: " + ", ".join(overlaps))


def assert_positive_horizon(frame: pd.DataFrame, horizon_field: str, name: str) -> None:
    values = pd.to_numeric(frame[horizon_field], errors="coerce")
    invalid = values.isna() | values.le(0)
    if invalid.any():
        raise LeakageViolation(f"{name}: {int(invalid.sum())} non-positive or missing horizons")


def assert_t1_time_alignment(frame: pd.DataFrame) -> None:
    current = pd.to_datetime(frame["current_date"], errors="coerce")
    target = pd.to_datetime(frame["target_date"], errors="coerce")
    expected_horizon = (target - current).dt.days
    stored = pd.to_numeric(frame["forecast_horizon_days"], errors="coerce")
    bad_date = current.isna() | target.isna() | target.le(current)
    bad_horizon = expected_horizon.ne(stored)
    if bad_date.any() or bad_horizon.any():
        raise LeakageViolation(
            "T1 forecast time alignment failed: "
            f"bad_dates={int(bad_date.sum())}, horizon_mismatches={int(bad_horizon.sum())}"
        )


def assert_static_features_constant_within_point(
    features: pd.DataFrame,
    static_fields: Sequence[str],
) -> None:
    varying: dict[str, int] = {}
    grouped = features.groupby("point_id", dropna=False)
    for field in static_fields:
        max_unique = grouped[field].nunique(dropna=False).max()
        if int(max_unique) > 1:
            varying[field] = int(max_unique)
    if varying:
        raise LeakageViolation(f"Static fields vary within point trajectories: {varying}")


def find_forbidden_split_api_usage(paths: Iterable[Path]) -> list[dict[str, str | int]]:
    """Find accidental row-random split calls in model-facing Python sources."""

    patterns = {
        "train_test_split": re.compile(r"\btrain_test_split\s*\("),
        "plain_KFold": re.compile(r"(?<![A-Za-z])KFold\s*\("),
        "shuffle_true": re.compile(r"\bshuffle\s*=\s*True\b"),
    }
    findings: list[dict[str, str | int]] = []
    for path in paths:
        if not path.is_file() or path.suffix.lower() != ".py":
            continue
        # The guard implementation and its own tests necessarily mention the
        # banned APIs. They are not model-facing consumers.
        if path.name in {
            "splits.py",
            "run_gate_a1.py",
            "build_gate_a1_notebook.py",
            "test_leakage_guards.py",
            "test_split_contract.py",
        }:
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for api, pattern in patterns.items():
                if pattern.search(line):
                    findings.append(
                        {"path": path.as_posix(), "line": line_number, "api": api, "text": line.strip()}
                    )
    return findings


def assert_no_forbidden_split_api_usage(paths: Iterable[Path]) -> None:
    findings = find_forbidden_split_api_usage(paths)
    if findings:
        raise LeakageViolation(f"Forbidden random split API usage found: {findings}")
