from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pandas as pd
import pytest
import yaml

from skru1.b6_registry import build_job_manifest, build_model_registry, expand_parameter_grid, registry_payload
from skru1.b6_governance import (
    effective_environment_settings,
    executable_model_ids,
    load_b6_execution_amendment,
)
from skru1.benchmarking import BenchmarkPlan, assert_train_only_worker_job
from skru1.environment_staging import enrich_report_hashes_from_pip_cache, sanitize_install_report
from skru1.gate_b6 import (
    _cpu_model,
    _durable_text_privacy_violations,
    _installed_version_matches,
    _parse_colon_lines,
    load_gate_b6_config,
    protocol_errata,
)
from skru1.leakage import LeakageViolation


ROOT = Path(__file__).resolve().parents[1]


def _config():
    return load_gate_b6_config(ROOT)[1]


def test_b6_registry_history_is_preserved_but_execution_catalog_has_22_models() -> None:
    config = _config()
    registry = build_model_registry(ROOT, config)
    assert len(registry) == 23
    assert sum(spec.status == "FROZEN_COMPARATOR" for spec in registry) == 8
    assert sum(spec.status == "PREREGISTERED_CANDIDATE" for spec in registry) == 15
    assert {spec.environment_id for spec in registry} == {"b6_cpu", "b6_ngboost", "b6_torch"}
    assert [spec.model_id for spec in registry if spec.environment_id == "b6_ngboost"] == ["Z12_ngboost"]
    benchmark = BenchmarkPlan.from_dict(
        json.loads(
            (ROOT / "artifacts" / "splits" / "t1_train_benchmark_v1" / "benchmark_plan.json").read_text(
                encoding="utf-8"
            )
        )
    )
    executable = executable_model_ids(ROOT, registry_payload(ROOT, config, benchmark))
    assert len(executable) == 22
    assert "Z15_tabpfn_v2_6" not in executable


def test_b6_parameter_grid_sizes_are_frozen() -> None:
    registry = {spec.model_id: spec for spec in build_model_registry(ROOT, _config())}
    expected = {
        "Z01_elastic_net": 12,
        "Z02_huber": 9,
        "Z03_rbf_svr": 18,
        "Z04_gaussian_process": 6,
        "Z05_gaussian_gee": 3,
        "Z06_hist_gradient_boosting": 16,
        "Z07_quantile_hist_gradient_boosting": 8,
        "Z08_xgboost": 16,
        "Z09_lightgbm": 16,
        "Z10_catboost": 16,
        "Z11_ebm": 12,
        "Z12_ngboost": 16,
        "Z13_residual_mlp": 8,
        "Z14_enfs_replica": 1,
        "Z15_tabpfn_v2_6": 1,
    }
    assert {model_id: len(registry[model_id].parameter_grid) for model_id in expected} == expected
    assert {item["alpha"] for item in registry["Z01_elastic_net"].parameter_grid} == {0.01, 0.1, 1.0, 10.0}
    assert {item["l1_ratio"] for item in registry["Z01_elastic_net"].parameter_grid} == {0.1, 0.5, 0.9}
    assert {item["working_correlation"] for item in registry["Z05_gaussian_gee"].parameter_grid} == {
        "Independence",
        "Exchangeable",
        "AR1",
    }
    assert registry["Z15_tabpfn_v2_6"].seed_policy["seeds"] == [42117, 42118, 42119, 42120, 42121]


def test_grid_expansion_is_deterministic_and_does_not_mutate_fixed_values() -> None:
    first = expand_parameter_grid({"b": [2, 1], "a": ["x", "y"]}, {"fixed": True})
    second = expand_parameter_grid({"b": [2, 1], "a": ["x", "y"]}, {"fixed": True})
    assert first == second
    assert len(first) == 4
    assert all(item["fixed"] is True for item in first)


def test_worker_cli_has_no_validation_or_test_arguments() -> None:
    text = "\n".join(
        (ROOT / "scripts" / name).read_text(encoding="utf-8")
        for name in ("run_gate_b6_worker.py", "run_gate_b6_full_train_worker.py")
    )
    assert "validation-manifest" not in text
    assert "test-manifest" not in text
    assert "candidate-record" not in text
    assert 'load_split_dataset("t1", "validation"' not in (
        ROOT / "src" / "skru1" / "b6_worker.py"
    ).read_text(encoding="utf-8")
    assert 'load_split_dataset("t1", "test"' not in (
        ROOT / "src" / "skru1" / "b6_worker.py"
    ).read_text(encoding="utf-8")


