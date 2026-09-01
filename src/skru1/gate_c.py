"""Gate C0: governance freeze and audit for full sequence models."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
import yaml

from .artifact_io import (
    artifact_inventory,
    resolve_repo_path,
    snapshot_paths,
    write_csv_atomic,
    write_json_atomic,
)
from .data_contracts import ContractViolation, discover_project_root, load_canonical_bundle, sha256_file
from .sequences import (
    SequenceBundle,
    assert_early_stopping_scope,
    assert_gate_c_data_boundary,
    build_fold_sequence_contracts,
    build_sequence_bundle,
    make_sequence_contract_payload,
    validate_sequence_bundle,
    write_frozen_csv,
    write_frozen_json,
)


def load_gate_c_config(root: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    project_root = discover_project_root(root)
    path = project_root / "configs" / "gate_c.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ContractViolation("configs/gate_c.yaml must contain a mapping")
    required = {
        "gate",
        "task",
        "source_split",
        "split_version",
        "artifact_version",
        "data_boundary",
        "governance",
        "sequence_contract",
        "preprocessing",
        "early_stopping",
        "resampling",
        "architecture_registry",
        "protected_roots",
        "artifacts",
    }
    missing = required - set(config)
    if missing:
        raise ContractViolation(f"Gate C config is missing keys: {sorted(missing)}")
    assert_gate_c_data_boundary(str(config["source_split"]))
    if config["data_boundary"].get("training_enabled_in_gate_c0") is not False:
        raise ContractViolation("Gate C0 must freeze the protocol without model training")
    if config["environment"].get("external_pretrained_models_allowed") is not False:
        raise ContractViolation("Gate C0 permits only local train-from-scratch sequence models")
    assert_early_stopping_scope(str(config["early_stopping"]["allowed_scope"]), config)
    _validate_governance(config)
    _validate_repository_relative_paths(project_root, config)
    return project_root, config


def run_gate_c_freeze(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze causal sequence tables, fold bindings, hashes, and predecessor snapshot."""

    paths = _paths(root, config)
    protected_before = snapshot_paths(root, config["protected_roots"])
    canonical = load_canonical_bundle(root)
    sequence = build_sequence_bundle(root, config, canonical)
    fold_contracts = build_fold_sequence_contracts(root, sequence, config)
    write_frozen_csv(paths["sequence_manifest"], sequence.manifest)
    write_frozen_csv(paths["sequence_rows"], sequence.rows)
    write_frozen_csv(paths["fold_sequence_contracts"], fold_contracts)
    contract = make_sequence_contract_payload(
        root,
        sequence,
        fold_contracts,
        config,
        canonical,
        sequence_manifest_path=paths["sequence_manifest"],
        sequence_rows_path=paths["sequence_rows"],
        fold_contracts_path=paths["fold_sequence_contracts"],
    )
    write_frozen_json(paths["sequence_contract"], contract)
    write_frozen_json(paths["protected_snapshot"], protected_before)
    split_files = [
        paths["sequence_manifest"],
        paths["sequence_rows"],
        paths["sequence_contract"],
        paths["fold_sequence_contracts"],
    ]
    write_csv_atomic(
        root,
        paths["split_inventory"],
        artifact_inventory(root, split_files),
        work_scope="gate_c0",
    )
    protected_after = snapshot_paths(root, config["protected_roots"])
    if protected_before != protected_after:
        raise ContractViolation("Protected Gate A/B artifacts changed during Gate C0 freeze")
    outer = fold_contracts.loc[fold_contracts["level"].eq("outer")]
    return {
        "phase": "freeze",
        "status": "PASS_PROTOCOL_FROZEN",
        "split_version": str(config["split_version"]),
        "origins": len(sequence.manifest),
        "normalized_rows": len(sequence.rows),
        "history_length_range": [
            int(sequence.manifest["history_length_raw"].min()),
            int(sequence.manifest["history_length_raw"].max()),
        ],
        "outer_folds": len(outer),
        "inner_folds": int(fold_contracts["level"].eq("inner").sum()),
        "sequence_contract_sha256": contract["contract_sha256"],
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "model_training_calls": 0,
    }


