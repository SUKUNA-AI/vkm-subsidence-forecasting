#!/usr/bin/env python3
"""Run the leakage-safe Gate B0/B1 T1 baseline protocol."""

from __future__ import annotations

import argparse
from collections import Counter
import importlib.metadata
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import numpy as np
import pandas as pd


ROOT_HINT = Path(__file__).resolve().parents[1]
SRC = ROOT_HINT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from skru1.data_contracts import load_canonical_bundle, sha256_file  # noqa: E402
from skru1.evaluation import (  # noqa: E402
    aggregate_development_metrics,
    build_gate_b0_b1_folds,
    compare_to_references,
    evaluate_development_models,
    fit_frozen_candidate,
    predict_frozen_candidate,
    rank_candidates,
)
from skru1.leakage import assert_no_forbidden_split_api_usage  # noqa: E402
from skru1.model_selection import (  # noqa: E402
    claim_test_access,
    finalize_test_access,
    freeze_candidate,
    load_frozen_model,
    load_gate_b_config,
    resolve_repo_path,
    write_csv_atomic,
    write_json_atomic,
)
from skru1.splits import (  # noqa: E402
    SealedTestError,
    combine_development_datasets,
    load_split_dataset,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gate B0/B1: T1 baselines, frozen candidate, and one-time test"
    )
    parser.add_argument(
        "--phase",
        choices=("develop", "final-test", "validate"),
        default="develop",
    )
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    root, config = load_gate_b_config(args.root)
    if args.phase == "develop":
        result = run_development(root, config)
    elif args.phase == "final-test":
        result = run_final_test(root, config)
    else:
        result = run_validation(root, config)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=_json_default))
    return 0


