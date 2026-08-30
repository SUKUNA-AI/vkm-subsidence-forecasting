"""Expanded, dependency-light metric layer for Gate B5/B6."""

from __future__ import annotations

from dataclasses import dataclass
from math import erf, pi, sqrt
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


def point_metrics(
    y_true: Iterable[float],
    y_pred: Iterable[float],
    *,
    sample_weight: Iterable[float] | None = None,
    b1_prediction: Iterable[float] | None = None,
    mase_denominator: float | None = None,
    last_rate: Iterable[float] | None = None,
    neutral_zone: Iterable[float] | None = None,
) -> dict[str, float | int]:
    truth = _array(y_true, "y_true")
    prediction = _array(y_pred, "y_pred")
    if truth.shape != prediction.shape:
        raise ValueError("y_true and y_pred must have the same shape")
    valid = np.isfinite(truth) & np.isfinite(prediction)
    if not valid.any():
        raise ValueError("No finite prediction pairs")
    truth = truth[valid]
    prediction = prediction[valid]
    error = prediction - truth
    absolute = np.abs(error)
    mae = float(np.mean(absolute))
    rmse = float(np.sqrt(np.mean(error**2)))
    bias = float(np.mean(error))
    denominator = float(np.sum((truth - np.mean(truth)) ** 2))
    r2 = float(1.0 - np.sum(error**2) / denominator) if denominator > 0 else float("nan")
    result: dict[str, float | int] = {
        "n": int(len(truth)),
        "mae": mae,
        "median_absolute_error": float(np.median(absolute)),
        "rmse": rmse,
        "bias": bias,
        "absolute_bias": abs(bias),
        "p90_absolute_error": float(np.quantile(absolute, 0.90)),
        "p95_absolute_error": float(np.quantile(absolute, 0.95)),
        "max_absolute_error": float(np.max(absolute)),
        "r2_descriptive": r2,
    }
    if sample_weight is not None:
        weights = _array(sample_weight, "sample_weight")
        if weights.shape != valid.shape:
            raise ValueError("sample_weight must have the same shape as y_true")
        weights = weights[valid]
        if (~np.isfinite(weights) | (weights < 0)).any() or weights.sum() <= 0:
            raise ValueError("sample_weight must be finite, non-negative, and non-zero")
        weights = weights / weights.sum()
        result["precision_weighted_mae"] = float(np.sum(weights * absolute))
        result["precision_weighted_rmse"] = float(np.sqrt(np.sum(weights * error**2)))
    else:
        result["precision_weighted_mae"] = float("nan")
        result["precision_weighted_rmse"] = float("nan")
    if b1_prediction is not None:
        reference = _array(b1_prediction, "b1_prediction")
        if reference.shape != valid.shape:
            raise ValueError("b1_prediction must have the same shape as y_true")
        reference_mae = float(np.mean(np.abs(reference[valid] - truth)))
        result["b1_mae"] = reference_mae
        result["b1_skill"] = float(1.0 - mae / reference_mae) if reference_mae > 0 else float("nan")
    else:
        result["b1_mae"] = float("nan")
        result["b1_skill"] = float("nan")
    if mase_denominator is not None and np.isfinite(mase_denominator) and mase_denominator > 0:
        result["mase"] = float(mae / mase_denominator)
        result["mase_available"] = True
    else:
        result["mase"] = float("nan")
        result["mase_available"] = False
    if last_rate is not None:
        last = _array(last_rate, "last_rate")
        if last.shape != valid.shape:
            raise ValueError("last_rate must have the same shape as y_true")
        last = last[valid]
        if neutral_zone is None:
            zone = np.zeros_like(last)
        else:
            all_zone = _array(neutral_zone, "neutral_zone")
            if all_zone.shape != valid.shape:
                raise ValueError("neutral_zone must have the same shape as y_true")
            zone = np.maximum(all_zone[valid], 0.0)
        true_direction = neutral_direction(truth - last, zone)
        prediction_direction = neutral_direction(prediction - last, zone)
        result["direction_accuracy"] = float(np.mean(true_direction == prediction_direction))
        result["direction_neutral_fraction"] = float(np.mean(true_direction == 0))
    else:
        result["direction_accuracy"] = float("nan")
        result["direction_neutral_fraction"] = float("nan")
    return result


def neutral_direction(delta: Iterable[float], neutral_zone: Iterable[float]) -> np.ndarray:
    values = np.asarray(delta, dtype=float)
    zones = np.asarray(neutral_zone, dtype=float)
    if values.shape != zones.shape:
        raise ValueError("delta and neutral_zone must have identical shapes")
    return np.where(np.abs(values) <= zones, 0, np.sign(values)).astype(int)


