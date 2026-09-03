from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from skru1.data_contracts import ContractViolation, sha256_file
from skru1.gate_c1 import WINDOWS_CUDA_POST_COMMIT_EXIT_CODE, load_gate_c1_config
from skru1.gate_c1_interfaces import (
    C1_REQUIRED_MODELS,
    C1_SEEDS,
    SequenceModelSpec,
    assert_c2_model_admitted,
    assert_train_only_c1_job,
    canonical_json_sha256,
)


ROOT = Path(__file__).resolve().parents[1]


def test_protected_gate_c0_and_governance_hashes_are_exact() -> None:
    _, config = load_gate_c1_config(ROOT)
    for relative, expected in config["frozen_predecessors"]["files"].items():
        assert sha256_file(ROOT / relative) == expected
    contract = json.loads((ROOT / config["sequence"]["sequence_contract"]).read_text(encoding="utf-8"))
    digest = contract.pop("contract_sha256")
    assert digest == canonical_json_sha256(contract)
    assert digest == "439a3031133051c0f3dd9f8d84438d2b2e73a62486d9312c8940faa8c2ffe95f"


def test_frozen_gate_c0_registry_has_exact_required_grids() -> None:
    _, config = load_gate_c1_config(ROOT)
    gate_c = __import__("yaml").safe_load(
        (ROOT / config["architecture_source"]["config"]).read_text(encoding="utf-8")
    )
    required = [row for row in gate_c["architecture_registry"] if row["status"] == "REQUIRED_COMPACT_SCREEN"]
    assert tuple(row["model_id"] for row in required) == C1_REQUIRED_MODELS
    counts = {
        row["model_id"]: __import__("math").prod(len(values) for values in row["grid"].values())
        for row in required
    }
    assert counts == {
        "C01_compact_gru": 16,
        "C02_compact_lstm": 16,
        "C03_causal_tcn": 16,
        "C04_probabilistic_gru_student_t": 8,
    }
    assert sum(counts.values()) == 56
    assert tuple(config["training"]["seeds"]) == C1_SEEDS


def test_rolling_contract_has_11_outer_33_inner_and_13_unique_pairs() -> None:
    _, config = load_gate_c1_config(ROOT)
    contracts = pd.read_csv(ROOT / config["sequence"]["fold_contracts"])
    outer = contracts.loc[contracts["level"].eq("outer") & contracts["design"].eq("rolling_origin")]
    inner = contracts.loc[contracts["level"].eq("inner") & contracts["design"].eq("rolling_origin")]
    assert len(outer) == 11
    assert len(inner) == 33
    assert inner.groupby("parent_fold_id")["fold_id"].nunique().eq(3).all()
    assert inner[
        [
            "train_sequence_pairs_sha256",
            "validation_sequence_pairs_sha256",
            "train_origins",
            "validation_origins",
        ]
    ].drop_duplicates().shape[0] == 13
    assert pd.to_datetime(outer["train_target_date_max"]).lt(
        pd.to_datetime(outer["validation_target_date_min"])
    ).all()
    assert pd.to_datetime(inner["train_target_date_max"]).lt(
        pd.to_datetime(inner["validation_target_date_min"])
    ).all()


def test_logical_and_physical_fit_formulas_are_frozen() -> None:
    _, config = load_gate_c1_config(ROOT)
    assert 56 * 11 * 3 * 5 == config["cache"]["logical_inner_fits"] == 9240
    assert 56 * 13 * 5 == config["cache"]["expected_physical_inner_fits"] == 3640
    assert 4 * 11 == config["expected_counts"]["outer_jobs"] == 44
    assert 4 * 11 * 5 == config["expected_counts"]["outer_refits"] == 220


def test_checkpoint_and_cuda_execution_contracts_are_frozen() -> None:
    _, config = load_gate_c1_config(ROOT)
    checkpoints = config["checkpointing"]
    assert checkpoints["root"] == "work/gate_c1/checkpoints"
    assert checkpoints["keep_top_k"] == 5
    assert checkpoints["stage_interval_epochs"] == 50
    assert checkpoints["inner_ranking"] == "metric_ascending_then_epoch"
    assert checkpoints["outer_selection"] == "fixed_final_epoch"
    assert checkpoints["persistence_scope"] == "work_only"
    assert checkpoints["outer_labels_allowed"] is False
    training = config["training"]
    assert training["optimizer_backend"] == "fused_adamw_cuda"
    assert training["validation_metric_device"] == "cuda"
    assert training["recurrent_execution"] == "vectorized_right_padding_dense_cuda"
    assert training["torch_compile"] is False


