"""Frozen nested train-only benchmark geometry for Gate B5/B6."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .benchmarking import BenchmarkPlan, FEATURE_VIEW_NAMES, feature_view_columns
from .data_contracts import CanonicalBundle, ContractViolation, sha256_file
from .splits import (
    FrozenManifestError,
    ManifestDataset,
    attach_spatial_zones,
    build_spatial_zone_map,
    sample_id_list_sha256,
)
from .transition_validation import classify_transition_proxy, fit_transition_thresholds


@dataclass(frozen=True)
class BenchmarkFold:
    level: str
    design: str
    fold_id: str
    parent_fold_id: str
    held_out_key: str
    held_out_group: str
    validation_target_date: str
    train_sample_ids: tuple[str, ...]
    validation_sample_ids: tuple[str, ...]


def build_benchmark_folds(
    train: ManifestDataset,
    bundle: CanonicalBundle,
    config: Mapping[str, Any],
) -> tuple[ManifestDataset, list[BenchmarkFold], list[BenchmarkFold], pd.DataFrame]:
    """Build the preregistered 11/42/12 outer folds and three inner folds each."""

    if train.provenance.task != "t1" or train.provenance.split != "train":
        raise ContractViolation("Gate B5 requires exactly t1_v1/train")
    zone_map, _ = build_spatial_zone_map(bundle)
    source = attach_spatial_zones(train, zone_map)
    frame = source.frame.copy()
    frame["target_date"] = pd.to_datetime(frame["target_date"], errors="raise")
    frame["current_date"] = pd.to_datetime(frame["current_date"], errors="raise")
    dates = frame["target_date"]

    settings = config["resampling"]
    rolling_dates = tuple(pd.Timestamp(value) for value in settings["rolling_outer_dates"])
    spatial_dates = tuple(pd.Timestamp(value) for value in settings["spatial_outer_dates"])
    focused_dates = {pd.Timestamp(value) for value in settings["focused_rolling_only_dates"]}
    profiles = tuple(sorted(frame["profile_id"].astype(str).unique()))
    zones = tuple(sorted(frame["zone_id"].astype(str).unique()))
    if len(profiles) != int(settings["expected_profiles"]):
        raise ContractViolation(f"Profile universe changed: {len(profiles)}")
    if len(zones) != int(settings["expected_zones"]):
        raise ContractViolation(f"Zone universe changed: {len(zones)}")
    available_dates = set(pd.Timestamp(value) for value in dates.unique())
    if set(rolling_dates) - available_dates or set(spatial_dates) - available_dates:
        raise ContractViolation("Configured B5 target date is absent from t1_v1/train")
    if focused_dates & set(spatial_dates):
        raise ContractViolation("Focused incomplete campaigns cannot enter spatial CV")

    outer: list[BenchmarkFold] = []
    for target_date in rolling_dates:
        outer.append(
            _make_fold(
                frame,
                level="outer",
                design="rolling_origin",
                fold_id=f"rolling_origin_{target_date.date().isoformat()}",
                validation_target_date=target_date,
            )
        )

    for target_date in spatial_dates:
        for profile in profiles:
            outer.append(
                _make_fold(
                    frame,
                    level="outer",
                    design="spatiotemporal_leave_profile_out",
                    fold_id=f"lpo_{target_date.date().isoformat()}_{profile}",
                    validation_target_date=target_date,
                    held_out_key="profile_id",
                    held_out_group=profile,
                )
            )
        for zone in zones:
            outer.append(
                _make_fold(
                    frame,
                    level="outer",
                    design="spatiotemporal_leave_zone_out",
                    fold_id=f"lzo_{target_date.date().isoformat()}_{zone}",
                    validation_target_date=target_date,
                    held_out_key="zone_id",
                    held_out_group=zone,
                )
            )

    expected = {str(key): int(value) for key, value in settings["expected_outer_counts"].items()}
    actual = pd.Series([fold.design for fold in outer]).value_counts().to_dict()
    if actual != expected:
        raise ContractViolation(f"B5 outer fold counts changed: actual={actual}, expected={expected}")
    first = next(fold for fold in outer if fold.design == "rolling_origin")
    first_train = _select(frame, first.train_sample_ids)
    if len(first_train) != 316 or first_train["target_date"].nunique() != 8:
        raise ContractViolation(
            "First B5 rolling fold must have 316 rows from eight prior campaigns"
        )

    inner: list[BenchmarkFold] = []
    for parent in outer:
        parent_train = _select(frame, parent.train_sample_ids)
        eligible_dates = sorted(pd.Timestamp(value) for value in parent_train["target_date"].unique())
        candidates = [
            value
            for index, value in enumerate(eligible_dates)
            if index >= int(settings["minimum_inner_train_dates"])
        ]
        chosen = candidates[-int(settings["inner_forward_folds"]) :]
        if len(chosen) != int(settings["inner_forward_folds"]):
            raise ContractViolation(f"Cannot build three inner folds for {parent.fold_id}")
        for inner_index, target_date in enumerate(chosen, start=1):
            inner.append(
                _make_fold(
                    parent_train,
                    level="inner",
                    design=parent.design,
                    fold_id=f"{parent.fold_id}__inner_{inner_index}_{target_date.date().isoformat()}",
                    parent_fold_id=parent.fold_id,
                    validation_target_date=target_date,
                    held_out_key=parent.held_out_key,
                    held_out_group=parent.held_out_group,
                    # Parent data already has the held-out group removed.  The
                    # flag below still validates that invariant explicitly.
                    restrict_validation_to_held_group=False,
                )
            )

    contracts = validate_benchmark_folds(source, outer, inner, config)
    return source, outer, inner, contracts


def _make_fold(
    frame: pd.DataFrame,
    *,
    level: str,
    design: str,
    fold_id: str,
    validation_target_date: pd.Timestamp,
    parent_fold_id: str = "",
    held_out_key: str = "",
    held_out_group: str = "",
    restrict_validation_to_held_group: bool = True,
) -> BenchmarkFold:
    dates = pd.to_datetime(frame["target_date"], errors="raise")
    train_mask = dates.lt(validation_target_date)
    validation_mask = dates.eq(validation_target_date)
    if held_out_key:
        group = frame[held_out_key].astype(str)
        train_mask &= group.ne(held_out_group)
        if restrict_validation_to_held_group:
            validation_mask &= group.eq(held_out_group)
        else:
            validation_mask &= group.ne(held_out_group)
    train_ids = tuple(frame.loc[train_mask, "sample_id"].astype(str))
    validation_ids = tuple(frame.loc[validation_mask, "sample_id"].astype(str))
    if not train_ids or not validation_ids:
        raise ContractViolation(f"Empty train or validation role in {fold_id}")
    return BenchmarkFold(
        level=level,
        design=design,
        fold_id=fold_id,
        parent_fold_id=parent_fold_id,
        held_out_key=held_out_key,
        held_out_group=held_out_group,
        validation_target_date=validation_target_date.date().isoformat(),
        train_sample_ids=train_ids,
        validation_sample_ids=validation_ids,
    )


def validate_benchmark_folds(
    source: ManifestDataset,
    outer: Sequence[BenchmarkFold],
    inner: Sequence[BenchmarkFold],
    config: Mapping[str, Any],
) -> pd.DataFrame:
    indexed = source.frame.set_index("sample_id", drop=False)
    source_ids = set(indexed.index.astype(str))
    parents = {fold.fold_id: fold for fold in outer}
    rows: list[dict[str, Any]] = []
    for fold in [*outer, *inner]:
        train_ids = tuple(map(str, fold.train_sample_ids))
        validation_ids = tuple(map(str, fold.validation_sample_ids))
        if len(train_ids) != len(set(train_ids)) or len(validation_ids) != len(set(validation_ids)):
            raise ContractViolation(f"Duplicate sample IDs in {fold.fold_id}")
        if set(train_ids) & set(validation_ids):
            raise ContractViolation(f"Overlapping train/validation roles in {fold.fold_id}")
        if (set(train_ids) | set(validation_ids)) - source_ids:
            raise ContractViolation(f"Unknown sample IDs in {fold.fold_id}")
        train_frame = indexed.loc[list(train_ids)].reset_index(drop=True)
        validation_frame = indexed.loc[list(validation_ids)].reset_index(drop=True)
        train_max = pd.Timestamp(pd.to_datetime(train_frame["target_date"]).max())
        validation_min = pd.Timestamp(pd.to_datetime(validation_frame["target_date"]).min())
        validation_max = pd.Timestamp(pd.to_datetime(validation_frame["target_date"]).max())
        if train_max >= validation_min or validation_min != validation_max:
            raise ContractViolation(f"Fold is not strict single-date forward-only: {fold.fold_id}")
        if fold.level == "inner":
            parent = parents.get(fold.parent_fold_id)
            if parent is None:
                raise ContractViolation(f"Unknown inner parent: {fold.parent_fold_id}")
            if not (set(train_ids) | set(validation_ids)).issubset(set(parent.train_sample_ids)):
                raise ContractViolation(f"Inner fold escapes parent training role: {fold.fold_id}")
        if fold.held_out_key:
            if fold.held_out_group in set(train_frame[fold.held_out_key].astype(str)):
                raise ContractViolation(f"Held group leaked into train role: {fold.fold_id}")
            if fold.level == "outer" and set(validation_frame[fold.held_out_key].astype(str)) != {
                fold.held_out_group
            }:
                raise ContractViolation(f"Outer held-group validation mismatch: {fold.fold_id}")
            if fold.level == "inner" and fold.held_out_group in set(
                validation_frame[fold.held_out_key].astype(str)
            ):
                raise ContractViolation(f"Held group leaked into inner validation: {fold.fold_id}")
        thresholds_config = config["transition_validation"]
        thresholds = fit_transition_thresholds(
            train_frame,
            acceleration_quantile=float(thresholds_config["acceleration_absolute_quantile"]),
            volatility_quantile=float(thresholds_config["volatility_quantile"]),
            missing_campaigns_threshold=int(thresholds_config["missing_campaigns_threshold"]),
        )
        train_transition = classify_transition_proxy(train_frame, thresholds)
        validation_transition = classify_transition_proxy(validation_frame, thresholds)
        rows.append(
            {
                "level": fold.level,
                "design": fold.design,
                "fold_id": fold.fold_id,
                "parent_fold_id": fold.parent_fold_id,
                "held_out_key": fold.held_out_key,
                "held_out_group": fold.held_out_group,
                "validation_target_date": fold.validation_target_date,
                **_role_summary("train", train_frame, train_ids, train_transition, source.feature_columns),
                **_role_summary(
                    "validation", validation_frame, validation_ids, validation_transition, source.feature_columns
                ),
                "forward_only": True,
                "forward_gap_days": int((validation_min - train_max).days),
                "held_group_absent_from_train": bool(
                    not fold.held_out_key
                    or fold.held_out_group not in set(train_frame[fold.held_out_key].astype(str))
                ),
                "transition_thresholds_json": _json(thresholds.to_dict()),
            }
        )
    result = pd.DataFrame(rows).sort_values(
        ["level", "design", "fold_id"], kind="mergesort"
    ).reset_index(drop=True)
    expected_outer = {
        str(key): int(value) for key, value in config["resampling"]["expected_outer_counts"].items()
    }
    actual_outer = (
        result.loc[result["level"].eq("outer")]
        .groupby("design")["fold_id"]
        .nunique()
        .to_dict()
    )
    if actual_outer != expected_outer:
        raise ContractViolation("Validated B5 outer count mismatch")
    inner_counts = result.loc[result["level"].eq("inner")].groupby("parent_fold_id")["fold_id"].nunique()
    if len(inner_counts) != sum(expected_outer.values()) or not inner_counts.eq(3).all():
        raise ContractViolation("Every B5 outer fold must have exactly three inner folds")
    return result


def _role_summary(
    prefix: str,
    frame: pd.DataFrame,
    sample_ids: Sequence[str],
    transitions: pd.DataFrame,
    feature_columns: Sequence[str],
) -> dict[str, Any]:
    target = pd.to_numeric(frame["observed_rate_mm_y"], errors="raise")
    current = pd.to_datetime(frame["current_date"], errors="raise")
    target_date = pd.to_datetime(frame["target_date"], errors="raise")
    missingness = float(frame.loc[:, list(feature_columns)].isna().to_numpy().mean())
    distribution = {
        "min": float(target.min()),
        "q25": float(target.quantile(0.25)),
        "median": float(target.median()),
        "mean": float(target.mean()),
        "q75": float(target.quantile(0.75)),
        "max": float(target.max()),
        "std": float(target.std(ddof=0)),
    }
    transition_distribution = {
        str(key): int(value)
        for key, value in transitions["transition_segment"].value_counts().sort_index().items()
    }
    return {
        f"{prefix}_rows": int(len(frame)),
        f"{prefix}_points": int(frame["point_id"].astype(str).nunique()),
        f"{prefix}_profiles": int(frame["profile_id"].astype(str).nunique()),
        f"{prefix}_zones": int(frame["zone_id"].astype(str).nunique()),
        f"{prefix}_current_date_min": current.min().date().isoformat(),
        f"{prefix}_current_date_max": current.max().date().isoformat(),
        f"{prefix}_target_date_min": target_date.min().date().isoformat(),
        f"{prefix}_target_date_max": target_date.max().date().isoformat(),
        f"{prefix}_sample_ids_sha256": sample_id_list_sha256(sample_ids),
        f"{prefix}_missing_fraction": missingness,
        f"{prefix}_target_distribution_json": _json(distribution),
        f"{prefix}_transition_distribution_json": _json(transition_distribution),
    }


def assignment_frame(folds: Sequence[BenchmarkFold]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for fold in folds:
        for role, ids in (("train", fold.train_sample_ids), ("validation", fold.validation_sample_ids)):
            rows.extend(
                {
                    "level": fold.level,
                    "design": fold.design,
                    "fold_id": fold.fold_id,
                    "parent_fold_id": fold.parent_fold_id,
                    "held_out_key": fold.held_out_key,
                    "held_out_group": fold.held_out_group,
                    "validation_target_date": fold.validation_target_date,
                    "role": role,
                    "sample_id": str(sample_id),
                }
                for sample_id in ids
            )
    return pd.DataFrame(rows)


def freeze_benchmark_manifests(
    root: Path,
    source: ManifestDataset,
    outer: Sequence[BenchmarkFold],
    inner: Sequence[BenchmarkFold],
    contracts: pd.DataFrame,
    config: Mapping[str, Any],
) -> BenchmarkPlan:
    paths = config["artifacts"]
    outer_path = root / paths["outer_assignments"]
    inner_path = root / paths["inner_assignments"]
    contracts_path = root / paths["fold_contracts"]
    feature_views_path = root / paths["feature_views"]
    plan_path = root / paths["benchmark_plan"]
    outer_assignments = assignment_frame(outer)
    inner_assignments = assignment_frame(inner)
    feature_payload = {
        "schema_version": 1,
        "formal_feature_contract_sha256": source.frame.attrs.get(
            "feature_contract_sha256", ""
        ),
        "views": {
            name: {
                "fields": list(feature_view_columns(name, _feature_contract_from_source(source, config, root))),
                "fields_sha256": sample_id_list_sha256(
                    feature_view_columns(name, _feature_contract_from_source(source, config, root))
                ),
                "identifiers_in_X": [],
            }
            for name in FEATURE_VIEW_NAMES
        },
    }
    # Use the canonical contract hash, rather than a DataFrame attribute, as
    # the authoritative feature-view provenance.
    contract = _feature_contract_from_source(source, config, root)
    feature_payload["formal_feature_contract_sha256"] = contract.source_sha256
    _write_frozen_csv(outer_path, outer_assignments)
    _write_frozen_csv(inner_path, inner_assignments)
    _write_frozen_csv(contracts_path, contracts)
    _write_frozen_json(feature_views_path, feature_payload)
    expected_counts = {
        str(key): int(value) for key, value in config["resampling"]["expected_outer_counts"].items()
    }
    plan = BenchmarkPlan(
        benchmark_version=str(config["benchmark_version"]),
        source_split="t1_v1/train",
        source_sample_ids_sha256=source.provenance.sample_ids_sha256,
        outer_fold_count=len(outer),
        inner_fold_count=len(inner),
        expected_outer_counts=expected_counts,
        outer_validation_hashes={fold.fold_id: sample_id_list_sha256(fold.validation_sample_ids) for fold in outer},
        feature_views_sha256=sha256_file(feature_views_path),
        config_sha256=sha256_file(root / "configs" / "gate_b5.yaml"),
        preregistered_source_hashes={
            str(relative): sha256_file(root / str(relative))
            for relative in config["preregistered_sources"]
        },
    )
    _write_frozen_json(plan_path, plan.to_dict())
    return plan


def _feature_contract_from_source(
    source: ManifestDataset, config: Mapping[str, Any], root: Path
):
    # Local import avoids a data-contract import cycle during module loading.
    from .data_contracts import FeatureContract, load_canonical_bundle

    bundle = load_canonical_bundle(root)
    if tuple(source.feature_columns) != tuple(bundle.feature_contract.allowed_features):
        raise ContractViolation("B5 source feature schema changed")
    return FeatureContract.from_csv(bundle.paths["feature_contract"])


def _select(frame: pd.DataFrame, sample_ids: Iterable[str]) -> pd.DataFrame:
    indexed = frame.set_index("sample_id", drop=False)
    return indexed.loc[list(map(str, sample_ids))].reset_index(drop=True)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_frozen_csv(path: Path, frame: pd.DataFrame) -> None:
    _write_frozen_text(path, frame.to_csv(index=False, lineterminator="\n"))


def _write_frozen_json(path: Path, payload: Mapping[str, Any]) -> None:
    _write_frozen_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_frozen_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise FrozenManifestError(f"Refusing to mutate frozen B5 manifest: {path}")
        return
    path.write_text(text, encoding="utf-8", newline="\n")
