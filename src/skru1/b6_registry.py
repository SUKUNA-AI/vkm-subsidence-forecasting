"""Frozen Gate B6 model registry and train-only job manifest."""

from __future__ import annotations

from itertools import product
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from .benchmarking import BenchmarkPlan, MetricSuite, ModelSpec, canonical_json_sha256
from .data_contracts import ContractViolation, sha256_file


def expand_parameter_grid(
    grid: Mapping[str, Sequence[Any]],
    fixed: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], ...]:
    fixed_parameters = dict(fixed or {})
    if not grid:
        return (fixed_parameters,)
    names = tuple(grid)
    values = [tuple(grid[name]) for name in names]
    if any(not candidates for candidates in values):
        raise ContractViolation("B6 parameter grids cannot contain an empty axis")
    rows = []
    for combination in product(*values):
        parameters = dict(fixed_parameters)
        parameters.update(dict(zip(names, combination, strict=True)))
        rows.append(parameters)
    rows.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return tuple(rows)


def build_model_registry(root: Path, config: Mapping[str, Any]) -> list[ModelSpec]:
    comparator_parameters = _resolved_comparator_parameters(root)
    registry: list[ModelSpec] = []
    for raw in config["frozen_comparators"]:
        model_id = str(raw["model_id"])
        source = comparator_parameters[model_id]
        registry.append(
            ModelSpec(
                model_id=model_id,
                family=str(raw["family"]),
                environment_id=str(raw["environment_id"]),
                feature_view=str(raw["feature_view"]),
                seed_policy={"mode": "fixed", "seeds": [int(config["random_seed"])]},
                parameter_grid=(),
                fixed_parameters=dict(source["parameters"]),
                probabilistic_capabilities=tuple(source.get("probabilistic_capabilities", ())),
                fit_context_requirements=tuple(source.get("fit_context_requirements", ())),
                status="FROZEN_COMPARATOR",
            )
        )
    for raw in config["models"]:
        seeds = tuple(map(int, raw["seeds"]))
        grid = expand_parameter_grid(raw.get("grid", {}), raw.get("fixed", {}))
        registry.append(
            ModelSpec(
                model_id=str(raw["model_id"]),
                family=str(raw["family"]),
                environment_id=str(raw["environment_id"]),
                feature_view=str(raw["feature_view"]),
                seed_policy={"mode": "fixed", "seeds": list(seeds)},
                parameter_grid=grid,
                fixed_parameters=dict(raw.get("fixed", {})),
                probabilistic_capabilities=tuple(raw.get("probabilistic_capabilities", ())),
                fit_context_requirements=tuple(raw.get("fit_context_requirements", ())),
                status="PREREGISTERED_CANDIDATE",
            )
        )
    ids = [spec.model_id for spec in registry]
    if len(ids) != len(set(ids)):
        raise ContractViolation("B6 model registry contains duplicate model IDs")
    environments = set(config["environments"])
    unknown = {spec.environment_id for spec in registry} - environments
    if unknown:
        raise ContractViolation(f"B6 registry references unknown environments: {sorted(unknown)}")
    return registry


def registry_payload(
    root: Path,
    config: Mapping[str, Any],
    benchmark_plan: BenchmarkPlan,
) -> dict[str, Any]:
    specs = build_model_registry(root, config)
    payload = {
        "schema_version": 1,
        "gate": config["gate"],
        "scientific_scope": "train_only_internal_research",
        "source_split": "t1_v1/train",
        "benchmark_plan_sha256": benchmark_plan.plan_sha256,
        "gate_b6_config_sha256": sha256_file(root / "configs" / "gate_b6.yaml"),
        "metric_suite": MetricSuite().__dict__,
        "metric_suite_sha256": MetricSuite().suite_sha256,
        "models": [spec.to_dict() for spec in specs],
        "model_count": len(specs),
        "candidate_count": sum(spec.status == "PREREGISTERED_CANDIDATE" for spec in specs),
        "frozen_comparator_count": sum(spec.status == "FROZEN_COMPARATOR" for spec in specs),
    }
    payload["registry_sha256"] = canonical_json_sha256(payload)
    return payload


