from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from skru1.data_contracts import ContractViolation, load_canonical_bundle, sha256_file
from skru1.holdout_intake import (
    evaluate_holdout_once,
    freeze_holdout,
    inspect_holdout_candidate,
    load_holdout_v3_config,
)


ROOT = Path(__file__).resolve().parents[1]


def test_holdout_v3_is_pending_without_real_package() -> None:
    root, config = load_holdout_v3_config(ROOT)
    status, inventory, origins = inspect_holdout_candidate(
        root,
        config,
        write_status=False,
    )
    assert status["status"] == "PENDING_DATA"
    assert status["eligible"] is False
    assert status["sealed_target_values_read"] is False
    assert origins is None
    assert inventory["exists"].sum() == 0
    assert not inventory.loc[
        inventory["role"].eq("sealed_targets"), "content_parsed_before_freeze"
    ].iloc[0]


def test_holdout_v3_freeze_rejects_absent_package() -> None:
    root, config = load_holdout_v3_config(ROOT)
    with pytest.raises(ContractViolation, match="cannot be frozen"):
        freeze_holdout(root, config)


def test_holdout_v3_policy_keeps_old_sets_excluded() -> None:
    _, config = load_holdout_v3_config(ROOT)
    excluded = {row["split"]: row["permitted_role"] for row in config["excluded_evaluation_sets"]}
    assert excluded == {
        "t1_v1/test": "historical_diagnostic_only",
        "t1_v1/validation": "historical_descriptive_only",
    }
    assert config["freeze_protocol"]["one_access_event_only"] is True
    assert config["freeze_protocol"]["failed_access_is_consumed"] is True
    assert config["freeze_protocol"]["primary_model_must_be_declared_before_access"] is True
    assert config["freeze_protocol"]["candidate_suite"] == (
        "artifacts/governance/final_candidate_suite_v4.json"
    )


def test_holdout_v3_synthetic_success_consumes_exactly_one_access() -> None:
    root, base_config = load_holdout_v3_config(ROOT)
    config, targets = _synthetic_future_package(root, base_config, valid_targets=True)
    status, _, _ = inspect_holdout_candidate(root, config, write_status=False)
    assert status["status"] == "READY_TO_FREEZE"
    assert status["sealed_target_values_read"] is False
    frozen = freeze_holdout(root, config)
    assert frozen["status"] == "FROZEN_UNOPENED"
    report = evaluate_holdout_once(root, config)
    assert report["status"] == "ONE_SHOT_EVALUATION_COMPLETE"
    assert report["primary_model_id"] == "B7_two_regime_imm"
    assert report["rows"] == len(targets) == 100
    assert len(report["metrics"]) == 6
    assert {row["model_id"] for row in report["metrics"]} == {
        "B1_persistence_last_rate",
        "B5_fixed_kalman",
        "B6_adaptive_kalman",
        "B7_two_regime_imm",
        "B8_student_t_robust_imm",
        "Z01_elastic_net",
    }
    ledger_path = ROOT / config["artifacts"]["access_ledger"]
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["state"] == "CONSUMED_SUCCESS"
    assert ledger["target_values_read"] is True
    with pytest.raises(PermissionError, match="already been consumed"):
        evaluate_holdout_once(root, config)


def test_holdout_v3_failed_label_parse_is_also_consumed() -> None:
    root, base_config = load_holdout_v3_config(ROOT)
    config, _ = _synthetic_future_package(root, base_config, valid_targets=False)
    status, _, _ = inspect_holdout_candidate(root, config, write_status=False)
    assert status["status"] == "READY_TO_FREEZE"
    freeze_holdout(root, config)
    with pytest.raises(ContractViolation, match="target schema"):
        evaluate_holdout_once(root, config)
    ledger_path = ROOT / config["artifacts"]["access_ledger"]
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["state"] == "CONSUMED_FAILED"
    assert ledger["target_values_read"] is True
    with pytest.raises(PermissionError, match="already been consumed"):
        evaluate_holdout_once(root, config)


def _synthetic_future_package(
    root: Path,
    base_config: dict,
    *,
    valid_targets: bool,
) -> tuple[dict, pd.DataFrame]:
    config = deepcopy(base_config)
    token = uuid4().hex
    relative_root = Path("work") / "holdout_v3_protocol_tests" / token
    package_dir = relative_root / "package"
    absolute_package = root / package_dir
    absolute_package.mkdir(parents=True, exist_ok=False)
    config["candidate_package"]["directory"] = package_dir.as_posix()
    config["artifacts"] = {
        "status": (relative_root / "status.json").as_posix(),
        "intake_inventory": (relative_root / "inventory.csv").as_posix(),
        "frozen_root": (relative_root / "frozen").as_posix(),
        "frozen_record": (relative_root / "frozen" / "frozen_record.json").as_posix(),
        "sample_manifest": (relative_root / "splits" / "manifest.csv").as_posix(),
        "access_ledger": (relative_root / "frozen" / "access_ledger.json").as_posix(),
        "predictions": (relative_root / "evaluation" / "predictions.csv").as_posix(),
        "metrics": (relative_root / "evaluation" / "metrics.csv").as_posix(),
        "final_report": (relative_root / "evaluation" / "final_report.json").as_posix(),
    }

    bundle = load_canonical_bundle(root)
    unique_points = bundle.features.drop_duplicates("point_id", keep="last").copy()
    assert unique_points["point_id"].nunique() >= 98
    origins = pd.concat([unique_points.iloc[:98], unique_points.iloc[:2]], ignore_index=True)
    origins = origins.loc[:, bundle.features.columns].copy()
    origins["sample_id"] = [f"synthetic-holdout-{token}-{index:03d}" for index in range(100)]
    first = np.arange(100) < 50
    origins["current_date"] = np.where(first, "2025-10-15", "2026-02-15")
    origins["target_date"] = np.where(first, "2026-01-15", "2026-05-15")
    origins["forecast_horizon_days"] = np.where(first, 92, 89)
    origins["current_campaign_id"] = np.where(first, "SYN-C0", "SYN-C1")
    origins["target_campaign_id"] = np.where(first, "SYN-H1", "SYN-H2")
    origins["split"] = "synthetic_holdout_protocol_test"
    origin_path = absolute_package / config["candidate_package"]["origins_file"]
    origins.to_csv(origin_path, index=False, lineterminator="\n")

    targets = pd.DataFrame(
        {
            "sample_id": origins["sample_id"].astype(str),
            "observed_rate_mm_y": pd.to_numeric(
                origins["last_rate_mm_y"], errors="coerce"
            ).fillna(0.0)
            + 1.0,
        }
    )
    target_path = absolute_package / config["candidate_package"]["sealed_targets_file"]
    if valid_targets:
        targets.to_csv(target_path, index=False, lineterminator="\n")
    else:
        target_path.write_text("not_the_frozen_target_schema\n1\n", encoding="utf-8")
    package_manifest = {
        "schema_version": 1,
        "package_id": f"synthetic-protocol-test-{token}",
        "holdout_type": "future_temporal_holdout",
        "target_definition": "T1_RATE_NEXT_PLANNED",
        "origins_file": config["candidate_package"]["origins_file"],
        "origins_sha256": sha256_file(origin_path),
        "sealed_targets_file": config["candidate_package"]["sealed_targets_file"],
        "sealed_targets_sha256": sha256_file(target_path),
        "source_package_is_new": True,
        "labels_unseen_before_candidate_freeze": True,
    }
    (absolute_package / config["candidate_package"]["package_manifest"]).write_text(
        json.dumps(package_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return config, targets
