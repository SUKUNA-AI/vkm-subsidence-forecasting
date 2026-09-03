"""Independent scoring, temporal metrics, and admission for Gate C1."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .artifact_io import write_csv_atomic, write_json_atomic
from .benchmark_metrics import interval_score, mase_denominator_from_train, point_metrics
from .b6_evaluation import precision_weights_from_train
from .data_contracts import ContractViolation, sha256_file
from .gate_c1_interfaces import (
    C1_REQUIRED_MODELS,
    C1_SEEDS,
    SequencePredictionBundle,
    TemporalAdmissionRecord,
    canonical_json_sha256,
    ordered_sample_hash,
)
from .gate_c1_probabilistic import quantile_grid_crps, student_t_nll
from .splits import load_split_dataset


COMPARATOR_IDS = (
    "B1_persistence_last_rate",
    "B7_two_regime_imm",
    "B8_student_t_robust_imm",
)
SCORED_METADATA_COLUMNS = (
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
)


def aggregate_and_score(
    root: Path,
    config: Mapping[str, Any],
    registry: Mapping[str, Any],
    job_manifest: Mapping[str, Any],
    protocol_freeze: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify unlabeled shards, then perform the sole outer-label join."""

    root = root.resolve()
    artifact_root = root / config["artifacts"]["root"]
    jobs = list(job_manifest["jobs"])
    registry_by_id = {item["model_id"]: item for item in registry["models"]}
    shard_frames: list[pd.DataFrame] = []
    shard_inventory_rows: list[dict[str, Any]] = []
    worker_status_rows: list[dict[str, Any]] = []
    tuning_frames: list[pd.DataFrame] = []
    oof_frames: list[pd.DataFrame] = []
    checkpoint_rows: list[dict[str, Any]] = []
    checkpoint_cache: dict[tuple[str, str], dict[str, Any]] = {}
    for job in jobs:
        model_id = str(job["model_id"])
        fold_id = str(job["outer_fold_id"])
        safe_fold = fold_id.replace(":", "_")
        status_path = artifact_root / "worker_status" / model_id / f"{safe_fold}.json"
        shard_path = artifact_root / "prediction_shards" / model_id / f"{safe_fold}.csv"
        tuning_path = artifact_root / "tuning_shards" / model_id / f"{safe_fold}.csv"
        oof_path = artifact_root / "selected_inner_oof_shards" / model_id / f"{safe_fold}.csv"
        if not all(path.is_file() for path in (status_path, shard_path, tuning_path, oof_path)):
            raise ContractViolation(f"Gate C1 has an unregistered missing job artifact: {job['job_id']}")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("status") != "COMPLETED" or status.get("outer_validation_labels_loaded_by_worker"):
            raise ContractViolation(f"Gate C1 worker status failed: {job['job_id']}")
        if status.get("unlabeled_prediction_sha256") != sha256_file(shard_path):
            raise ContractViolation(f"Gate C1 worker shard hash changed: {job['job_id']}")
        frame = pd.read_csv(shard_path)
        expected_ids = _role_ids_for_job(root, config, fold_id, role="validation")
        SequencePredictionBundle.validate(
            frame,
            expected_sample_ids=expected_ids,
            expected_model_id=model_id,
            expected_fold_id=fold_id,
        )
        if set(frame["config_sha256"].astype(str)) != {protocol_freeze["config_sha256"]}:
            raise ContractViolation("Gate C1 shard config hash differs from protocol freeze")
        if set(frame["code_sha256"].astype(str)) != {protocol_freeze["code_sha256"]}:
            raise ContractViolation("Gate C1 shard code hash differs from protocol freeze")
        if set(frame["environment_sha256"].astype(str)) != {
            protocol_freeze["environment_sha256"]
        }:
            raise ContractViolation("Gate C1 shard environment hash differs from preflight")
        shard_frames.append(frame)
        tuning_frame = pd.read_csv(tuning_path)
        tuning_frames.append(tuning_frame)
        for record in tuning_frame[
            ["checkpoint_manifest", "checkpoint_manifest_sha256"]
        ].drop_duplicates().to_dict("records"):
            manifest = _load_checkpoint_manifest(
                root,
                relative_path=str(record["checkpoint_manifest"]),
                expected_sha256=str(record["checkpoint_manifest_sha256"]),
                expected_role="inner",
                cache=checkpoint_cache,
            )
            checkpoint_rows.append(_checkpoint_inventory_row(root, manifest))
        outer_manifests = status.get("outer_checkpoint_manifests", [])
        if len(outer_manifests) != len(C1_SEEDS):
            raise ContractViolation(f"Gate C1 outer checkpoints are incomplete: {job['job_id']}")
        for record in outer_manifests:
            manifest = _load_checkpoint_manifest(
                root,
                relative_path=str(record["path"]),
                expected_sha256=str(record["sha256"]),
                expected_role="outer",
                cache=checkpoint_cache,
            )
            if int(manifest["selected_epoch"]) != int(status["outer_epoch_count"]):
                raise ContractViolation("Gate C1 outer checkpoint changed fixed-epoch selection")
            checkpoint_rows.append(_checkpoint_inventory_row(root, manifest))
        oof_frames.append(pd.read_csv(oof_path))
        worker_status_rows.append(_flatten_worker_status(status))
        shard_inventory_rows.append(
            {
                "job_id": job["job_id"],
                "model_id": model_id,
                "fold_id": fold_id,
                "path": shard_path.relative_to(root).as_posix(),
                "bytes": shard_path.stat().st_size,
                "sha256": sha256_file(shard_path),
                "rows": len(frame),
                "seeds": int(frame["seed"].nunique()),
                "expected_sample_ids_sha256": ordered_sample_hash(expected_ids),
                "contains_y_true": False,
                "hash_frozen_before_label_access": True,
            }
        )
    single_seed = pd.concat(shard_frames, ignore_index=True)
    checkpoint_inventory = (
        pd.DataFrame(checkpoint_rows)
        .drop_duplicates("fit_id", keep="first")
        .sort_values(["role", "model_id", "fold_id", "seed", "fit_id"], kind="mergesort")
        .reset_index(drop=True)
    )
    if (
        len(checkpoint_inventory) != 3860
        or checkpoint_inventory["fit_id"].nunique() != 3860
        or int(checkpoint_inventory["role"].eq("inner").sum()) != 3640
        or int(checkpoint_inventory["role"].eq("outer").sum()) != 220
        or not checkpoint_inventory["keep_top_k"].eq(5).all()
        or checkpoint_inventory["outer_labels_used_for_ranking"].astype(bool).any()
    ):
        raise ContractViolation("Gate C1 checkpoint inventory is incomplete or unsafe")
    expected_single = int(config["expected_counts"]["deep_single_seed_prediction_rows"])
    if len(single_seed) != expected_single:
        raise ContractViolation(f"Gate C1 deep prediction count changed: {len(single_seed)}")
    _validate_global_single_seed_completeness(single_seed, jobs)

    shard_inventory = pd.DataFrame(shard_inventory_rows).sort_values(
        ["model_id", "fold_id"], kind="mergesort"
    )
    shard_inventory_sha = canonical_json_sha256(shard_inventory.to_dict("records"))
    ledger = {
        "schema_version": 1,
        "access_event": 1,
        "access_purpose": "independent_outer_scoring_after_unlabeled_shard_hash_freeze",
        "shards_verified": len(shard_inventory),
        "shard_inventory_content_sha256": shard_inventory_sha,
        "all_shards_unlabeled": True,
        "all_shards_hash_frozen_before_access": True,
        "worker_outer_validation_labels_loaded": False,
        "scorer_source_split": "t1_v1/train",
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "new_holdout_seen": False,
        "access_is_model_facing": False,
    }
    # This is the first point at which outer-validation labels enter C1.  The
    # worker has already terminated and all 44 shard hashes are frozen above.
    source = load_split_dataset("t1", "train", root=root)
    canonical = source.frame.copy()
    indexed = canonical.set_index("sample_id", drop=False)
    labels = indexed["observed_rate_mm_y"]
    ledger["canonical_train_manifest_sha256"] = source.provenance.manifest_file_sha256
    ledger["canonical_train_sample_ids_sha256"] = source.provenance.sample_ids_sha256
    ledger["outer_label_universe_sha256"] = canonical_json_sha256(
        [
            {"sample_id": sample_id, "target": float(labels.loc[sample_id])}
            for sample_id in sorted(single_seed["sample_id"].astype(str).unique())
        ]
    )
    ledger["ledger_sha256"] = canonical_json_sha256(ledger)

    comparators = _load_comparators(root, config, canonical)
    metadata = _validation_metadata(comparators, canonical)
    deep_ensemble = build_five_seed_ensemble(single_seed)
    deep = pd.concat((single_seed, deep_ensemble), ignore_index=True, sort=False)
    deep = deep.merge(metadata, on="sample_id", how="left", validate="many_to_one")
    if deep[list(SCORED_METADATA_COLUMNS)].isna().any().any():
        raise ContractViolation("Gate C1 scorer could not attach complete canonical metadata")
    deep["y_true"] = pd.to_numeric(labels.loc[deep["sample_id"].astype(str)].to_numpy(), errors="raise")
    scored = pd.concat((deep, comparators), ignore_index=True, sort=False)
    scored = scored.sort_values(
        ["model_id", "aggregation", "seed", "fold_id", "sample_id"], kind="mergesort"
    ).reset_index(drop=True)
    if len(scored) != int(config["expected_counts"]["scored_prediction_rows"]):
        raise ContractViolation("Gate C1 total scored prediction row count changed")
    if scored.duplicated(["model_id", "aggregation", "seed", "fold_id", "sample_id"]).any():
        raise ContractViolation("Gate C1 scored predictions contain duplicates")

    outer_assignments = pd.read_csv(root / config["resampling"]["outer_assignments"])
    fold_metrics = temporal_fold_metrics(scored, canonical, outer_assignments)
    aggregate_metrics = temporal_aggregate_metrics(scored, fold_metrics)
    seed_stability = seed_stability_metrics(aggregate_metrics, fold_metrics)
    native = student_t_native_metrics(scored)
    statuses = pd.DataFrame(worker_status_rows)
    screening, records = temporal_screening_register(
        scored,
        fold_metrics,
        aggregate_metrics,
        statuses,
        registry_by_id,
        config,
        protocol_freeze,
    )
    rejection = screening.loc[~screening["status"].eq("PASSED_TEMPORAL_SCREEN")].copy()
    admitted_ids = screening.loc[
        screening["status"].eq("PASSED_TEMPORAL_SCREEN"), "model_id"
    ].astype(str).tolist()

    paths = config["artifacts"]
    write_csv_atomic(root, root / paths["shard_inventory"], shard_inventory, work_scope="gate_c1")
    write_json_atomic(root, root / paths["label_access_ledger"], ledger, work_scope="gate_c1")
    write_csv_atomic(
        root, root / paths["tuning_inventory"], pd.concat(tuning_frames, ignore_index=True), work_scope="gate_c1"
    )
    write_csv_atomic(
        root, root / paths["selected_inner_oof"], pd.concat(oof_frames, ignore_index=True), work_scope="gate_c1"
    )
    write_csv_atomic(root, root / paths["worker_status"], statuses, work_scope="gate_c1")
    write_csv_atomic(
        root,
        root / paths["checkpoint_inventory"],
        checkpoint_inventory,
        work_scope="gate_c1",
    )
    write_csv_atomic(root, root / paths["scored_predictions"], scored, work_scope="gate_c1")
    write_csv_atomic(root, root / paths["fold_metrics"], fold_metrics, work_scope="gate_c1")
    write_csv_atomic(root, root / paths["aggregate_metrics"], aggregate_metrics, work_scope="gate_c1")
    write_csv_atomic(root, root / paths["seed_stability"], seed_stability, work_scope="gate_c1")
    write_csv_atomic(root, root / paths["native_metrics"], native, work_scope="gate_c1")
    write_csv_atomic(root, root / paths["screening_register"], screening, work_scope="gate_c1")
    write_csv_atomic(root, root / paths["rejection_register"], rejection, work_scope="gate_c1")
    compute = compute_resource_inventory(scored, tuning=pd.concat(tuning_frames, ignore_index=True))
    write_csv_atomic(root, root / paths["compute_inventory"], compute, work_scope="gate_c1")

    prediction_hash = sha256_file(root / paths["scored_predictions"])
    metric_hash = sha256_file(root / paths["aggregate_metrics"])
    admission = {
        "schema_version": 1,
        "gate": "C1_COMPACT_SEQUENCE_TEMPORAL_SCREEN",
        "status": "PASS_C1_TEMPORAL_SCREEN",
        "scientific_scope": "train_only_internal_research",
        "admitted_model_ids": admitted_ids,
        "records": [record.to_dict() for record in records],
        "gate_c0_contract_content_sha256": config["frozen_predecessors"][
            "gate_c0_contract_content_sha256"
        ],
        "gate_c1_config_sha256": protocol_freeze["config_sha256"],
        "gate_c1_code_sha256": protocol_freeze["code_sha256"],
        "environment_sha256": protocol_freeze["environment_sha256"],
        "temporal_predictions_sha256": prediction_hash,
        "temporal_aggregate_metrics_sha256": metric_hash,
        "admission_criteria": config["temporal_admission"],
        "historical_validation_loaded": False,
        "current_test_loaded": False,
        "new_holdout_seen": False,
        "profile_zone_transition_audit_executed": False,
        "conformal_calibration_executed": False,
        "suite_v5_created": False,
        "suite_v4_primary_remains": "B7_two_regime_imm",
    }
    admission["admission_manifest_sha256"] = canonical_json_sha256(admission)
    write_json_atomic(root, root / paths["admission_manifest"], admission, work_scope="gate_c1")
    return {
        "phase": "aggregate",
        "status": "PASS_C1_TEMPORAL_SCREEN",
        "admitted_model_ids": admitted_ids,
        "scored_prediction_rows": len(scored),
        "logical_inner_evaluations": int(sum(len(frame) for frame in tuning_frames)),
        "unlabeled_shards": len(shard_inventory),
        "outer_label_access_events": 1,
        "checkpoint_manifests": len(checkpoint_inventory),
    }


