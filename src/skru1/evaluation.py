"""Controlled validation designs and metrics for T1 Gate B0/B1."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .baselines import TARGET_COLUMN, T1Model, build_model, train_only_precision_weights
from .data_contracts import CanonicalBundle, ContractViolation, FeatureContract
from .metrics import regression_metrics
from .splits import (
    ManifestDataset,
    SplitProvenance,
    attach_spatial_zones,
    build_spatial_zone_map,
    combine_development_datasets,
    rolling_origin_assignments,
    sample_id_list_sha256,
)


@dataclass(frozen=True)
class EvaluationFold:
    design: str
    fold_id: str
    held_out_group: str
    train_sample_ids: tuple[str, ...]
    validation_sample_ids: tuple[str, ...]


def build_gate_b0_b1_folds(
    train: ManifestDataset,
    validation: ManifestDataset,
    bundle: CanonicalBundle,
    *,
    rolling_folds: int = 5,
) -> tuple[ManifestDataset, list[EvaluationFold], pd.DataFrame]:
    """Build one temporal, five rolling, 14 profile, and four zone folds.

    Profile/zone evaluation is deliberately forward-only: estimator labels
    come from the frozen train period and held-out predictions come from the
    frozen validation period. This keeps the spatial stress tests from quietly
    admitting validation labels into fit.
    """

    development = combine_development_datasets([train, validation])
    zone_map, _ = build_spatial_zone_map(bundle)
    development = attach_spatial_zones(development, zone_map)
    train_with_zone = attach_spatial_zones(train, zone_map)
    validation_with_zone = attach_spatial_zones(validation, zone_map)

    folds: list[EvaluationFold] = [
        EvaluationFold(
            design="temporal_holdout",
            fold_id="temporal_validation_2024",
            held_out_group="",
            train_sample_ids=train.sample_ids,
            validation_sample_ids=validation.sample_ids,
        )
    ]

    rolling = rolling_origin_assignments(
        [train, validation], maximum_folds=rolling_folds
    )
    for fold_id, fold in rolling.groupby("fold_id", sort=True):
        folds.append(
            EvaluationFold(
                design="rolling_origin",
                fold_id=str(fold_id),
                held_out_group="",
                train_sample_ids=tuple(
                    fold.loc[fold["role"].eq("train"), "sample_id"].astype(str)
                ),
                validation_sample_ids=tuple(
                    fold.loc[fold["role"].eq("validation"), "sample_id"].astype(str)
                ),
            )
        )

    for group_field, design in (
        ("profile_id", "leave_profile_out"),
        ("zone_id", "leave_zone_out"),
    ):
        groups = sorted(validation_with_zone.frame[group_field].astype(str).unique())
        for group in groups:
            training_ids = tuple(
                train_with_zone.frame.loc[
                    train_with_zone.frame[group_field].astype(str).ne(group), "sample_id"
                ].astype(str)
            )
            validation_ids = tuple(
                validation_with_zone.frame.loc[
                    validation_with_zone.frame[group_field].astype(str).eq(group), "sample_id"
                ].astype(str)
            )
            if not training_ids or not validation_ids:
                raise ContractViolation(f"Empty {design} fold for group {group}")
            folds.append(
                EvaluationFold(
                    design=design,
                    fold_id=f"{design}_{group}",
                    held_out_group=group,
                    train_sample_ids=training_ids,
                    validation_sample_ids=validation_ids,
                )
            )

    summary = validate_fold_contracts(development, folds)
    return development, folds, summary


def validate_fold_contracts(
    development: ManifestDataset,
    folds: Sequence[EvaluationFold],
) -> pd.DataFrame:
    """Assert disjoint roles and forward target dates for every fold."""

    source = development.frame.set_index("sample_id", drop=False)
    rows: list[dict[str, Any]] = []
    for fold in folds:
        train_ids = set(fold.train_sample_ids)
        validation_ids = set(fold.validation_sample_ids)
        overlap = train_ids & validation_ids
        if overlap:
            raise ContractViolation(f"Fold roles overlap for {fold.fold_id}: {len(overlap)} IDs")
        missing = (train_ids | validation_ids) - set(source.index.astype(str))
        if missing:
            raise ContractViolation(f"Fold {fold.fold_id} references unknown sample IDs")
        train_dates = pd.to_datetime(source.loc[list(fold.train_sample_ids), "target_date"])
        validation_dates = pd.to_datetime(
            source.loc[list(fold.validation_sample_ids), "target_date"]
        )
        train_max = pd.Timestamp(train_dates.max())
        validation_min = pd.Timestamp(validation_dates.min())
        if train_max >= validation_min:
            raise ContractViolation(
                f"Fold {fold.fold_id} is not forward-only: {train_max} >= {validation_min}"
            )
        rows.append(
            {
                "design": fold.design,
                "fold_id": fold.fold_id,
                "held_out_group": fold.held_out_group,
                "train_rows": len(fold.train_sample_ids),
                "validation_rows": len(fold.validation_sample_ids),
                "train_target_date_max": train_max.date().isoformat(),
                "validation_target_date_min": validation_min.date().isoformat(),
                "validation_target_date_max": pd.Timestamp(validation_dates.max()).date().isoformat(),
                "train_sample_ids_sha256": sample_id_list_sha256(fold.train_sample_ids),
                "validation_sample_ids_sha256": sample_id_list_sha256(
                    fold.validation_sample_ids
                ),
            }
        )
    summary = pd.DataFrame(rows)
    expected = {
        "temporal_holdout": 1,
        "rolling_origin": 5,
        "leave_profile_out": 14,
        "leave_zone_out": 4,
    }
    actual = summary.groupby("design")["fold_id"].nunique().to_dict()
    if actual != expected:
        raise ContractViolation(f"Gate B0/B1 fold counts changed: actual={actual}, expected={expected}")
    return summary


def evaluate_development_models(
    development: ManifestDataset,
    folds: Sequence[EvaluationFold],
    *,
    model_specs: Sequence[Mapping[str, Any]],
    contract: FeatureContract,
    random_seed: int,
    weight_clip: tuple[float, float],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fit every configured model independently inside every governed fold."""

    history = causal_feature_history(development)
    prediction_frames: list[pd.DataFrame] = []
    metric_rows: list[dict[str, Any]] = []
    state_rows: list[dict[str, Any]] = []
    for fold in folds:
        train_fold = derived_dataset(
            development,
            fold.train_sample_ids,
            split="train",
            label=fold.fold_id,
        )
        validation_fold = derived_dataset(
            development,
            fold.validation_sample_ids,
            split="validation",
            label=fold.fold_id,
        )
        truth = pd.to_numeric(
            validation_fold.frame[TARGET_COLUMN], errors="raise"
        ).to_numpy(float)
        evaluation_weights = train_only_precision_weights(
            validation_fold.frame, lower=weight_clip[0], upper=weight_clip[1]
        )
        for spec in model_specs:
            model = build_model(
                spec,
                contract=contract,
                random_seed=random_seed,
                weight_clip=weight_clip,
            )
            fit_started = perf_counter()
            model.fit(train_fold)
            fit_seconds = perf_counter() - fit_started
            predict_started = perf_counter()
            prediction = model.predict(validation_fold, history_frame=history)
            inference_seconds = perf_counter() - predict_started
            if prediction.shape != truth.shape or not np.isfinite(prediction).all():
                raise RuntimeError(
                    f"{model.model_id}/{fold.fold_id} produced non-finite or misaligned predictions"
                )
            metrics = regression_metrics(truth, prediction)
            weighted = regression_metrics(
                truth, prediction, sample_weight=evaluation_weights
            )
            metric_rows.append(
                {
                    "design": fold.design,
                    "fold_id": fold.fold_id,
                    "held_out_group": fold.held_out_group,
                    "model_id": model.model_id,
                    "family": model.family,
                    "train_rows": len(train_fold.frame),
                    "validation_rows": len(validation_fold.frame),
                    "fit_seconds": fit_seconds,
                    "inference_seconds": inference_seconds,
                    **metrics,
                    **{f"precision_weighted_{key}": value for key, value in weighted.items()},
                }
            )
            state = model.state_dict()
            state_rows.append(
                {
                    "design": fold.design,
                    "fold_id": fold.fold_id,
                    "model_id": model.model_id,
                    "parameter_count": state.get("parameter_count", 0),
                    "feature_count": state.get("feature_count", 0),
                    "state_sha256": _mapping_sha256(state),
                }
            )
            metadata_columns = [
                "sample_id",
                "point_id",
                "profile_id",
                "zone_id",
                "current_date",
                "target_date",
                "forecast_horizon_days",
                "sigma_rate_mm_y",
            ]
            available = [
                column for column in metadata_columns if column in validation_fold.frame
            ]
            predictions = validation_fold.frame.loc[:, available].copy()
            predictions.insert(0, "design", fold.design)
            predictions.insert(1, "fold_id", fold.fold_id)
            predictions.insert(2, "held_out_group", fold.held_out_group)
            predictions.insert(3, "model_id", model.model_id)
            predictions.insert(4, "family", model.family)
            predictions["y_true"] = truth
            predictions["y_pred"] = prediction
            predictions["error"] = prediction - truth
            predictions["absolute_error"] = np.abs(prediction - truth)
            prediction_frames.append(predictions)
    return (
        pd.concat(prediction_frames, ignore_index=True),
        pd.DataFrame(metric_rows),
        pd.DataFrame(state_rows),
    )


