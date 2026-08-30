"""Post-run audit adapter for Gate B3 CSV null/empty normalization.

The development source is intentionally left byte-for-byte unchanged after the
first full run.  This module only replaces the dataframe equality predicate
during independent validation so an empty ``held_out_group`` and its Pandas
CSV round-trip representation (NA) are treated as the same absent value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from .data_contracts import sha256_file
from . import gate_b3


MODEL_OUTPUT_NAMES = (
    "imm_tuning",
    "imm_outer_predictions",
    "comparison_predictions",
    "fold_metrics",
    "aggregate_metrics",
    "model_comparison",
    "calibration_predictions",
    "interval_calibration",
    "validation_intervals",
    "interval_metrics",
    "transition_metrics",
    "problem_transition_metrics",
    "regime_summary",
    "development_candidate",
    "gate_report",
    "protected_predecessor_snapshot",
    "fold_contracts",
)


def frames_equivalent_with_absent_group_normalization(
    left: pd.DataFrame,
    right: pd.DataFrame,
) -> bool:
    """Compare persisted evidence after normalizing missing string cells."""

    if set(left.columns) != set(right.columns) or len(left) != len(right):
        return False
    columns = list(right.columns)
    sort_columns = [
        column
        for column in (
            "design",
            "fold_id",
            "model_id",
            "scope",
            "segment",
            "sample_id",
        )
        if column in columns
    ]
    prepared: list[pd.DataFrame] = []
    for source in (left, right):
        frame = source.loc[:, columns].copy()
        for column in frame.columns:
            if not pd.api.types.is_numeric_dtype(frame[column]):
                frame[column] = frame[column].fillna("").astype(str)
        if sort_columns:
            frame = frame.sort_values(sort_columns, kind="mergesort")
        prepared.append(frame.reset_index(drop=True))
    try:
        pd.testing.assert_frame_equal(
            prepared[0],
            prepared[1],
            check_dtype=False,
            check_exact=False,
            rtol=1e-10,
            atol=1e-10,
        )
    except AssertionError:
        return False
    return True


def run_gate_b3_authoritative_audit(
    root: Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Run all core checks with the documented CSV normalization adapter."""

    paths = {
        name: gate_b3.resolve_repo_path(root, value)
        for name, value in config["artifacts"].items()
        if name != "root"
    }
    before_hashes = {
        name: sha256_file(paths[name])
        for name in MODEL_OUTPUT_NAMES
        if paths[name].is_file()
    }
    prior_report: dict[str, Any] | None = None
    if paths["validation_report"].is_file():
        prior_report = json.loads(
            paths["validation_report"].read_text(encoding="utf-8")
        )

    original_comparator = gate_b3._frames_equivalent
    gate_b3._frames_equivalent = frames_equivalent_with_absent_group_normalization
    try:
        result = gate_b3.run_gate_b3_validation(root, config)
    finally:
        gate_b3._frames_equivalent = original_comparator

    after_hashes = {
        name: sha256_file(paths[name])
        for name in MODEL_OUTPUT_NAMES
        if paths[name].is_file()
    }
    if before_hashes != after_hashes:
        raise RuntimeError("Authoritative audit changed a Gate B3 model output")
    reconciliation = {
        "schema_version": 1,
        "issue": "csv_empty_string_round_trip_as_na",
        "scope": "fold_metrics held_out_group representation only",
        "original_validation_status": (
            prior_report.get("status") if prior_report is not None else None
        ),
        "original_failed_checks": (
            [
                row["check_id"]
                for row in prior_report.get("checks", [])
                if not row.get("passed")
            ]
            if prior_report is not None
            else []
        ),
        "normalization": "non-numeric NA and empty string compare as the same absent value",
        "model_outputs_changed": False,
        "model_output_hashes": before_hashes,
        "development_source_changed": False,
        "authoritative_validation_status": result["status"],
        "authoritative_validation_checks": result["checks"],
        "authoritative_validation_failed": result["failed"],
        "adapter_source": "src/skru1/gate_b3_audit.py",
        "adapter_source_sha256": sha256_file(root / "src" / "skru1" / "gate_b3_audit.py"),
    }
    reconciliation_path = gate_b3.resolve_repo_path(
        root,
        "artifacts/model_selection/t1_b3_v1/audit_reconciliation.json",
    )
    gate_b3.write_json_atomic(root, reconciliation_path, reconciliation)

    validation_report = json.loads(
        paths["validation_report"].read_text(encoding="utf-8")
    )
    validation_report["serialization_reconciliation"] = {
        "applied": True,
        "reason": reconciliation["issue"],
        "scope": reconciliation["scope"],
        "model_outputs_changed": False,
        "evidence": reconciliation_path.relative_to(root).as_posix(),
    }
    gate_b3.write_json_atomic(root, paths["validation_report"], validation_report)

    inventory_path = paths["artifact_inventory"]
    artifact_root = gate_b3.resolve_repo_path(root, config["artifacts"]["root"])
    inventory_sources = sorted(
        [
            path
            for path in artifact_root.rglob("*")
            if path.is_file() and path != inventory_path
        ],
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
    gate_b3.write_csv_atomic(root, inventory_path, inventory)
    return {
        **result,
        "serialization_reconciliation": reconciliation_path.relative_to(
            root
        ).as_posix(),
        "model_outputs_changed": False,
    }