def _load_checkpoint_manifest(
    root: Path,
    *,
    relative_path: str,
    expected_sha256: str,
    expected_role: str,
    cache: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    key = (relative_path, expected_sha256)
    if key in cache:
        payload = cache[key]
        if payload["role"] != expected_role:
            raise ContractViolation("Gate C1 cached checkpoint role mismatch")
        return payload
    path = (root / relative_path).resolve()
    checkpoint_root = (root / "work" / "gate_c1" / "checkpoints").resolve()
    try:
        path.relative_to(checkpoint_root)
    except ValueError as exc:
        raise ContractViolation("Gate C1 checkpoint manifest escapes work/") from exc
    if not path.is_file() or sha256_file(path) != expected_sha256:
        raise ContractViolation("Gate C1 checkpoint manifest is missing or changed")
    payload = json.loads(path.read_text(encoding="utf-8"))
    supplied = payload.pop("manifest_content_sha256", None)
    if supplied != canonical_json_sha256(payload):
        raise ContractViolation("Gate C1 checkpoint manifest content hash mismatch")
    payload["manifest_content_sha256"] = supplied
    if (
        payload.get("status") != "COMPLETE"
        or payload.get("role") != expected_role
        or int(payload.get("keep_top_k", -1)) != 5
        or payload.get("persistence_scope") != "work_only"
        or payload.get("outer_labels_used_for_ranking") is not False
    ):
        raise ContractViolation("Gate C1 checkpoint policy mismatch")
    records = payload.get("checkpoints", [])
    if not records or len(records) != int(payload.get("retained_checkpoint_count", -1)):
        raise ContractViolation("Gate C1 checkpoint retained-state count mismatch")
    for record in records:
        checkpoint = (root / str(record["path"])).resolve()
        try:
            checkpoint.relative_to(checkpoint_root)
        except ValueError as exc:
            raise ContractViolation("Gate C1 ranked checkpoint escapes work/") from exc
        if not checkpoint.is_file() or sha256_file(checkpoint) != record.get("sha256"):
            raise ContractViolation("Gate C1 ranked checkpoint hash mismatch")
    cache[key] = payload
    return payload


def _checkpoint_inventory_row(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    records = list(manifest["checkpoints"])
    return {
        "fit_id": str(manifest["fit_id"]),
        "role": str(manifest["role"]),
        "model_id": str(manifest["provenance"]["model_id"]),
        "fold_id": str(manifest["provenance"]["fold_id"]),
        "seed": int(manifest["provenance"]["seed"]),
        "manifest_path": f"work/gate_c1/checkpoints/{manifest['role']}/{manifest['fit_id']}/manifest.json",
        "manifest_content_sha256": str(manifest["manifest_content_sha256"]),
        "ranking_policy": str(manifest["ranking_policy"]),
        "selection_policy": str(manifest["selection_policy"]),
        "keep_top_k": int(manifest["keep_top_k"]),
        "retained_checkpoint_count": int(manifest["retained_checkpoint_count"]),
        "top_k_fully_populated": bool(manifest["top_k_fully_populated"]),
        "selected_epoch": int(manifest["selected_epoch"]),
        "selected_metric": float(manifest["selected_metric"]),
        "latest_stage_epoch": int(manifest["latest_stage"]["epoch"]),
        "ranked_checkpoint_bytes": int(
            sum((root / str(record["path"])).stat().st_size for record in records)
        ),
        "resumed_from_recovery": bool(manifest["resumed_from_recovery"]),
        "persistence_scope": str(manifest["persistence_scope"]),
        "outer_labels_used_for_ranking": bool(manifest["outer_labels_used_for_ranking"]),
    }


def build_five_seed_ensemble(single_seed: pd.DataFrame) -> pd.DataFrame:
    deep = single_seed.loc[single_seed["model_id"].isin(C1_REQUIRED_MODELS)].copy()
    rows: list[dict[str, Any]] = []
    for (model_id, fold_id, sample_id), frame in deep.groupby(
        ["model_id", "fold_id", "sample_id"], sort=True
    ):
        frame = frame.sort_values("seed", kind="mergesort")
        if tuple(frame["seed"].astype(int)) != C1_SEEDS:
            raise ContractViolation("Gate C1 ensemble requires exactly five ordered fixed seeds")
        record = frame.iloc[0].to_dict()
        record["seed"] = -1
        record["y_pred"] = float(frame["y_pred"].mean())
        record["fit_seconds"] = float(frame["fit_seconds"].sum())
        record["inference_seconds"] = float(frame["inference_seconds"].sum())
        record["peak_ram_mb"] = float(frame["peak_ram_mb"].max())
        record["peak_vram_mb"] = float(frame["peak_vram_mb"].max())
        record["aggregation"] = "mean_of_fixed_seeds"
        for column in (
            "distribution_family",
            "distribution_loc",
            "distribution_scale",
            "distribution_df",
            "q025",
            "q10",
            "q25",
            "q50",
            "q75",
            "q90",
            "q975",
        ):
            if column in record:
                record[column] = np.nan
        rows.append(record)
    result = pd.DataFrame(rows)
    if len(result) != 4 * 595:
        raise ContractViolation("Gate C1 deep ensemble row count changed")
    return result


def temporal_fold_metrics(
    scored: pd.DataFrame, source: pd.DataFrame, outer_assignments: pd.DataFrame
) -> pd.DataFrame:
    b1 = scored.loc[
        scored["model_id"].eq("B1_persistence_last_rate"), ["fold_id", "sample_id", "y_pred"]
    ].rename(columns={"y_pred": "b1_prediction"})
    working = scored.merge(b1, on=["fold_id", "sample_id"], how="left", validate="many_to_one")
    if working["b1_prediction"].isna().any():
        raise ContractViolation("Gate C1 metrics lack paired B1 predictions")
    indexed = source.set_index("sample_id", drop=False)
    rows = []
    for keys, frame in working.groupby(
        ["model_id", "family", "aggregation", "seed", "fold_id"], sort=True, dropna=False
    ):
        model_id, family, aggregation, seed, fold_id = keys
        train_ids = tuple(
            outer_assignments.loc[
                outer_assignments["fold_id"].astype(str).eq(str(fold_id))
                & outer_assignments["role"].eq("train"),
                "sample_id",
            ].astype(str)
        )
        training = indexed.loc[list(train_ids)].reset_index(drop=True)
        weights = precision_weights_from_train(training, frame)
        metrics = point_metrics(
            frame["y_true"],
            frame["y_pred"],
            sample_weight=weights,
            b1_prediction=frame["b1_prediction"],
            mase_denominator=mase_denominator_from_train(training),
            last_rate=frame["last_rate_mm_y"],
            neutral_zone=1.96 * pd.to_numeric(frame["sigma_rate_mm_y"], errors="coerce").fillna(0),
        )
        rows.append(
            {
                "model_id": model_id,
                "family": family,
                "aggregation": aggregation,
                "seed": int(seed),
                "fold_id": fold_id,
                "target_date": pd.Timestamp(pd.to_datetime(frame["target_date"]).iloc[0]).date().isoformat(),
                "rows": len(frame),
                "fit_seconds": float(frame["fit_seconds"].iloc[0]),
                "inference_seconds": float(frame["inference_seconds"].iloc[0]),
                "peak_ram_mb": float(frame["peak_ram_mb"].iloc[0]),
                "peak_vram_mb": float(frame["peak_vram_mb"].iloc[0]),
                "parameter_count": int(frame["parameter_count"].iloc[0]),
                "epoch_count": int(frame["epoch_count"].iloc[0]),
                **metrics,
            }
        )
    result = pd.DataFrame(rows)
    b1_folds = result.loc[
        result["model_id"].eq("B1_persistence_last_rate"), ["fold_id", "mae"]
    ].rename(columns={"mae": "b1_fold_mae"})
    result = result.merge(b1_folds, on="fold_id", how="left", validate="many_to_one")
    result["fold_mae_ratio_vs_b1"] = result["mae"] / result["b1_fold_mae"]
    return result.sort_values(
        ["model_id", "aggregation", "seed", "target_date"], kind="mergesort"
    ).reset_index(drop=True)


def temporal_aggregate_metrics(scored: pd.DataFrame, fold_metrics: pd.DataFrame) -> pd.DataFrame:
    b1 = scored.loc[
        scored["model_id"].eq("B1_persistence_last_rate"), ["sample_id", "y_pred"]
    ].rename(columns={"y_pred": "b1_prediction"})
    working = scored.merge(b1, on="sample_id", how="left", validate="many_to_one")
    rows = []
    for keys, frame in working.groupby(
        ["model_id", "family", "aggregation", "seed"], sort=True, dropna=False
    ):
        model_id, family, aggregation, seed = keys
        folds = fold_metrics.loc[
            fold_metrics["model_id"].eq(model_id)
            & fold_metrics["aggregation"].eq(aggregation)
            & fold_metrics["seed"].eq(int(seed))
        ]
        metrics = point_metrics(
            frame["y_true"],
            frame["y_pred"],
            b1_prediction=frame["b1_prediction"],
            last_rate=frame["last_rate_mm_y"],
            neutral_zone=1.96 * pd.to_numeric(frame["sigma_rate_mm_y"], errors="coerce").fillna(0),
        )
        rows.append(
            {
                "model_id": model_id,
                "family": family,
                "aggregation": aggregation,
                "seed": int(seed),
                "rolling_folds": int(folds["fold_id"].nunique()),
                "pooled_rows": len(frame),
                **metrics,
                "median_fold_mae": float(folds["mae"].median()),
                "iqr_fold_mae": float(folds["mae"].quantile(0.75) - folds["mae"].quantile(0.25)),
                "min_fold_mae": float(folds["mae"].min()),
                "max_fold_mae": float(folds["mae"].max()),
                "fold_mae_range": float(folds["mae"].max() - folds["mae"].min()),
                "max_fold_mae_ratio_vs_b1": float(folds["fold_mae_ratio_vs_b1"].max()),
                "fit_seconds_total": float(folds["fit_seconds"].sum()),
                "inference_seconds_total": float(folds["inference_seconds"].sum()),
                "peak_ram_mb_max": float(folds["peak_ram_mb"].max()),
                "peak_vram_mb_max": float(folds["peak_vram_mb"].max()),
                "parameter_count_max": int(folds["parameter_count"].max()),
                "epoch_count_median": float(folds["epoch_count"].median()),
            }
        )
    result = pd.DataFrame(rows)
    b1_row = result.loc[result["model_id"].eq("B1_persistence_last_rate")].iloc[0]
    result["pooled_mae_ratio_vs_b1"] = result["mae"] / float(b1_row["mae"])
    result["median_fold_mae_ratio_vs_b1"] = result["median_fold_mae"] / float(
        b1_row["median_fold_mae"]
    )
    return result.sort_values(["aggregation", "mae", "model_id", "seed"], kind="mergesort").reset_index(
        drop=True
    )


def seed_stability_metrics(aggregate: pd.DataFrame, folds: pd.DataFrame) -> pd.DataFrame:
    rows = []
    b1_fold = folds.loc[
        folds["model_id"].eq("B1_persistence_last_rate"), ["fold_id", "mae"]
    ].rename(columns={"mae": "b1_mae"})
    b7_fold = folds.loc[
        folds["model_id"].eq("B7_two_regime_imm"), ["fold_id", "mae"]
    ].rename(columns={"mae": "b7_mae"})
    for model_id in C1_REQUIRED_MODELS:
        seed_rows = aggregate.loc[
            aggregate["model_id"].eq(model_id) & aggregate["aggregation"].eq("single_seed")
        ].sort_values("seed")
        ensemble = aggregate.loc[
            aggregate["model_id"].eq(model_id)
            & aggregate["aggregation"].eq("mean_of_fixed_seeds")
        ].iloc[0]
        values = seed_rows["mae"].to_numpy(float)
        model_folds = folds.loc[
            folds["model_id"].eq(model_id)
            & folds["aggregation"].eq("mean_of_fixed_seeds"),
            ["fold_id", "mae"],
        ]
        paired = model_folds.merge(b1_fold, on="fold_id").merge(b7_fold, on="fold_id")
        rows.append(
            {
                "model_id": model_id,
                "seeds": len(values),
                "seed_mae_mean": float(np.mean(values)),
                "seed_mae_median": float(np.median(values)),
                "seed_mae_iqr": float(np.quantile(values, 0.75) - np.quantile(values, 0.25)),
                "seed_mae_cv": float(np.std(values, ddof=0) / np.mean(values)),
                "seed_mae_min": float(np.min(values)),
                "seed_mae_max": float(np.max(values)),
                "seed_mae_range": float(np.max(values) - np.min(values)),
                "ensemble_mae": float(ensemble["mae"]),
                "ensemble_gain_vs_median_seed": float(np.median(values) - ensemble["mae"]),
                "dates_improved_vs_b1": int((paired["mae"] < paired["b1_mae"]).sum()),
                "dates_improved_vs_b7": int((paired["mae"] < paired["b7_mae"]).sum()),
                "seed_iqr_descriptive_pass": bool(
                    np.quantile(values, 0.75) - np.quantile(values, 0.25) <= 0.50
                ),
                "seed_cv_descriptive_pass": bool(np.std(values, ddof=0) / np.mean(values) <= 0.10),
            }
        )
    return pd.DataFrame(rows).sort_values("model_id", kind="mergesort").reset_index(drop=True)


def student_t_native_metrics(scored: pd.DataFrame) -> pd.DataFrame:
    frame = scored.loc[
        scored["model_id"].eq("C04_probabilistic_gru_student_t")
        & scored["aggregation"].eq("single_seed")
    ].copy()
    rows = []
    groupers: list[tuple[str, Sequence[str]]] = [
        ("fold_seed", ["seed", "fold_id"]),
        ("seed_aggregate", ["seed"]),
    ]
    for scope, columns in groupers:
        grouper: Any = columns[0] if len(columns) == 1 else columns
        for key, group in frame.groupby(grouper, sort=True):
            key_tuple = key if isinstance(key, tuple) else (key,)
            truth = group["y_true"].to_numpy(float)
            loc = group["distribution_loc"].to_numpy(float)
            scale = group["distribution_scale"].to_numpy(float)
            df = group["distribution_df"].to_numpy(float)
            record = {
                "scope": scope,
                "seed": int(key_tuple[0]),
                "fold_id": str(key_tuple[1]) if len(key_tuple) > 1 else "ALL",
                "rows": len(group),
                "crps": float(np.mean(quantile_grid_crps(truth, loc, scale, df))),
                "nll": float(np.mean(student_t_nll(truth, loc, scale, df))),
            }
            widths = []
            scores = []
            for label, lo, hi, alpha in (
                ("50", "q25", "q75", 0.50),
                ("80", "q10", "q90", 0.20),
                ("95", "q025", "q975", 0.05),
            ):
                lower = group[lo].to_numpy(float)
                upper = group[hi].to_numpy(float)
                width = upper - lower
                score = interval_score(truth, lower, upper, alpha=alpha)
                record[f"coverage_{label}"] = float(np.mean((truth >= lower) & (truth <= upper)))
                record[f"mean_width_{label}"] = float(np.mean(width))
                record[f"median_width_{label}"] = float(np.median(width))
                record[f"interval_score_{label}"] = float(np.mean(score))
                widths.append(width)
                scores.append(score)
            record["mean_interval_width"] = float(np.mean(np.concatenate(widths)))
            record["median_interval_width"] = float(np.median(np.concatenate(widths)))
            record["mean_interval_score"] = float(np.mean(np.concatenate(scores)))
            rows.append(record)
    return pd.DataFrame(rows).sort_values(["scope", "seed", "fold_id"], kind="mergesort").reset_index(
        drop=True
    )


def temporal_screening_register(
    scored: pd.DataFrame,
    fold_metrics: pd.DataFrame,
    aggregate: pd.DataFrame,
    statuses: pd.DataFrame,
    registry_by_id: Mapping[str, Mapping[str, Any]],
    config: Mapping[str, Any],
    protocol_freeze: Mapping[str, Any],
) -> tuple[pd.DataFrame, list[TemporalAdmissionRecord]]:
    policy = config["temporal_admission"]
    rows = []
    records = []
    for model_id in C1_REQUIRED_MODELS:
        canonical = aggregate.loc[
            aggregate["model_id"].eq(model_id)
            & aggregate["aggregation"].eq("mean_of_fixed_seeds")
        ]
        model_folds = fold_metrics.loc[
            fold_metrics["model_id"].eq(model_id)
            & fold_metrics["aggregation"].eq("mean_of_fixed_seeds")
        ]
        model_single = scored.loc[
            scored["model_id"].eq(model_id) & scored["aggregation"].eq("single_seed")
        ]
        model_status = statuses.loc[statuses["model_id"].eq(model_id)]
        if canonical.empty:
            raise ContractViolation(f"Gate C1 canonical ensemble missing: {model_id}")
        observed = canonical.iloc[0]
        checks = {
            "all_11_outer_folds_complete": int(observed["rolling_folds"]) == 11,
            "all_5_seeds_present": set(model_single["seed"].astype(int)) == set(C1_SEEDS),
            "exact_expected_sample_ids": _model_seed_coverage_exact(model_single),
            "no_duplicate_predictions": not model_single.duplicated(
                ["seed", "fold_id", "sample_id"]
            ).any(),
            "finite_predictions": bool(np.isfinite(model_single["y_pred"].to_numpy(float)).all()),
            "pooled_mae_within_10_percent_b1": float(observed["pooled_mae_ratio_vs_b1"])
            <= float(policy["pooled_mae_ratio_vs_b1_max"]),
            "median_fold_mae_within_10_percent_b1": float(
                observed["median_fold_mae_ratio_vs_b1"]
            )
            <= float(policy["median_fold_mae_ratio_vs_b1_max"]),
            "no_fold_exceeds_2x_b1": float(model_folds["fold_mae_ratio_vs_b1"].max())
            <= float(policy["worst_fold_mae_ratio_vs_b1_max"]),
            "all_outer_refits_completed": len(model_status) == 11
            and model_status["status"].eq("COMPLETED").all(),
            "preprocessing_environment_leakage_checks_passed": bool(
                protocol_freeze["preflight_status"] == "PASS"
            ),
        }
        quality_keys = {
            "pooled_mae_within_10_percent_b1",
            "median_fold_mae_within_10_percent_b1",
            "no_fold_exceeds_2x_b1",
        }
        execution_keys = {"all_11_outer_folds_complete", "all_5_seeds_present", "all_outer_refits_completed"}
        protocol_keys = set(checks) - quality_keys - execution_keys
        if not all(checks[key] for key in protocol_keys):
            status = "FAIL_PROTOCOL"
        elif not all(checks[key] for key in execution_keys):
            status = "REJECTED_MODEL_EXECUTION"
        elif not all(checks[key] for key in quality_keys):
            status = "REJECTED_TEMPORAL_SCREEN"
        else:
            status = "PASSED_TEMPORAL_SCREEN"
        values = {
            "rolling_folds": int(observed["rolling_folds"]),
            "seeds": int(model_single["seed"].nunique()),
            "prediction_rows": len(model_single),
            "pooled_mae": float(observed["mae"]),
            "pooled_mae_ratio_vs_b1": float(observed["pooled_mae_ratio_vs_b1"]),
            "median_fold_mae": float(observed["median_fold_mae"]),
            "median_fold_mae_ratio_vs_b1": float(observed["median_fold_mae_ratio_vs_b1"]),
            "max_fold_mae_ratio_vs_b1": float(model_folds["fold_mae_ratio_vs_b1"].max()),
        }
        spec = registry_by_id[model_id]
        record = TemporalAdmissionRecord(
            model_id=model_id,
            status=status,
            checks=checks,
            observed=values,
            model_spec_sha256=spec["spec_sha256"],
            config_sha256=protocol_freeze["config_sha256"],
            code_sha256=protocol_freeze["code_sha256"],
            environment_sha256=protocol_freeze["environment_sha256"],
        )
        records.append(record)
        rows.append(
            {
                "model_id": model_id,
                "status": status,
                **values,
                **checks,
                "admitted_to_c2": record.admitted,
                "scientific_scope": "train_only_internal_research",
            }
        )
    result = pd.DataFrame(rows).sort_values("model_id", kind="mergesort").reset_index(drop=True)
    if result["status"].eq("FAIL_PROTOCOL").any():
        raise ContractViolation("Gate C1 temporal screen detected a protocol failure")
    return result, records


def compute_resource_inventory(scored: pd.DataFrame, *, tuning: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for model_id in C1_REQUIRED_MODELS:
        outer = scored.loc[
            scored["model_id"].eq(model_id) & scored["aggregation"].eq("single_seed")
        ].drop_duplicates(["fold_id", "seed"])
        inner = tuning.loc[tuning["model_id"].eq(model_id)]
        rows.append(
            {
                "model_id": model_id,
                "logical_inner_evaluations": len(inner),
                "physical_inner_fits_executed": int((~inner["physical_fit_reused"].astype(bool)).sum()),
                "physical_inner_fits_reused": int(inner["physical_fit_reused"].astype(bool).sum()),
                "outer_refits": len(outer),
                "inner_fit_seconds_logical_sum": float(inner["fit_seconds"].sum()),
                "outer_fit_seconds": float(outer["fit_seconds"].sum()),
                "outer_inference_seconds": float(outer["inference_seconds"].sum()),
                "peak_ram_mb": float(max(inner["peak_ram_mb"].max(), outer["peak_ram_mb"].max())),
                "peak_vram_mb": float(max(inner["peak_vram_mb"].max(), outer["peak_vram_mb"].max())),
                "parameter_count_min": int(outer["parameter_count"].min()),
                "parameter_count_max": int(outer["parameter_count"].max()),
                "epoch_count_min": int(outer["epoch_count"].min()),
                "epoch_count_max": int(outer["epoch_count"].max()),
            }
        )
    return pd.DataFrame(rows)


def _load_comparators(root: Path, config: Mapping[str, Any], canonical: pd.DataFrame) -> pd.DataFrame:
    source = pd.read_csv(root / config["comparators"]["source"])
    selected = source.loc[source["model_id"].isin(COMPARATOR_IDS)].copy()
    selected = selected.loc[selected["aggregation"].eq("single_seed")]
    if len(selected) != 3 * 595 or selected.groupby("model_id").size().ne(595).any():
        raise ContractViolation("Frozen B1/B7/B8 comparator coverage changed")
    expected_ids = set(selected.loc[selected["model_id"].eq(COMPARATOR_IDS[0]), "sample_id"].astype(str))
    for model_id, frame in selected.groupby("model_id"):
        if set(frame["sample_id"].astype(str)) != expected_ids:
            raise ContractViolation(f"Comparator universe differs for {model_id}")
    label_map = canonical.set_index("sample_id")["observed_rate_mm_y"]
    expected_truth = pd.to_numeric(label_map.loc[selected["sample_id"].astype(str)].to_numpy(), errors="raise")
    if not np.allclose(expected_truth, selected["y_true"].to_numpy(float), atol=1e-12, rtol=0):
        raise ContractViolation("Frozen comparator truths differ from canonical t1_v1/train labels")
    selected["config_sha256"] = "frozen_b6"
    selected["code_sha256"] = "frozen_b6"
    selected["environment_sha256"] = "frozen_b6"
    selected["expected_sample_ids_sha256"] = selected.groupby(["model_id", "fold_id"])[
        "sample_id"
    ].transform(lambda values: ordered_sample_hash(tuple(values.astype(str))))
    selected["epoch_count"] = pd.to_numeric(selected.get("effective_iterations"), errors="coerce").fillna(0).astype(int)
    selected["aggregation"] = "single_seed"
    columns = [
        "model_id",
        "family",
        "fold_id",
        "seed",
        "sample_id",
        "y_pred",
        "environment_id",
        "model_spec_sha256",
        "config_sha256",
        "code_sha256",
        "environment_sha256",
        "expected_sample_ids_sha256",
        "selected_parameter_sha256",
        "selected_parameter_json",
        "epoch_count",
        "parameter_count",
        "fit_seconds",
        "inference_seconds",
        "peak_ram_mb",
        "peak_vram_mb",
        "aggregation",
        *SCORED_METADATA_COLUMNS,
        "y_true",
    ]
    return selected.loc[:, columns]


def _validation_metadata(comparators: pd.DataFrame, canonical: pd.DataFrame) -> pd.DataFrame:
    b1 = comparators.loc[comparators["model_id"].eq("B1_persistence_last_rate")]
    zone = b1[["sample_id", "zone_id"]].drop_duplicates("sample_id")
    columns = [name for name in SCORED_METADATA_COLUMNS if name != "zone_id"]
    metadata = canonical[["sample_id", *columns]].merge(zone, on="sample_id", how="inner", validate="one_to_one")
    return metadata[["sample_id", *SCORED_METADATA_COLUMNS]]


def _role_ids_for_job(root: Path, config: Mapping[str, Any], fold_id: str, *, role: str) -> tuple[str, ...]:
    assignments = pd.read_csv(root / config["resampling"]["outer_assignments"])
    values = tuple(
        assignments.loc[
            assignments["fold_id"].astype(str).eq(fold_id) & assignments["role"].eq(role),
            "sample_id",
        ].astype(str)
    )
    if not values:
        raise ContractViolation(f"Missing Gate C1 {role} IDs: {fold_id}")
    return values


def _validate_global_single_seed_completeness(frame: pd.DataFrame, jobs: Sequence[Mapping[str, Any]]) -> None:
    expected = {(str(job["model_id"]), str(job["outer_fold_id"])) for job in jobs}
    actual = set(zip(frame["model_id"].astype(str), frame["fold_id"].astype(str), strict=False))
    if actual != expected:
        raise ContractViolation("Gate C1 single-seed shards do not cover exact 44 jobs")
    for (model_id, fold_id), group in frame.groupby(["model_id", "fold_id"], sort=True):
        if set(group["seed"].astype(int)) != set(C1_SEEDS):
            raise ContractViolation(f"Gate C1 missing seed rows: {model_id}/{fold_id}")


def _model_seed_coverage_exact(frame: pd.DataFrame) -> bool:
    reference: tuple[str, ...] | None = None
    for _, seed_frame in frame.groupby("seed", sort=True):
        ids = tuple(seed_frame.sort_values(["fold_id", "sample_id"])["sample_id"].astype(str))
        if len(ids) != 595 or len(set(ids)) != 595:
            return False
        if reference is None:
            reference = ids
        elif ids != reference:
            return False
    return reference is not None


def _flatten_worker_status(status: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in status.items()
        if not isinstance(value, (dict, list)) or key == "seeds"
    }