def test_worker_job_rejects_validation_test_holdout_and_outer_labels() -> None:
    safe = {"source_split": "t1_v1/train", "job_id": "C01::rolling"}
    assert_train_only_c1_job(safe)
    for payload in (
        {**safe, "validation_manifest": "artifacts/splits/t1_v1/validation.csv"},
        {**safe, "test_manifest": "artifacts/splits/t1_v1/test.csv"},
        {**safe, "holdout_manifest": "inputs/holdout.csv"},
        {**safe, "outer_labels": [1.0]},
        {**safe, "source_split": "t1_v1/validation"},
    ):
        with pytest.raises(ContractViolation):
            assert_train_only_c1_job(payload)


def test_windows_cuda_worker_flushes_before_hard_process_exit() -> None:
    source = (ROOT / "scripts" / "run_gate_c1_worker.py").read_text(encoding="utf-8")
    stdout_flush = source.index("sys.stdout.flush()")
    stderr_flush = source.index("sys.stderr.flush()")
    hard_exit = source.index("os._exit(exit_code)")
    assert stdout_flush < hard_exit
    assert stderr_flush < hard_exit


def test_post_commit_teardown_code_is_narrow_and_has_a_frozen_ledger() -> None:
    _, config = load_gate_c1_config(ROOT)
    assert WINDOWS_CUDA_POST_COMMIT_EXIT_CODE == 3221226505 == 0xC0000409
    assert config["artifacts"]["worker_exit_ledger"].endswith(
        "t1_gate_c1_compact_screen_v1/worker_process_exit_ledger.csv"
    )
    assert config["artifacts"]["execution_incident"].endswith(
        "t1_gate_c1_compact_screen_v1/execution_incident_register.json"
    )
    source = (ROOT / "src" / "skru1" / "gate_c1.py").read_text(encoding="utf-8")
    assert "_validate_completed_worker_artifacts" in source
    assert "post_commit_teardown_anomaly" in source


def test_c2_admission_is_fail_closed() -> None:
    manifest = {
        "admitted_model_ids": ["C01_compact_gru"],
        "records": [{"model_id": "C01_compact_gru", "status": "PASSED_TEMPORAL_SCREEN"}],
    }
    assert_c2_model_admitted("C01_compact_gru", manifest)
    with pytest.raises(ContractViolation):
        assert_c2_model_admitted("C02_compact_lstm", manifest)
    with pytest.raises(ContractViolation):
        assert_c2_model_admitted("B7_two_regime_imm", manifest)


def test_sequence_model_spec_rejects_seed_or_channel_drift() -> None:
    base = dict(
        model_id="C01_compact_gru",
        family="gated_recurrent_unit",
        probabilistic=False,
        parameter_grid=({"hidden_size": 16, "layers": 1, "dropout": 0.0, "weight_decay": 0.0001},),
        seeds=C1_SEEDS,
        numeric_channels=(
            "last_settlement_mm",
            "last_rate_mm_y",
            "current_standard_uncertainty_mm",
            "days_since_previous_observation",
            "missing_campaigns_since_previous",
        ),
        categorical_channels=("current_campaign_type",),
        masks=("padding_mask", "observation_mask", "missing_campaign_mask"),
        training_objective="huber_delta_1_standardized",
        selection_objective="pooled_inner_mae",
        parameter_count_limit=100000,
        environment_id="gate_c_torch",
    )
    SequenceModelSpec(**base)
    with pytest.raises(ContractViolation):
        SequenceModelSpec(**{**base, "seeds": C1_SEEDS[:-1]})
    with pytest.raises(ContractViolation):
        SequenceModelSpec(**{**base, "numeric_channels": (*base["numeric_channels"], "point_id")})
