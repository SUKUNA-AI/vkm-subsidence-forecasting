#!/usr/bin/env python3
"""Run the Gate A1 data audit and materialize frozen split evidence."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
SRC = SCRIPT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from skru1.data_contracts import CanonicalBundle, load_canonical_bundle, sha256_file  # noqa: E402
from skru1.leakage import (  # noqa: E402
    assert_estimator_feature_safety,
    assert_feature_table_has_no_forbidden_fields,
    find_forbidden_split_api_usage,
)
from skru1.preprocessing import TrainOnlyPreprocessor  # noqa: E402
from skru1.splits import (  # noqa: E402
    MANIFEST_LAYOUT,
    ManifestDataset,
    SealedTestError,
    SplitProvenance,
    UnsafeSplitError,
    attach_spatial_zones,
    build_spatial_zone_map,
    classify_temporal_split,
    combine_development_datasets,
    expected_manifest_frames,
    leave_one_group_out_assignments,
    load_split_dataset,
    reject_plain_kfold,
    reject_random_train_test_split,
    rolling_origin_assignments,
    sample_id_list_sha256,
    write_frozen_manifests,
)


ARTIFACT_DIR = Path("artifacts/data_quality")
REPORT_PATH = Path("docs/reports/GATE_A1_DATA_QUALITY_RU.md")
ZONE_DIR = Path("artifacts/splits/spatial_quadrants_v1")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=SCRIPT_ROOT, help="Repository root")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate existing frozen manifests and inputs without rewriting reports",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run_gate_a1(args.root.resolve(), write_outputs=not args.check_only)
    print(
        json.dumps(
            {
                "gate": report["gate"],
                "status": report["status"],
                "critical_failures": report["summary"]["critical_failures"],
                "checks": report["summary"]["checks"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["summary"]["critical_failures"] == 0 else 2


def run_gate_a1(root: Path, *, write_outputs: bool = True) -> dict[str, Any]:
    bundle = load_canonical_bundle(root)
    expected = expected_manifest_frames(bundle)
    if write_outputs:
        manifest_evidence = write_frozen_manifests(bundle)
    else:
        manifest_evidence = _validate_existing_manifests(bundle, expected)

    split_summary = build_split_summary(bundle, expected, manifest_evidence)
    missingness = build_feature_missingness(bundle, expected)
    membership_mapping = build_membership_mapping(bundle)
    join_coverage = build_join_coverage(bundle, membership_mapping)
    drift = build_drift_summary(bundle, expected)
    zone_map, zone_metadata = build_spatial_zone_map(bundle)

    if write_outputs:
        _write_frozen_csv(bundle.root / ZONE_DIR / "point_zone_map.csv", zone_map)
        _write_frozen_json(bundle.root / ZONE_DIR / "metadata.json", zone_metadata)

    validation_design = build_validation_design(bundle, zone_map)
    checks = build_checks(bundle, expected, membership_mapping, zone_map, validation_design)
    findings = build_findings(bundle, split_summary, membership_mapping, drift, zone_metadata)

    critical_failures = sum(
        check["severity"] == "critical" and check["status"] == "FAIL" for check in checks
    )
    failed_noncritical = sum(
        check["severity"] != "critical" and check["status"] == "FAIL" for check in checks
    )
    status = "FAIL" if critical_failures else ("PASS_WITH_WARNINGS" if failed_noncritical or findings else "PASS")

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    report: dict[str, Any] = {
        "schema_version": 1,
        "gate": "A1_DATA_QUALITY",
        "status": status,
        "generated_at_utc": generated_at,
        "dataset_version": bundle.config["dataset_version"],
        "code_state": _git_state(bundle.root),
        "canonical_inputs": {
            key: {
                "path": path.relative_to(bundle.root).as_posix(),
                "sha256": sha256_file(path),
                "role": "canonical",
            }
            for key, path in bundle.paths.items()
        },
        "historical_only_inputs": {
            key: {
                "path": path.relative_to(bundle.root).as_posix(),
                "sha256": sha256_file(path),
                "role": "historical_comparison_only",
            }
            for key, path in bundle.historical_paths.items()
        },
        "summary": {
            "checks": len(checks),
            "critical_failures": int(critical_failures),
            "noncritical_failures": int(failed_noncritical),
            "findings": len(findings),
            "t1_candidate_origins": int(len(bundle.features)),
            "t1_available_labels": int(bundle.operational_targets["target_available"].eq(True).sum()),
            "t5_complete_labels": int(bundle.early_warning_labels["horizon_complete"].eq(True).sum()),
            "t5_censored_labels": int(bundle.early_warning_labels["horizon_complete"].eq(False).sum()),
        },
        "grain": grain_summary(bundle),
        "membership_reconciliation": membership_reconciliation_summary(membership_mapping),
        "split_manifests": _records(split_summary),
        "spatial_holdout": {
            **zone_metadata,
            "zones": _records(
                zone_map.groupby("zone_id", as_index=False).agg(
                    points=("point_id", "nunique"), profiles=("profile_id", "nunique")
                )
            ),
            "limitation": (
                "No authoritative operational zone_id exists for the 98 WORK points; "
                "v1 uses frozen coordinate quadrants as split-only proxy zones."
            ),
        },
        "test_access": {
            "status": "sealed",
            "model_loader": "skru1.splits.load_split_dataset",
            "candidate_record": bundle.config["test_access"]["candidate_record"],
            "physical_scope": (
                "Runtime/process guard for repository model code. Canonical source CSV files remain readable "
                "for audit and are not cryptographically blinded."
            ),
        },
        "checks": checks,
        "findings": findings,
        "validation_design": _records(validation_design),
    }

    if write_outputs:
        output = bundle.root / ARTIFACT_DIR
        output.mkdir(parents=True, exist_ok=True)
        write_csv(output / "split_summary.csv", split_summary)
        write_csv(output / "feature_missingness_by_split.csv", missingness)
        write_csv(output / "duplicate_and_grain_checks.csv", pd.DataFrame(checks))
        write_csv(output / "join_coverage.csv", join_coverage)
        write_csv(output / "drift_summary.csv", drift)
        write_csv(output / "gate_a1_findings.csv", pd.DataFrame(findings))
        write_csv(output / "membership_inconsistency_mapping.csv", membership_mapping)
        write_csv(output / "validation_design_summary.csv", validation_design)

        artifact_paths = [
            output / "split_summary.csv",
            output / "feature_missingness_by_split.csv",
            output / "duplicate_and_grain_checks.csv",
            output / "join_coverage.csv",
            output / "drift_summary.csv",
            output / "gate_a1_findings.csv",
            output / "membership_inconsistency_mapping.csv",
            output / "validation_design_summary.csv",
            bundle.root / ZONE_DIR / "point_zone_map.csv",
            bundle.root / ZONE_DIR / "metadata.json",
            *[evidence.manifest_path for evidence in manifest_evidence.values()],
        ]
        report["artifacts"] = {
            path.relative_to(bundle.root).as_posix(): sha256_file(path) for path in artifact_paths
        }
        write_json(output / "gate_a1_report.json", report)
        report_markdown = render_russian_report(report, split_summary, findings, validation_design)
        report_path = bundle.root / REPORT_PATH
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_markdown, encoding="utf-8", newline="\n")
    return report


def build_split_summary(
    bundle: CanonicalBundle,
    manifests: Mapping[tuple[str, str], pd.DataFrame],
    evidence: Mapping[tuple[str, str], SplitProvenance],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    feature_index = bundle.features.set_index("sample_id", drop=False)
    t1_index = bundle.operational_targets.set_index("sample_id", drop=False)
    t5_index = bundle.early_warning_labels.set_index("sample_id", drop=False)
    allowed = list(bundle.feature_contract.allowed_features)
    for task in ("t1", "t5"):
        for split in MANIFEST_LAYOUT[task]:
            ids = manifests[(task, split)]["sample_id"].astype(str).tolist()
            features = feature_index.loc[ids]
            labels = (t1_index if task == "t1" else t5_index).loc[ids]
            missing_cells = int(features[allowed].isna().sum().sum())
            feature_cells = int(len(features) * len(allowed))
            if task == "t1":
                distribution = continuous_distribution(labels["observed_rate_mm_y"])
                positive = negative = censored = 0
                label_status = labels["label_status"].astype(str).value_counts().sort_index().to_dict()
                horizon_end_min = horizon_end_max = None
            else:
                complete_targets = pd.to_numeric(labels["onset_180d"], errors="coerce")
                positive = int(complete_targets.eq(1).sum())
                negative = int(complete_targets.eq(0).sum())
                censored = int(labels["horizon_complete"].eq(False).sum())
                distribution = {
                    "positive": positive,
                    "negative": negative,
                    "censored": censored,
                    "positive_fraction_complete": _safe_ratio(positive, positive + negative),
                }
                label_status = labels["label_status"].astype(str).value_counts().sort_index().to_dict()
                horizon_end = pd.to_datetime(labels["label_horizon_end"])
                horizon_end_min = _date_min(horizon_end)
                horizon_end_max = _date_max(horizon_end)
            rows.append(
                {
                    "task": task.upper(),
                    "version": evidence[(task, split)].version,
                    "split": split,
                    "manifest_path": evidence[(task, split)].manifest_path.relative_to(bundle.root).as_posix(),
                    "rows": len(ids),
                    "sample_ids_sha256": evidence[(task, split)].sample_ids_sha256,
                    "manifest_file_sha256": evidence[(task, split)].manifest_file_sha256,
                    "current_date_min": _date_min(features["current_date"]),
                    "current_date_max": _date_max(features["current_date"]),
                    "target_date_min": _date_min(features["target_date"]),
                    "target_date_max": _date_max(features["target_date"]),
                    "label_horizon_end_min": horizon_end_min,
                    "label_horizon_end_max": horizon_end_max,
                    "points": int(features["point_id"].nunique()),
                    "profiles": int(features["profile_id"].nunique()),
                    "allowed_feature_cells": feature_cells,
                    "missing_feature_cells": missing_cells,
                    "missing_feature_fraction": _safe_ratio(missing_cells, feature_cells),
                    "target_distribution_json": compact_json(distribution),
                    "label_status_json": compact_json(label_status),
                    "positive": positive,
                    "negative": negative,
                    "censored": censored,
                }
            )
    return pd.DataFrame(rows)


def build_feature_missingness(
    bundle: CanonicalBundle,
    manifests: Mapping[tuple[str, str], pd.DataFrame],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    feature_index = bundle.features.set_index("sample_id", drop=False)
    for task in ("t1", "t5"):
        for split in MANIFEST_LAYOUT[task]:
            ids = manifests[(task, split)]["sample_id"].astype(str).tolist()
            features = feature_index.loc[ids]
            for feature in bundle.feature_contract.allowed_features:
                missing = int(features[feature].isna().sum())
                rows.append(
                    {
                        "task": task.upper(),
                        "split": split,
                        "feature": feature,
                        "rows": len(features),
                        "missing_count": missing,
                        "missing_fraction": _safe_ratio(missing, len(features)),
                    }
                )
    return pd.DataFrame(rows)


def build_membership_mapping(bundle: CanonicalBundle) -> pd.DataFrame:
    membership = pd.read_csv(bundle.supporting_paths["membership"])
    leveling = pd.read_csv(bundle.supporting_paths["adjusted_leveling"])
    observed = membership[membership["observed"].eq(True)].copy()
    adjusted_keys = pd.MultiIndex.from_frame(leveling[["campaign_id", "point_id"]].drop_duplicates())
    observed_keys = pd.MultiIndex.from_frame(observed[["campaign_id", "point_id"]])
    mismatch = observed.loc[~observed_keys.isin(adjusted_keys)].copy()

    target_columns = [
        "sample_id",
        "point_id",
        "target_campaign_id",
        "target_date",
        "label_status",
        "target_available",
    ]
    target_lookup = bundle.operational_targets[target_columns].rename(
        columns={"sample_id": "target_origin_sample_id"}
    )
    mapping = mismatch.merge(
        target_lookup,
        left_on=["campaign_id", "point_id"],
        right_on=["target_campaign_id", "point_id"],
        how="left",
        validate="one_to_one",
    )
    mapped = mapping["target_origin_sample_id"].notna()
    reasons = np.select(
        [mapped, mapping["point_type"].eq("REF")],
        [
            "maps_to_unlabeled_model_origin",
            "reference_point_outside_WORK_model_universe",
        ],
        default="no_eligible_prior_origin_in_canonical_candidate_frame",
    )
    mapping["adjusted_leveling_present"] = False
    mapping["maps_to_unlabeled_origin"] = mapped
    mapping["mapping_reason"] = reasons
    columns = [
        "campaign_id",
        "date",
        "campaign_type",
        "point_id",
        "profile_id",
        "point_type",
        "membership_status",
        "adjusted_leveling_present",
        "target_origin_sample_id",
        "target_campaign_id",
        "target_date",
        "label_status",
        "target_available",
        "maps_to_unlabeled_origin",
        "mapping_reason",
    ]
    return mapping[columns].sort_values(["campaign_id", "point_id"]).reset_index(drop=True)


def build_join_coverage(bundle: CanonicalBundle, membership_mapping: pd.DataFrame) -> pd.DataFrame:
    feature_ids = set(bundle.features["sample_id"].astype(str))
    t1_ids = set(bundle.operational_targets["sample_id"].astype(str))
    t5_ids = set(bundle.early_warning_labels["sample_id"].astype(str))
    membership = pd.read_csv(bundle.supporting_paths["membership"])
    leveling = pd.read_csv(bundle.supporting_paths["adjusted_leveling"])
    observed = membership[membership["observed"].eq(True)]
    adjusted_keys = set(map(tuple, leveling[["campaign_id", "point_id"]].itertuples(index=False, name=None)))
    matched_membership = sum(
        (campaign_id, point_id) in adjusted_keys
        for campaign_id, point_id in observed[["campaign_id", "point_id"]].itertuples(index=False, name=None)
    )
    available = int(bundle.operational_targets["target_available"].eq(True).sum())
    complete_t5 = int(bundle.early_warning_labels["horizon_complete"].eq(True).sum())
    rows = [
        join_row("JOIN-001", "canonical features", "T1 targets", "sample_id", len(feature_ids), len(feature_ids & t1_ids)),
        join_row("JOIN-002", "canonical features", "T5 labels", "sample_id", len(feature_ids), len(feature_ids & t5_ids)),
        join_row(
            "JOIN-003",
            "observed membership",
            "adjusted leveling",
            "campaign_id + point_id",
            len(observed),
            matched_membership,
            status="WARN" if matched_membership != len(observed) else "PASS",
            details="18 source membership claims have no adjusted leveling authority.",
        ),
        join_row(
            "JOIN-004",
            "membership inconsistencies",
            "unlabeled T1 origins",
            "target_campaign_id + point_id",
            len(membership_mapping),
            int(membership_mapping["maps_to_unlabeled_origin"].sum()),
            status="PASS",
            details="The 18 membership rows map to 6 model origins at the coarser origin grain.",
        ),
        join_row(
            "JOIN-005",
            "T1 candidate origins",
            "available operational labels",
            "sample_id",
            len(bundle.operational_targets),
            available,
            status="WARN" if available != len(bundle.operational_targets) else "PASS",
            details="Unavailable targets remain outside train/validation/test manifests.",
        ),
        join_row(
            "JOIN-006",
            "T5 candidate origins",
            "complete 180-day labels",
            "sample_id",
            len(bundle.early_warning_labels),
            complete_t5,
            status="WARN" if complete_t5 != len(bundle.early_warning_labels) else "PASS",
            details="Right-censored test origins are frozen separately.",
        ),
    ]
    return pd.DataFrame(rows)


def build_drift_summary(
    bundle: CanonicalBundle,
    manifests: Mapping[tuple[str, str], pd.DataFrame],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    feature_index = bundle.features.set_index("sample_id", drop=False)
    for task in ("t1", "t5"):
        train_ids = manifests[(task, "train")]["sample_id"].astype(str).tolist()
        reference = feature_index.loc[train_ids]
        comparisons = [split for split in MANIFEST_LAYOUT[task] if split != "train"]
        for split in comparisons:
            compare_ids = manifests[(task, split)]["sample_id"].astype(str).tolist()
            compare = feature_index.loc[compare_ids]
            for feature in bundle.feature_contract.allowed_features:
                ref_values = reference[feature]
                cmp_values = compare[feature]
                base: dict[str, Any] = {
                    "task": task.upper(),
                    "reference_split": "train",
                    "comparison_split": split,
                    "feature": feature,
                    "reference_rows": len(reference),
                    "comparison_rows": len(compare),
                    "missing_fraction_reference": float(ref_values.isna().mean()),
                    "missing_fraction_comparison": float(cmp_values.isna().mean()),
                }
                if pd.api.types.is_numeric_dtype(ref_values):
                    ref_num = pd.to_numeric(ref_values, errors="coerce")
                    cmp_num = pd.to_numeric(cmp_values, errors="coerce")
                    ref_mean = float(ref_num.mean()) if ref_num.notna().any() else math.nan
                    cmp_mean = float(cmp_num.mean()) if cmp_num.notna().any() else math.nan
                    ref_std = float(ref_num.std(ddof=0)) if ref_num.notna().any() else math.nan
                    smd = (cmp_mean - ref_mean) / ref_std if np.isfinite(ref_std) and ref_std > 0 else math.nan
                    base.update(
                        {
                            "feature_type": "numeric",
                            "mean_reference": ref_mean,
                            "mean_comparison": cmp_mean,
                            "std_reference": ref_std,
                            "standardized_mean_difference": smd,
                            "absolute_smd": abs(smd) if np.isfinite(smd) else math.nan,
                            "total_variation_distance": math.nan,
                        }
                    )
                else:
                    ref_dist = ref_values.astype("string").fillna("__MISSING__").value_counts(normalize=True)
                    cmp_dist = cmp_values.astype("string").fillna("__MISSING__").value_counts(normalize=True)
                    categories = ref_dist.index.union(cmp_dist.index)
                    tv = 0.5 * float(
                        sum(abs(float(ref_dist.get(category, 0.0)) - float(cmp_dist.get(category, 0.0))) for category in categories)
                    )
                    base.update(
                        {
                            "feature_type": "categorical",
                            "mean_reference": math.nan,
                            "mean_comparison": math.nan,
                            "std_reference": math.nan,
                            "standardized_mean_difference": math.nan,
                            "absolute_smd": math.nan,
                            "total_variation_distance": tv,
                        }
                    )
                rows.append(base)
    return pd.DataFrame(rows)


def build_validation_design(bundle: CanonicalBundle, zone_map: pd.DataFrame) -> pd.DataFrame:
    train = load_split_dataset("t1", "train", root=bundle.root)
    validation = load_split_dataset("t1", "validation", root=bundle.root)
    development = combine_development_datasets([train, validation])
    rolling = rolling_origin_assignments([train, validation])
    profile = leave_one_group_out_assignments(development, group_field="profile_id")
    zoned = attach_spatial_zones(development, zone_map)
    zone = leave_one_group_out_assignments(zoned, group_field="zone_id")
    rows: list[dict[str, Any]] = []
    for design, frame in (("rolling_origin", rolling), ("leave_profile_out", profile), ("leave_zone_out", zone)):
        for fold_id, fold in frame.groupby("fold_id", sort=True):
            train_ids = fold.loc[fold["role"].eq("train"), "sample_id"].astype(str).tolist()
            validation_ids = fold.loc[fold["role"].eq("validation"), "sample_id"].astype(str).tolist()
            rows.append(
                {
                    "task": "T1",
                    "design": design,
                    "fold_id": fold_id,
                    "held_out_group": fold["held_out_group"].iloc[0] if "held_out_group" in fold else "",
                    "validation_date": fold["validation_date"].iloc[0] if "validation_date" in fold else "",
                    "train_rows": len(train_ids),
                    "validation_rows": len(validation_ids),
                    "train_sample_ids_sha256": sample_id_list_sha256(train_ids),
                    "validation_sample_ids_sha256": sample_id_list_sha256(validation_ids),
                }
            )
    return pd.DataFrame(rows)


def build_checks(
    bundle: CanonicalBundle,
    manifests: Mapping[tuple[str, str], pd.DataFrame],
    membership_mapping: pd.DataFrame,
    zone_map: pd.DataFrame,
    validation_design: pd.DataFrame,
) -> list[dict[str, Any]]:
    features = bundle.features
    t1 = bundle.operational_targets
    t5 = bundle.early_warning_labels
    t1_overlap = pairwise_overlap_count({split: manifests[("t1", split)]["sample_id"] for split in MANIFEST_LAYOUT["t1"]})
    t5_overlap = pairwise_overlap_count({split: manifests[("t5", split)]["sample_id"] for split in MANIFEST_LAYOUT["t5"]})
    t1_expected = classify_temporal_split(t1["target_date"], bundle)
    t5_expected = classify_temporal_split(t5["label_horizon_end"], bundle)
    static_table = pd.read_csv(bundle.supporting_paths["static_features"])
    static_fields = [
        column
        for column in static_table.columns
        if column in bundle.feature_contract.allowed_features
    ]
    static_varying = sum(
        int(features.groupby("point_id")[column].nunique(dropna=False).max() > 1)
        for column in static_fields
    )
    samples_target_token = features["sample_id"].astype(str).str.rsplit("::", n=1).str[-1]
    sample_token_mismatch = int(samples_target_token.ne(features["target_campaign_id"].astype(str)).sum())
    source_paths = [*list((bundle.root / "src").rglob("*.py")), *list((bundle.root / "scripts").rglob("*.py"))]
    forbidden_split_usage = find_forbidden_split_api_usage(source_paths)

    checks: list[dict[str, Any]] = []
    add = lambda *args, **kwargs: checks.append(check_row(*args, **kwargs))
    add("A1-CAN-001", "critical", "canonical", set(bundle.config["canonical"]) == {"features", "operational_targets", "feature_contract", "target_contract", "early_warning_labels"}, len(bundle.config["canonical"]), 5, "Only the registered next-planned tables and contracts are canonical.")
    add("A1-CAN-002", "critical", "canonical", all(path not in bundle.paths.values() for path in bundle.historical_paths.values()), 0, 0, "Historical next_cycle tables do not overlap the canonical registry.")
    add("A1-ID-001", "critical", "uniqueness", features["sample_id"].is_unique, int(features["sample_id"].duplicated().sum()), 0, "Canonical feature sample_id uniqueness.")
    add("A1-ID-002", "critical", "uniqueness", t1["sample_id"].is_unique, int(t1["sample_id"].duplicated().sum()), 0, "T1 target sample_id uniqueness.")
    add("A1-ID-003", "critical", "uniqueness", t5["sample_id"].is_unique, int(t5["sample_id"].duplicated().sum()), 0, "T5 label sample_id uniqueness.")
    origin_duplicates = int(features.duplicated(["point_id", "current_campaign_id"]).sum())
    add("A1-GRAIN-001", "critical", "grain", origin_duplicates == 0, origin_duplicates, 0, "One row per point/current campaign origin.")
    add("A1-GRAIN-002", "critical", "grain", len(features) == 1274, len(features), 1274, "Frozen canonical origin row count.")
    add("A1-GRAIN-003", "critical", "grain", features["point_id"].nunique() == 98, int(features["point_id"].nunique()), 98, "Dependent point trajectories.")
    add("A1-GRAIN-004", "critical", "grain", features["profile_id"].nunique() == 14, int(features["profile_id"].nunique()), 14, "Profile groups.")
    repeated_points = int(features.groupby("point_id").size().gt(1).sum())
    add("A1-GRAIN-005", "critical", "dependency", repeated_points == 98, repeated_points, 98, "Every work point repeats across temporal origins.")
    shared_dates = int(features.groupby("current_date")["profile_id"].nunique().gt(1).sum())
    add("A1-GRAIN-006", "critical", "dependency", shared_dates > 0, shared_dates, ">0", "Campaign dates are shared across profiles.")
    add("A1-GRAIN-007", "critical", "dependency", static_varying == 0, static_varying, 0, f"{len(static_fields)} split-safe static fields remain constant within point.")
    add("A1-SPLIT-001", "critical", "split", t1_overlap == 0, t1_overlap, 0, "No T1 sample_id overlap across manifests.")
    add("A1-SPLIT-002", "critical", "split", t5_overlap == 0, t5_overlap, 0, "No T5 sample_id overlap across manifests.")
    t1_split_mismatch = int(t1_expected.ne(t1["split"].astype("string")).sum())
    add("A1-SPLIT-003", "critical", "split", t1_split_mismatch == 0, t1_split_mismatch, 0, "T1 split is strictly derived from target_date.")
    t5_split_mismatch = int(t5_expected.ne(t5["split_by_horizon_end"].astype("string")).sum())
    add("A1-SPLIT-004", "critical", "split", t5_split_mismatch == 0, t5_split_mismatch, 0, "T5 split is strictly derived from label_horizon_end.")
    invalid_t1_horizon = int(pd.to_numeric(t1["forecast_horizon_days"], errors="coerce").le(0).sum())
    add("A1-TIME-001", "critical", "temporal", invalid_t1_horizon == 0, invalid_t1_horizon, 0, "T1 forecast_horizon_days > 0.")
    invalid_t5_horizon = int(pd.to_numeric(t5["label_horizon_days"], errors="coerce").le(0).sum())
    add("A1-TIME-002", "critical", "temporal", invalid_t5_horizon == 0, invalid_t5_horizon, 0, "T5 label_horizon_days > 0.")

    feature_contract_ok, feature_contract_message = captures_assertion(
        lambda: assert_feature_table_has_no_forbidden_fields(features, bundle.feature_contract)
    )
    add("A1-LEAK-001", "critical", "leakage", feature_contract_ok, 0 if feature_contract_ok else 1, 0, feature_contract_message or "Canonical feature table is free of private/hidden/generator truth.")
    estimator_ok, estimator_message = captures_assertion(
        lambda: assert_estimator_feature_safety(bundle.feature_contract.allowed_features, bundle.feature_contract)
    )
    add("A1-LEAK-002", "critical", "leakage", estimator_ok, 0 if estimator_ok else 1, 0, estimator_message or "Estimator schema equals the formal allowlist and excludes IDs/campaign IDs.")
    add("A1-LEAK-003", "critical", "leakage", len(forbidden_split_usage) == 0, len(forbidden_split_usage), 0, "No model-facing source uses train_test_split, plain KFold, or shuffle=True.")

    random_guard_ok = raises_unsafe_split(reject_random_train_test_split)
    add("A1-LEAK-004", "critical", "leakage", random_guard_ok, int(random_guard_ok), 1, "Random train-test requests terminate with UnsafeSplitError.")
    kfold_guard_ok = raises_unsafe_split(reject_plain_kfold)
    add("A1-LEAK-005", "critical", "leakage", kfold_guard_ok, int(kfold_guard_ok), 1, "Ordinary KFold requests terminate with UnsafeSplitError.")

    train = load_split_dataset("t1", "train", root=bundle.root)
    validation = load_split_dataset("t1", "validation", root=bundle.root)
    preprocessor = TrainOnlyPreprocessor(bundle.feature_contract)
    preprocessor.fit(train)
    train_fit_ok = preprocessor.fitted_train_sample_hash_ == train.provenance.sample_ids_sha256
    validation_fit_blocked = captures_exception(lambda: TrainOnlyPreprocessor(bundle.feature_contract).fit(validation), Exception)
    add("A1-PREP-001", "critical", "preprocessing", train_fit_ok and validation_fit_blocked, int(train_fit_ok and validation_fit_blocked), 1, "Preprocessing fit accepts manifest train and rejects validation.")
    t5_model_frame = load_split_dataset("t5", "train", root=bundle.root).frame
    t5_forbidden_model_columns = {
        "activity_180d",
        "ongoing_acceleration_180d",
        "current_true_rate_mm_y",
        "max_delta_rate_next_180d_mm_y",
        "max_acceleration_next_180d_mm_y2",
        "sustained_two_months",
        "current_regime_stage",
        "first_onset_date",
        "use_class",
    } & set(t5_model_frame.columns)
    add("A1-LEAK-006", "critical", "leakage", not t5_forbidden_model_columns, len(t5_forbidden_model_columns), 0, "T5 model-facing frames expose only onset_180d and censor/split metadata, never private evaluation diagnostics.")
    test_sealed = captures_exception(
        lambda: load_split_dataset("t1", "test", root=bundle.root, candidate_record=bundle.root / "work" / "missing_candidate_record.json"),
        SealedTestError,
    )
    add("A1-TEST-001", "critical", "test_access", test_sealed, int(test_sealed), 1, "Model-facing test load is sealed without a matching frozen candidate record.")

    mapped = int(membership_mapping["maps_to_unlabeled_origin"].sum())
    unlabeled_ids = set(t1.loc[t1["label_status"].eq("observed_but_no_adjusted_leveling"), "sample_id"].astype(str))
    mapped_ids = set(membership_mapping.loc[membership_mapping["maps_to_unlabeled_origin"], "target_origin_sample_id"].astype(str))
    reconciliation_ok = len(membership_mapping) == 18 and mapped == 6 and mapped_ids == unlabeled_ids
    add("A1-JOIN-001", "critical", "referential_integrity", reconciliation_ok, f"{len(membership_mapping)} -> {mapped}", "18 -> 6", "Membership rows and affected origin samples are reconciled by campaign_id + point_id.")
    add("A1-ID-004", "high", "identifier_semantics", sample_token_mismatch == 0, sample_token_mismatch, 0, "sample_id is unique but its final token still reflects the historical next-available target in these rows; treat it as opaque.")

    design_counts = validation_design.groupby("design")["fold_id"].nunique().to_dict()
    add("A1-VAL-001", "critical", "validation_design", design_counts.get("rolling_origin", 0) >= 3, design_counts.get("rolling_origin", 0), ">=3", "Expanding rolling-origin folds are deterministic and time ordered.")
    add("A1-VAL-002", "critical", "validation_design", design_counts.get("leave_profile_out", 0) == 14, design_counts.get("leave_profile_out", 0), 14, "One held-out fold per profile.")
    zone_count = int(zone_map["zone_id"].nunique())
    add("A1-VAL-003", "critical", "validation_design", design_counts.get("leave_zone_out", 0) == zone_count >= 2, design_counts.get("leave_zone_out", 0), zone_count, "One held-out fold per frozen geometric proxy zone.")
    return checks


def build_findings(
    bundle: CanonicalBundle,
    split_summary: pd.DataFrame,
    membership_mapping: pd.DataFrame,
    drift: pd.DataFrame,
    zone_metadata: Mapping[str, Any],
) -> list[dict[str, Any]]:
    features = bundle.features
    t5 = bundle.early_warning_labels
    mismatch_tokens = int(
        features["sample_id"].astype(str).str.rsplit("::", n=1).str[-1].ne(features["target_campaign_id"].astype(str)).sum()
    )
    terrain_missing = float(features["terrain_TRI_relative"].isna().mean())
    lithology_uncertainty_missing = float(features["lithology__standard_uncertainty"].isna().mean())
    t5_positive = int(t5.loc[t5["horizon_complete"].eq(True), "onset_180d"].eq(1).sum())
    test_complete = split_summary[(split_summary["task"].eq("T5")) & (split_summary["split"].eq("test_complete"))]
    test_positive = int(test_complete["positive"].iloc[0])
    largest_drift = (
        drift[drift["task"].eq("T1") & drift["comparison_split"].eq("test")]
        .sort_values("absolute_smd", ascending=False, na_position="last")
        .head(1)
    )
    drift_feature = str(largest_drift["feature"].iloc[0])
    drift_smd = float(largest_drift["absolute_smd"].iloc[0])
    reconciliation = membership_reconciliation_summary(membership_mapping)
    return [
        finding("A1-F-001", "high", "dependency", "CONTROLLED", "high", f"{len(features)} origins collapse to {features['point_id'].nunique()} point trajectories and {features['profile_id'].nunique()} profiles.", "Row-wise uncertainty estimates would be overconfident and random splits would leak trajectory identity.", "Use temporal manifests, rolling origin, leave-profile-out, and leave-zone-out only."),
        finding("A1-F-002", "high", "referential_integrity", "CONTROLLED", "high", f"{reconciliation['membership_rows_without_adjustment']} inconsistent membership rows map to {reconciliation['affected_model_origins']} unlabeled origins; {reconciliation['reference_rows_outside_model_universe']} are REF and {reconciliation['work_rows_without_eligible_origin']} WORK rows have no eligible prior origin.", "The previously disconnected counts 18 and 6 can now be audited row by row.", "Keep the 6 origins out of loss; patch membership status in the next source-data revision."),
        finding("A1-F-003", "high", "identifier_semantics", "OPEN", "high", f"{mismatch_tokens} sample_id values encode a historical target token different from canonical target_campaign_id.", "Humans or code that parses sample_id may silently recover the wrong target semantics.", "Treat sample_id as an opaque join key in v1; regenerate IDs and version all manifests in a future data revision."),
        finding("A1-F-004", "medium", "completeness", "OPEN", "high", f"terrain_TRI_relative missingness={terrain_missing:.3%}; lithology uncertainty missingness={lithology_uncertainty_missing:.3%}.", "Imputation and missingness indicators will materially affect several feature families.", "Fit imputation on train only, retain missing indicators, and report ablations for sparse terrain/uncertainty fields."),
        finding("A1-F-005", "high", "class_balance", "OPEN", "high", f"T5 has {t5_positive} complete positive labels in total and only {test_positive} in test_complete.", "Headline classification metrics will have very high variance and threshold tuning is fragile.", "Use average precision, fixed-FPR recall, confidence intervals, and treat T5 conclusions as exploratory."),
        finding("A1-F-006", "high", "temporal_drift", "OPEN", "high", f"Largest T1 train-to-test numeric drift is {drift_feature} with |SMD|={drift_smd:.3f}.", "Temporal generalization is harder than within-period validation and must drive model selection.", "Report drift-aware temporal results and avoid random resampling."),
        finding("A1-F-007", "medium", "spatial_validation", "CONTROLLED", "high", f"No authoritative zone_id exists; {zone_metadata['version']} freezes coordinate-median quadrants for split-only use.", "The proxy supports reproducible spatial OOD checks but is not an engineering zoning claim.", "Replace with a domain-governed zone map in a new split version when available."),
        finding("A1-F-008", "medium", "test_governance", "CONTROLLED", "high", "The model-facing loader seals test until a matching frozen-candidate record is present; raw source CSVs remain audit-readable.", "Accidental model/test coupling is blocked in project APIs, but deliberate direct file access is not cryptographically prevented.", "Run models through skru1.splits.load_split_dataset and retain source-code scanning in CI."),
    ]


def grain_summary(bundle: CanonicalBundle) -> dict[str, Any]:
    features = bundle.features
    per_point = features.groupby("point_id").size()
    shared_campaign_dates = features.groupby("current_date")["profile_id"].nunique()
    return {
        "origin_rows": int(len(features)),
        "unique_sample_ids": int(features["sample_id"].nunique()),
        "point_trajectories": int(features["point_id"].nunique()),
        "profiles": int(features["profile_id"].nunique()),
        "origins_per_point_min": int(per_point.min()),
        "origins_per_point_median": float(per_point.median()),
        "origins_per_point_max": int(per_point.max()),
        "points_repeated_over_time": int(per_point.gt(1).sum()),
        "current_dates_shared_by_multiple_profiles": int(shared_campaign_dates.gt(1).sum()),
    }


def membership_reconciliation_summary(mapping: pd.DataFrame) -> dict[str, int]:
    return {
        "membership_rows_without_adjustment": int(len(mapping)),
        "affected_model_origins": int(mapping["maps_to_unlabeled_origin"].sum()),
        "reference_rows_outside_model_universe": int(mapping["mapping_reason"].eq("reference_point_outside_WORK_model_universe").sum()),
        "work_rows_without_eligible_origin": int(mapping["mapping_reason"].eq("no_eligible_prior_origin_in_canonical_candidate_frame").sum()),
    }


def continuous_distribution(values: pd.Series) -> dict[str, float | int]:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return {
        "count": int(len(numeric)),
        "min": float(numeric.min()),
        "p05": float(numeric.quantile(0.05)),
        "p25": float(numeric.quantile(0.25)),
        "median": float(numeric.median()),
        "mean": float(numeric.mean()),
        "p75": float(numeric.quantile(0.75)),
        "p95": float(numeric.quantile(0.95)),
        "max": float(numeric.max()),
        "std": float(numeric.std(ddof=0)),
    }


def pairwise_overlap_count(split_to_ids: Mapping[str, Iterable[str]]) -> int:
    sets = {key: set(map(str, values)) for key, values in split_to_ids.items()}
    names = list(sets)
    return sum(len(sets[left] & sets[right]) for index, left in enumerate(names) for right in names[index + 1 :])


def check_row(
    check_id: str,
    severity: str,
    dimension: str,
    passed: bool,
    observed: Any,
    expected: Any,
    details: str,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "severity": severity,
        "dimension": dimension,
        "status": "PASS" if bool(passed) else "FAIL",
        "observed": observed,
        "expected": expected,
        "details": details,
    }


def finding(
    finding_id: str,
    severity: str,
    dimension: str,
    status: str,
    confidence: str,
    evidence: str,
    impact: str,
    remediation: str,
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "severity": severity,
        "dimension": dimension,
        "status": status,
        "confidence": confidence,
        "evidence": evidence,
        "impact": impact,
        "remediation": remediation,
    }


def join_row(
    check_id: str,
    left_source: str,
    right_source: str,
    join_keys: str,
    left_rows: int,
    matched_rows: int,
    *,
    status: str | None = None,
    details: str = "",
) -> dict[str, Any]:
    unmatched = int(left_rows - matched_rows)
    return {
        "check_id": check_id,
        "left_source": left_source,
        "right_source": right_source,
        "join_keys": join_keys,
        "left_rows": int(left_rows),
        "matched_rows": int(matched_rows),
        "unmatched_rows": unmatched,
        "coverage": _safe_ratio(matched_rows, left_rows),
        "status": status or ("PASS" if unmatched == 0 else "WARN"),
        "details": details,
    }


def render_russian_report(
    report: Mapping[str, Any],
    split_summary: pd.DataFrame,
    findings: Sequence[Mapping[str, Any]],
    validation_design: pd.DataFrame,
) -> str:
    reconciliation = report["membership_reconciliation"]
    grain = report["grain"]
    split_columns = [
        "task",
        "split",
        "rows",
        "current_date_min",
        "current_date_max",
        "target_date_min",
        "target_date_max",
        "points",
        "profiles",
        "missing_feature_fraction",
        "positive",
        "negative",
        "censored",
    ]
    design_counts = validation_design.groupby("design")["fold_id"].nunique().reset_index(name="folds")
    findings_table = pd.DataFrame(findings)[["finding_id", "severity", "status", "evidence", "remediation"]]
    lines = [
        "# Gate A1 — аудит качества данных и leakage-контракт",
        "",
        f"**Вердикт: `{report['status']}`. Критических сбоев: {report['summary']['critical_failures']}.**",
        "",
        "Gate A1 подтверждает пригодность канонических T1-данных для первого baseline-контура при обязательном использовании frozen manifests. T5 подготовлена технически, но из-за малого числа положительных случаев её выводы должны оставаться исследовательскими. Test закрыт model-facing загрузчиком до фиксации финального кандидата.",
        "",
        "## 1. Канонические источники",
        "",
        "Основная выборка строится только из `next_planned_features.csv`, `next_planned_operational_targets.csv`, `formal_feature_contract.csv` и `target_contract.json`. `early_warning_labels_formal.csv` является канонической таблицей labels для T5. Старые `next_cycle_features.csv` и `next_cycle_targets.csv` зарегистрированы как `historical_comparison_only` и не возвращаются модельным загрузчиком.",
        "",
        "Каждый вход записан в `gate_a1_report.json` с относительным путём и SHA-256.",
        "",
        "## 2. Grain и зависимости",
        "",
        f"Каноническая таблица содержит {grain['origin_rows']} origin-строк, {grain['point_trajectories']} временных траекторий точек и {grain['profiles']} профилей. Все {grain['points_repeated_over_time']} точек повторяются во времени; {grain['current_dates_shared_by_multiple_profiles']} campaign dates одновременно представлены более чем в одном профиле. Поэтому строка не является независимой статистической единицей.",
        "",
        "Обычные `KFold`, `train_test_split`, `ShuffleSplit` и `shuffle=True` запрещены и контролируются тестами/сканированием исходников. Статические признаки неизменны внутри point trajectory и не должны создавать иллюзию дополнительных независимых наблюдений.",
        "",
        "## 3. Frozen split manifests",
        "",
        markdown_table(split_summary[split_columns]),
        "",
        "`missing_feature_fraction` рассчитана по всем ячейкам исполняемого allowlist. SHA-256 упорядоченного списка `sample_id`, диапазоны дат и распределения target находятся в полном `split_summary.csv` и JSON-отчёте.",
        "",
        "T1 manifests содержат только 1 216 строк с `target_available=True` и статусом `observed`: train=911, validation=130, test=175. T5: train=942 complete, validation=211 complete, test_complete=28, test_censored=93.",
        "",
        "## 4. Почему 18 membership-строк превращаются в 6 origins",
        "",
        f"По ключу `(campaign_id, point_id)` найдено {reconciliation['membership_rows_without_adjustment']} строк `observed=True` без строки в `leveling_adjusted_epochs.csv`. Из них {reconciliation['affected_model_origins']} являются целевой плановой эпохой для существующего WORK-origin и поэтому дают ровно 6 строк `observed_but_no_adjusted_leveling`. Ещё {reconciliation['reference_rows_outside_model_universe']} относятся к REF-точкам, не входящим в модельный WORK-universe, а {reconciliation['work_rows_without_eligible_origin']} WORK-строк не имеют допустимого предыдущего origin в каноническом candidate frame.",
        "",
        "Полная построчная трассировка сохранена в `artifacts/data_quality/membership_inconsistency_mapping.csv`. Шесть затронутых origins не входят ни в один T1 manifest и не участвуют в loss.",
        "",
        "## 5. Leakage и preprocessing",
        "",
        "`formal_feature_contract.csv` исполняется кодом: estimator получает ровно строки `allowed=True`. Идентификаторы, campaign IDs, private/generator поля, `true_*`, onset/regime/process fields и outcome-поля блокируются. Единственное target-поле в estimator — `target_campaign_type`, которое контракт трактует как заранее известную часть замороженного плана наблюдений; `forecast_horizon_days` также известен в момент прогноза.",
        "",
        "`TrainOnlyPreprocessor.fit()` принимает только `ManifestDataset` со split=`train`. Fit на validation/test завершается ошибкой. Model-facing test load требует frozen-candidate record с совпадающими хэшами train, validation и feature contract.",
        "",
        "> Ограничение: это программный барьер воспроизводимого проекта. Исходные CSV физически читаемы для аудита и не являются криптографически blinded.",
        "",
        "## 6. Схемы оценки",
        "",
        markdown_table(design_counts),
        "",
        "Rolling-origin использует expanding window и никогда не помещает более позднюю дату в train относительно validation. Leave-profile-out создаёт 14 folds. Так как авторитетного `zone_id` для 98 WORK-точек нет, leave-zone-out v1 использует четыре замороженных геометрических квадранта, определённых медианами `x_local_m/y_local_m`. Координаты служат только split metadata и запрещены как estimator features; эти proxy-зоны не являются инженерным районированием.",
        "",
        "## 7. Основные находки и ограничения",
        "",
        markdown_table(findings_table),
        "",
        "Особенно важно: 58 `sample_id` сохранили target-токен старой next-available семантики, который не совпадает с каноническим `target_campaign_id`. В v1 ID допустим только как непрозрачный ключ. Парсить из него target campaign запрещено.",
        "",
        "## 8. Воспроизведение",
        "",
        "```powershell",
        ".\\.venv\\Scripts\\python.exe scripts\\run_gate_a1.py --root .",
        ".\\.venv\\Scripts\\python.exe scripts\\build_gate_a1_notebook.py --root .",
        ".\\.venv\\Scripts\\python.exe -m pytest",
        "```",
        "",
        "Машинный источник истины: `artifacts/data_quality/gate_a1_report.json`. Notebook предназначен для инспекции и не заменяет исполняемые тесты.",
        "",
    ]
    return "\n".join(lines)


def markdown_table(frame: pd.DataFrame) -> str:
    display = frame.copy().fillna("")
    columns = [str(column) for column in display.columns]
    header = "| " + " | ".join(_escape_markdown(column) for column in columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    rows = [
        "| " + " | ".join(_escape_markdown(_display_value(value)) for value in row) + " |"
        for row in display.itertuples(index=False, name=None)
    ]
    return "\n".join([header, separator, *rows])


def captures_assertion(function: Callable[[], Any]) -> tuple[bool, str]:
    try:
        function()
    except Exception as exc:  # evidence is retained in the machine check
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def captures_exception(function: Callable[[], Any], expected: type[BaseException]) -> bool:
    try:
        function()
    except expected:
        return True
    except Exception:
        return False
    return False


def raises_unsafe_split(function: Callable[..., Any]) -> bool:
    return captures_exception(function, UnsafeSplitError)


def _validate_existing_manifests(
    bundle: CanonicalBundle,
    expected: Mapping[tuple[str, str], pd.DataFrame],
) -> dict[tuple[str, str], SplitProvenance]:
    from skru1.splits import read_manifest

    evidence: dict[tuple[str, str], SplitProvenance] = {}
    for (task, split), expected_frame in expected.items():
        path = bundle.root / MANIFEST_LAYOUT[task][split]
        existing = read_manifest(path)
        ids = tuple(existing["sample_id"].astype(str))
        if ids != tuple(expected_frame["sample_id"].astype(str)):
            raise RuntimeError(f"Frozen manifest differs from canonical inputs: {path}")
        evidence[(task, split)] = SplitProvenance(
            task=task,
            split=split,
            version=str(bundle.config["split_contract"][f"{task}_version"]),
            manifest_path=path,
            manifest_file_sha256=sha256_file(path),
            sample_ids_sha256=sample_id_list_sha256(ids),
            row_count=len(ids),
            test_authorized=False,
        )
    return evidence


def _write_frozen_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = frame.to_csv(index=False, lineterminator="\n")
    if path.exists() and path.read_text(encoding="utf-8") != payload:
        raise RuntimeError(f"Refusing to change frozen split metadata: {path}")
    if not path.exists():
        path.write_text(payload, encoding="utf-8", newline="\n")


def _write_frozen_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_json_ready(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != text:
        raise RuntimeError(f"Refusing to change frozen split metadata: {path}")
    if not path.exists():
        path.write_text(text, encoding="utf-8", newline="\n")


def write_csv(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, lineterminator="\n", encoding="utf-8")


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(_json_ready(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def compact_json(payload: Any) -> str:
    return json.dumps(_json_ready(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if pd.isna(value) if not isinstance(value, (str, bytes)) else False:
        return None
    return value


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [_json_ready(record) for record in frame.to_dict(orient="records")]


def _date_min(values: pd.Series) -> str | None:
    dates = pd.to_datetime(values, errors="coerce").dropna()
    return dates.min().date().isoformat() if not dates.empty else None


def _date_max(values: pd.Series) -> str | None:
    dates = pd.to_datetime(values, errors="coerce").dropna()
    return dates.max().date().isoformat() if not dates.empty else None


def _safe_ratio(numerator: int | float, denominator: int | float) -> float:
    return float(numerator / denominator) if denominator else math.nan


def _git_state(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.strip()

    revision = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return {"commit": revision or None, "working_tree_dirty": bool(status)}


def _display_value(value: Any) -> str:
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        return f"{value:.6g}"
    return str(value)


def _escape_markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
