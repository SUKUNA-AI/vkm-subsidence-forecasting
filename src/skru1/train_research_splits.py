"""Frozen train-only resampling design for Gate B4.

The latest target campaign inside ``t1_v1/train`` is an internal audit tail.
All temporal, rolling, profile, and zone validation rows remain members of the
original train manifest.  No canonical validation or test manifest is needed
to construct this design.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from .data_contracts import CanonicalBundle, ContractViolation, sha256_file
from .evaluation import EvaluationFold
from .splits import (
    FrozenManifestError,
    ManifestDataset,
    attach_spatial_zones,
    build_spatial_zone_map,
    rolling_origin_assignments,
    sample_id_list_sha256,
)


def build_train_only_folds(
    train: ManifestDataset,
    bundle: CanonicalBundle,
    *,
    rolling_folds: int = 5,
) -> tuple[ManifestDataset, list[EvaluationFold], pd.DataFrame]:
    """Build 1 temporal, 5 rolling, 14 profile, and 4 zone train-only folds."""

    if train.provenance.task != "t1" or train.provenance.split != "train":
        raise ContractViolation("Gate B4 split construction requires t1_v1/train")
    zone_map, _ = build_spatial_zone_map(bundle)
    source = attach_spatial_zones(train, zone_map)
    dates = pd.to_datetime(source.frame["target_date"], errors="coerce")
    if dates.isna().any():
        raise ContractViolation("Gate B4 train target_date contains invalid values")
    audit_date = pd.Timestamp(dates.max())
    core_mask = dates.lt(audit_date)
    audit_mask = dates.eq(audit_date)
    core_ids = tuple(source.frame.loc[core_mask, "sample_id"].astype(str))
    audit_ids = tuple(source.frame.loc[audit_mask, "sample_id"].astype(str))
    if not core_ids or not audit_ids:
        raise ContractViolation("Gate B4 internal temporal split is empty")

    folds: list[EvaluationFold] = [
        EvaluationFold(
            design="internal_temporal",
            fold_id=f"train_tail_{audit_date.date().isoformat()}",
            held_out_group="",
            train_sample_ids=core_ids,
            validation_sample_ids=audit_ids,
        )
    ]

    assignments = rolling_origin_assignments(
        [source],
        minimum_train_dates=4,
        maximum_folds=rolling_folds,
    )
    for fold_id, fold in assignments.groupby("fold_id", sort=True):
        folds.append(
            EvaluationFold(
                design="train_rolling_origin",
                fold_id=str(fold_id),
                held_out_group="",
                train_sample_ids=tuple(
                    fold.loc[fold["role"].eq("train"), "sample_id"].astype(str)
                ),
                validation_sample_ids=tuple(
                    fold.loc[
                        fold["role"].eq("validation"), "sample_id"
                    ].astype(str)
                ),
            )
        )

    for group_field, design in (
        ("profile_id", "train_leave_profile_out"),
        ("zone_id", "train_leave_zone_out"),
    ):
        groups = sorted(source.frame.loc[audit_mask, group_field].astype(str).unique())
        for group in groups:
            train_ids = tuple(
                source.frame.loc[
                    core_mask & source.frame[group_field].astype(str).ne(group),
                    "sample_id",
                ].astype(str)
            )
            validation_ids = tuple(
                source.frame.loc[
                    audit_mask & source.frame[group_field].astype(str).eq(group),
                    "sample_id",
                ].astype(str)
            )
            if not train_ids or not validation_ids:
                raise ContractViolation(f"Empty Gate B4 {design} fold for {group}")
            folds.append(
                EvaluationFold(
                    design=design,
                    fold_id=f"{design}_{group}",
                    held_out_group=group,
                    train_sample_ids=train_ids,
                    validation_sample_ids=validation_ids,
                )
            )

    contracts = validate_train_only_fold_contracts(source, folds)
    return source, folds, contracts


def validate_train_only_fold_contracts(
    source: ManifestDataset,
    folds: Sequence[EvaluationFold],
) -> pd.DataFrame:
    """Assert membership, disjointness, grouping, and strict forward order."""

    indexed = source.frame.set_index("sample_id", drop=False)
    source_ids = set(indexed.index.astype(str))
    rows: list[dict[str, Any]] = []
    for fold in folds:
        train_ids = tuple(map(str, fold.train_sample_ids))
        validation_ids = tuple(map(str, fold.validation_sample_ids))
        if len(train_ids) != len(set(train_ids)) or len(validation_ids) != len(
            set(validation_ids)
        ):
            raise ContractViolation(f"Duplicate sample IDs in Gate B4 fold {fold.fold_id}")
        if set(train_ids) & set(validation_ids):
            raise ContractViolation(f"Overlapping roles in Gate B4 fold {fold.fold_id}")
        if (set(train_ids) | set(validation_ids)) - source_ids:
            raise ContractViolation(f"Unknown sample IDs in Gate B4 fold {fold.fold_id}")
        train_frame = indexed.loc[list(train_ids)]
        validation_frame = indexed.loc[list(validation_ids)]
        train_max = pd.Timestamp(pd.to_datetime(train_frame["target_date"]).max())
        validation_min = pd.Timestamp(
            pd.to_datetime(validation_frame["target_date"]).min()
        )
        validation_max = pd.Timestamp(
            pd.to_datetime(validation_frame["target_date"]).max()
        )
        if train_max >= validation_min:
            raise ContractViolation(
                f"Gate B4 fold is not forward-only: {fold.fold_id} ({train_max} >= {validation_min})"
            )
        if fold.design == "train_leave_profile_out":
            if fold.held_out_group in set(train_frame["profile_id"].astype(str)):
                raise ContractViolation(f"Profile leaked into fit for {fold.fold_id}")
            if set(validation_frame["profile_id"].astype(str)) != {
                fold.held_out_group
            }:
                raise ContractViolation(f"Profile holdout mismatch for {fold.fold_id}")
        if fold.design == "train_leave_zone_out":
            if fold.held_out_group in set(train_frame["zone_id"].astype(str)):
                raise ContractViolation(f"Zone leaked into fit for {fold.fold_id}")
            if set(validation_frame["zone_id"].astype(str)) != {
                fold.held_out_group
            }:
                raise ContractViolation(f"Zone holdout mismatch for {fold.fold_id}")
        rows.append(
            {
                "design": fold.design,
                "fold_id": fold.fold_id,
                "held_out_group": fold.held_out_group,
                "train_rows": len(train_ids),
                "validation_rows": len(validation_ids),
                "train_target_date_max": train_max.date().isoformat(),
                "validation_target_date_min": validation_min.date().isoformat(),
                "validation_target_date_max": validation_max.date().isoformat(),
                "train_sample_ids_sha256": sample_id_list_sha256(train_ids),
                "validation_sample_ids_sha256": sample_id_list_sha256(
                    validation_ids
                ),
            }
        )
    result = pd.DataFrame(rows)
    expected = {
        "internal_temporal": 1,
        "train_rolling_origin": 5,
        "train_leave_profile_out": 14,
        "train_leave_zone_out": 4,
    }
    actual = result.groupby("design")["fold_id"].nunique().to_dict()
    if actual != expected:
        raise ContractViolation(
            f"Gate B4 train-only fold counts changed: actual={actual}, expected={expected}"
        )
    return result.sort_values(["design", "fold_id"], kind="mergesort").reset_index(
        drop=True
    )


def freeze_train_only_manifests(
    root: Path,
    source: ManifestDataset,
    folds: Sequence[EvaluationFold],
    contracts: pd.DataFrame,
    *,
    split_root: Path,
) -> dict[str, Any]:
    """Write the v1 train-research manifests once and reject later mutation."""

    target_dates = pd.to_datetime(source.frame["target_date"])
    audit_date = pd.Timestamp(target_dates.max())
    core = source.frame.loc[target_dates.lt(audit_date), ["sample_id"]].copy()
    audit = source.frame.loc[target_dates.eq(audit_date), ["sample_id"]].copy()
    assignments: list[dict[str, str]] = []
    for fold in folds:
        for role, ids in (
            ("train", fold.train_sample_ids),
            ("validation", fold.validation_sample_ids),
        ):
            assignments.extend(
                {
                    "design": fold.design,
                    "fold_id": fold.fold_id,
                    "held_out_group": fold.held_out_group,
                    "role": role,
                    "sample_id": str(sample_id),
                }
                for sample_id in ids
            )
    assignment_frame = pd.DataFrame(assignments)
    paths = {
        "core": split_root / "core.csv",
        "audit_tail": split_root / "audit_tail.csv",
        "fold_assignments": split_root / "fold_assignments.csv",
        "fold_contracts": split_root / "fold_contracts.csv",
    }
    _write_frozen_frame(paths["core"], core)
    _write_frozen_frame(paths["audit_tail"], audit)
    _write_frozen_frame(paths["fold_assignments"], assignment_frame)
    _write_frozen_frame(paths["fold_contracts"], contracts)
    return {
        "schema_version": 1,
        "split_version": "t1_train_research_v1",
        "source_split": "t1_v1/train",
        "source_rows": len(source.frame),
        "source_sample_ids_sha256": source.provenance.sample_ids_sha256,
        "audit_target_date": audit_date.date().isoformat(),
        "core_rows": len(core),
        "audit_tail_rows": len(audit),
        "audit_tail_points": int(
            source.frame.loc[target_dates.eq(audit_date), "point_id"].astype(str).nunique()
        ),
        "audit_tail_profiles": int(
            source.frame.loc[target_dates.eq(audit_date), "profile_id"].astype(str).nunique()
        ),
        "audit_tail_zones": int(
            source.frame.loc[target_dates.eq(audit_date), "zone_id"].astype(str).nunique()
        ),
        "design_counts": contracts.groupby("design")["fold_id"].nunique().to_dict(),
        "files": {
            path.relative_to(root).as_posix(): sha256_file(path)
            for path in paths.values()
        },
    }


def _write_frozen_frame(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = frame.to_csv(index=False, lineterminator="\n")
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != text:
            raise FrozenManifestError(
                f"Refusing to mutate frozen train-research manifest: {path}"
            )
        return
    path.write_text(text, encoding="utf-8", newline="\n")