def build_job_manifest(
    registry: Mapping[str, Any],
    benchmark_plan: BenchmarkPlan,
    outer_contracts,
    inner_contracts,
) -> dict[str, Any]:
    jobs: list[dict[str, Any]] = []
    inner_by_parent = {
        str(parent): group.sort_values("validation_target_date")["fold_id"].astype(str).tolist()
        for parent, group in inner_contracts.groupby("parent_fold_id", sort=True)
    }
    for model in registry["models"]:
        for outer in outer_contracts.itertuples(index=False):
            design = str(outer.design)
            phase = "screen" if design == "rolling_origin" else "robustness"
            jobs.append(
                {
                    "job_id": f"{model['model_id']}::{outer.fold_id}",
                    "phase": phase,
                    "conditional_on_temporal_screen": phase == "robustness"
                    and model["status"] != "FROZEN_COMPARATOR",
                    "model_id": model["model_id"],
                    "model_spec_sha256": model["spec_sha256"],
                    "environment_id": model["environment_id"],
                    "source_split": "t1_v1/train",
                    "outer_fold_id": str(outer.fold_id),
                    "outer_design": design,
                    "outer_train_sample_ids_sha256": str(outer.train_sample_ids_sha256),
                    "outer_validation_sample_ids_sha256": str(outer.validation_sample_ids_sha256),
                    "inner_fold_ids": inner_by_parent[str(outer.fold_id)],
                    "benchmark_plan_sha256": benchmark_plan.plan_sha256,
                    "model_data_inputs": ["t1_v1/train"],
                }
            )
    payload = {
        "schema_version": 1,
        "source_split": "t1_v1/train",
        "benchmark_plan_sha256": benchmark_plan.plan_sha256,
        "model_registry_sha256": registry["registry_sha256"],
        "jobs": jobs,
        "job_count": len(jobs),
        "worker_cli_accepts_validation_or_test_manifest": False,
        "prohibited_model_inputs_exposed": False,
    }
    payload["job_manifest_sha256"] = canonical_json_sha256(payload)
    return payload


def model_spec_from_registry(registry: Mapping[str, Any], model_id: str) -> ModelSpec:
    match = next((item for item in registry["models"] if item["model_id"] == model_id), None)
    if match is None:
        raise KeyError(model_id)
    return ModelSpec(
        model_id=match["model_id"],
        family=match["family"],
        environment_id=match["environment_id"],
        feature_view=match["feature_view"],
        seed_policy=match["seed_policy"],
        parameter_grid=tuple(match["parameter_grid"]),
        fixed_parameters=match["fixed_parameters"],
        probabilistic_capabilities=tuple(match["probabilistic_capabilities"]),
        fit_context_requirements=tuple(match["fit_context_requirements"]),
        status=match["status"],
    )


def _resolved_comparator_parameters(root: Path) -> dict[str, dict[str, Any]]:
    b0 = yaml.safe_load((root / "configs" / "gate_b0_b1.yaml").read_text(encoding="utf-8"))
    b4 = yaml.safe_load((root / "configs" / "gate_b4.yaml").read_text(encoding="utf-8"))
    result = {
        str(spec["model_id"]): {
            "parameters": dict(spec["parameters"]),
            "probabilistic_capabilities": (),
        }
        for spec in b0["models"]
    }
    for spec in b4["frozen_comparators"]:
        model_id = str(spec["model_id"])
        result[model_id] = {
            "parameters": dict(spec["parameters"]),
            "probabilistic_capabilities": (
                ("normal_mean_variance", "native_intervals")
                if model_id in {"B6_adaptive_kalman", "B7_two_regime_imm"}
                else ()
            ),
        }
    candidate = json.loads(
        (root / "artifacts" / "model_selection" / "t1_b4_train_only_v1" / "research_candidate.json").read_text(
            encoding="utf-8"
        )
    )
    result["B8_student_t_robust_imm"] = {
        "parameters": dict(candidate["selected_parameters"]),
        "probabilistic_capabilities": ("native_mean_scale", "native_intervals"),
    }
    required = {
        "B1_persistence_last_rate",
        "B3_profile_robust_trend",
        "B5_fixed_kalman",
        "B6_adaptive_kalman",
        "B7_two_regime_imm",
        "B8_student_t_robust_imm",
        "M1_ridge",
        "M2_extra_trees",
    }
    if required - set(result):
        raise ContractViolation(f"Cannot resolve frozen comparator specs: {sorted(required - set(result))}")
    return result
