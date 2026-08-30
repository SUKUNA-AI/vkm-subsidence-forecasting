"""Finite-sample scaled conformal intervals for Gate B2."""

from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd


def finite_sample_conformal_quantile(
    scores: Iterable[float],
    *,
    coverage: float,
) -> tuple[float, float]:
    """Return conservative conformal quantile and its empirical probability.

    The requested central coverage ``c`` uses rank ``ceil((n + 1) * c)`` and
    NumPy's ``higher`` order statistic.  The probability is capped at one for
    small calibration samples.
    """

    values = np.asarray(list(scores), dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise ValueError("Conformal calibration requires at least one finite score")
    if not 0 < coverage < 1:
        raise ValueError("coverage must be strictly between zero and one")
    probability = min(1.0, float(np.ceil((values.size + 1) * coverage) / values.size))
    quantile = float(np.quantile(values, probability, method="higher"))
    return quantile, probability


def calibrate_scaled_conformal(
    calibration_predictions: pd.DataFrame,
    *,
    coverage_levels: Iterable[float],
    sigma_floor: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Create train-OOF nonconformity scores and calibration summary."""

    required = {"sample_id", "y_true", "y_pred", "raw_sigma"}
    missing = required - set(calibration_predictions)
    if missing:
        raise ValueError(f"Calibration predictions are missing columns: {sorted(missing)}")
    if sigma_floor <= 0:
        raise ValueError("sigma_floor must be positive")
    scored = calibration_predictions.copy()
    denominator = np.maximum(
        pd.to_numeric(scored["raw_sigma"], errors="coerce").to_numpy(float),
        sigma_floor,
    )
    error = np.abs(
        pd.to_numeric(scored["y_true"], errors="coerce").to_numpy(float)
        - pd.to_numeric(scored["y_pred"], errors="coerce").to_numpy(float)
    )
    scored["sigma_used"] = denominator
    scored["nonconformity_score"] = error / denominator
    if not np.isfinite(scored["nonconformity_score"]).all():
        raise ValueError("Calibration produced non-finite nonconformity scores")
    rows: list[dict[str, float | int | str]] = []
    for coverage in coverage_levels:
        coverage = float(coverage)
        qhat, probability = finite_sample_conformal_quantile(
            scored["nonconformity_score"], coverage=coverage
        )
        rows.append(
            {
                "coverage": coverage,
                "alpha": 1.0 - coverage,
                "calibration_rows": len(scored),
                "quantile_probability": probability,
                "qhat": qhat,
                "method": "scaled_absolute_residual_finite_sample_higher",
            }
        )
    return scored, pd.DataFrame(rows).sort_values("coverage").reset_index(drop=True)


def apply_scaled_conformal_intervals(
    predictions: pd.DataFrame,
    calibration: pd.DataFrame,
    *,
    sigma_floor: float,
) -> pd.DataFrame:
    """Apply train-calibrated central intervals to untouched validation rows."""

    required = {"sample_id", "y_true", "y_pred", "raw_sigma"}
    missing = required - set(predictions)
    if missing:
        raise ValueError(f"Validation predictions are missing columns: {sorted(missing)}")
    output = predictions.copy()
    sigma = np.maximum(
        pd.to_numeric(output["raw_sigma"], errors="coerce").to_numpy(float),
        sigma_floor,
    )
    output["sigma_used"] = sigma
    for row in calibration.itertuples(index=False):
        suffix = _coverage_suffix(float(row.coverage))
        half_width = float(row.qhat) * sigma
        output[f"lower_{suffix}"] = output["y_pred"].to_numpy(float) - half_width
        output[f"upper_{suffix}"] = output["y_pred"].to_numpy(float) + half_width
        output[f"covered_{suffix}"] = (
            output["y_true"].to_numpy(float) >= output[f"lower_{suffix}"].to_numpy(float)
        ) & (
            output["y_true"].to_numpy(float) <= output[f"upper_{suffix}"].to_numpy(float)
        )
    return output


def interval_metrics(
    interval_predictions: pd.DataFrame,
    calibration: pd.DataFrame,
) -> pd.DataFrame:
    """Compute coverage, width, and central interval score."""

    truth = pd.to_numeric(interval_predictions["y_true"], errors="raise").to_numpy(float)
    rows: list[dict[str, float | int]] = []
    for calibrated in calibration.itertuples(index=False):
        coverage = float(calibrated.coverage)
        alpha = 1.0 - coverage
        suffix = _coverage_suffix(coverage)
        lower = interval_predictions[f"lower_{suffix}"].to_numpy(float)
        upper = interval_predictions[f"upper_{suffix}"].to_numpy(float)
        covered = (truth >= lower) & (truth <= upper)
        width = upper - lower
        score = width.copy()
        below = truth < lower
        above = truth > upper
        score[below] += (2.0 / alpha) * (lower[below] - truth[below])
        score[above] += (2.0 / alpha) * (truth[above] - upper[above])
        rows.append(
            {
                "coverage_nominal": coverage,
                "rows": len(truth),
                "coverage_empirical": float(np.mean(covered)),
                "coverage_gap": float(np.mean(covered) - coverage),
                "mean_width_mm_y": float(np.mean(width)),
                "median_width_mm_y": float(np.median(width)),
                "mean_interval_score": float(np.mean(score)),
                "qhat": float(calibrated.qhat),
            }
        )
    return pd.DataFrame(rows).sort_values("coverage_nominal").reset_index(drop=True)


def _coverage_suffix(coverage: float) -> str:
    percentage = int(round(coverage * 100))
    if not np.isclose(coverage * 100, percentage):
        raise ValueError("Coverage levels must map to integer percentages")
    return str(percentage)