def test_all_runner_dispatches_isolated_workers_before_aggregation() -> None:
    text = (ROOT / "scripts" / "run_gate_b6.py").read_text(encoding="utf-8")
    screen_dispatch = text.index('dispatch_gate_b6_workers(root, config, phase="screen")')
    screen_aggregate = text.index("run_gate_b6_screen(root, config)", screen_dispatch)
    robust_dispatch = text.index(
        'dispatch_gate_b6_workers(root, config, phase="robustness")', screen_aggregate
    )
    robust_aggregate = text.index("run_gate_b6_robustness(root, config)", robust_dispatch)
    assert screen_dispatch < screen_aggregate < robust_dispatch < robust_aggregate


def test_b5_two_run_audit_covers_both_frozen_artifact_roots() -> None:
    text = (ROOT / "scripts" / "verify_gate_b5_two_run.py").read_text(encoding="utf-8")
    assert '"t1_train_benchmark_v1"' in text
    assert '"t1_b5_evidence_v1"' in text
    assert '"runs": 2' in text


def test_torch_local_version_and_cached_wheel_hash_are_verified() -> None:
    assert _installed_version_matches(
        "torch",
        "2.13.0+cu130",
        "2.13.0",
        cuda_wheel_index="https://download.pytorch.org/whl/cu130",
    )
    url = "https://download.example.invalid/model-1.0-py3-none-any.whl"
    cache_key = hashlib.sha224(url.encode("utf-8")).hexdigest()
    cache_root = ROOT / "work" / "test_fixtures" / "b6_pip_cache"
    body = cache_root / "http-v2" / cache_key[0] / cache_key[1] / cache_key[2] / cache_key[3] / cache_key[4] / f"{cache_key}.body"
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_bytes(b"exact wheel bytes")
    report = {
        "install": [
            {"download_info": {"url": url, "archive_info": {}}, "metadata": {"name": "model"}}
        ]
    }
    enriched = enrich_report_hashes_from_pip_cache(report, cache_root)
    archive = enriched["install"][0]["download_info"]["archive_info"]
    assert archive["hashes"]["sha256"] == hashlib.sha256(b"exact wheel bytes").hexdigest()
    assert archive["hash_source"] == "pip_http_cache_response_body"


def test_install_report_sanitizer_retains_only_reproducibility_fields() -> None:
    payload = {
        "version": "1",
        "pip_version": "26.0.1",
        "install": [
            {
                "download_info": {
                    "url": "https://example.invalid/model.whl",
                    "archive_info": {"hashes": {"sha256": "a" * 64}},
                },
                "requested": True,
                "metadata": {
                    "name": "example",
                    "version": "1.0",
                    "requires_python": ">=3.13",
                    "description": "C:\\\\private\\\\path must not survive",
                    "requires_dist": ["another-package"],
                },
            }
        ],
        "environment": {
            "python_version": "3.13",
            "platform_system": "Windows",
            "platform_version": "private free-form field",
        },
    }
    sanitized = sanitize_install_report(payload, ROOT)
    rendered = json.dumps(sanitized, sort_keys=True)
    assert "description" not in rendered
    assert "requires_dist" not in rendered
    assert "private" not in rendered
    assert sanitized["install"][0]["metadata"] == {
        "name": "example",
        "version": "1.0",
        "requires_python": ">=3.13",
    }
    assert sanitized["install"][0]["download_info"]["archive_info"]["hashes"]["sha256"] == "a" * 64


def test_nvidia_version_parser_does_not_require_process_banner() -> None:
    parsed = _parse_colon_lines(
        "NVIDIA-SMI version  : 616.56\nKMD version : 616.56\nCUDA UMD version : 13.4\n"
    )
    assert parsed == {
        "nvidia_smi_version": "616.56",
        "kmd_version": "616.56",
        "cuda_umd_version": "13.4",
    }


def test_cpu_model_capture_is_privacy_safe() -> None:
    model = _cpu_model()
    assert model is None or (model.strip() == model and "\\" not in model and "/" not in model)


def test_current_b6_durable_environment_evidence_has_no_local_paths_or_tokens() -> None:
    artifact_root = ROOT / "artifacts" / "model_selection" / "t1_b6_expanded_v1"
    if artifact_root.is_dir():
        assert _durable_text_privacy_violations(artifact_root) == []


def test_worker_manifest_guard_rejects_nested_prohibited_paths() -> None:
    with pytest.raises(LeakageViolation):
        assert_train_only_worker_job(
            {
                "source_split": "t1_v1/train",
                "jobs": [{"inputs": ["artifacts/splits/t1_v1/validation.csv"]}],
            }
        )


