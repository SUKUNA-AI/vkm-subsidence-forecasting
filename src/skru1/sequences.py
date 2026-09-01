"""Leakage-safe sequence representation for Gate C.

Gate C0 does not train a neural network.  This module freezes the only
model-facing chronology that later Gate C workers are allowed to consume.
Every sequence is built from adjusted observations of one work point and ends
at the forecast origin's ``current_date``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .artifact_io import resolve_repo_path
from .data_contracts import CanonicalBundle, ContractViolation, sha256_file
from .splits import (
    FrozenManifestError,
    ManifestDataset,
    attach_spatial_zones,
    build_spatial_zone_map,
    load_split_dataset,
    sample_id_list_sha256,
)


@dataclass(frozen=True)
class SequenceBundle:
    """Normalized sequence tables plus governed source provenance."""

    manifest: pd.DataFrame
    rows: pd.DataFrame
    source: ManifestDataset
    max_sequence_length: int


@dataclass(frozen=True)
class SequenceFitContext:
    """Train-only fit provenance passed separately from estimator features."""

    fold_id: str
    split: str
    role: str
    sample_ids_sha256: str
    feature_contract_sha256: str
    seed: int

    def validate(self) -> None:
        if self.split != "t1_v1/train":
            raise ContractViolation("Gate C preprocessing can only fit from t1_v1/train")
        if self.role != "train":
            raise ContractViolation("Gate C preprocessing fit requires provenance role=train")
        if not self.fold_id.strip():
            raise ContractViolation("Gate C fit context requires a non-empty fold_id")
        for name, value in (
            ("sample_ids_sha256", self.sample_ids_sha256),
            ("feature_contract_sha256", self.feature_contract_sha256),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ContractViolation(f"{name} must be a lowercase SHA-256 digest")


@dataclass
class TrainOnlySequencePreprocessor:
    """Small auditable scaler/imputer/encoder with a hard train-only fit guard."""

    numeric_fields: tuple[str, ...]
    categorical_fields: tuple[str, ...]
    medians_: dict[str, float] = field(default_factory=dict)
    means_: dict[str, float] = field(default_factory=dict)
    scales_: dict[str, float] = field(default_factory=dict)
    categories_: dict[str, tuple[str, ...]] = field(default_factory=dict)
    fit_context_: SequenceFitContext | None = None

    def fit(
        self,
        frame: pd.DataFrame,
        *,
        context: SequenceFitContext,
        config: Mapping[str, Any],
        feature_contract,
    ) -> "TrainOnlySequencePreprocessor":
        context.validate()
        assert_network_feature_columns(
            [*self.numeric_fields, *self.categorical_fields],
            config=config,
            feature_contract=feature_contract,
        )
        if "padding_mask" not in frame or "sample_id" not in frame:
            raise ContractViolation("Sequence preprocessor requires padding_mask and sample_id metadata")
        observed = frame.loc[pd.to_numeric(frame["padding_mask"], errors="raise").eq(0)].copy()
        ordered_ids = tuple(observed["sample_id"].astype(str).drop_duplicates())
        if sample_id_list_sha256(ordered_ids) != context.sample_ids_sha256:
            raise ContractViolation("Preprocessor rows do not match the fit-context sample hash")
        missing = set(self.numeric_fields + self.categorical_fields) - set(observed)
        if missing:
            raise ContractViolation(f"Preprocessor input is missing fields: {sorted(missing)}")
        for name in self.numeric_fields:
            values = pd.to_numeric(observed[name], errors="coerce")
            median = float(values.median()) if values.notna().any() else 0.0
            filled = values.fillna(median)
            mean = float(filled.mean())
            scale = float(filled.std(ddof=0))
            self.medians_[name] = median
            self.means_[name] = mean
            self.scales_[name] = scale if np.isfinite(scale) and scale > 0 else 1.0
        for name in self.categorical_fields:
            values = observed[name].astype("string").fillna("<MISSING>")
            self.categories_[name] = tuple(sorted(values.astype(str).unique()))
        self.fit_context_ = context
        return self

    def transform(self, frame: pd.DataFrame) -> pd.DataFrame:
        if self.fit_context_ is None:
            raise ContractViolation("Sequence preprocessor must be fitted before transform")
        output = pd.DataFrame(index=frame.index)
        for name in self.numeric_fields:
            values = pd.to_numeric(frame[name], errors="coerce").fillna(self.medians_[name])
            output[name] = (values - self.means_[name]) / self.scales_[name]
        for name in self.categorical_fields:
            mapping = {value: index for index, value in enumerate(self.categories_[name], start=1)}
            values = frame[name].astype("string").fillna("<MISSING>").astype(str)
            output[name] = values.map(mapping).fillna(0).astype("int64")
        return output

    def state_dict(self) -> dict[str, Any]:
        if self.fit_context_ is None:
            raise ContractViolation("Cannot serialize an unfitted sequence preprocessor")
        return {
            "schema_version": 1,
            "fit_context": self.fit_context_.__dict__,
            "numeric_fields": list(self.numeric_fields),
            "categorical_fields": list(self.categorical_fields),
            "medians": self.medians_,
            "means": self.means_,
            "scales": self.scales_,
            "categories": {key: list(value) for key, value in self.categories_.items()},
        }


def assert_gate_c_data_boundary(source_split: str) -> None:
    """Reject historical validation, disclosed test, or synthetic holdout aliases."""

    if source_split != "t1_v1/train":
        raise ContractViolation(
            "Gate C model-facing data boundary is exactly t1_v1/train; validation/test are sealed"
        )


def assert_early_stopping_scope(scope: str, config: Mapping[str, Any]) -> None:
    expected = str(config["early_stopping"]["allowed_scope"])
    if scope != expected:
        raise ContractViolation(
            "Gate C early stopping must use inner rolling validation nested inside t1_v1/train"
        )


def assert_network_feature_columns(
    columns: Iterable[str],
    *,
    config: Mapping[str, Any],
    feature_contract,
) -> tuple[str, ...]:
    """Prove that estimator channels are allowed and contain no identifiers."""

    actual = tuple(map(str, columns))
    if len(actual) != len(set(actual)):
        raise ContractViolation("Duplicate Gate C network feature columns")
    policy = config["sequence_contract"]
    forbidden_exact = {str(value).lower() for value in policy["forbidden_network_features"]}
    forbidden_fragments = tuple(str(value).lower() for value in policy["forbidden_name_fragments"])
    allowed = set(feature_contract.allowed_features)
    problems: list[str] = []
    for name in actual:
        lowered = name.lower()
        if lowered in forbidden_exact or any(fragment in lowered for fragment in forbidden_fragments):
            problems.append(name)
        elif name not in allowed:
            problems.append(name)
    if problems:
        raise ContractViolation(
            "Gate C network matrix violates the executable formal feature allowlist: "
            + ", ".join(sorted(problems))
        )
    return actual


def build_sequence_bundle(
    root: Path,
    config: Mapping[str, Any],
    canonical: CanonicalBundle,
) -> SequenceBundle:
    """Build all 911 causal origin sequences from governed observation tables."""

    assert_gate_c_data_boundary(str(config["source_split"]))
    train = load_split_dataset("t1", "train", root=root)
    zone_map, _ = build_spatial_zone_map(canonical)
    source = attach_spatial_zones(train, zone_map)
    policy = config["sequence_contract"]
    adjusted_path = resolve_repo_path(root, policy["chronology_source"])
    membership_path = resolve_repo_path(root, policy["membership_source"])
    manifest_path = resolve_repo_path(root, policy["source_manifest"])
    if train.provenance.manifest_path.resolve() != manifest_path.resolve():
        raise ContractViolation("Gate C source manifest differs from the governed t1_v1/train manifest")
    adjusted = pd.read_csv(adjusted_path)
    membership = pd.read_csv(membership_path)
    required_adjusted = {
        "campaign_id",
        "date",
        "profile_id",
        "point_id",
        "observed_settlement_mm",
        "standard_uncertainty_mm",
    }
    required_membership = {
        "campaign_id",
        "date",
        "campaign_type",
        "point_id",
        "membership_status",
    }
    if required_adjusted - set(adjusted):
        raise ContractViolation("Adjusted-leveling chronology is missing Gate C fields")
    if required_membership - set(membership):
        raise ContractViolation("Campaign membership is missing Gate C fields")
    if adjusted.duplicated(["campaign_id", "point_id"]).any():
        raise ContractViolation("Adjusted-leveling chronology has duplicate campaign/point observations")
    if membership.duplicated(["campaign_id", "point_id"]).any():
        raise ContractViolation("Campaign membership has duplicate campaign/point rows")

    adjusted = adjusted.copy()
    membership = membership.copy()
    adjusted["date"] = pd.to_datetime(adjusted["date"], errors="raise")
    membership["date"] = pd.to_datetime(membership["date"], errors="raise")
    points = set(source.frame["point_id"].astype(str))
    adjusted = adjusted.loc[adjusted["point_id"].astype(str).isin(points)].copy()
    membership = membership.loc[membership["point_id"].astype(str).isin(points)].copy()
    campaign_meta = membership[["campaign_id", "date", "campaign_type"]].drop_duplicates()
    if campaign_meta.groupby("campaign_id").size().gt(1).any():
        raise ContractViolation("Campaign date/type metadata is inconsistent across membership rows")
    campaign_meta = campaign_meta.sort_values(["date", "campaign_id"], kind="mergesort").reset_index(drop=True)
    campaign_position = {str(value): index for index, value in enumerate(campaign_meta["campaign_id"])}
    campaign_type = campaign_meta.set_index("campaign_id")["campaign_type"].astype(str).to_dict()
    membership_status = membership.set_index(["campaign_id", "point_id"])["membership_status"].astype(str)

    safe_observations = _prepare_observation_ledger(
        adjusted,
        campaign_position=campaign_position,
        campaign_meta=campaign_meta,
        campaign_type=campaign_type,
        membership_status=membership_status,
    )
    sequence_fields = assert_network_feature_columns(
        policy["sequence_feature_channels"],
        config=config,
        feature_contract=canonical.feature_contract,
    )
    max_length = int(policy["max_sequence_length"])
    minimum_history = int(policy["minimum_observed_history"])
    origin_frame = source.frame.copy()
    origin_frame["current_date"] = pd.to_datetime(origin_frame["current_date"], errors="raise")
    origin_frame["target_date"] = pd.to_datetime(origin_frame["target_date"], errors="raise")
    observation_by_point = {
        str(point_id): frame.sort_values(["observation_date", "campaign_id"], kind="mergesort").reset_index(drop=True)
        for point_id, frame in safe_observations.groupby("point_id", sort=True)
    }

    manifest_rows: list[dict[str, Any]] = []
    normalized_rows: list[dict[str, Any]] = []
    for origin in origin_frame.itertuples(index=False):
        point_id = str(origin.point_id)
        chronology = observation_by_point.get(point_id)
        if chronology is None:
            raise ContractViolation(f"No adjusted-leveling chronology for origin point {point_id}")
        history = chronology.loc[chronology["observation_date"].le(origin.current_date)].copy()
        raw_length = len(history)
        if raw_length < minimum_history:
            raise ContractViolation(f"Sequence shorter than the frozen minimum for {origin.sample_id}")
        if raw_length != int(origin.n_history):
            raise ContractViolation(f"n_history disagrees with adjusted chronology for {origin.sample_id}")
        if pd.Timestamp(history.iloc[-1]["observation_date"]) != pd.Timestamp(origin.current_date):
            raise ContractViolation(f"Sequence does not end at current_date for {origin.sample_id}")
        if str(history.iloc[-1]["campaign_id"]) != str(origin.current_campaign_id):
            raise ContractViolation(f"Sequence does not end at current campaign for {origin.sample_id}")
        used = history.iloc[-max_length:].copy()
        truncated = raw_length > max_length
        padding_count = max_length - len(used)
        if padding_count < 0:
            raise ContractViolation("Gate C sequence padding length became negative")
        target_observation_id = f"{point_id}::{origin.target_campaign_id}"
        actual_ids = tuple(used["observation_id"].astype(str))
        if target_observation_id in actual_ids:
            raise ContractViolation(f"Target observation leaked into sequence {origin.sample_id}")
        if pd.Timestamp(origin.target_date) <= pd.Timestamp(origin.current_date):
            raise ContractViolation(f"Non-positive forecast horizon in {origin.sample_id}")

        rows_for_origin: list[dict[str, Any]] = []
        for position in range(max_length):
            if position < padding_count:
                row = _padding_row(origin, position)
            else:
                observation = used.iloc[position - padding_count]
                row = _observation_row(origin, observation, position, position - padding_count + 1)
            rows_for_origin.append(row)
            normalized_rows.append(row)
        sequence_hash = sequence_sha256_from_rows(pd.DataFrame(rows_for_origin), sequence_fields)
        padding_mask = [int(row["padding_mask"]) for row in rows_for_origin]
        missing_mask = [int(row["missing_campaign_mask"]) for row in rows_for_origin]
        manifest_rows.append(
            {
                "sample_id": str(origin.sample_id),
                "point_id": point_id,
                "profile_id": str(origin.profile_id),
                "zone_id": str(origin.zone_id),
                "current_campaign_id": str(origin.current_campaign_id),
                "current_date": pd.Timestamp(origin.current_date).date().isoformat(),
                "target_campaign_id": str(origin.target_campaign_id),
                "target_date": pd.Timestamp(origin.target_date).date().isoformat(),
                "history_length_raw": int(raw_length),
                "history_length_used": int(len(used)),
                "max_sequence_length": max_length,
                "padding_count": int(padding_count),
                "truncated": bool(truncated),
                "observation_ids_json": _compact_json(list(actual_ids)),
                "observation_dates_json": _compact_json(
                    [pd.Timestamp(value).date().isoformat() for value in used["observation_date"]]
                ),
                "delta_t_days_json": _compact_json(
                    [int(value) for value in used["days_since_previous_observation"]]
                ),
                "padding_mask_json": _compact_json(padding_mask),
                "missing_campaign_mask_json": _compact_json(missing_mask),
                "missing_campaign_counts_json": _compact_json(
                    [int(row["missing_campaigns_since_previous"] or 0) for row in rows_for_origin]
                ),
                "actual_history_end_date": pd.Timestamp(used.iloc[-1]["observation_date"]).date().isoformat(),
                "future_observation_count": 0,
                "target_observation_present": False,
                "past_only_proof": True,
                "sequence_sha256": sequence_hash,
                "fold_provenance_relation": "normalized_hash_join_by_sample_id",
                "outer_assignment_source": str(policy["fold_provenance"]["outer_assignments"]),
                "inner_assignment_source": str(policy["fold_provenance"]["inner_assignments"]),
                "held_group_metadata_only": True,
            }
        )

    manifest = pd.DataFrame(manifest_rows)
    rows = pd.DataFrame(normalized_rows)
    result = SequenceBundle(
        manifest=manifest,
        rows=rows,
        source=source,
        max_sequence_length=max_length,
    )
    validate_sequence_bundle(result, config=config, canonical=canonical)
    return result


def _prepare_observation_ledger(
    adjusted: pd.DataFrame,
    *,
    campaign_position: Mapping[str, int],
    campaign_meta: pd.DataFrame,
    campaign_type: Mapping[str, str],
    membership_status: pd.Series,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for point_id, history in adjusted.groupby("point_id", sort=True):
        history = history.sort_values(["date", "campaign_id"], kind="mergesort").reset_index(drop=True)
        previous: pd.Series | None = None
        for _, observation in history.iterrows():
            if previous is None:
                delta_days = 0
                interval_rate = np.nan
                missing_count = 0
            else:
                delta_days = int((observation["date"] - previous["date"]).days)
                if delta_days <= 0:
                    raise ContractViolation(f"Non-positive observation interval for {point_id}")
                interval_rate = (
                    float(observation["observed_settlement_mm"])
                    - float(previous["observed_settlement_mm"])
                ) / delta_days * 365.25
                previous_position = campaign_position[str(previous["campaign_id"])]
                current_position = campaign_position[str(observation["campaign_id"])]
                missing_count = 0
                for campaign_id in campaign_meta.iloc[previous_position + 1 : current_position]["campaign_id"]:
                    key = (campaign_id, point_id)
                    if key in membership_status.index and str(membership_status.loc[key]) != "observed":
                        missing_count += 1
            campaign_id = str(observation["campaign_id"])
            rows.append(
                {
                    "observation_id": f"{point_id}::{campaign_id}",
                    "point_id": str(point_id),
                    "profile_id": str(observation["profile_id"]),
                    "campaign_id": campaign_id,
                    "observation_date": pd.Timestamp(observation["date"]),
                    "last_settlement_mm": float(observation["observed_settlement_mm"]),
                    "last_rate_mm_y": interval_rate,
                    "current_standard_uncertainty_mm": float(observation["standard_uncertainty_mm"]),
                    "days_since_previous_observation": int(delta_days),
                    "missing_campaigns_since_previous": int(missing_count),
                    "current_campaign_type": str(campaign_type[campaign_id]),
                }
            )
            previous = observation
    return pd.DataFrame(rows)


def _padding_row(origin: Any, position: int) -> dict[str, Any]:
    return {
        "sample_id": str(origin.sample_id),
        "point_id": str(origin.point_id),
        "profile_id": str(origin.profile_id),
        "zone_id": str(origin.zone_id),
        "sequence_position": int(position),
        "history_position": 0,
        "observation_id": "",
        "campaign_id": "",
        "observation_date": "",
        "last_settlement_mm": np.nan,
        "last_rate_mm_y": np.nan,
        "current_standard_uncertainty_mm": np.nan,
        "days_since_previous_observation": np.nan,
        "missing_campaigns_since_previous": 0,
        "current_campaign_type": "",
        "padding_mask": 1,
        "observation_mask": 0,
        "missing_campaign_mask": 0,
    }


def _observation_row(origin: Any, observation: pd.Series, position: int, history_position: int) -> dict[str, Any]:
    missing_count = int(observation["missing_campaigns_since_previous"])
    return {
        "sample_id": str(origin.sample_id),
        "point_id": str(origin.point_id),
        "profile_id": str(origin.profile_id),
        "zone_id": str(origin.zone_id),
        "sequence_position": int(position),
        "history_position": int(history_position),
        "observation_id": str(observation["observation_id"]),
        "campaign_id": str(observation["campaign_id"]),
        "observation_date": pd.Timestamp(observation["observation_date"]).date().isoformat(),
        "last_settlement_mm": float(observation["last_settlement_mm"]),
        "last_rate_mm_y": float(observation["last_rate_mm_y"]),
        "current_standard_uncertainty_mm": float(observation["current_standard_uncertainty_mm"]),
        "days_since_previous_observation": int(observation["days_since_previous_observation"]),
        "missing_campaigns_since_previous": missing_count,
        "current_campaign_type": str(observation["current_campaign_type"]),
        "padding_mask": 0,
        "observation_mask": 1,
        "missing_campaign_mask": int(missing_count > 0),
    }


def sequence_sha256_from_rows(frame: pd.DataFrame, feature_fields: Sequence[str]) -> str:
    actual = frame.loc[pd.to_numeric(frame["padding_mask"], errors="raise").eq(0)].copy()
    actual = actual.sort_values("sequence_position", kind="mergesort")
    payload = []
    for row in actual.to_dict("records"):
        payload.append(
            {
                "observation_id": str(row["observation_id"]),
                "observation_date": str(row["observation_date"]),
                "features": {name: _canonical_scalar(row[name]) for name in feature_fields},
                "missing_campaign_mask": int(row["missing_campaign_mask"]),
            }
        )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def validate_sequence_bundle(
    sequence: SequenceBundle,
    *,
    config: Mapping[str, Any],
    canonical: CanonicalBundle,
) -> dict[str, Any]:
    manifest = sequence.manifest.copy()
    rows = sequence.rows.copy()
    expected_ids = tuple(sequence.source.frame["sample_id"].astype(str))
    actual_ids = tuple(manifest["sample_id"].astype(str))
    if actual_ids != expected_ids:
        raise ContractViolation("Gate C sequence manifest is not the exact ordered t1_v1/train universe")
    if manifest["sample_id"].duplicated().any():
        raise ContractViolation("Gate C sequence manifest has duplicate sample IDs")
    max_length = int(config["sequence_contract"]["max_sequence_length"])
    counts = rows.groupby("sample_id", sort=False).size()
    if len(counts) != len(manifest) or not counts.eq(max_length).all():
        raise ContractViolation("Every Gate C origin must have exactly max_sequence_length normalized rows")
    if set(rows["sample_id"].astype(str)) != set(actual_ids):
        raise ContractViolation("Sequence rows and manifest have different sample universes")
    feature_fields = assert_network_feature_columns(
        config["sequence_contract"]["sequence_feature_channels"],
        config=config,
        feature_contract=canonical.feature_contract,
    )
    forbidden_output_fragments = tuple(
        str(value).lower() for value in config["sequence_contract"]["forbidden_name_fragments"]
    )
    for column in [*manifest.columns, *rows.columns]:
        if any(fragment in str(column).lower() for fragment in forbidden_output_fragments):
            raise ContractViolation(f"Forbidden hidden/private field in sequence artifacts: {column}")
    indexed_manifest = manifest.set_index("sample_id", drop=False)
    for sample_id, group in rows.groupby("sample_id", sort=False):
        group = group.sort_values("sequence_position", kind="mergesort")
        padding = pd.to_numeric(group["padding_mask"], errors="raise").astype(int).tolist()
        expected_padding = int(indexed_manifest.loc[sample_id, "padding_count"])
        if padding != [1] * expected_padding + [0] * (max_length - expected_padding):
            raise ContractViolation(f"Gate C padding is not deterministic left-padding: {sample_id}")
        actual = group.loc[pd.to_numeric(group["padding_mask"]).eq(0)].copy()
        if len(actual) != int(indexed_manifest.loc[sample_id, "history_length_used"]):
            raise ContractViolation(f"Sequence history length/mask mismatch: {sample_id}")
        current_date = pd.Timestamp(indexed_manifest.loc[sample_id, "current_date"])
        target_date = pd.Timestamp(indexed_manifest.loc[sample_id, "target_date"])
        observation_dates = pd.to_datetime(actual["observation_date"], errors="raise")
        if observation_dates.max() > current_date or observation_dates.iloc[-1] != current_date:
            raise ContractViolation(f"Future or incomplete chronology in Gate C sequence: {sample_id}")
        if current_date >= target_date:
            raise ContractViolation(f"Non-forward Gate C forecast origin: {sample_id}")
        target_observation_id = (
            str(indexed_manifest.loc[sample_id, "point_id"])
            + "::"
            + str(indexed_manifest.loc[sample_id, "target_campaign_id"])
        )
        if target_observation_id in set(actual["observation_id"].astype(str)):
            raise ContractViolation(f"Target observation appears in Gate C input: {sample_id}")
        recomputed = sequence_sha256_from_rows(group, feature_fields)
        if recomputed != str(indexed_manifest.loc[sample_id, "sequence_sha256"]):
            raise ContractViolation(f"Gate C sequence hash mismatch: {sample_id}")
    if manifest["future_observation_count"].astype(int).ne(0).any():
        raise ContractViolation("Gate C manifest reports future observations")
    if manifest["target_observation_present"].astype(bool).any():
        raise ContractViolation("Gate C manifest reports target-observation leakage")
    if manifest["truncated"].astype(bool).any():
        raise ContractViolation("Frozen t1_v1/train unexpectedly requires sequence truncation")
    return {
        "status": "PASS",
        "origins": len(manifest),
        "normalized_rows": len(rows),
        "points": int(manifest["point_id"].astype(str).nunique()),
        "profiles": int(manifest["profile_id"].astype(str).nunique()),
        "zones": int(manifest["zone_id"].astype(str).nunique()),
        "history_length_min": int(manifest["history_length_raw"].min()),
        "history_length_max": int(manifest["history_length_raw"].max()),
        "source_sample_ids_sha256": sample_id_list_sha256(actual_ids),
        "future_observations": 0,
        "target_observations_in_input": 0,
        "historical_validation_loaded": False,
        "current_test_loaded": False,
    }


def build_fold_sequence_contracts(
    root: Path,
    sequence: SequenceBundle,
    config: Mapping[str, Any],
) -> pd.DataFrame:
    """Bind sequence hashes to the frozen B5 fold assignments without copying them."""

    policy = config["sequence_contract"]["fold_provenance"]
    benchmark_plan_path = resolve_repo_path(root, policy["benchmark_plan"])
    benchmark_plan = json.loads(benchmark_plan_path.read_text(encoding="utf-8"))
    manifest = sequence.manifest.set_index("sample_id", drop=False)
    rows: list[dict[str, Any]] = []
    for level, key in (("outer", "outer_assignments"), ("inner", "inner_assignments")):
        assignment_path = resolve_repo_path(root, policy[key])
        assignments = pd.read_csv(assignment_path, dtype={"sample_id": "string"}).fillna("")
        required = {
            "level",
            "design",
            "fold_id",
            "parent_fold_id",
            "held_out_key",
            "held_out_group",
            "validation_target_date",
            "role",
            "sample_id",
        }
        if required - set(assignments):
            raise ContractViolation(f"B5 {level} assignments are missing Gate C provenance fields")
        if set(assignments["sample_id"].astype(str)) - set(manifest.index.astype(str)):
            raise ContractViolation(f"B5 {level} assignments contain unknown Gate C origins")
        for fold_id, fold in assignments.groupby("fold_id", sort=True):
            metadata = fold.iloc[0]
            if fold[["design", "parent_fold_id", "held_out_key", "held_out_group", "validation_target_date"]].nunique().max() > 1:
                raise ContractViolation(f"Inconsistent fold metadata for {fold_id}")
            role_frames: dict[str, pd.DataFrame] = {}
            role_ids: dict[str, tuple[str, ...]] = {}
            for role in ("train", "validation"):
                ids = tuple(fold.loc[fold["role"].eq(role), "sample_id"].astype(str))
                if not ids or len(ids) != len(set(ids)):
                    raise ContractViolation(f"Empty or duplicated role in Gate C fold {fold_id}")
                role_ids[role] = ids
                role_frames[role] = manifest.loc[list(ids)].reset_index(drop=True)
            if set(role_ids["train"]) & set(role_ids["validation"]):
                raise ContractViolation(f"Overlapping roles in Gate C fold {fold_id}")
            train_target_max = pd.to_datetime(role_frames["train"]["target_date"], errors="raise").max()
            validation_target = pd.to_datetime(role_frames["validation"]["target_date"], errors="raise")
            if train_target_max >= validation_target.min() or validation_target.nunique() != 1:
                raise ContractViolation(f"Gate C fold is not strict forward-only: {fold_id}")
            held_key = str(metadata["held_out_key"])
            held_group = str(metadata["held_out_group"])
            held_absent_train = True
            held_validation_matches = True
            if held_key:
                if held_key not in manifest.columns:
                    raise ContractViolation(f"Held key missing from Gate C sequence metadata: {held_key}")
                held_absent_train = held_group not in set(role_frames["train"][held_key].astype(str))
                if level == "outer":
                    held_validation_matches = set(role_frames["validation"][held_key].astype(str)) == {held_group}
                else:
                    held_validation_matches = held_group not in set(role_frames["validation"][held_key].astype(str))
                if not held_absent_train or not held_validation_matches:
                    raise ContractViolation(f"Held profile/zone leaked into Gate C fold {fold_id}")
            rows.append(
                {
                    "level": level,
                    "design": str(metadata["design"]),
                    "fold_id": str(fold_id),
                    "parent_fold_id": str(metadata["parent_fold_id"]),
                    "held_out_key": held_key,
                    "held_out_group": held_group,
                    "validation_target_date": str(metadata["validation_target_date"]),
                    "train_origins": len(role_ids["train"]),
                    "validation_origins": len(role_ids["validation"]),
                    "train_sequence_pairs_sha256": _sequence_pair_hash(role_frames["train"]),
                    "validation_sequence_pairs_sha256": _sequence_pair_hash(role_frames["validation"]),
                    "train_input_date_max": str(role_frames["train"]["actual_history_end_date"].max()),
                    "validation_input_date_max": str(role_frames["validation"]["actual_history_end_date"].max()),
                    "train_target_date_max": pd.Timestamp(train_target_max).date().isoformat(),
                    "validation_target_date_min": pd.Timestamp(validation_target.min()).date().isoformat(),
                    "forward_only": True,
                    "future_observations_in_inputs": 0,
                    "target_observations_in_inputs": 0,
                    "held_group_absent_from_train": bool(held_absent_train),
                    "held_group_validation_contract": bool(held_validation_matches),
                    "preprocessing_fit_role": "train",
                    "early_stopping_scope": str(config["early_stopping"]["allowed_scope"]),
                    "assignment_source": str(policy[key]),
                    "assignment_sha256": sha256_file(assignment_path),
                    "benchmark_plan_sha256": sha256_file(benchmark_plan_path),
                    "benchmark_plan_id": str(benchmark_plan["benchmark_version"]),
                }
            )
    result = pd.DataFrame(rows).sort_values(["level", "design", "fold_id"], kind="mergesort").reset_index(drop=True)
    expected = {str(key): int(value) for key, value in config["resampling"]["required_outer_counts"].items()}
    actual = (
        result.loc[result["level"].eq("outer")]
        .groupby("design")["fold_id"]
        .nunique()
        .to_dict()
    )
    if actual != expected:
        raise ContractViolation(f"Gate C outer fold counts changed: actual={actual}, expected={expected}")
    inner_counts = result.loc[result["level"].eq("inner")].groupby("parent_fold_id")["fold_id"].nunique()
    required_inner = int(config["resampling"]["required_inner_folds_per_outer"])
    if len(inner_counts) != sum(expected.values()) or not inner_counts.eq(required_inner).all():
        raise ContractViolation("Every Gate C outer fold must retain exactly three nested inner folds")
    return result


def make_sequence_contract_payload(
    root: Path,
    sequence: SequenceBundle,
    fold_contracts: pd.DataFrame,
    config: Mapping[str, Any],
    canonical: CanonicalBundle,
    *,
    sequence_manifest_path: Path,
    sequence_rows_path: Path,
    fold_contracts_path: Path,
) -> dict[str, Any]:
    policy = config["sequence_contract"]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "gate": str(config["gate"]),
        "split_version": str(config["split_version"]),
        "scientific_scope": str(config["scientific_scope"]),
        "source_split": "t1_v1/train",
        "source_manifest_sha256": sha256_file(resolve_repo_path(root, policy["source_manifest"])),
        "source_sample_ids_sha256": sequence.source.provenance.sample_ids_sha256,
        "feature_contract_sha256": canonical.feature_contract.source_sha256,
        "target_contract_sha256": canonical.target_contract.source_sha256,
        "chronology_source": str(policy["chronology_source"]),
        "chronology_source_sha256": sha256_file(resolve_repo_path(root, policy["chronology_source"])),
        "membership_source": str(policy["membership_source"]),
        "membership_source_sha256": sha256_file(resolve_repo_path(root, policy["membership_source"])),
        "sequence_manifest_sha256": sha256_file(sequence_manifest_path),
        "sequence_rows_sha256": sha256_file(sequence_rows_path),
        "fold_sequence_contracts_sha256": sha256_file(fold_contracts_path),
        "origins": len(sequence.manifest),
        "normalized_rows": len(sequence.rows),
        "points": int(sequence.manifest["point_id"].astype(str).nunique()),
        "profiles": int(sequence.manifest["profile_id"].astype(str).nunique()),
        "zones": int(sequence.manifest["zone_id"].astype(str).nunique()),
        "history_length_min": int(sequence.manifest["history_length_raw"].min()),
        "history_length_max": int(sequence.manifest["history_length_raw"].max()),
        "max_sequence_length": sequence.max_sequence_length,
        "padding_side": str(policy["padding_side"]),
        "sequence_feature_channels": list(policy["sequence_feature_channels"]),
        "structural_channels": list(policy["structural_channels"]),
        "identifiers_in_network_matrix": [],
        "future_observations_in_inputs": 0,
        "target_observations_in_inputs": 0,
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "new_holdout_seen": False,
        "model_training_calls": 0,
        "outer_fold_counts": (
            fold_contracts.loc[fold_contracts["level"].eq("outer")]
            .groupby("design")["fold_id"]
            .nunique()
            .to_dict()
        ),
        "inner_fold_count": int(fold_contracts["level"].eq("inner").sum()),
        "early_stopping_scope": str(config["early_stopping"]["allowed_scope"]),
        "preregistered_source_hashes": {
            str(relative): sha256_file(resolve_repo_path(root, relative))
            for relative in config["preregistered_sources"]
        },
    }
    payload["contract_sha256"] = sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def write_frozen_csv(path: Path, frame: pd.DataFrame) -> None:
    text = frame.to_csv(index=False, lineterminator="\n")
    write_frozen_text(path, text)


def write_frozen_json(path: Path, payload: Mapping[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_frozen_text(path, text)


def write_frozen_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise FrozenManifestError(f"Refusing to mutate frozen Gate C0 manifest: {path}")
        return
    path.write_text(text, encoding="utf-8", newline="\n")


def _sequence_pair_hash(frame: pd.DataFrame) -> str:
    pairs = sorted(
        f"{sample_id}\t{sequence_hash}"
        for sample_id, sequence_hash in frame[["sample_id", "sequence_sha256"]].itertuples(index=False)
    )
    return sha256("\n".join(pairs).encode("utf-8")).hexdigest()


def _canonical_scalar(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer, float, np.floating)):
        return format(float(value), ".12g")
    return str(value)


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