def mase_denominator_from_train(train_frame: pd.DataFrame) -> float | None:
    """Mean absolute within-point historical target change with >=3 changes."""

    changes: list[float] = []
    for _, group in train_frame.sort_values(["point_id", "target_date"]).groupby("point_id", sort=False):
        values = pd.to_numeric(group["observed_rate_mm_y"], errors="coerce").to_numpy(float)
        values = values[np.isfinite(values)]
        if len(values) >= 4:
            changes.extend(np.abs(np.diff(values)).tolist())
    if not changes:
        return None
    value = float(np.mean(changes))
    return value if np.isfinite(value) and value > 0 else None


def interval_score(
    y_true: Iterable[float],
    lower: Iterable[float],
    upper: Iterable[float],
    *,
    alpha: float,
) -> np.ndarray:
    if not 0 < alpha < 1:
        raise ValueError("alpha must lie in (0, 1)")
    truth = _array(y_true, "y_true")
    lo = _array(lower, "lower")
    hi = _array(upper, "upper")
    if not (truth.shape == lo.shape == hi.shape):
        raise ValueError("Interval arrays must have the same shape")
    if np.any(lo > hi):
        raise ValueError("Interval lower bound exceeds upper bound")
    return (hi - lo) + (2.0 / alpha) * (lo - truth) * (truth < lo) + (2.0 / alpha) * (
        truth - hi
    ) * (truth > hi)


def weighted_interval_score(
    y_true: Iterable[float],
    median: Iterable[float],
    intervals: Mapping[float, tuple[Iterable[float], Iterable[float]]],
) -> np.ndarray:
    truth = _array(y_true, "y_true")
    centre = _array(median, "median")
    if truth.shape != centre.shape:
        raise ValueError("truth and median must have the same shape")
    total = 0.5 * np.abs(truth - centre)
    for coverage, (lower, upper) in sorted(intervals.items()):
        alpha = 1.0 - float(coverage)
        total = total + (alpha / 2.0) * interval_score(truth, lower, upper, alpha=alpha)
    return total / (len(intervals) + 0.5)


def normal_crps(y_true: Iterable[float], mean: Iterable[float], std: Iterable[float]) -> np.ndarray:
    truth = _array(y_true, "y_true")
    location = _array(mean, "mean")
    scale = _array(std, "std")
    if not (truth.shape == location.shape == scale.shape):
        raise ValueError("Normal CRPS arrays must have the same shape")
    if np.any(scale <= 0) or not np.isfinite(scale).all():
        raise ValueError("Normal predictive standard deviation must be positive and finite")
    z = (truth - location) / scale
    phi = np.exp(-0.5 * z**2) / sqrt(2.0 * pi)
    cdf = np.vectorize(lambda value: 0.5 * (1.0 + erf(value / sqrt(2.0))))(z)
    return scale * (z * (2.0 * cdf - 1.0) + 2.0 * phi - 1.0 / sqrt(pi))


def normal_nll(y_true: Iterable[float], mean: Iterable[float], std: Iterable[float]) -> np.ndarray:
    truth = _array(y_true, "y_true")
    location = _array(mean, "mean")
    scale = _array(std, "std")
    if not (truth.shape == location.shape == scale.shape):
        raise ValueError("Normal NLL arrays must have the same shape")
    if np.any(scale <= 0) or not np.isfinite(scale).all():
        raise ValueError("Normal predictive standard deviation must be positive and finite")
    return 0.5 * np.log(2.0 * pi * scale**2) + 0.5 * ((truth - location) / scale) ** 2


def interval_metrics(
    frame: pd.DataFrame,
    *,
    levels: Sequence[float] = (0.50, 0.80, 0.95),
    prefix: str = "conformal",
) -> dict[str, float | int]:
    truth = pd.to_numeric(frame["y_true"], errors="raise").to_numpy(float)
    intervals: dict[float, tuple[np.ndarray, np.ndarray]] = {}
    result: dict[str, float | int] = {"n": int(len(frame))}
    for level in levels:
        tag = _coverage_tag(level)
        lower = pd.to_numeric(frame[f"{prefix}_lower_{tag}"], errors="raise").to_numpy(float)
        upper = pd.to_numeric(frame[f"{prefix}_upper_{tag}"], errors="raise").to_numpy(float)
        intervals[float(level)] = (lower, upper)
        covered = (truth >= lower) & (truth <= upper)
        score = interval_score(truth, lower, upper, alpha=1.0 - float(level))
        width = upper - lower
        result[f"coverage_{tag}"] = float(np.mean(covered))
        result[f"mean_width_{tag}"] = float(np.mean(width))
        result[f"median_width_{tag}"] = float(np.median(width))
        result[f"interval_score_{tag}"] = float(np.mean(score))
    median_column = f"{prefix}_median"
    median = frame[median_column] if median_column in frame else frame["y_pred"]
    result["weighted_interval_score"] = float(
        np.mean(weighted_interval_score(truth, median, intervals))
    )
    return result


