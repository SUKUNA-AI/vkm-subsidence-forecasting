#!/usr/bin/env python
"""Import/fit/predict and two-run determinism smoke checks for one B6 environment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from time import perf_counter
import traceback

import numpy as np
import joblib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skru1.adaptive_kalman import prepare_kalman_history
from skru1.artifact_io import write_json_atomic
from skru1.b6_governance import excluded_model_records
from skru1.b6_models import create_adapter
from skru1.b6_registry import build_model_registry
from skru1.data_contracts import load_canonical_bundle
from skru1.evaluation import causal_feature_history, derived_dataset
from skru1.gate_b6 import load_gate_b6_config
from skru1.splits import load_split_dataset


def smoke_parameters(parameters: dict, family: str) -> dict:
    output = dict(parameters)
    for key in ("n_estimators", "iterations", "max_rounds", "max_iter"):
        if key in output:
            output[key] = min(int(output[key]), 30)
    if family in {"residual_mlp", "protocol_safe_enfs_replica"}:
        output["max_epochs"] = 5
        output["patience"] = 2
        output["early_stopping_patience"] = 2
    if family == "gaussian_process":
        output["optimizer_restarts"] = 0
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-id", required=True, choices=("b6_cpu", "b6_ngboost", "b6_torch"))
    parser.add_argument(
        "--model-id",
        action="append",
        default=[],
        help="Optional model filter; may be repeated. The default exercises every model in the environment.",
    )
    args = parser.parse_args()
    root, config = load_gate_b6_config()
    excluded = excluded_model_records(root)
    registry = [
        spec
        for spec in build_model_registry(root, config)
        if spec.environment_id == args.environment_id and spec.model_id not in excluded
    ]
    if args.model_id:
        requested = set(args.model_id)
        available = {spec.model_id for spec in registry}
        unknown = sorted(requested - available)
        if unknown:
            parser.error(
                f"model(s) are unavailable or governance-excluded in {args.environment_id}: {unknown}"
            )
        registry = [spec for spec in registry if spec.model_id in requested]
    bundle = load_canonical_bundle(root)
    source = load_split_dataset("t1", "train", root=root)
    dates = source.frame["target_date"]
    unique = sorted(dates.unique())
    train_ids = tuple(source.frame.loc[dates.isin(unique[:8]), "sample_id"].astype(str))
    validation_ids = tuple(source.frame.loc[dates.eq(unique[8]), "sample_id"].astype(str))
    train = derived_dataset(source, train_ids, split="train", label=f"{args.environment_id}_smoke_train")
    validation = derived_dataset(
        source, validation_ids, split="validation", label=f"{args.environment_id}_smoke_validation"
    )
    raw = causal_feature_history(source)
    prepared = prepare_kalman_history(raw)
    rows = []
    determinism = []
    for spec in registry:
        parameters = dict(spec.fixed_parameters if not spec.parameter_grid else spec.parameter_grid[0])
        parameters = smoke_parameters(parameters, spec.family)
        predictions = []
        first_adapter = None
        started = perf_counter()
        try:
            for _ in range(2):
                adapter = create_adapter(
                    spec,
                    parameters,
                    contract=bundle.feature_contract,
                    seed=int(spec.seed_policy["seeds"][0]),
                    raw_history=raw,
                    prepared_history=prepared,
                )
                adapter.fit(train, validation=validation)
                if first_adapter is None:
                    first_adapter = adapter
                predictions.append(adapter.predict(validation).mean)
            roundtrip_path = (
                root
                / "work"
                / "environment_staging"
                / args.environment_id
                / f"{spec.model_id}__serialization_smoke.joblib"
            )
            roundtrip_path.parent.mkdir(parents=True, exist_ok=True)
            joblib.dump(first_adapter, roundtrip_path)
            restored = joblib.load(roundtrip_path)
            roundtrip_prediction = restored.predict(validation).mean
            serialized_bytes = roundtrip_path.stat().st_size
            roundtrip_path.unlink()
            if not np.allclose(
                predictions[0], roundtrip_prediction, rtol=0, atol=1e-10, equal_nan=False
            ):
                raise RuntimeError("serialized adapter prediction changed after round-trip")
            tolerance = 1e-5 if args.environment_id == "b6_torch" else 1e-10
            maximum_delta = float(np.max(np.abs(predictions[0] - predictions[1])))
            rows.append(
                {
                    "model_id": spec.model_id,
                    "status": "PASS",
                    "rows": len(validation.frame),
                    "finite": bool(np.isfinite(predictions[0]).all()),
                    "elapsed_seconds": perf_counter() - started,
                    "serialization_roundtrip": "PASS",
                    "serialized_adapter_bytes": serialized_bytes,
                    "smoke_parameters": parameters,
                }
            )
            determinism.append(
                {
                    "model_id": spec.model_id,
                    "status": "PASS" if maximum_delta <= tolerance else "FAIL",
                    "maximum_absolute_delta": maximum_delta,
                    "tolerance": tolerance,
                    "runs": 2,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "model_id": spec.model_id,
                    "status": "FAIL",
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(limit=8),
                }
            )
            determinism.append({"model_id": spec.model_id, "status": "NOT_RUN_AFTER_SMOKE_FAILURE"})
    staging = root / "work" / "environment_staging" / args.environment_id
    staging.mkdir(parents=True, exist_ok=True)
    smoke_report = {
        "schema_version": 1,
        "environment_id": args.environment_id,
        "status": _overall_smoke_status(rows),
        "governance_excluded_model_ids": sorted(excluded),
        "models": rows,
    }
    determinism_report = {
        "schema_version": 1,
        "environment_id": args.environment_id,
        "status": _overall_smoke_status(determinism),
        "governance_excluded_model_ids": sorted(excluded),
        "models": determinism,
    }
    filtered_suffix = "__" + "__".join(sorted(args.model_id)) if args.model_id else ""
    write_json_atomic(
        root,
        staging / f"smoke_report{filtered_suffix}.json",
        smoke_report,
        work_scope="b6_environment_smoke",
    )
    write_json_atomic(
        root,
        staging / f"determinism_report{filtered_suffix}.json",
        determinism_report,
        work_scope="b6_environment_smoke",
    )
    if not args.model_id:
        durable = (
            root
            / "artifacts"
            / "model_selection"
            / "t1_b6_expanded_v1"
            / "environments"
            / args.environment_id
        )
        write_json_atomic(
            root, durable / "smoke_report.json", smoke_report, work_scope="b6_environment_smoke"
        )
        write_json_atomic(
            root,
            durable / "determinism_report.json",
            determinism_report,
            work_scope="b6_environment_smoke",
        )
    print(json.dumps({"smoke": smoke_report["status"], "determinism": determinism_report["status"]}))
    return 0 if smoke_report["status"] == determinism_report["status"] == "PASS" else 2


def _overall_smoke_status(rows: list[dict[str, object]]) -> str:
    statuses = {str(row.get("status")) for row in rows}
    if statuses == {"PASS"}:
        return "PASS"
    return "FAIL"


if __name__ == "__main__":
    raise SystemExit(main())