def run_gate_c_analysis(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Create architecture eligibility and data-geometry evidence without fitting models."""

    paths = _paths(root, config)
    _require_frozen(paths)
    protected = json.loads(paths["protected_snapshot"].read_text(encoding="utf-8"))
    if protected != snapshot_paths(root, config["protected_roots"]):
        raise ContractViolation("Protected predecessor snapshot changed before Gate C0 analysis")
    manifest = pd.read_csv(paths["sequence_manifest"])
    sequence_rows = pd.read_csv(paths["sequence_rows"])
    fold_contracts = pd.read_csv(paths["fold_sequence_contracts"])
    architecture = architecture_eligibility_table(manifest, config)
    lengths = sequence_length_distribution(manifest)
    gaps = sequence_gap_summary(sequence_rows)
    folds = fold_summary(fold_contracts)
    write_csv_atomic(root, paths["architecture_eligibility"], architecture, work_scope="gate_c0")
    write_csv_atomic(root, paths["length_distribution"], lengths, work_scope="gate_c0")
    write_csv_atomic(root, paths["gap_summary"], gaps, work_scope="gate_c0")
    write_csv_atomic(root, paths["fold_summary"], folds, work_scope="gate_c0")
    report = gate_c0_report(
        root,
        config,
        manifest=manifest,
        sequence_rows=sequence_rows,
        fold_contracts=fold_contracts,
        architecture=architecture,
        protected_snapshot=protected,
    )
    write_json_atomic(root, paths["gate_report"], report, work_scope="gate_c0")
    if protected != snapshot_paths(root, config["protected_roots"]):
        raise ContractViolation("Protected predecessor snapshot changed during Gate C0 analysis")
    return {
        "phase": "analyze",
        "status": report["status"],
        "eligible_required_architectures": int(
            architecture["status"].eq("REQUIRED_COMPACT_SCREEN").sum()
        ),
        "conditional_architectures": int(
            architecture["status"].eq("CONDITIONAL_COMPACT_SCREEN").sum()
        ),
        "not_eligible_architectures": int(
            architecture["status"].eq("NOT_ELIGIBLE_DATA_GEOMETRY").sum()
        ),
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "model_training_calls": 0,
    }


def run_gate_c_validation(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Independently rebuild causal inputs and validate frozen artifacts byte-for-byte."""

    paths = _paths(root, config)
    _require_frozen(paths, include_analysis=True)
    protected = json.loads(paths["protected_snapshot"].read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    def check(check_id: str, passed: bool, detail: str) -> None:
        checks.append({"check_id": check_id, "status": "PASS" if passed else "FAIL", "detail": detail})
        if not passed:
            raise ContractViolation(f"Gate C0 validation failed: {check_id}: {detail}")

    canonical = load_canonical_bundle(root)
    regenerated = build_sequence_bundle(root, config, canonical)
    regenerated_folds = build_fold_sequence_contracts(root, regenerated, config)
    saved_manifest = pd.read_csv(paths["sequence_manifest"])
    saved_rows = pd.read_csv(paths["sequence_rows"])
    saved_folds = pd.read_csv(paths["fold_sequence_contracts"])
    saved_bundle = SequenceBundle(
        manifest=saved_manifest,
        rows=saved_rows,
        source=regenerated.source,
        max_sequence_length=int(config["sequence_contract"]["max_sequence_length"]),
    )
    proof = validate_sequence_bundle(saved_bundle, config=config, canonical=canonical)
    check("sequence_contract_runtime", proof["status"] == "PASS", "causal manifest and masks validated")
    check(
        "sequence_manifest_reproducible",
        _frame_text_sha256(regenerated.manifest) == sha256_file(paths["sequence_manifest"]),
        "regenerated sequence manifest matches frozen file",
    )
    check(
        "sequence_rows_reproducible",
        _frame_text_sha256(regenerated.rows) == sha256_file(paths["sequence_rows"]),
        "regenerated normalized sequence rows match frozen file",
    )
    check(
        "fold_contracts_reproducible",
        _frame_text_sha256(regenerated_folds) == sha256_file(paths["fold_sequence_contracts"]),
        "regenerated fold-sequence contracts match frozen file",
    )
    check(
        "saved_fold_counts",
        len(saved_folds.loc[saved_folds["level"].eq("outer")]) == 65
        and len(saved_folds.loc[saved_folds["level"].eq("inner")]) == 195,
        "exact 65 outer and 195 inner folds",
    )
    check(
        "strict_forward_only",
        saved_folds["forward_only"].map(_as_bool).all(),
        "all outer and inner folds are forward-only",
    )
    check(
        "held_groups_excluded",
        saved_folds["held_group_absent_from_train"].map(_as_bool).all()
        and saved_folds["held_group_validation_contract"].map(_as_bool).all(),
        "held profile/zone is absent from train and follows validation contract",
    )
    check(
        "no_validation_or_test_access",
        proof["historical_validation_loaded"] is False and proof["current_test_loaded"] is False,
        "only t1_v1/train was loaded",
    )
    check(
        "no_model_training",
        json.loads(paths["gate_report"].read_text(encoding="utf-8"))["model_training_calls"] == 0,
        "Gate C0 contains no fit calls",
    )
    check(
        "predecessor_snapshot_unchanged",
        protected == snapshot_paths(root, config["protected_roots"]),
        "suite v4, B0-B6, B5 folds, and holdout v3 remain unchanged",
    )
    contract = json.loads(paths["sequence_contract"].read_text(encoding="utf-8"))
    contract_digest = contract.pop("contract_sha256")
    recomputed_contract_digest = sha256(
        json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    check(
        "sequence_contract_digest",
        contract_digest == recomputed_contract_digest,
        "sequence contract self-digest is valid",
    )
    validation = {
        "schema_version": 1,
        "gate": str(config["gate"]),
        "status": "PASS_PROTOCOL_FROZEN",
        "scientific_scope": "train_only_internal_research",
        "checks": checks,
        "check_count": len(checks),
        "failed_checks": 0,
        "origins": len(saved_manifest),
        "normalized_rows": len(saved_rows),
        "outer_folds": 65,
        "inner_folds": 195,
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "new_holdout_seen": False,
        "model_training_calls": 0,
        "suite_v4_unchanged": True,
        "holdout_v3_unchanged": True,
    }
    write_json_atomic(root, paths["validation_report"], validation, work_scope="gate_c0")
    audit_files = [
        paths["protected_snapshot"],
        paths["architecture_eligibility"],
        paths["length_distribution"],
        paths["gap_summary"],
        paths["fold_summary"],
        paths["gate_report"],
        paths["validation_report"],
    ]
    # Notebook execution is a separate, artifact-only phase.  When its report
    # and figures already exist, include them in the same durable SHA-256
    # inventory; the core validator remains runnable before notebook creation.
    delivery_root = paths["gate_report"].parent
    notebook_report = delivery_root / "notebook_execution_report.json"
    if notebook_report.exists():
        audit_files.append(notebook_report)
    figures_root = delivery_root / "figures"
    if figures_root.exists():
        audit_files.extend(sorted(path for path in figures_root.glob("*.png") if path.is_file()))
    write_csv_atomic(
        root,
        paths["artifact_inventory"],
        artifact_inventory(root, audit_files),
        work_scope="gate_c0",
    )
    return {
        "phase": "validate",
        "status": validation["status"],
        "checks": len(checks),
        "failed_checks": 0,
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "model_training_calls": 0,
    }


def architecture_eligibility_table(
    manifest: pd.DataFrame,
    config: Mapping[str, Any],
) -> pd.DataFrame:
    minimum = int(manifest["history_length_raw"].min())
    maximum = int(manifest["history_length_raw"].max())
    median = float(manifest["history_length_raw"].median())
    rows: list[dict[str, Any]] = []
    for spec in config["architecture_registry"]:
        status = str(spec["status"])
        rows.append(
            {
                "model_id": str(spec["model_id"]),
                "family": str(spec["family"]),
                "status": status,
                "probabilistic": bool(spec.get("probabilistic", False)),
                "condition": str(spec.get("condition", "")),
                "reason": str(spec.get("reason", "")),
                "grid_json": json.dumps(spec.get("grid", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "origins": len(manifest),
                "points": int(manifest["point_id"].astype(str).nunique()),
                "profiles": int(manifest["profile_id"].astype(str).nunique()),
                "history_length_min": minimum,
                "history_length_median": median,
                "history_length_max": maximum,
                "irregular_intervals": True,
                "missing_campaigns_present": True,
                "parameter_budget": int(config["compact_screen"]["max_parameter_count"]),
                "selection_data": "t1_v1/train_only",
                "model_trained_in_c0": False,
            }
        )
    result = pd.DataFrame(rows)
    if not result["model_id"].str.fullmatch(r"C\d{2}_[a-z0-9_]+").all():
        raise ContractViolation("Gate C architecture IDs must use the frozen Cnn_name scheme")
    if result["model_id"].duplicated().any():
        raise ContractViolation("Duplicate Gate C architecture model IDs")
    return result


def sequence_length_distribution(manifest: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    total = len(manifest)
    for length, frame in manifest.groupby("history_length_raw", sort=True):
        rows.append(
            {
                "history_length": int(length),
                "origins": len(frame),
                "origin_fraction": float(len(frame) / total),
                "points": int(frame["point_id"].astype(str).nunique()),
                "profiles": int(frame["profile_id"].astype(str).nunique()),
                "zones": int(frame["zone_id"].astype(str).nunique()),
                "padding_count": int(frame["padding_count"].iloc[0]),
            }
        )
    return pd.DataFrame(rows)


def sequence_gap_summary(sequence_rows: pd.DataFrame) -> pd.DataFrame:
    actual = sequence_rows.loc[pd.to_numeric(sequence_rows["padding_mask"], errors="raise").eq(0)].copy()
    actual["days_since_previous_observation"] = pd.to_numeric(
        actual["days_since_previous_observation"], errors="raise"
    )
    actual["missing_campaigns_since_previous"] = pd.to_numeric(
        actual["missing_campaigns_since_previous"], errors="raise"
    ).astype(int)
    actual["gap_day_bin"] = pd.cut(
        actual["days_since_previous_observation"],
        bins=[-1, 0, 90, 150, 210, np.inf],
        labels=["origin_token", "1_90", "91_150", "151_210", "gt_210"],
    ).astype(str)
    actual["missing_campaign_bin"] = np.select(
        [
            actual["missing_campaigns_since_previous"].eq(0),
            actual["missing_campaigns_since_previous"].eq(1),
        ],
        ["0", "1"],
        default="ge_2",
    )
    rows: list[dict[str, Any]] = []
    for dimension, column in (
        ("gap_days", "gap_day_bin"),
        ("missing_campaigns", "missing_campaign_bin"),
    ):
        for segment, frame in actual.groupby(column, sort=True):
            rows.append(
                {
                    "dimension": dimension,
                    "segment": str(segment),
                    "observation_tokens": len(frame),
                    "origins": int(frame["sample_id"].astype(str).nunique()),
                    "points": int(frame["point_id"].astype(str).nunique()),
                    "profiles": int(frame["profile_id"].astype(str).nunique()),
                    "median_delta_t_days": float(frame["days_since_previous_observation"].median()),
                    "max_missing_campaigns": int(frame["missing_campaigns_since_previous"].max()),
                }
            )
    return pd.DataFrame(rows)


def fold_summary(fold_contracts: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (level, design), frame in fold_contracts.groupby(["level", "design"], sort=True):
        rows.append(
            {
                "level": str(level),
                "design": str(design),
                "folds": int(frame["fold_id"].nunique()),
                "train_origins_min": int(frame["train_origins"].min()),
                "train_origins_median": float(frame["train_origins"].median()),
                "train_origins_max": int(frame["train_origins"].max()),
                "validation_origins_min": int(frame["validation_origins"].min()),
                "validation_origins_median": float(frame["validation_origins"].median()),
                "validation_origins_max": int(frame["validation_origins"].max()),
                "all_forward_only": bool(frame["forward_only"].map(_as_bool).all()),
                "all_held_groups_excluded": bool(
                    frame["held_group_absent_from_train"].map(_as_bool).all()
                ),
            }
        )
    return pd.DataFrame(rows)


def gate_c0_report(
    root: Path,
    config: Mapping[str, Any],
    *,
    manifest: pd.DataFrame,
    sequence_rows: pd.DataFrame,
    fold_contracts: pd.DataFrame,
    architecture: pd.DataFrame,
    protected_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    actual = sequence_rows.loc[pd.to_numeric(sequence_rows["padding_mask"], errors="raise").eq(0)].copy()
    gaps = pd.to_numeric(actual["days_since_previous_observation"], errors="raise")
    missing = pd.to_numeric(actual["missing_campaigns_since_previous"], errors="raise")
    contract = json.loads(_paths(root, config)["sequence_contract"].read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "gate": str(config["gate"]),
        "status": "PASS_PROTOCOL_FROZEN",
        "scientific_scope": "train_only_internal_research",
        "claim_boundary": "no_final_model_quality_claim_without_new_future_or_external_holdout",
        "governance": {
            "suite_v4_immutable": True,
            "suite_v4_primary": str(config["governance"]["predecessor_primary"]),
            "suite_v5_may_be_created_from_nested_train_only_evidence": True,
            "suite_v5_fallback": str(config["governance"]["fallback_primary"]),
            "primary_change_after_holdout_access": "prohibited",
            "final_holdout_v3_unchanged": True,
            "suite_v5_requires_new_holdout_policy_version": True,
        },
        "data": {
            "source": "t1_v1/train",
            "origins": len(manifest),
            "normalized_rows": len(sequence_rows),
            "observation_tokens": len(actual),
            "points": int(manifest["point_id"].astype(str).nunique()),
            "profiles": int(manifest["profile_id"].astype(str).nunique()),
            "zones": int(manifest["zone_id"].astype(str).nunique()),
            "target_dates": int(pd.to_datetime(manifest["target_date"]).nunique()),
            "history_length_min": int(manifest["history_length_raw"].min()),
            "history_length_median": float(manifest["history_length_raw"].median()),
            "history_length_max": int(manifest["history_length_raw"].max()),
            "delta_t_days_min_positive": int(gaps.loc[gaps.gt(0)].min()),
            "delta_t_days_median_positive": float(gaps.loc[gaps.gt(0)].median()),
            "delta_t_days_max": int(gaps.max()),
            "missing_campaigns_max": int(missing.max()),
            "origins_with_any_missing_campaign": int(
                actual.loc[missing.gt(0), "sample_id"].astype(str).nunique()
            ),
            "source_sample_ids_sha256": contract["source_sample_ids_sha256"],
        },
        "sequence_contract": {
            "contract_sha256": contract["contract_sha256"],
            "max_sequence_length": int(config["sequence_contract"]["max_sequence_length"]),
            "padding_side": "left",
            "future_observations_in_inputs": 0,
            "target_observations_in_inputs": 0,
            "truncated_train_sequences": int(manifest["truncated"].map(_as_bool).sum()),
            "identifier_features_in_network": [],
            "sequence_feature_channels": list(config["sequence_contract"]["sequence_feature_channels"]),
            "formal_feature_contract_sha256": contract["feature_contract_sha256"],
        },
        "benchmark": {
            "outer_counts": (
                fold_contracts.loc[fold_contracts["level"].eq("outer")]
                .groupby("design")["fold_id"]
                .nunique()
                .to_dict()
            ),
            "inner_folds": int(fold_contracts["level"].eq("inner").sum()),
            "all_forward_only": bool(fold_contracts["forward_only"].map(_as_bool).all()),
            "all_held_groups_excluded": bool(
                fold_contracts["held_group_absent_from_train"].map(_as_bool).all()
            ),
            "preprocessing_fit_scope": "each_fold_train_only",
            "early_stopping_scope": str(config["early_stopping"]["allowed_scope"]),
        },
        "architecture_prescreen": {
            "required_compact_screen": architecture.loc[
                architecture["status"].eq("REQUIRED_COMPACT_SCREEN"), "model_id"
            ].tolist(),
            "conditional_compact_screen": architecture.loc[
                architecture["status"].eq("CONDITIONAL_COMPACT_SCREEN"), "model_id"
            ].tolist(),
            "not_eligible_data_geometry": architecture.loc[
                architecture["status"].eq("NOT_ELIGIBLE_DATA_GEOMETRY"), "model_id"
            ].tolist(),
        },
        "environment": {
            "environment_id": str(config["environment"]["environment_id"]),
            "lock": str(config["environment"]["lock"]),
            "torch_wheel_index": str(config["environment"]["torch_wheel_index"]),
            "network_access_during_training": False,
        },
        "protected_predecessor_snapshot_sha256": protected_snapshot["snapshot_sha256"],
        "protected_predecessors_match": True,
        "historical_validation_loaded": False,
        "current_t1_test_loaded": False,
        "new_holdout_seen": False,
        "model_training_calls": 0,
    }


def _validate_governance(config: Mapping[str, Any]) -> None:
    governance = config["governance"]
    required_truths = (
        "gate_c_may_create_suite_v5",
        "suite_v5_freeze_must_precede_new_holdout_labels",
        "suite_v5_requires_new_holdout_policy_version",
    )
    if not all(governance.get(key) is True for key in required_truths):
        raise ContractViolation("Gate C suite-v5 governance is incomplete")
    if governance.get("predecessor_suite_mutable") is not False:
        raise ContractViolation("Suite v4 must remain immutable")
    if governance.get("fallback_primary") != "B7_two_regime_imm":
        raise ContractViolation("Gate C fallback primary must remain frozen B7")
    if governance.get("primary_change_after_holdout_access") != "prohibited":
        raise ContractViolation("Primary changes after holdout access must be prohibited")


def _validate_repository_relative_paths(root: Path, config: Mapping[str, Any]) -> None:
    candidates = [
        config["sequence_contract"]["chronology_source"],
        config["sequence_contract"]["membership_source"],
        config["sequence_contract"]["origin_source"],
        config["sequence_contract"]["source_manifest"],
        *config["sequence_contract"]["fold_provenance"].values(),
        config["environment"]["lock"],
        *config["protected_roots"],
        *config["preregistered_sources"],
        *config["artifacts"].values(),
    ]
    for value in candidates:
        if value == "normalized_hash_join_by_sample_id":
            continue
        resolve_repo_path(root, value)


def _paths(root: Path, config: Mapping[str, Any]) -> dict[str, Path]:
    return {
        str(name): resolve_repo_path(root, relative)
        for name, relative in config["artifacts"].items()
    }


def _require_frozen(paths: Mapping[str, Path], *, include_analysis: bool = False) -> None:
    required = [
        "sequence_manifest",
        "sequence_rows",
        "sequence_contract",
        "fold_sequence_contracts",
        "protected_snapshot",
    ]
    if include_analysis:
        required.extend(
            [
                "architecture_eligibility",
                "length_distribution",
                "gap_summary",
                "fold_summary",
                "gate_report",
            ]
        )
    missing = [str(paths[name]) for name in required if not paths[name].is_file()]
    if missing:
        raise FileNotFoundError(f"Run earlier Gate C0 phases first: {missing}")


def _frame_text_sha256(frame: pd.DataFrame) -> str:
    return sha256(frame.to_csv(index=False, lineterminator="\n").encode("utf-8")).hexdigest()


def _as_bool(value: Any) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    lowered = str(value).strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no", ""}:
        return False
    raise ContractViolation(f"Cannot parse boolean value: {value!r}")
