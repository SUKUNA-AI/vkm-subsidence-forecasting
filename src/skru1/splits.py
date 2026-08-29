"""Frozen temporal manifests and guarded model-facing split loaders."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from .data_contracts import CanonicalBundle, ContractViolation, load_canonical_bundle, sha256_file
from .leakage import (
    LeakageViolation,
    assert_disjoint_sample_sets,
    assert_expected_origin_grain,
    assert_positive_horizon,
    assert_t1_time_alignment,
    assert_unique_sample_ids,
)


class UnsafeSplitError(LeakageViolation):
    """Raised for row-random or ordinary K-fold validation attempts."""


class FrozenManifestError(ContractViolation):
    """Raised when code attempts to mutate an existing versioned manifest."""


class SealedTestError(PermissionError):
    """Raised when model code requests test data before a candidate is frozen."""


MANIFEST_LAYOUT: Mapping[str, Mapping[str, str]] = {
    "t1": {
        "train": "artifacts/splits/t1_v1/train.csv",
        "validation": "artifacts/splits/t1_v1/validation.csv",
        "test": "artifacts/splits/t1_v1/test.csv",
    },
    "t5": {
        "train": "artifacts/splits/t5_v1/train.csv",
        "validation": "artifacts/splits/t5_v1/validation.csv",
        "test_complete": "artifacts/splits/t5_v1/test_complete.csv",
        "test_censored": "artifacts/splits/t5_v1/test_censored.csv",
    },
}

TEST_SPLITS = frozenset({("t1", "test"), ("t5", "test_complete"), ("t5", "test_censored")})


@dataclass(frozen=True)
class SplitProvenance:
    task: str
    split: str
    version: str
    manifest_path: Path
    manifest_file_sha256: str
    sample_ids_sha256: str
    row_count: int
    test_authorized: bool
    candidate_id: str | None = None


@dataclass(frozen=True)
class ManifestDataset:
    """A model frame carrying evidence that its rows came from a frozen manifest."""

    frame: pd.DataFrame
    feature_columns: tuple[str, ...]
    provenance: SplitProvenance

    @property
    def sample_ids(self) -> tuple[str, ...]:
        return tuple(self.frame["sample_id"].astype(str))


def sample_id_list_sha256(sample_ids: Iterable[str]) -> str:
    """Hash the ordered ID list, independent of CSV dialect and line endings."""

    payload = "\n".join(map(str, sample_ids)).encode("utf-8")
    return sha256(payload).hexdigest()


def classify_temporal_split(values: pd.Series, bundle: CanonicalBundle) -> pd.Series:
    dates = pd.to_datetime(values, errors="coerce")
    contract = bundle.config["split_contract"]
    train_end = pd.Timestamp(contract["train_target_end"])
    validation_start = pd.Timestamp(contract["validation_target_start"])
    validation_end = pd.Timestamp(contract["validation_target_end"])
    test_start = pd.Timestamp(contract["test_target_start"])
    result = pd.Series(pd.NA, index=dates.index, dtype="string")
    result.loc[dates.le(train_end)] = "train"
    result.loc[dates.between(validation_start, validation_end, inclusive="both")] = "validation"
    result.loc[dates.ge(test_start)] = "test"
    if result.isna().any():
        bad = dates[result.isna()].astype(str).unique().tolist()
        raise ContractViolation(f"Dates fall outside the split contract: {bad}")
    return result


def expected_manifest_frames(bundle: CanonicalBundle) -> dict[tuple[str, str], pd.DataFrame]:
    """Build the governed ID lists in memory from canonical targets."""

    t1 = bundle.operational_targets.copy()
    assert_unique_sample_ids(t1, "T1 operational targets")
    assert_expected_origin_grain(t1, "T1 operational targets")
    assert_positive_horizon(t1, "forecast_horizon_days", "T1 operational targets")
    assert_t1_time_alignment(t1)
    expected_t1_split = classify_temporal_split(t1["target_date"], bundle)
    if not expected_t1_split.eq(t1["split"].astype("string")).all():
        count = int((~expected_t1_split.eq(t1["split"].astype("string"))).sum())
        raise ContractViolation(f"T1 split regression is not strictly governed by target_date ({count} rows)")
    available = t1["target_available"].eq(True) & t1["label_status"].eq("observed")
    if t1.loc[available, "observed_rate_mm_y"].isna().any():
        raise ContractViolation("T1 available rows contain missing observed_rate_mm_y")

    result: dict[tuple[str, str], pd.DataFrame] = {}
    for split in ("train", "validation", "test"):
        selected = t1[available & expected_t1_split.eq(split)].copy()
        selected = _ordered_ids(selected, ["target_date", "current_date", "profile_id", "point_id"])
        result[("t1", split)] = selected[["sample_id"]].reset_index(drop=True)

    t5 = bundle.early_warning_labels.copy()
    assert_unique_sample_ids(t5, "T5 labels")
    assert_positive_horizon(t5, "label_horizon_days", "T5 labels")
    current = pd.to_datetime(t5["current_date"], errors="coerce")
    horizon_end = pd.to_datetime(t5["label_horizon_end"], errors="coerce")
    expected_end = current + pd.to_timedelta(pd.to_numeric(t5["label_horizon_days"]), unit="D")
    if current.isna().any() or horizon_end.isna().any() or not expected_end.eq(horizon_end).all():
        raise ContractViolation("T5 label_horizon_end is inconsistent with current_date + label_horizon_days")
    expected_t5_split = classify_temporal_split(t5["label_horizon_end"], bundle)
    if not expected_t5_split.eq(t5["split_by_horizon_end"].astype("string")).all():
        count = int((~expected_t5_split.eq(t5["split_by_horizon_end"].astype("string"))).sum())
        raise ContractViolation(f"T5 split is not strictly governed by label_horizon_end ({count} rows)")

    complete = t5["horizon_complete"].eq(True) & t5["label_status"].eq("complete")
    censored = t5["horizon_complete"].eq(False) & t5["label_status"].eq("right_censored")
    invalid_complete_targets = complete & ~t5["onset_180d"].isin([0, 1])
    if invalid_complete_targets.any():
        raise ContractViolation("T5 complete rows must have binary onset_180d")
    if t5.loc[censored, "onset_180d"].notna().any():
        raise ContractViolation("T5 censored rows must not expose onset_180d")
    non_test_censored = censored & ~expected_t5_split.eq("test")
    if non_test_censored.any():
        raise ContractViolation("Frozen T5 v1 layout has no train/validation censored manifest")

    for split in ("train", "validation"):
        selected = t5[complete & expected_t5_split.eq(split)].copy()
        selected = _ordered_ids(selected, ["label_horizon_end", "current_date", "profile_id", "point_id"])
        result[("t5", split)] = selected[["sample_id"]].reset_index(drop=True)
    for manifest_split, mask in (("test_complete", complete), ("test_censored", censored)):
        selected = t5[mask & expected_t5_split.eq("test")].copy()
        selected = _ordered_ids(selected, ["label_horizon_end", "current_date", "profile_id", "point_id"])
        result[("t5", manifest_split)] = selected[["sample_id"]].reset_index(drop=True)

    assert_disjoint_sample_sets(
        {split: result[("t1", split)]["sample_id"] for split in MANIFEST_LAYOUT["t1"]}
    )
    assert_disjoint_sample_sets(
        {split: result[("t5", split)]["sample_id"] for split in MANIFEST_LAYOUT["t5"]}
    )
    return result


def write_frozen_manifests(
    bundle: CanonicalBundle,
    *,
    output_root: Path | None = None,
) -> dict[tuple[str, str], SplitProvenance]:
    """Create v1 manifests once; later runs may only confirm identical content."""

    root = (output_root or bundle.root).resolve()
    expected = expected_manifest_frames(bundle)
    evidence: dict[tuple[str, str], SplitProvenance] = {}
    for (task, split), frame in expected.items():
        relative = Path(MANIFEST_LAYOUT[task][split])
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = read_manifest(path)
            if tuple(existing["sample_id"].astype(str)) != tuple(frame["sample_id"].astype(str)):
                raise FrozenManifestError(
                    f"Refusing to mutate frozen manifest {path}. Create a new split version instead."
                )
        else:
            frame.to_csv(path, index=False, lineterminator="\n", encoding="utf-8")
        ids = tuple(frame["sample_id"].astype(str))
        evidence[(task, split)] = SplitProvenance(
            task=task,
            split=split,
            version=_split_version(bundle, task),
            manifest_path=path,
            manifest_file_sha256=sha256_file(path),
            sample_ids_sha256=sample_id_list_sha256(ids),
            row_count=len(ids),
            test_authorized=False,
        )
    return evidence


def read_manifest(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"sample_id": "string"})
    if list(frame.columns) != ["sample_id"]:
        raise FrozenManifestError(f"Manifest must contain exactly one sample_id column: {path}")
    if frame["sample_id"].isna().any() or frame["sample_id"].duplicated().any():
        raise FrozenManifestError(f"Manifest sample IDs must be non-null and unique: {path}")
    return frame


def load_split_dataset(
    task: str,
    split: str,
    *,
    root: str | Path | None = None,
    candidate_record: str | Path | None = None,
) -> ManifestDataset:
    """Load a model frame exclusively through a frozen manifest.

    Test manifests are sealed unless a matching frozen-candidate record is
    supplied. Gate A1 itself builds aggregate audit evidence without this
    model-facing loader, so audit generation does not create a bypass.
    """

    task = task.lower()
    if task not in MANIFEST_LAYOUT or split not in MANIFEST_LAYOUT[task]:
        raise KeyError(f"Unknown task/split combination: {task}/{split}")
    bundle = load_canonical_bundle(root)
    expected = expected_manifest_frames(bundle)[(task, split)]
    path = bundle.root / MANIFEST_LAYOUT[task][split]
    if not path.is_file():
        raise FileNotFoundError(f"Frozen split manifest is missing: {path}")
    manifest = read_manifest(path)
    if tuple(manifest["sample_id"].astype(str)) != tuple(expected["sample_id"].astype(str)):
        raise FrozenManifestError(f"Manifest no longer matches canonical inputs: {path}")

    candidate_id: str | None = None
    authorized = False
    if (task, split) in TEST_SPLITS:
        candidate_id = _authorize_test_access(bundle, task, candidate_record)
        authorized = True

    ids = manifest["sample_id"].astype(str).tolist()
    features = bundle.features.set_index("sample_id", drop=False).loc[ids].reset_index(drop=True)
    if task == "t1":
        labels = bundle.operational_targets.set_index("sample_id", drop=False).loc[ids].reset_index(drop=True)
        label_only = [column for column in labels.columns if column == "sample_id" or column not in features.columns]
    else:
        labels = bundle.early_warning_labels.set_index("sample_id", drop=False).loc[ids].reset_index(drop=True)
        # The source T5 table also contains evaluation-only diagnostics derived
        # from private truth. They are useful to the audit, but must never be
        # returned by the model-facing loader.
        safe_t5_label_columns = [
            "sample_id",
            "label_horizon_days",
            "label_horizon_end",
            "split_by_horizon_end",
            "horizon_complete",
            "label_status",
            "onset_180d",
        ]
        label_only = [
            column
            for column in safe_t5_label_columns
            if column == "sample_id" or column not in features.columns
        ]
    frame = features.merge(labels[label_only], on="sample_id", how="inner", validate="one_to_one", sort=False)
    frame = frame.set_index("sample_id", drop=False).loc[ids].reset_index(drop=True)
    provenance = SplitProvenance(
        task=task,
        split=split,
        version=_split_version(bundle, task),
        manifest_path=path,
        manifest_file_sha256=sha256_file(path),
        sample_ids_sha256=sample_id_list_sha256(ids),
        row_count=len(ids),
        test_authorized=authorized,
        candidate_id=candidate_id,
    )
    return ManifestDataset(
        frame=frame,
        feature_columns=bundle.feature_contract.allowed_features,
        provenance=provenance,
    )


def reject_random_train_test_split(*_: Any, **__: Any) -> None:
    raise UnsafeSplitError(
        "Random row train-test split is prohibited. Load train/validation/test through frozen manifests."
    )


def reject_plain_kfold(*_: Any, **__: Any) -> None:
    raise UnsafeSplitError(
        "Ordinary KFold is prohibited for repeated temporal trajectories. Use rolling-origin or grouped holdouts."
    )


def validate_splitter_name(name: str) -> None:
    normalized = reformat_splitter_name(name)
    forbidden = {"kfold", "randomsplit", "randomtraintestsplit", "traintestsplit", "shufflesplit"}
    if normalized in forbidden or ("kfold" in normalized and "group" not in normalized):
        raise UnsafeSplitError(f"Forbidden split strategy: {name}")


def reformat_splitter_name(name: str) -> str:
    return "".join(character for character in name.lower() if character.isalnum())


def rolling_origin_assignments(
    datasets: Sequence[ManifestDataset],
    *,
    date_field: str = "target_date",
    minimum_train_dates: int = 4,
    maximum_folds: int = 5,
) -> pd.DataFrame:
    """Create deterministic expanding-window folds from non-test manifests."""

    if not datasets:
        raise ValueError("At least one manifest-governed dataset is required")
    for dataset in datasets:
        if dataset.provenance.split.startswith("test"):
            raise SealedTestError("Rolling-origin design must not consume test manifests")
    frame = pd.concat([dataset.frame for dataset in datasets], ignore_index=True)
    dates = pd.to_datetime(frame[date_field], errors="coerce")
    if dates.isna().any():
        raise ContractViolation(f"Rolling-origin date field contains missing values: {date_field}")
    unique_dates = sorted(pd.Timestamp(value) for value in dates.unique())
    if len(unique_dates) <= minimum_train_dates:
        raise ContractViolation("Not enough unique dates for rolling-origin validation")
    validation_dates = unique_dates[minimum_train_dates:]
    validation_dates = validation_dates[-maximum_folds:]
    rows: list[dict[str, Any]] = []
    for fold_number, validation_date in enumerate(validation_dates, start=1):
        train_mask = dates.lt(validation_date)
        validation_mask = dates.eq(validation_date)
        for role, mask in (("train", train_mask), ("validation", validation_mask)):
            for sample_id in frame.loc[mask, "sample_id"].astype(str):
                rows.append(
                    {
                        "fold_id": f"rolling_{fold_number:02d}",
                        "role": role,
                        "sample_id": sample_id,
                        "validation_date": validation_date.date().isoformat(),
                    }
                )
    assignments = pd.DataFrame(rows)
    _assert_rolling_origin_order(assignments, frame, date_field)
    return assignments


def combine_development_datasets(datasets: Sequence[ManifestDataset]) -> ManifestDataset:
    """Combine manifest-governed train/validation frames without admitting test."""

    if not datasets:
        raise ValueError("At least one dataset is required")
    tasks = {dataset.provenance.task for dataset in datasets}
    versions = {dataset.provenance.version for dataset in datasets}
    feature_schemas = {dataset.feature_columns for dataset in datasets}
    if len(tasks) != 1 or len(versions) != 1 or len(feature_schemas) != 1:
        raise ContractViolation("Development datasets must share task, version, and feature schema")
    if any(dataset.provenance.split.startswith("test") for dataset in datasets):
        raise SealedTestError("Development combinations cannot contain test manifests")
    frame = pd.concat([dataset.frame for dataset in datasets], ignore_index=True)
    if frame["sample_id"].duplicated().any():
        raise ContractViolation("Development manifests overlap in sample_id")
    combined_ids = tuple(frame["sample_id"].astype(str))
    source_paths = "+".join(dataset.provenance.manifest_path.as_posix() for dataset in datasets)
    provenance = SplitProvenance(
        task=next(iter(tasks)),
        split="development",
        version=next(iter(versions)),
        manifest_path=Path("<combined-development-manifests>"),
        manifest_file_sha256=sha256(source_paths.encode("utf-8")).hexdigest(),
        sample_ids_sha256=sample_id_list_sha256(combined_ids),
        row_count=len(combined_ids),
        test_authorized=False,
    )
    return ManifestDataset(
        frame=frame,
        feature_columns=next(iter(feature_schemas)),
        provenance=provenance,
    )


def leave_one_group_out_assignments(
    dataset: ManifestDataset,
    *,
    group_field: str,
) -> pd.DataFrame:
    """Create deterministic leave-profile/zone-out train and validation roles."""

    if dataset.provenance.split.startswith("test"):
        raise SealedTestError("Grouped validation design must not consume test manifests")
    if group_field not in dataset.frame:
        raise ContractViolation(f"Group field is absent: {group_field}")
    if dataset.frame[group_field].isna().any():
        raise ContractViolation(f"Group field contains nulls: {group_field}")
    groups = sorted(dataset.frame[group_field].astype(str).unique())
    if len(groups) < 2:
        raise ContractViolation(f"Need at least two groups for leave-one-group-out: {group_field}")
    rows: list[dict[str, str]] = []
    for group in groups:
        for sample_id, sample_group in dataset.frame[["sample_id", group_field]].itertuples(index=False):
            rows.append(
                {
                    "fold_id": f"leave_{group_field}_{group}",
                    "held_out_group": group,
                    "role": "validation" if str(sample_group) == group else "train",
                    "sample_id": str(sample_id),
                }
            )
    return pd.DataFrame(rows)


def build_spatial_zone_map(bundle: CanonicalBundle) -> tuple[pd.DataFrame, dict[str, float | str]]:
    """Freeze four coordinate quadrants as split-only proxy zones.

    The source package has no authoritative operational ``zone_id`` for the 98
    work points. Coordinates are therefore used only to define a reproducible
    spatial OOD grouping; they remain forbidden estimator features.
    """

    points = pd.read_csv(bundle.supporting_paths["survey_points"])
    work = points.loc[points["point_type"].eq("WORK")].copy()
    if work["point_id"].duplicated().any():
        raise ContractViolation("survey_points has duplicate WORK point IDs")
    x_cut = float(work["x_local_m"].median())
    y_cut = float(work["y_local_m"].median())
    east_west = work["x_local_m"].ge(x_cut).map({False: "W", True: "E"})
    north_south = work["y_local_m"].ge(y_cut).map({False: "S", True: "N"})
    work["zone_id"] = "GEO_" + north_south + east_west
    mapping = work[["point_id", "profile_id", "zone_id"]].sort_values("point_id").reset_index(drop=True)
    metadata: dict[str, float | str] = {
        "version": str(bundle.config["spatial_holdout"]["version"]),
        "method": str(bundle.config["spatial_holdout"]["method"]),
        "x_median_cut_m": x_cut,
        "y_median_cut_m": y_cut,
        "source_sha256": sha256_file(bundle.supporting_paths["survey_points"]),
    }
    return mapping, metadata


def attach_spatial_zones(dataset: ManifestDataset, zone_map: pd.DataFrame) -> ManifestDataset:
    frame = dataset.frame.merge(zone_map, on=["point_id", "profile_id"], how="left", validate="many_to_one")
    if frame["zone_id"].isna().any():
        raise ContractViolation("Some model samples do not map to a frozen spatial proxy zone")
    return ManifestDataset(frame=frame, feature_columns=dataset.feature_columns, provenance=dataset.provenance)


def _authorize_test_access(
    bundle: CanonicalBundle,
    task: str,
    candidate_record: str | Path | None,
) -> str:
    configured = Path(bundle.config["test_access"]["candidate_record"])
    record_path = Path(candidate_record) if candidate_record is not None else bundle.root / configured
    if not record_path.is_absolute():
        record_path = bundle.root / record_path
    if not record_path.is_file():
        raise SealedTestError(
            "Test is sealed. Freeze a final candidate and provide a matching candidate record before loading it."
        )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    required = {"candidate_id", "status", "task", "split_version", "feature_contract_sha256", "manifest_hashes"}
    missing = required - set(record)
    if missing:
        raise SealedTestError(f"Frozen-candidate record is missing fields: {sorted(missing)}")
    if record["status"] != "frozen" or str(record["task"]).lower() != task:
        raise SealedTestError("Candidate record is not a frozen candidate for the requested task")
    if record["split_version"] != _split_version(bundle, task):
        raise SealedTestError("Candidate split version does not match the requested frozen manifests")
    if record["feature_contract_sha256"] != bundle.feature_contract.source_sha256:
        raise SealedTestError("Candidate feature-contract hash does not match the current contract")
    expected = expected_manifest_frames(bundle)
    for split in ("train", "validation"):
        expected_hash = sample_id_list_sha256(expected[(task, split)]["sample_id"].astype(str))
        if record["manifest_hashes"].get(split) != expected_hash:
            raise SealedTestError(f"Candidate {split} manifest hash does not match")
    candidate_id = str(record["candidate_id"]).strip()
    if not candidate_id:
        raise SealedTestError("candidate_id must be non-empty")
    return candidate_id


def _ordered_ids(frame: pd.DataFrame, fields: Sequence[str]) -> pd.DataFrame:
    return frame.sort_values([*fields, "sample_id"], kind="mergesort")


def _split_version(bundle: CanonicalBundle, task: str) -> str:
    return str(bundle.config["split_contract"][f"{task}_version"])


def _assert_rolling_origin_order(assignments: pd.DataFrame, source: pd.DataFrame, date_field: str) -> None:
    dates = source.set_index("sample_id")[date_field]
    for fold_id, fold in assignments.groupby("fold_id"):
        train_dates = pd.to_datetime(dates.loc[fold.loc[fold["role"].eq("train"), "sample_id"]])
        validation_dates = pd.to_datetime(dates.loc[fold.loc[fold["role"].eq("validation"), "sample_id"]])
        if train_dates.empty or validation_dates.empty or train_dates.max() >= validation_dates.min():
            raise UnsafeSplitError(f"Rolling-origin fold violates time order: {fold_id}")