def aggregate_development_metrics(
    predictions: pd.DataFrame,
    *,
    weight_clip: tuple[float, float],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (design, model_id, family), frame in predictions.groupby(
        ["design", "model_id", "family"], sort=True
    ):
        weights = train_only_precision_weights(
            frame, lower=weight_clip[0], upper=weight_clip[1]
        )
        metrics = regression_metrics(frame["y_true"], frame["y_pred"])
        weighted = regression_metrics(
            frame["y_true"], frame["y_pred"], sample_weight=weights
        )
        rows.append(
            {
                "design": design,
                "model_id": model_id,
                "family": family,
                "folds": int(frame["fold_id"].nunique()),
                **metrics,
                **{f"precision_weighted_{key}": value for key, value in weighted.items()},
            }
        )
    return pd.DataFrame(rows).sort_values(["design", "mae", "model_id"]).reset_index(
        drop=True
    )


def compare_to_references(
    aggregate: pd.DataFrame,
    *,
    reference_model: str,
    kalman_reference: str,
) -> pd.DataFrame:
    references = aggregate.loc[
        aggregate["model_id"].isin([reference_model, kalman_reference]),
        ["design", "model_id", "mae"],
    ].pivot(index="design", columns="model_id", values="mae")
    rows: list[dict[str, Any]] = []
    for row in aggregate.itertuples(index=False):
        b1 = float(references.loc[row.design, reference_model])
        kalman = float(references.loc[row.design, kalman_reference])
        rows.append(
            {
                "design": row.design,
                "model_id": row.model_id,
                "mae": float(row.mae),
                "reference_b1_mae": b1,
                "delta_vs_b1_mae": float(row.mae - b1),
                "improvement_vs_b1_percent": float(100.0 * (b1 - row.mae) / b1),
                "reference_fixed_kalman_mae": kalman,
                "delta_vs_fixed_kalman_mae": float(row.mae - kalman),
                "improvement_vs_fixed_kalman_percent": float(
                    100.0 * (kalman - row.mae) / kalman
                ),
            }
        )
    return pd.DataFrame(rows).sort_values(["design", "mae", "model_id"]).reset_index(
        drop=True
    )


def rank_candidates(
    aggregate: pd.DataFrame,
    *,
    model_specs: Sequence[Mapping[str, Any]],
    selection_config: Mapping[str, Any],
) -> pd.DataFrame:
    """Apply the predeclared normalized-MAE score and complexity penalty."""

    weights = {
        str(key): float(value)
        for key, value in selection_config["normalized_mae_weights"].items()
    }
    if not np.isclose(sum(weights.values()), 1.0):
        raise ValueError("Selection design weights must sum to one")
    reference_model = str(selection_config["reference_model"])
    metric_lookup = aggregate.set_index(["design", "model_id"])["mae"]
    penalties = {
        str(spec["model_id"]): float(spec.get("complexity_penalty", 0.0))
        for spec in model_specs
    }
    rows: list[dict[str, Any]] = []
    for model_id in penalties:
        row: dict[str, Any] = {
            "model_id": model_id,
            "complexity_penalty": penalties[model_id],
        }
        normalized_total = 0.0
        for design, weight in weights.items():
            model_mae = float(metric_lookup.loc[(design, model_id)])
            reference_mae = float(metric_lookup.loc[(design, reference_model)])
            ratio = model_mae / reference_mae
            row[f"{design}_mae"] = model_mae
            row[f"{design}_normalized_mae"] = ratio
            normalized_total += weight * ratio
        row["weighted_normalized_mae"] = normalized_total
        row["selection_score"] = normalized_total + penalties[model_id]
        rows.append(row)
    ranking = pd.DataFrame(rows).sort_values(
        ["selection_score", "temporal_holdout_mae", "model_id"], kind="mergesort"
    )
    ranking.insert(0, "rank", np.arange(1, len(ranking) + 1))
    ranking["selected"] = ranking["rank"].eq(1)
    return ranking.reset_index(drop=True)


def fit_frozen_candidate(
    development: ManifestDataset,
    *,
    model_spec: Mapping[str, Any],
    contract: FeatureContract,
    random_seed: int,
    weight_clip: tuple[float, float],
) -> tuple[T1Model, dict[str, Any]]:
    training = derived_dataset(
        development,
        development.sample_ids,
        split="train",
        label="frozen_candidate_development",
    )
    model = build_model(
        model_spec,
        contract=contract,
        random_seed=random_seed,
        weight_clip=weight_clip,
    )
    started = perf_counter()
    model.fit(training)
    fit_seconds = perf_counter() - started
    state = model.state_dict()
    state["fit_seconds"] = fit_seconds
    state["development_rows"] = len(training.frame)
    state["development_sample_ids_sha256"] = training.provenance.sample_ids_sha256
    return model, state


def predict_frozen_candidate(
    model: T1Model,
    dataset: ManifestDataset,
    *,
    history_datasets: Sequence[ManifestDataset],
    weight_clip: tuple[float, float],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    history = causal_feature_history(*history_datasets)
    started = perf_counter()
    prediction = model.predict(dataset, history_frame=history)
    inference_seconds = perf_counter() - started
    truth = pd.to_numeric(dataset.frame[TARGET_COLUMN], errors="raise").to_numpy(float)
    if prediction.shape != truth.shape or not np.isfinite(prediction).all():
        raise RuntimeError("Frozen candidate produced invalid test predictions")
    weights = train_only_precision_weights(
        dataset.frame, lower=weight_clip[0], upper=weight_clip[1]
    )
    metrics = regression_metrics(truth, prediction)
    weighted = regression_metrics(truth, prediction, sample_weight=weights)
    output = dataset.frame.loc[
        :,
        [
            "sample_id",
            "point_id",
            "profile_id",
            "current_date",
            "target_date",
            "forecast_horizon_days",
            "sigma_rate_mm_y",
        ],
    ].copy()
    output.insert(0, "model_id", model.model_id)
    output["y_true"] = truth
    output["y_pred"] = prediction
    output["error"] = prediction - truth
    output["absolute_error"] = np.abs(prediction - truth)
    report = {
        "model_id": model.model_id,
        "split": dataset.provenance.split,
        "rows": len(dataset.frame),
        "sample_ids_sha256": dataset.provenance.sample_ids_sha256,
        "inference_seconds": inference_seconds,
        **metrics,
        **{f"precision_weighted_{key}": value for key, value in weighted.items()},
    }
    return output, report


def causal_feature_history(*datasets: ManifestDataset) -> pd.DataFrame:
    """Return non-label feature history; future rows are filtered per prediction."""

    if not datasets:
        raise ValueError("At least one dataset is required for feature history")
    feature_columns = datasets[0].feature_columns
    if any(dataset.feature_columns != feature_columns for dataset in datasets):
        raise ContractViolation("History datasets have different estimator schemas")
    columns = list(
        dict.fromkeys(
            ["sample_id", "point_id", "profile_id", "current_date", *feature_columns]
        )
    )
    frame = pd.concat(
        [dataset.frame.loc[:, columns] for dataset in datasets], ignore_index=True
    )
    if frame["sample_id"].duplicated().any():
        frame = frame.drop_duplicates("sample_id", keep="first")
    return frame


def derived_dataset(
    source: ManifestDataset,
    sample_ids: Iterable[str],
    *,
    split: str,
    label: str,
) -> ManifestDataset:
    ids = tuple(map(str, sample_ids))
    if len(ids) != len(set(ids)):
        raise ContractViolation(f"Derived dataset {label} has duplicate sample IDs")
    indexed = source.frame.set_index("sample_id", drop=False)
    missing = set(ids) - set(indexed.index.astype(str))
    if missing:
        raise ContractViolation(f"Derived dataset {label} has {len(missing)} unknown sample IDs")
    frame = indexed.loc[list(ids)].reset_index(drop=True)
    provenance = SplitProvenance(
        task=source.provenance.task,
        split=split,
        version=source.provenance.version,
        manifest_path=Path("artifacts") / "splits" / "derived" / f"{label}.csv",
        manifest_file_sha256=sha256(label.encode("utf-8")).hexdigest(),
        sample_ids_sha256=sample_id_list_sha256(ids),
        row_count=len(ids),
        test_authorized=False,
    )
    return ManifestDataset(
        frame=frame,
        feature_columns=source.feature_columns,
        provenance=provenance,
    )


def _mapping_sha256(payload: Mapping[str, Any]) -> str:
    import json

    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return sha256(normalized.encode("utf-8")).hexdigest()

