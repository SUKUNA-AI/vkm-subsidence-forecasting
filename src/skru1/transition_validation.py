"""Origin-only transition proxy and segmented validation for Gate B2."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from .metrics import regression_metrics


@dataclass(frozen=True)
class TransitionThresholds:
    acceleration_absolute: float
    volatility: float
    missing_campaigns: int
    acceleration_quantile: float
    volatility_quantile: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fit_transition_thresholds(
    train_frame: pd.DataFrame,
    *,
    acceleration_quantile: float,
    volatility_quantile: float,
    missing_campaigns_threshold: int,
) -> TransitionThresholds:
    """Fit all transition thresholds using the current training fold only."""

    if not 0.5 <= acceleration_quantile < 1:
        raise ValueError("acceleration_quantile must be in [0.5, 1)")
    if not 0.5 <= volatility_quantile < 1:
        raise ValueError("volatility_quantile must be in [0.5, 1)")
    if missing_campaigns_threshold < 1:
        raise ValueError("missing_campaigns_threshold must be positive")
    acceleration = pd.to_numeric(
        train_frame["recent_acceleration_mm_y2"], errors="coerce"
    ).abs()
    volatility = pd.to_numeric(
        train_frame["std_last_3_rates_mm_y"], errors="coerce"
    )
    acceleration_threshold = float(acceleration.dropna().quantile(acceleration_quantile))
    volatility_threshold = float(volatility.dropna().quantile(volatility_quantile))
    if not np.isfinite(acceleration_threshold) or acceleration_threshold <= 0:
        raise ValueError("Training fold cannot identify a positive acceleration threshold")
    if not np.isfinite(volatility_threshold) or volatility_threshold <= 0:
        raise ValueError("Training fold cannot identify a positive volatility threshold")
    return TransitionThresholds(
        acceleration_absolute=acceleration_threshold,
        volatility=volatility_threshold,
        missing_campaigns=int(missing_campaigns_threshold),
        acceleration_quantile=float(acceleration_quantile),
        volatility_quantile=float(volatility_quantile),
    )


def classify_transition_proxy(
    frame: pd.DataFrame,
    thresholds: TransitionThresholds,
) -> pd.DataFrame:
    """Return mutually exclusive origin-known transition categories."""

    acceleration = pd.to_numeric(
        frame["recent_acceleration_mm_y2"], errors="coerce"
    ).fillna(0.0)
    volatility = pd.to_numeric(
        frame["std_last_3_rates_mm_y"], errors="coerce"
    ).fillna(0.0)
    gaps = pd.to_numeric(
        frame["missing_campaigns_since_previous"], errors="coerce"
    ).fillna(0.0)
    accelerating = acceleration.ge(thresholds.acceleration_absolute)
    decelerating = acceleration.le(-thresholds.acceleration_absolute)
    volatile_or_gap = (~accelerating) & (~decelerating) & (
        volatility.ge(thresholds.volatility)
        | gaps.ge(thresholds.missing_campaigns)
    )
    segment = np.select(
        [accelerating, decelerating, volatile_or_gap],
        ["accelerating", "decelerating", "volatile_or_gap"],
        default="stable",
    )
    output = pd.DataFrame(index=frame.index)
    output["transition_segment"] = segment
    output["is_transition"] = output["transition_segment"].ne("stable")
    output["transition_acceleration_flag"] = accelerating.to_numpy(bool)
    output["transition_deceleration_flag"] = decelerating.to_numpy(bool)
    output["transition_volatility_or_gap_flag"] = volatile_or_gap.to_numpy(bool)
    return output.reset_index(drop=True)


def transition_metrics(predictions: pd.DataFrame) -> pd.DataFrame:
    """Compute all/stable/transition/mechanism metrics by design and model."""

    required = {
        "design",
        "model_id",
        "sample_id",
        "point_id",
        "profile_id",
        "y_true",
        "y_pred",
        "transition_segment",
        "is_transition",
    }
    missing = required - set(predictions)
    if missing:
        raise ValueError(f"Transition predictions are missing columns: {sorted(missing)}")
    rows: list[dict[str, Any]] = []
    for (design, model_id), frame in predictions.groupby(["design", "model_id"], sort=True):
        scopes: list[tuple[str, str, pd.DataFrame]] = [("all", "all", frame)]
        scopes.extend(
            (
                "stable_vs_transition",
                label,
                frame.loc[frame["is_transition"].astype(bool).eq(label == "transition")],
            )
            for label in ("stable", "transition")
        )
        scopes.extend(
            ("mechanism", str(label), segment)
            for label, segment in frame.groupby("transition_segment", sort=True)
        )
        for scope, segment, subset in scopes:
            if subset.empty:
                continue
            metrics = regression_metrics(subset["y_true"], subset["y_pred"])
            rows.append(
                {
                    "design": design,
                    "model_id": model_id,
                    "scope": scope,
                    "segment": segment,
                    "rows": len(subset),
                    "points": subset["point_id"].astype(str).nunique(),
                    "profiles": subset["profile_id"].astype(str).nunique(),
                    **metrics,
                }
            )
    result = pd.DataFrame(rows)
    reference = result.loc[
        result["model_id"].eq("B1_persistence_last_rate"),
        ["design", "scope", "segment", "mae"],
    ].rename(columns={"mae": "reference_b1_mae"})
    result = result.merge(reference, on=["design", "scope", "segment"], how="left")
    result["improvement_vs_b1_percent"] = 100.0 * (
        result["reference_b1_mae"] - result["mae"]
    ) / result["reference_b1_mae"]
    return result.sort_values(
        ["design", "scope", "segment", "mae", "model_id"], kind="mergesort"
    ).reset_index(drop=True)
