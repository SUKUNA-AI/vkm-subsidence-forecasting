"""Deterministic train-only temporal/world and spatial representation folds."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from hashlib import sha256
from pathlib import Path
import json

import pandas as pd

from .scenario_adapter_v2 import ModelDataBundle, make_dataset
from .scenario_boundary_v2 import verified_manifest, verified_payload, json_sha256
from .splits import sample_id_list_sha256

AGGREGATES = ("profile_mean_settlement_mm", "profile_mean_rate_mm_y",
              "profile_rate_std_mm_y", "profile_n_observed")


@dataclass(frozen=True)
class Fold:
    fold_id: str
    kind: str
    validation_start: str
    validation_end: str
    embargo_days: int
    fit_ids: tuple[str, ...]
    validation_ids: tuple[str, ...]
    held_group: str | None = None
    world_bucket: int | None = None

    def record(self):
        record = asdict(self)
        for role, ids in (("fit", self.fit_ids), ("validation", self.validation_ids)):
            record.pop(role + "_ids")
            record[role + "_count"] = len(ids)
            record[role + "_sample_ids_sha256"] = sample_id_list_sha256(ids)
        return record


def group_manifest(root: str | Path, bundle: ModelDataBundle) -> pd.DataFrame:
    """Metadata only: no mechanism, source donor, coordinates or target values."""
    root = Path(root).resolve()
    manifest = verified_manifest(root)
    assignments = pd.read_csv(verified_payload(root, manifest, "split_assignments.csv.gz"),
                              usecols=["sample_id", "zone_id"])
    groups = bundle.frames.loc[:, ["sample_id", "scenario_id", "base_point_id", "base_profile_id"]].merge(
        assignments, on="sample_id", validate="one_to_one", sort=False)
    if not groups.scenario_id.str.fullmatch(r"V2-W\d{3}-O[1-5]").all():
        raise ValueError("Unrecognized frozen world/replica identifier")
    groups["latent_world_id"] = groups.scenario_id.str.rsplit("-", n=1).str[0]
    worlds = groups.latent_world_id.unique()
    buckets = {world: int(sha256(world.encode()).hexdigest(), 16) % 3 for world in worlds}
    groups["world_bucket"] = groups.latent_world_id.map(buckets)
    if len(worlds) != 128 or groups.groupby("latent_world_id").scenario_id.nunique().ne(5).any():
        raise ValueError("World/replica group cardinality changed")
    if groups.groupby("base_point_id").zone_id.nunique().ne(1).any():
        raise ValueError("Inconsistent proxy zone lookup")
    groups["outer_role"] = groups.sample_id.map(bundle.roles)
    return groups


def build_folds(bundle: ModelDataBundle, groups: pd.DataFrame, config: dict) -> tuple[Fold, ...]:
    origins = bundle.frames.set_index("sample_id")
    g = groups.set_index("sample_id").loc[origins.index]
    train = g.outer_role.eq("train")
    result = []

    def add(fold_id, kind, year, fit_groups, val_groups, held=None, bucket=None):
        start, end = pd.Timestamp(year, 1, 1), pd.Timestamp(year, 12, 31)
        boundary = start - pd.Timedelta(days=int(config["embargo_days"]))
        fit = train & fit_groups & origins.target_date.lt(boundary)
        val = train & val_groups & origins.current_date.between(start, end)
        fold = Fold(fold_id, kind, str(start.date()), str(end.date()), int(config["embargo_days"]),
                    tuple(origins.index[fit]), tuple(origins.index[val]), held, bucket)
        validate_fold(fold, bundle, groups)
        result.append(fold)

    for bucket, year in enumerate(config["inner_validation_years"]):
        add(f"inner_{year}_world_{bucket}", "temporal_world", int(year),
            g.world_bucket.ne(bucket), g.world_bucket.eq(bucket), bucket=bucket)
    for kind, column in (("profile", "base_profile_id"), ("zone", "zone_id")):
        for held in sorted(g[column].unique()):
            add(f"leave_{kind}_{held}", kind, int(config["spatial_validation_year"]),
                g[column].ne(held), g[column].eq(held), held=held)
    return tuple(result)


def validate_fold(fold: Fold, bundle: ModelDataBundle, groups: pd.DataFrame):
    if not fold.fit_ids or not fold.validation_ids:
        raise ValueError("Empty fold")
    if len(set(fold.fit_ids)) != len(fold.fit_ids) or len(set(fold.validation_ids)) != len(fold.validation_ids):
        raise ValueError("Duplicate fold origins")
    if set(fold.fit_ids) & set(fold.validation_ids):
        raise ValueError("Overlapping fold origins")
    ids = [*fold.fit_ids, *fold.validation_ids]
    if not bundle.roles.loc[ids].eq("train").all():
        raise ValueError("Inner fold escaped outer train")
    frame = bundle.frames.set_index("sample_id")
    fit, val = frame.loc[list(fold.fit_ids)], frame.loc[list(fold.validation_ids)]
    if not fit.target_date.lt(pd.Timestamp(fold.validation_start) - pd.Timedelta(days=fold.embargo_days)).all():
        raise ValueError("Embargo violated")
    if not val.current_date.between(pd.Timestamp(fold.validation_start), pd.Timestamp(fold.validation_end)).all():
        raise ValueError("Validation outside window")
    if fit.target_date.max() >= val.current_date.min():
        raise ValueError("Training target interval intersects validation origin")
    g = groups.set_index("sample_id")
    gf, gv = g.loc[list(fold.fit_ids)], g.loc[list(fold.validation_ids)]
    if fold.kind == "temporal_world":
        if set(gf.latent_world_id) & set(gv.latent_world_id):
            raise ValueError("World replicas split between fit/validation")
    else:
        column = {"profile": "base_profile_id", "zone": "zone_id"}[fold.kind]
        if gf[column].eq(fold.held_group).any() or not gv[column].eq(fold.held_group).all():
            raise ValueError("Spatial holdout contamination")


def allowed_context(bundle, groups, fold):
    h = bundle.history.frame
    if fold.kind == "profile":
        return h.loc[h.base_profile_id.ne(fold.held_group)].copy()
    if fold.kind == "zone":
        lookup = groups[["base_point_id", "zone_id"]].drop_duplicates().set_index("base_point_id").zone_id
        zones = h.base_point_id.map(lookup)
        if zones.isna().any():
            raise ValueError("Missing zone mapping")
        return h.loc[zones.ne(fold.held_group)].copy()
    return h.copy()


def spatial_features(frame, context):
    """Overlay only four allowed aggregates; missing context remains NaN/count zero."""
    grouped = context.groupby(["profile_id", "current_date"], sort=False)
    agg = grouped.agg(profile_mean_settlement_mm=("last_settlement_mm", "mean"),
                      profile_mean_rate_mm_y=("last_rate_mm_y", "mean"),
                      profile_n_observed=("point_id", "size"))
    agg["profile_rate_std_mm_y"] = grouped.last_rate_mm_y.std(ddof=0)
    result = frame.drop(columns=list(AGGREGATES)).merge(agg, on=["profile_id", "current_date"],
                                                     how="left", validate="many_to_one", sort=False)
    result["profile_n_observed"] = result.profile_n_observed.fillna(0).astype(int)
    return result.loc[:, frame.columns]


def fold_datasets(bundle: ModelDataBundle, groups: pd.DataFrame, fold: Fold):
    validate_fold(fold, bundle, groups)
    # Only outer train contains targets. Validation targets are removed here.
    source = bundle.train.frame.set_index("sample_id", drop=False)
    fit = source.loc[list(fold.fit_ids)].reset_index(drop=True).copy()
    validation = source.loc[list(fold.validation_ids)].reset_index(drop=True).drop(columns="observed_rate_mm_y")
    if fold.kind != "temporal_world":
        context = allowed_context(bundle, groups, fold)
        fit, validation = spatial_features(fit, context), spatial_features(validation, context)
    digest = json_sha256(fold.record())
    return (make_dataset(fit, "train", digest, candidate_id=fold.fold_id),
            make_dataset(validation, "inner_validation", digest, candidate_id=fold.fold_id))


def load_frozen_folds(directory: str | Path, bundle, groups, config) -> tuple[Fold, ...]:
    """Reconstruct compact manifests and verify all explicit ID memberships."""
    directory = Path(directory)
    manifest = json.loads((directory / "split_manifest.json").read_text(encoding="utf-8"))
    from .scenario_boundary_v2 import file_sha256
    for row in manifest["files"]:
        if file_sha256(directory / row["path"]) != row["sha256"]:
            raise ValueError("Split manifest file mismatch")
    folds = build_folds(bundle, groups, config)
    if [fold.record() for fold in folds] != manifest["folds"]:
        raise ValueError("Frozen fold definitions changed")
    membership = pd.read_csv(directory / "temporal_membership.csv.gz")
    for fold in folds[:3]:
        for role, ids in (("fit", fold.fit_ids), ("validation", fold.validation_ids)):
            actual = tuple(membership.loc[membership.fold_id.eq(fold.fold_id) & membership.role.eq(role), "sample_id"])
            if actual != ids:
                raise ValueError("Temporal fold membership mismatch")
    return folds