@dataclass(frozen=True)
class ScaledConformalCalibration:
    levels: tuple[float, ...]
    quantiles: Mapping[str, float]
    median_horizon_days: float
    median_uncertainty: float
    uncertainty_floor: float
    scale_clip: tuple[float, float]
    calibration_rows: int
    calibration_sample_ids_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "levels": list(self.levels),
            "quantiles": dict(self.quantiles),
            "median_horizon_days": self.median_horizon_days,
            "median_uncertainty": self.median_uncertainty,
            "uncertainty_floor": self.uncertainty_floor,
            "scale_clip": list(self.scale_clip),
            "calibration_rows": self.calibration_rows,
            "calibration_sample_ids_sha256": self.calibration_sample_ids_sha256,
        }


def fit_scaled_conformal(
    inner_oof: pd.DataFrame,
    *,
    levels: Sequence[float] = (0.50, 0.80, 0.95),
    scale_clip: tuple[float, float] = (0.25, 4.0),
) -> ScaledConformalCalibration:
    required = {
        "sample_id",
        "y_true",
        "y_pred",
        "forecast_horizon_days",
        "current_standard_uncertainty_mm",
    }
    missing = required - set(inner_oof.columns)
    if missing:
        raise ValueError(f"Inner OOF calibration is missing columns: {sorted(missing)}")
    if "provenance_role" in inner_oof and not inner_oof["provenance_role"].eq("inner_validation").all():
        raise ValueError("Conformal calibration accepts only inner-validation OOF residuals")
    horizon = pd.to_numeric(inner_oof["forecast_horizon_days"], errors="raise").to_numpy(float)
    uncertainty = pd.to_numeric(
        inner_oof["current_standard_uncertainty_mm"], errors="coerce"
    ).to_numpy(float)
    median_horizon = float(np.nanmedian(horizon))
    finite_uncertainty = uncertainty[np.isfinite(uncertainty) & (uncertainty > 0)]
    uncertainty_floor = float(np.quantile(finite_uncertainty, 0.10)) if len(finite_uncertainty) else 1.0
    uncertainty = np.where(np.isfinite(uncertainty) & (uncertainty > 0), uncertainty, uncertainty_floor)
    median_uncertainty = float(np.median(uncertainty))
    scale = _conformal_scale(
        horizon,
        uncertainty,
        median_horizon,
        median_uncertainty,
        scale_clip,
    )
    residual = np.abs(
        pd.to_numeric(inner_oof["y_true"], errors="raise").to_numpy(float)
        - pd.to_numeric(inner_oof["y_pred"], errors="raise").to_numpy(float)
    ) / scale
    quantiles = {
        _coverage_tag(level): _finite_sample_quantile(residual, float(level)) for level in levels
    }
    from .splits import sample_id_list_sha256

    return ScaledConformalCalibration(
        levels=tuple(map(float, levels)),
        quantiles=quantiles,
        median_horizon_days=median_horizon,
        median_uncertainty=median_uncertainty,
        uncertainty_floor=uncertainty_floor,
        scale_clip=tuple(map(float, scale_clip)),
        calibration_rows=len(inner_oof),
        calibration_sample_ids_sha256=sample_id_list_sha256(inner_oof["sample_id"].astype(str)),
    )


def apply_scaled_conformal(
    predictions: pd.DataFrame,
    calibration: ScaledConformalCalibration,
) -> pd.DataFrame:
    output = predictions.copy()
    horizon = pd.to_numeric(output["forecast_horizon_days"], errors="raise").to_numpy(float)
    uncertainty = pd.to_numeric(output["current_standard_uncertainty_mm"], errors="coerce").to_numpy(float)
    uncertainty = np.where(
        np.isfinite(uncertainty) & (uncertainty > 0), uncertainty, calibration.uncertainty_floor
    )
    scale = _conformal_scale(
        horizon,
        uncertainty,
        calibration.median_horizon_days,
        calibration.median_uncertainty,
        calibration.scale_clip,
    )
    centre = pd.to_numeric(output["y_pred"], errors="raise").to_numpy(float)
    output["conformal_median"] = centre
    for level in calibration.levels:
        tag = _coverage_tag(level)
        radius = calibration.quantiles[tag] * scale
        output[f"conformal_lower_{tag}"] = centre - radius
        output[f"conformal_upper_{tag}"] = centre + radius
    return output