def run_development(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    frozen_record = resolve_repo_path(root, config["artifacts"]["candidate_record"])
    if frozen_record.exists():
        raise RuntimeError(
            "A frozen candidate already exists. Development artifacts are immutable for this version."
        )
    gate_a1_path = root / "artifacts" / "data_quality" / "gate_a1_report.json"
    gate_a1 = json.loads(gate_a1_path.read_text(encoding="utf-8"))
    if gate_a1.get("summary", {}).get("critical_failures") != 0:
        raise RuntimeError("Gate B0/B1 requires Gate A1 with zero critical failures")

    bundle = load_canonical_bundle(root)
    train = load_split_dataset("t1", "train", root=root)
    validation = load_split_dataset("t1", "validation", root=root)
    if train.provenance.version != config["split_version"]:
        raise RuntimeError("Gate B0/B1 split version differs from the frozen T1 manifest")

    policy = config["development_policy"]
    weight_clip = tuple(float(value) for value in policy["training_weights"]["inverse_variance_clip"])
    development, folds, fold_contracts = build_gate_b0_b1_folds(
        train,
        validation,
        bundle,
        rolling_folds=int(policy["rolling_origin_folds"]),
    )
    predictions, fold_metrics, model_states = evaluate_development_models(
        development,
        folds,
        model_specs=config["models"],
        contract=bundle.feature_contract,
        random_seed=int(config["random_seed"]),
        weight_clip=weight_clip,
    )
    aggregate = aggregate_development_metrics(predictions, weight_clip=weight_clip)
    selection = config["selection"]
    comparison = compare_to_references(
        aggregate,
        reference_model=str(selection["reference_model"]),
        kalman_reference=str(selection["fixed_kalman_reference"]),
    )
    ranking = rank_candidates(
        aggregate,
        model_specs=config["models"],
        selection_config=selection,
    )
    selected_row = ranking.loc[ranking["selected"]].iloc[0]
    selected_model_id = str(selected_row["model_id"])
    selected_spec = next(
        spec for spec in config["models"] if spec["model_id"] == selected_model_id
    )
    frozen_model, frozen_state = fit_frozen_candidate(
        development,
        model_spec=selected_spec,
        contract=bundle.feature_contract,
        random_seed=int(config["random_seed"]),
        weight_clip=weight_clip,
    )

    paths = {
        name: resolve_repo_path(root, value)
        for name, value in config["artifacts"].items()
        if name != "root"
    }
    write_csv_atomic(root, paths["development_predictions"], predictions)
    write_csv_atomic(root, paths["fold_contracts"], fold_contracts)
    write_csv_atomic(root, paths["fold_metrics"], fold_metrics)
    write_csv_atomic(root, paths["model_states"], model_states)
    write_csv_atomic(root, paths["aggregate_metrics"], aggregate)
    write_csv_atomic(root, paths["model_comparison"], comparison)
    write_csv_atomic(root, paths["candidate_ranking"], ranking)

    design_counts = Counter(fold.design for fold in folds)
    temporal_best = (
        aggregate.loc[aggregate["design"].eq("temporal_holdout")]
        .sort_values(["mae", "model_id"])
        .iloc[0]
    )
    report = {
        "schema_version": 1,
        "gate": config["gate"],
        "status": "PASS_WITH_CAVEATS",
        "test_data_loaded": False,
        "gate_a1_report": {
            "path": gate_a1_path.relative_to(root).as_posix(),
            "sha256": sha256_file(gate_a1_path),
            "status": gate_a1["status"],
            "critical_failures": gate_a1["summary"]["critical_failures"],
        },
        "data": {
            "dataset_version": bundle.target_contract.payload["dataset_version"],
            "task": "T1_RATE_NEXT_PLANNED",
            "target": "observed_rate_mm_y",
            "unit": "mm/year",
            "train_rows": len(train.frame),
            "validation_rows": len(validation.frame),
            "train_sample_ids_sha256": train.provenance.sample_ids_sha256,
            "validation_sample_ids_sha256": validation.provenance.sample_ids_sha256,
            "feature_contract_sha256": bundle.feature_contract.source_sha256,
            "target_contract_sha256": bundle.target_contract.source_sha256,
        },
        "validation_design": {
            "fold_counts": dict(sorted(design_counts.items())),
            "forward_only": True,
            "grouped_holdout_mode": policy["grouped_holdout_mode"],
            "fold_contracts": paths["fold_contracts"].relative_to(root).as_posix(),
        },
        "models": [
            {
                "model_id": spec["model_id"],
                "family": spec["family"],
                "parameters": spec["parameters"],
                "complexity_penalty": spec["complexity_penalty"],
            }
            for spec in config["models"]
        ],
        "selection": {
            "selected_model": selected_model_id,
            "selection_score": float(selected_row["selection_score"]),
            "temporal_holdout_mae": float(selected_row["temporal_holdout_mae"]),
            "temporal_best_model": str(temporal_best["model_id"]),
            "temporal_best_mae": float(temporal_best["mae"]),
            "policy": dict(selection),
            "frozen_model_state": frozen_state,
        },
        "artifacts": {
            name: path.relative_to(root).as_posix()
            for name, path in paths.items()
            if name
            in {
                "development_predictions",
                "fold_contracts",
                "fold_metrics",
                "model_states",
                "aggregate_metrics",
                "model_comparison",
                "candidate_ranking",
            }
        },
        "environment": _package_versions(),
        "checks": {
            "test_not_loaded_during_selection": True,
            "fit_preprocessing_train_only": True,
            "canonical_training_weight_ignored": True,
            "random_split_forbidden": True,
            "identifier_estimator_features": 0,
            "all_predictions_finite": bool(np.isfinite(predictions["y_pred"]).all()),
            "expected_fold_count": len(folds) == 24,
        },
        "caveats": [
            "This is a Gate B0/B1 stage candidate, not the final thesis model.",
            "The adaptive B6 Kalman comparator required by the full screening acceptance gate is deferred to the next model stage.",
            "Leave-profile-out and leave-zone-out use 2024 validation rows with only pre-2024 labels in fit; local feature history remains available to the fixed state-space baseline.",
            "The frozen validation set has 130 origins over 90 points, so row-level precision must not be interpreted as 130 independent trajectories.",
        ],
    }
    write_json_atomic(root, paths["development_report"], report)
    source_paths = [
        root / "configs" / "gate_b0_b1.yaml",
        root / "src" / "skru1" / "baselines.py",
        root / "src" / "skru1" / "evaluation.py",
        root / "src" / "skru1" / "model_selection.py",
        root / "src" / "skru1" / "preprocessing.py",
        root / "src" / "skru1" / "splits.py",
        root / "requirements" / "modeling.lock.txt",
    ]
    candidate = freeze_candidate(
        root=root,
        config=config,
        bundle=bundle,
        train=train,
        validation=validation,
        selected_ranking=selected_row.to_dict(),
        selected_model_spec=selected_spec,
        model_state=frozen_state,
        model=frozen_model,
        development_report_path=paths["development_report"],
        source_paths=source_paths,
    )
    return {
        "phase": "develop",
        "status": report["status"],
        "test_data_loaded": False,
        "candidate_id": candidate["candidate_id"],
        "selected_model": selected_model_id,
        "temporal_validation_mae": float(selected_row["temporal_holdout_mae"]),
        "folds": len(folds),
    }


def run_final_test(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    paths = {
        name: resolve_repo_path(root, value)
        for name, value in config["artifacts"].items()
        if name != "root"
    }
    candidate_path = paths["candidate_record"]
    if not candidate_path.is_file():
        raise SealedTestError("Run --phase develop and freeze one candidate before final-test")
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    if sha256_file(paths["development_report"]) != candidate["development_report_sha256"]:
        raise RuntimeError("Development report changed after candidate freeze")
    if sha256_file(paths["candidate_config"]) != candidate["candidate_config_sha256"]:
        raise RuntimeError("Candidate config changed after freeze")

    ledger = claim_test_access(root=root, config=config, candidate_record=candidate)
    try:
        train = load_split_dataset("t1", "train", root=root)
        validation = load_split_dataset("t1", "validation", root=root)
        development = combine_development_datasets([train, validation])
        # This is the only model-facing T1 test loader call in the repository workflow.
        test = load_split_dataset(
            "t1", "test", root=root, candidate_record=candidate_path
        )
        if test.provenance.candidate_id != candidate["candidate_id"]:
            raise RuntimeError("Test loader did not bind access to the frozen candidate")
        model = load_frozen_model(root, candidate)
        weight_clip = tuple(
            float(value)
            for value in config["development_policy"]["training_weights"][
                "inverse_variance_clip"
            ]
        )
        predictions, metrics = predict_frozen_candidate(
            model,
            test,
            history_datasets=[development, test],
            weight_clip=weight_clip,
        )
        metrics_frame = pd.DataFrame([metrics])
        write_csv_atomic(root, paths["test_predictions"], predictions)
        write_csv_atomic(root, paths["test_metrics"], metrics_frame)
        final_report = {
            "schema_version": 1,
            "gate": config["gate"],
            "status": "FROZEN_TEST_EVALUATED",
            "candidate_id": candidate["candidate_id"],
            "selected_model": candidate["selected_model"],
            "candidate_record_sha256": sha256_file(candidate_path),
            "test_access_event_id": ledger["access_event_id"],
            "test_access_count": 1,
            "test_rows": len(test.frame),
            "test_sample_ids_sha256": test.provenance.sample_ids_sha256,
            "metrics": metrics,
            "post_test_tuning_allowed": False,
            "caveat": "The result is terminal for this frozen Gate B0/B1 candidate and must not be used to alter its configuration.",
        }
        write_json_atomic(root, paths["final_test_report"], final_report)
        finalized = finalize_test_access(
            root=root,
            config=config,
            status="consumed",
            outputs={
                "test_predictions": paths["test_predictions"],
                "test_metrics": paths["test_metrics"],
                "final_test_report": paths["final_test_report"],
            },
        )
        return {
            "phase": "final-test",
            "status": finalized["status"],
            "candidate_id": candidate["candidate_id"],
            "model_id": metrics["model_id"],
            "test_rows": metrics["rows"],
            "test_mae": metrics["mae"],
        }
    except Exception as exc:
        finalize_test_access(
            root=root,
            config=config,
            status="failed_after_claim",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


def run_validation(root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    paths = {
        name: resolve_repo_path(root, value)
        for name, value in config["artifacts"].items()
        if name != "root"
    }
    candidate = json.loads(paths["candidate_record"].read_text(encoding="utf-8"))
    ledger = json.loads(paths["test_access_ledger"].read_text(encoding="utf-8"))
    development_predictions = pd.read_csv(paths["development_predictions"])
    aggregate = pd.read_csv(paths["aggregate_metrics"])
    ranking = pd.read_csv(paths["candidate_ranking"])
    test_predictions = pd.read_csv(paths["test_predictions"])
    test_metrics = pd.read_csv(paths["test_metrics"]).iloc[0]

    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, observed: Any, expected: Any) -> None:
        checks.append(
            {
                "check_id": check_id,
                "passed": bool(passed),
                "observed": observed,
                "expected": expected,
            }
        )

    for row in aggregate.itertuples(index=False):
        subset = development_predictions.loc[
            development_predictions["design"].eq(row.design)
            & development_predictions["model_id"].eq(row.model_id)
        ]
        independent = _independent_regression_metrics(subset["y_true"], subset["y_pred"])
        for metric in ("mae", "rmse", "bias", "r2"):
            observed = float(getattr(row, metric))
            expected = float(independent[metric])
            add(
                f"aggregate_{row.design}_{row.model_id}_{metric}",
                np.isclose(observed, expected, rtol=1e-10, atol=1e-10),
                observed,
                expected,
            )
    independent_test = _independent_regression_metrics(
        test_predictions["y_true"], test_predictions["y_pred"]
    )
    for metric in ("mae", "rmse", "bias", "r2"):
        observed = float(test_metrics[metric])
        expected = float(independent_test[metric])
        add(
            f"test_{metric}",
            np.isclose(observed, expected, rtol=1e-10, atol=1e-10),
            observed,
            expected,
        )

    add(
        "candidate_is_unique_rank_one",
        int(ranking["selected"].astype(bool).sum()) == 1
        and str(ranking.loc[ranking["selected"].astype(bool), "model_id"].iloc[0])
        == candidate["selected_model"],
        candidate["selected_model"],
        "unique selected rank 1",
    )
    add(
        "candidate_model_hash",
        sha256_file(paths["model_artifact"]) == candidate["model_artifact_sha256"],
        sha256_file(paths["model_artifact"]),
        candidate["model_artifact_sha256"],
    )
    add(
        "test_access_consumed_once",
        ledger.get("status") == "consumed" and ledger.get("candidate_id") == candidate["candidate_id"],
        ledger.get("status"),
        "consumed",
    )
    add(
        "development_contains_no_test_dates",
        pd.to_datetime(development_predictions["target_date"]).max() < pd.Timestamp("2025-01-01"),
        str(pd.to_datetime(development_predictions["target_date"]).max().date()),
        "before 2025-01-01",
    )
    add(
        "test_predictions_match_manifest_count",
        len(test_predictions) == int(ledger["test_rows"]) == 175,
        len(test_predictions),
        175,
    )
    source_paths = list((root / "src" / "skru1").glob("*.py")) + list(
        (root / "scripts").glob("*.py")
    )
    try:
        assert_no_forbidden_split_api_usage(source_paths)
        forbidden_split_status = True
        forbidden_split_observed: Any = 0
    except Exception as exc:  # included in machine-readable validation
        forbidden_split_status = False
        forbidden_split_observed = str(exc)
    add(
        "no_forbidden_random_split_api",
        forbidden_split_status,
        forbidden_split_observed,
        0,
    )
    for name, evidence in ledger.get("output_hashes", {}).items():
        output_path = root / evidence["path"]
        add(
            f"ledger_output_hash_{name}",
            sha256_file(output_path) == evidence["sha256"],
            sha256_file(output_path),
            evidence["sha256"],
        )

    failed = [check for check in checks if not check["passed"]]
    validation_report = {
        "schema_version": 1,
        "gate": config["gate"],
        "overall_assessment": "Share with caveats" if not failed else "Needs revision",
        "status": "PASS" if not failed else "FAIL",
        "question": "Are the Gate B0/B1 development comparison, frozen candidate, and one-time T1 test result internally reproducible?",
        "checks": checks,
        "summary": {"checks": len(checks), "failed": len(failed)},
        "methodology_review": {
            "temporal_selection": "verified from saved predictions and dates",
            "calculation_spot_check": "all unweighted MAE/RMSE/bias/R2 values independently recomputed from prediction rows",
            "test_access": "ledger consumed once; validation reads frozen outputs and does not call the test loader",
        },
        "remaining_caveats": [
            "Full screening acceptance still requires an adaptive B6 comparator.",
            "Uncertainty intervals and transition-specific evaluation are outside Gate B0/B1.",
            "Grouped validation has correlated repeated origins and should not be read as IID evidence.",
        ],
        "runner": {
            "path": "scripts/run_gate_b0_b1.py",
            "sha256": sha256_file(root / "scripts" / "run_gate_b0_b1.py"),
        },
    }
    write_json_atomic(root, paths["validation_report"], validation_report)
    artifact_root = resolve_repo_path(root, config["artifacts"]["root"])
    inventory_path = artifact_root / "artifact_inventory.csv"
    inventory_sources = sorted(
        [path for path in artifact_root.rglob("*") if path.is_file() and path != inventory_path]
        + [paths["candidate_record"]],
        key=lambda path: path.relative_to(root).as_posix(),
    )
    inventory = pd.DataFrame(
        [
            {
                "relative_path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in inventory_sources
        ]
    )
    write_csv_atomic(root, inventory_path, inventory)
    return {
        "phase": "validate",
        "status": validation_report["status"],
        "overall_assessment": validation_report["overall_assessment"],
        "checks": len(checks),
        "failed": len(failed),
        "artifact_inventory": inventory_path.relative_to(root).as_posix(),
    }


def _independent_regression_metrics(
    y_true: pd.Series, y_pred: pd.Series
) -> dict[str, float]:
    truth = np.asarray(y_true, dtype=float)
    prediction = np.asarray(y_pred, dtype=float)
    error = prediction - truth
    denominator = float(np.sum(np.square(truth - float(np.mean(truth)))))
    return {
        "mae": float(np.mean(np.abs(error))),
        "rmse": float(np.sqrt(np.mean(np.square(error)))),
        "bias": float(np.mean(error)),
        "r2": float(1.0 - np.sum(np.square(error)) / denominator),
    }


def _package_versions() -> dict[str, str]:
    names = ["numpy", "pandas", "scipy", "scikit-learn", "joblib", "PyYAML"]
    return {name: importlib.metadata.version(name) for name in names}


def _json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, (pd.Timestamp, Path)):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