def test_frozen_job_manifest_exposes_only_train_model_data() -> None:
    config = _config()
    benchmark = BenchmarkPlan.from_dict(
        json.loads(
            (ROOT / "artifacts" / "splits" / "t1_train_benchmark_v1" / "benchmark_plan.json").read_text(
                encoding="utf-8"
            )
        )
    )
    contracts = pd.read_csv(
        ROOT / "artifacts" / "splits" / "t1_train_benchmark_v1" / "fold_contracts.csv",
        keep_default_na=False,
    )
    registry = registry_payload(ROOT, config, benchmark)
    manifest = build_job_manifest(
        registry,
        benchmark,
        contracts.loc[contracts["level"].eq("outer")],
        contracts.loc[contracts["level"].eq("inner")],
    )
    assert_train_only_worker_job(manifest)
    assert manifest["prohibited_model_inputs_exposed"] is False
    assert all(job["model_data_inputs"] == ["t1_v1/train"] for job in manifest["jobs"])


def test_environment_locks_are_separate_and_original_modeling_lock_is_unchanged() -> None:
    config = _config()
    locks = {settings["lock"] for settings in config["environments"].values()}
    assert locks == {
        "requirements/b6_cpu.lock.txt",
        "requirements/b6_ngboost.lock.txt",
        "requirements/b6_torch.lock.txt",
    }
    modeling = (ROOT / "requirements" / "modeling.lock.txt").read_text(encoding="utf-8")
    assert "scikit-learn==1.9.0" in modeling
    assert "ngboost" not in modeling.lower()
    assert "torch" not in modeling.lower()
    assert "tabpfn" not in modeling.lower()
    effective_torch = effective_environment_settings(ROOT, config, "b6_torch")
    assert effective_torch["lock"] == "requirements/b6_torch_runtime.lock.txt"


def test_nonoperative_torch_model_list_typo_is_recorded_without_changing_registry() -> None:
    config = _config()
    benchmark = BenchmarkPlan.from_dict(
        json.loads(
            (ROOT / "artifacts" / "splits" / "t1_train_benchmark_v1" / "benchmark_plan.json").read_text(
                encoding="utf-8"
            )
        )
    )
    registry = registry_payload(ROOT, config, benchmark)
    errata = protocol_errata(config, registry, root=ROOT)
    assert errata["status"] == "RECORDED"
    record = next(item for item in errata["records"] if item["erratum_id"] == "B6-ERRATUM-001")
    assert record["scientific_protocol_changed"] is False
    assert record["authoritative_values"] == [
        "Z13_residual_mlp",
        "Z14_enfs_replica",
        "Z15_tabpfn_v2_6",
    ]
    geometry = next(item for item in errata["records"] if item["erratum_id"] == "B6-ERRATUM-002")
    assert geometry["stored_value"] == 560
    assert geometry["one_step_forecast_horizon_days_max"] == 210
    assert geometry["eligibility_decision_changed"] is False
    tabpfn = next(item for item in errata["records"] if item["erratum_id"] == "B6-ERRATUM-003")
    assert tabpfn["authoritative_value"] == "SAFE_ALL"
    assert tabpfn["superseded_for_execution_by"] == "B6-GOV-001"
    assert tabpfn["runtime_relevance_after_amendment"].startswith("none")
    assert tabpfn["scientific_protocol_changed"] is False


def test_tabpfn_is_governance_excluded_without_staging_or_runtime_path() -> None:
    amendment = load_b6_execution_amendment(ROOT)
    record = amendment["excluded_models"][0]
    assert record["model_id"] == "Z15_tabpfn_v2_6"
    assert record["execution_status"] == "EXCLUDED_GOVERNANCE_USER_WITHDRAWAL"
    assert record["predictions_existed_at_exclusion"] is False
    assert record["license_accepted"] is False
    assert record["weights_downloaded"] is False
    assert all(value is False for value in amendment["runtime_policy"].values())
    assert amendment["environment_overrides"]["b6_torch"]["effective_runtime_lock"] == (
        "requirements/b6_torch_runtime.lock.txt"
    )
    assert "tabpfn" not in (
        ROOT / "requirements" / "b6_torch_runtime.lock.txt"
    ).read_text(encoding="utf-8").lower()
    assert not (ROOT / "scripts" / "stage_tabpfn_model.py").exists()
    source = (ROOT / "src" / "skru1" / "b6_models.py").read_text(encoding="utf-8")
    assert "from tabpfn" not in source


def test_suite_v4_rule_is_hard_gates_then_lexicographic_fallback() -> None:
    policy = _config()["suite_v4_eligibility"]
    assert policy["fallback_primary"] == "B7_two_regime_imm"
    assert policy["lexicographic_order"] == [
        "rolling_mae",
        "transition_mae",
        "worst_zone_mae",
        "conformal_95_weighted_interval_score",
        "fit_time",
        "model_id",
    ]
    assert policy["pass_fallback_status"] == "PASS_NO_NEW_PRIMARY"