def paired_cluster_sensitivity(
    predictions: pd.DataFrame,
    *,
    model_id: str,
    reference_model_id: str,
    cluster_column: str,
    replicates: int = 2000,
    seed: int = 42117,
) -> dict[str, float | int | str]:
    subset = predictions.loc[
        predictions["model_id"].isin([model_id, reference_model_id]),
        ["sample_id", "model_id", "absolute_error", cluster_column],
    ]
    wide = subset.pivot(index=["sample_id", cluster_column], columns="model_id", values="absolute_error")
    if model_id not in wide or reference_model_id not in wide:
        raise ValueError("Paired comparison is missing a model")
    wide = wide.dropna(subset=[model_id, reference_model_id]).reset_index()
    wide["delta"] = wide[model_id] - wide[reference_model_id]
    clusters = tuple(sorted(wide[cluster_column].astype(str).unique()))
    if len(clusters) < 2:
        raise ValueError("Cluster sensitivity requires at least two clusters")
    by_cluster = {key: group["delta"].to_numpy(float) for key, group in wide.groupby(cluster_column)}
    rng = np.random.default_rng(seed)
    estimates = np.empty(replicates, dtype=float)
    for index in range(replicates):
        sampled = rng.choice(clusters, size=len(clusters), replace=True)
        values = np.concatenate([by_cluster[key] for key in sampled])
        estimates[index] = float(np.mean(values))
    return {
        "model_id": model_id,
        "reference_model_id": reference_model_id,
        "cluster_column": cluster_column,
        "clusters": len(clusters),
        "paired_rows": len(wide),
        "mean_absolute_error_delta": float(wide["delta"].mean()),
        "lower_025": float(np.quantile(estimates, 0.025)),
        "upper_975": float(np.quantile(estimates, 0.975)),
        "improved_cluster_fraction": float(
            np.mean([np.mean(by_cluster[key]) < 0 for key in clusters])
        ),
        "replicates": int(replicates),
        "seed": int(seed),
        "interpretation": "sensitivity_only_not_iid_inference",
    }


def leave_one_cluster_out_jackknife(
    predictions: pd.DataFrame,
    *,
    model_id: str,
    reference_model_id: str,
    cluster_column: str,
) -> pd.DataFrame:
    subset = predictions.loc[
        predictions["model_id"].isin([model_id, reference_model_id]),
        ["sample_id", "model_id", "absolute_error", cluster_column],
    ]
    wide = subset.pivot(index=["sample_id", cluster_column], columns="model_id", values="absolute_error")
    wide = wide.dropna(subset=[model_id, reference_model_id]).reset_index()
    wide["delta"] = wide[model_id] - wide[reference_model_id]
    rows: list[dict[str, Any]] = []
    for cluster in sorted(wide[cluster_column].astype(str).unique()):
        kept = wide.loc[wide[cluster_column].astype(str).ne(cluster)]
        rows.append(
            {
                "model_id": model_id,
                "reference_model_id": reference_model_id,
                "cluster_column": cluster_column,
                "left_out_cluster": cluster,
                "remaining_rows": len(kept),
                "mean_absolute_error_delta": float(kept["delta"].mean()),
            }
        )
    return pd.DataFrame(rows)


def _conformal_scale(
    horizon: np.ndarray,
    uncertainty: np.ndarray,
    median_horizon: float,
    median_uncertainty: float,
    clip: tuple[float, float],
) -> np.ndarray:
    if median_horizon <= 0 or median_uncertainty <= 0:
        raise ValueError("Conformal scale medians must be positive")
    raw = np.sqrt(np.maximum(horizon, 1.0) / median_horizon) * np.sqrt(
        np.maximum(uncertainty, 1e-12) / median_uncertainty
    )
    return np.clip(raw, clip[0], clip[1])


def _finite_sample_quantile(values: np.ndarray, coverage: float) -> float:
    if not 0 < coverage < 1 or not len(values):
        raise ValueError("Invalid conformal coverage or empty residuals")
    rank = int(np.ceil((len(values) + 1) * coverage))
    rank = min(max(rank, 1), len(values))
    return float(np.sort(values)[rank - 1])


def _coverage_tag(level: float) -> str:
    return str(int(round(100 * float(level))))


def _array(values: Iterable[float], name: str) -> np.ndarray:
    result = np.asarray(list(values), dtype=float)
    if result.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional")
    return result

