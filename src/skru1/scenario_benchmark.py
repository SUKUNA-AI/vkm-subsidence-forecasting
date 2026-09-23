"""Fixed B1/IMM benchmark on SKRU1 scenario simulation v1."""
from __future__ import annotations

import csv
import hashlib
import json
import platform
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .baselines import PersistenceLastRate
from .imm_kalman import TwoRegimeIMMRate
from .scenario_adapter import ScenarioModelBundle, load_scenario_model_bundle
from .scenario_simulation import repository_path, sha256_file


YEAR_DAYS = 365.25


def finite_sample_higher(scores: np.ndarray, coverage: float) -> float:
    """Conformal order statistic ceil((n+1)*coverage), with higher rank."""
    values = np.asarray(scores, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise ValueError("Calibration scores are empty")
    if not 0.0 < coverage < 1.0:
        raise ValueError("Coverage must be strictly between zero and one")
    rank = int(np.ceil((values.size + 1) * float(coverage)))
    if rank > values.size:
        raise ValueError(
            f"Finite calibration sample cannot attain coverage={coverage}: n={values.size}"
        )
    return float(np.partition(values, rank - 1)[rank - 1])


def _prediction_frame(
    bundle: ScenarioModelBundle,
    model_id: str,
    prediction: np.ndarray,
    raw_sigma: np.ndarray,
    diagnostics: pd.DataFrame,
    radii: Mapping[float, float],
    *,
    scaled: bool,
) -> pd.DataFrame:
    source = bundle.evaluation.frame
    columns = [
        "sample_id",
        "scenario_id",
        "base_point_id",
        "base_profile_id",
        "current_date",
        "target_date",
        "forecast_horizon_days",
        "evaluation_scope",
        "experiment_role",
        "dynamic_mechanism",
        "missingness_mechanism",
        "measurement_error_mechanism",
        "target_observed_available",
        "observed_rate_mm_y",
        "latent_increment_mm",
        "latent_rate_mm_y",
    ]
    frame = source.loc[:, columns].copy().reset_index(drop=True)
    frame.insert(1, "model_id", model_id)
    frame["predicted_rate_mm_y"] = prediction
    frame["predicted_increment_mm"] = prediction * (
        frame["forecast_horizon_days"].to_numpy(float) / YEAR_DAYS
    )
    frame["raw_sigma_mm_y"] = raw_sigma
    frame["latent_rate_error_mm_y"] = prediction - frame["latent_rate_mm_y"].to_numpy(float)
    frame["latent_increment_error_mm"] = (
        frame["predicted_increment_mm"] - frame["latent_increment_mm"]
    )
    observed = pd.to_numeric(frame["observed_rate_mm_y"], errors="coerce").to_numpy(float)
    frame["observed_rate_error_mm_y"] = np.where(
        np.isfinite(observed), prediction - observed, np.nan
    )
    for level, radius in radii.items():
        label = f"p{int(round(level * 100)):02d}"
        half_width = radius * raw_sigma if scaled else np.full(len(frame), radius)
        frame[f"interval_{label}_lower_mm_y"] = prediction - half_width
        frame[f"interval_{label}_upper_mm_y"] = prediction + half_width
    for column in diagnostics.columns:
        frame[column] = diagnostics[column].to_numpy()
    return frame


def _metric_row(frame: pd.DataFrame, *, group_name: str, group_value: str) -> dict[str, Any]:
    latent_rate_error = frame["latent_rate_error_mm_y"].to_numpy(float)
    latent_increment_error = frame["latent_increment_error_mm"].to_numpy(float)
    observed_error = pd.to_numeric(frame["observed_rate_error_mm_y"], errors="coerce").to_numpy(float)
    observed_finite = np.isfinite(observed_error)
    return {
        "model_id": str(frame["model_id"].iloc[0]),
        "group_name": group_name,
        "group_value": group_value,
        "n": int(len(frame)),
        "observed_target_n": int(observed_finite.sum()),
        "latent_rate_mae_mm_y": float(np.mean(np.abs(latent_rate_error))),
        "latent_rate_rmse_mm_y": float(np.sqrt(np.mean(np.square(latent_rate_error)))),
        "latent_rate_bias_mm_y": float(np.mean(latent_rate_error)),
        "latent_increment_mae_mm": float(np.mean(np.abs(latent_increment_error))),
        "observed_rate_mae_mm_y": float(np.mean(np.abs(observed_error[observed_finite])))
        if observed_finite.any()
        else np.nan,
    }


def metric_table(predictions: pd.DataFrame, group_fields: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for model_id, model in predictions.groupby("model_id", sort=True):
        rows.append(_metric_row(model, group_name="overall", group_value="all"))
        for field in group_fields:
            for value, group in model.groupby(field, sort=True):
                rows.append(
                    _metric_row(group, group_name=field, group_value=str(value))
                )
    return pd.DataFrame(rows)


def interval_metric_table(
    predictions: pd.DataFrame,
    group_fields: list[str],
    levels: list[float],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    groups: list[tuple[str, str, pd.DataFrame]] = [("overall", "all", predictions)]
    for field in group_fields:
        groups.extend(
            (field, str(value), group)
            for value, group in predictions.groupby(field, sort=True)
        )
    for group_name, group_value, group in groups:
        for model_id, model in group.groupby("model_id", sort=True):
            truth = model["latent_rate_mm_y"].to_numpy(float)
            for level in levels:
                label = f"p{int(round(level * 100)):02d}"
                lower = model[f"interval_{label}_lower_mm_y"].to_numpy(float)
                upper = model[f"interval_{label}_upper_mm_y"].to_numpy(float)
                rows.append(
                    {
                        "model_id": model_id,
                        "group_name": group_name,
                        "group_value": group_value,
                        "coverage_level": level,
                        "n": len(model),
                        "latent_empirical_coverage": float(
                            np.mean((truth >= lower) & (truth <= upper))
                        ),
                        "mean_full_width_mm_y": float(np.mean(upper - lower)),
                        "median_full_width_mm_y": float(np.median(upper - lower)),
                    }
                )
    return pd.DataFrame(rows)


def paired_table(predictions: pd.DataFrame, group_fields: list[str]) -> pd.DataFrame:
    wide = predictions.pivot(
        index="sample_id", columns="model_id", values="latent_rate_error_mm_y"
    )
    required = {"B1_persistence_last_rate", "B7_two_regime_imm"}
    if set(wide) != required or wide.isna().any().any():
        raise ValueError("B1/IMM predictions are not exactly paired")
    metadata = predictions.drop_duplicates("sample_id").set_index("sample_id")
    wide = wide.join(metadata[[*group_fields]])
    rows: list[dict[str, Any]] = []
    groups: list[tuple[str, str, pd.DataFrame]] = [("overall", "all", wide)]
    for field in group_fields:
        groups.extend((field, str(value), group) for value, group in wide.groupby(field, sort=True))
    for group_name, group_value, group in groups:
        b1 = np.abs(group["B1_persistence_last_rate"].to_numpy(float))
        imm = np.abs(group["B7_two_regime_imm"].to_numpy(float))
        b1_mae = float(np.mean(b1))
        imm_mae = float(np.mean(imm))
        rows.append(
            {
                "group_name": group_name,
                "group_value": group_value,
                "n": len(group),
                "b1_latent_rate_mae_mm_y": b1_mae,
                "imm_latent_rate_mae_mm_y": imm_mae,
                "imm_skill_vs_b1_percent": 100.0 * (b1_mae - imm_mae) / b1_mae,
                "imm_row_win_fraction": float(np.mean(imm < b1)),
                "mean_abs_error_difference_imm_minus_b1_mm_y": float(np.mean(imm - b1)),
            }
        )
    return pd.DataFrame(rows)


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    compression: dict[str, Any] | None = None
    if path.suffix == ".gz":
        compression = {"method": "gzip", "compresslevel": 9, "mtime": 0}
    frame.to_csv(
        path,
        index=False,
        encoding="utf-8-sig",
        lineterminator="\n",
        float_format="%.9f",
        na_rep="",
        quoting=csv.QUOTE_MINIMAL,
        compression=compression,
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def run_scenario_benchmark(
    root: Path,
    config_path: Path,
    output: Path,
    *,
    script_path: Path,
) -> dict[str, Any]:
    root = root.resolve()
    config_path = config_path.resolve()
    output = output.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    declared = repository_path(root, str(config["output_directory"]))
    work_root = (root / "work/scenario_b1_imm_v1").resolve()
    if output != declared and not output.is_relative_to(work_root):
        raise ValueError("Output must be the declared artifact or work/scenario_b1_imm_v1")
    bundle = load_scenario_model_bundle(
        root,
        manifest_relative_path=str(config["scenario_dataset_manifest"]),
        expected_dataset_id=str(config["expected_dataset_id"]),
    )
    minimum = config["acceptance"]
    if len(bundle.train.frame) < int(minimum["minimum_train_rows"]):
        raise ValueError("Insufficient train rows")
    if len(bundle.calibration.frame) < int(minimum["minimum_calibration_rows"]):
        raise ValueError("Insufficient calibration rows")
    if len(bundle.evaluation.frame) < int(minimum["minimum_evaluation_rows"]):
        raise ValueError("Insufficient evaluation rows")

    b1_spec = config["models"]["b1"]
    imm_spec = config["models"]["imm"]
    b1 = PersistenceLastRate(
        model_id=str(b1_spec["model_id"]), parameters=b1_spec["parameters"]
    )
    imm = TwoRegimeIMMRate(
        model_id=str(imm_spec["model_id"]), parameters=imm_spec["parameters"]
    )
    runtime_rows: list[dict[str, Any]] = []
    started = perf_counter()
    b1.fit(bundle.train)
    runtime_rows.append({"model_id": b1.model_id, "operation": "fit", "seconds": perf_counter() - started, "rows": len(bundle.train.frame)})
    started = perf_counter()
    imm.fit(bundle.train)
    runtime_rows.append({"model_id": imm.model_id, "operation": "fit", "seconds": perf_counter() - started, "rows": len(bundle.train.frame)})

    started = perf_counter()
    b1_calibration = b1.predict(bundle.calibration)
    runtime_rows.append({"model_id": b1.model_id, "operation": "calibration_predict", "seconds": perf_counter() - started, "rows": len(bundle.calibration.frame)})
    started = perf_counter()
    imm_calibration, imm_calibration_sigma, _ = imm.predict_distribution(
        bundle.calibration, history_frame=bundle.causal_history
    )
    runtime_rows.append({"model_id": imm.model_id, "operation": "calibration_predict", "seconds": perf_counter() - started, "rows": len(bundle.calibration.frame)})
    calibration_truth = bundle.calibration.frame["observed_rate_mm_y"].to_numpy(float)
    b1_scores = np.abs(calibration_truth - b1_calibration)
    imm_scores = np.abs(calibration_truth - imm_calibration) / imm_calibration_sigma
    levels = [float(level) for level in config["interval_calibration"]["coverage_levels"]]
    b1_radii = {level: finite_sample_higher(b1_scores, level) for level in levels}
    imm_radii = {level: finite_sample_higher(imm_scores, level) for level in levels}
    calibration_rows = [
        {
            "model_id": model_id,
            "coverage_level": level,
            "calibration_rows": len(bundle.calibration.frame),
            "score_type": score_type,
            "finite_sample_rank": int(np.ceil((len(bundle.calibration.frame) + 1) * level)),
            "radius": radius,
        }
        for model_id, score_type, radii in [
            (b1.model_id, "absolute_residual_mm_y", b1_radii),
            (imm.model_id, "absolute_residual_divided_by_raw_sigma", imm_radii),
        ]
        for level, radius in radii.items()
    ]

    started = perf_counter()
    b1_prediction = b1.predict(bundle.evaluation)
    b1_inference = perf_counter() - started
    runtime_rows.append({"model_id": b1.model_id, "operation": "evaluation_predict", "seconds": b1_inference, "rows": len(bundle.evaluation.frame)})
    started = perf_counter()
    imm_prediction, imm_sigma, imm_diagnostics = imm.predict_distribution(
        bundle.evaluation, history_frame=bundle.causal_history
    )
    imm_inference = perf_counter() - started
    runtime_rows.append({"model_id": imm.model_id, "operation": "evaluation_predict", "seconds": imm_inference, "rows": len(bundle.evaluation.frame)})

    b1_frame = _prediction_frame(
        bundle,
        b1.model_id,
        b1_prediction,
        np.full(len(b1_prediction), np.nan),
        pd.DataFrame(index=np.arange(len(b1_prediction))),
        b1_radii,
        scaled=False,
    )
    imm_frame = _prediction_frame(
        bundle,
        imm.model_id,
        imm_prediction,
        imm_sigma,
        imm_diagnostics,
        imm_radii,
        scaled=True,
    )
    predictions = pd.concat([b1_frame, imm_frame], ignore_index=True)
    predictions = predictions.sort_values(["sample_id", "model_id"], kind="mergesort").reset_index(drop=True)
    group_fields = [str(field) for field in config["report_groups"]]
    metrics = metric_table(predictions, group_fields)
    interval_metrics = interval_metric_table(predictions, group_fields, levels)
    paired = paired_table(predictions, group_fields)

    heldout_mechanisms = sorted(
        bundle.catalog.loc[
            bundle.catalog["experiment_role"].eq("heldout_mechanism"),
            "dynamic_mechanism",
        ].unique()
    )
    fallback_fraction = float(imm_frame["numerical_fallback_used"].fillna(False).mean())
    probability_sum = (
        imm_frame["stable_probability"].to_numpy(float)
        + imm_frame["transition_probability"].to_numpy(float)
    )
    interval_ordered = True
    for level in levels:
        label = f"p{int(round(level * 100)):02d}"
        interval_ordered &= (
            predictions[f"interval_{label}_lower_mm_y"]
            .le(predictions["predicted_rate_mm_y"])
            .all()
            and predictions[f"interval_{label}_upper_mm_y"]
            .ge(predictions["predicted_rate_mm_y"])
            .all()
        )
    manifest_inputs = [str(item["path"]) for item in bundle.manifest["inputs"]]
    checks = {
        "scenario_manifest_and_outputs_hash_verified": True,
        "minimum_train_rows": len(bundle.train.frame) >= int(minimum["minimum_train_rows"]),
        "minimum_calibration_rows": len(bundle.calibration.frame) >= int(minimum["minimum_calibration_rows"]),
        "minimum_evaluation_rows": len(bundle.evaluation.frame) >= int(minimum["minimum_evaluation_rows"]),
        "heldout_mechanism_count": len(heldout_mechanisms) >= int(minimum["minimum_heldout_mechanisms"]),
        "heldout_mechanisms_absent_from_fit_and_calibration": set(heldout_mechanisms).isdisjoint(
            set(bundle.train.frame["dynamic_mechanism"])
            | set(bundle.calibration.frame["dynamic_mechanism"])
        ),
        "predictions_exactly_paired": len(predictions) == 2 * len(bundle.evaluation.frame)
        and predictions.groupby("sample_id")["model_id"].nunique().eq(2).all(),
        "predictions_finite": np.isfinite(predictions["predicted_rate_mm_y"]).all(),
        "imm_sigma_finite_positive": np.isfinite(imm_sigma).all() and (imm_sigma > 0).all(),
        "imm_probabilities_normalized": np.allclose(probability_sum, 1.0, rtol=0, atol=1e-12),
        "imm_numerical_fallback_fraction_allowed": fallback_fraction
        <= float(minimum["maximum_numerical_fallback_fraction"]),
        "intervals_ordered": bool(interval_ordered),
        "calibration_uses_observed_development_rows_only": bundle.calibration.frame[
            "target_observed_available"
        ].all()
        and bundle.calibration.frame["experiment_role"].eq("development").all(),
        "latent_truth_not_in_feature_allowlist": not any(
            column.startswith("latent_") for column in bundle.train.feature_columns
        ),
        "legacy_test_and_external_holdout_not_loaded": not any(
            "artifacts/splits/t1_v1/test" in path or "final_holdout" in path
            for path in manifest_inputs
        ),
        "fixed_imm_parameters_no_scenario_tuning": config["selection_policy"]
        == "no_hyperparameter_tuning_fixed_historical_imm_parameters",
        "no_model_selection_claim": config["acceptance"]["model_selection_or_field_accuracy_claim"] is False,
    }
    validation = {
        "status": "PASS_EXECUTED_NO_SELECTION" if all(checks.values()) else "FAIL",
        "checks": {key: bool(value) for key, value in checks.items()},
        "experiment_id": config["experiment_id"],
        "claim_domain": config["claim_domain"],
        "train_rows": len(bundle.train.frame),
        "calibration_rows": len(bundle.calibration.frame),
        "evaluation_rows": len(bundle.evaluation.frame),
        "evaluation_observed_target_rows": int(bundle.evaluation.frame["target_observed_available"].sum()),
        "heldout_mechanisms": heldout_mechanisms,
        "models_executed": [b1.model_id, imm.model_id],
        "imm_numerical_fallback_fraction": fallback_fraction,
        "legacy_test_rows_loaded": 0,
        "external_holdout_rows_loaded": 0,
        "selection_performed": False,
        "field_accuracy_claim": False,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
    }
    if not all(checks.values()):
        raise ValueError(f"Scenario benchmark validation failed: {checks}")

    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "predictions.csv.gz", predictions)
    _write_csv(output / "metrics.csv", metrics)
    _write_csv(output / "interval_metrics.csv", interval_metrics)
    _write_csv(output / "paired_comparison.csv", paired)
    _write_csv(output / "calibration_quantiles.csv", pd.DataFrame(calibration_rows))
    _write_csv(output / "runtime.csv", pd.DataFrame(runtime_rows))
    _write_json(
        output / "model_states.json",
        {"b1": b1.state_dict(), "imm": imm.state_dict()},
    )
    _write_json(output / "validation_report.json", validation)
    overall = paired.loc[
        paired["group_name"].eq("overall") & paired["group_value"].eq("all")
    ].iloc[0]
    readme = f"""# {config['experiment_id']}

B1 и двухрежимный IMM подключены к исправленному сценарию через отдельный
manifest-checked адаптер. Использованы {len(bundle.train.frame)} строк fit,
{len(bundle.calibration.frame)} строк калибровки и {len(bundle.evaluation.frame)}
парных строк оценки. Исторический T1 test и внешний holdout не загружались.

В объединённой внутренней сценарной оценке MAE относительно скрытой скорости:
B1 — {overall['b1_latent_rate_mae_mm_y']:.3f} мм/год, IMM —
{overall['imm_latent_rate_mae_mm_y']:.3f} мм/год; сценарный skill IMM к B1 —
{overall['imm_skill_vs_b1_percent']:.2f}%. Это не выбор финальной модели и не
оценка полевой точности. Разрезы по механизму динамики, пропускам и ошибке
наблюдения находятся в `metrics.csv` и `paired_comparison.csv`.

Интервалы откалиброваны только на development-calibration. Покрытие в
`interval_metrics.csv` является эмпирическим покрытием скрытой истины в
зафиксированных сценариях.

Воспроизведение:

```powershell
.\\.venv\\Scripts\\python.exe scripts\\run_scenario_b1_imm_v1.py --root .
```
"""
    (output / "README.md").write_text(readme, encoding="utf-8", newline="\n")

    module_path = Path(__file__).resolve()
    adapter_path = module_path.with_name("scenario_adapter.py")
    model_paths = [module_path.with_name(name) for name in ["baselines.py", "imm_kalman.py", "adaptive_kalman.py"]]
    input_paths = [
        bundle.manifest_path,
        config_path,
        module_path,
        adapter_path,
        *model_paths,
        script_path.resolve(),
    ]
    output_names = [
        "README.md",
        "predictions.csv.gz",
        "metrics.csv",
        "interval_metrics.csv",
        "paired_comparison.csv",
        "calibration_quantiles.csv",
        "runtime.csv",
        "model_states.json",
        "validation_report.json",
    ]
    manifest = {
        "schema_version": 1,
        "experiment_id": config["experiment_id"],
        "scenario_dataset_id": bundle.manifest["dataset_id"],
        "scenario_source_data_commit": bundle.manifest["source_data_commit"],
        "claim_domain": config["claim_domain"],
        "command": "python scripts/run_scenario_b1_imm_v1.py --root .",
        "inputs": [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for path in input_paths
        ],
        "outputs": [
            {
                "path": name,
                "sha256": sha256_file(output / name),
                "size_bytes": (output / name).stat().st_size,
                "deterministic": name != "runtime.csv",
            }
            for name in output_names
        ],
    }
    _write_json(output / "manifest.json", manifest)
    return validation
