"""Train-only adaptive Kalman baseline for Gate B2.

The model uses identifiers only to retrieve a point's causal measurement
history.  Adaptation is driven exclusively by origin-known allowlisted fields;
all normalization scales are fitted inside the supplied training fold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .baselines import TARGET_COLUMN
from .leakage import LeakageViolation
from .splits import ManifestDataset


HISTORY_COLUMNS = (
    "point_id",
    "current_date",
    "last_settlement_mm",
    "last_rate_mm_y",
    "current_standard_uncertainty_mm",
    "recent_acceleration_mm_y2",
    "std_last_3_rates_mm_y",
    "missing_campaigns_since_previous",
)


@dataclass(frozen=True)
class PointHistory:
    dates: np.ndarray
    settlement: np.ndarray
    rate: np.ndarray
    sigma: np.ndarray
    acceleration: np.ndarray
    volatility: np.ndarray
    missing_campaigns: np.ndarray


@dataclass(frozen=True)
class PreparedKalmanHistory:
    points: Mapping[str, PointHistory]
    source_rows: int


def prepare_kalman_history(history_frame: pd.DataFrame) -> PreparedKalmanHistory:
    """Convert non-label feature history to deterministic point arrays."""

    missing = set(HISTORY_COLUMNS) - set(history_frame)
    if missing:
        raise ValueError(f"Adaptive Kalman history is missing columns: {sorted(missing)}")
    frame = history_frame.loc[:, HISTORY_COLUMNS].copy()
    frame["current_date"] = pd.to_datetime(frame["current_date"], errors="coerce")
    if frame["current_date"].isna().any():
        raise ValueError("Adaptive Kalman history contains invalid current_date values")
    numeric = [column for column in HISTORY_COLUMNS if column not in {"point_id", "current_date"}]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    histories: dict[str, PointHistory] = {}
    for point_id, group in frame.groupby("point_id", sort=False):
        ordered = group.sort_values("current_date", kind="mergesort").drop_duplicates(
            "current_date", keep="last"
        )
        histories[str(point_id)] = PointHistory(
            dates=ordered["current_date"].to_numpy(dtype="datetime64[ns]"),
            settlement=ordered["last_settlement_mm"].to_numpy(float),
            rate=ordered["last_rate_mm_y"].to_numpy(float),
            sigma=ordered["current_standard_uncertainty_mm"].to_numpy(float),
            acceleration=ordered["recent_acceleration_mm_y2"].to_numpy(float),
            volatility=ordered["std_last_3_rates_mm_y"].to_numpy(float),
            missing_campaigns=ordered["missing_campaigns_since_previous"].to_numpy(float),
        )
    return PreparedKalmanHistory(points=histories, source_rows=len(frame))


@dataclass
class AdaptiveKalmanRate:
    """Constant-velocity Kalman filter with train-scaled process adaptation.

    Position is the primary measurement.  The allowlisted last observed rate is
    assimilated as a noisy rate anchor whose variance is derived from origin
    measurement uncertainty and the elapsed observation interval.  A small,
    train-selected acceleration gain may extrapolate the filtered velocity to
    the centre of the forecast interval.
    """

    model_id: str
    parameters: Mapping[str, Any]
    family: str = "adaptive_kalman"
    fallback_rate_: float | None = None
    acceleration_scale_: float | None = None
    volatility_scale_: float | None = None
    train_sample_ids_sha256_: str | None = None

    def fit(self, dataset: ManifestDataset) -> "AdaptiveKalmanRate":
        if not isinstance(dataset, ManifestDataset) or dataset.provenance.split != "train":
            raise LeakageViolation("Adaptive Kalman fit requires train provenance")
        target = pd.to_numeric(dataset.frame[TARGET_COLUMN], errors="coerce").dropna()
        if target.empty:
            raise ValueError("Adaptive Kalman training target is empty")
        self.fallback_rate_ = float(target.median())
        quantile = float(self.parameters["adaptation_quantile"])
        if not 0.5 <= quantile < 1.0:
            raise ValueError("adaptation_quantile must be in [0.5, 1.0)")
        acceleration = pd.to_numeric(
            dataset.frame["recent_acceleration_mm_y2"], errors="coerce"
        ).abs()
        volatility = pd.to_numeric(
            dataset.frame["std_last_3_rates_mm_y"], errors="coerce"
        )
        self.acceleration_scale_ = _positive_scale(acceleration, quantile)
        self.volatility_scale_ = _positive_scale(volatility, quantile)
        self.train_sample_ids_sha256_ = dataset.provenance.sample_ids_sha256
        self._validate_parameters()
        return self

    def predict(
        self,
        dataset: ManifestDataset,
        *,
        history_frame: pd.DataFrame | PreparedKalmanHistory | None = None,
    ) -> np.ndarray:
        mean, _, _ = self.predict_distribution(dataset, history_frame=history_frame)
        return mean

    def predict_distribution(
        self,
        dataset: ManifestDataset,
        *,
        history_frame: pd.DataFrame | PreparedKalmanHistory | None,
    ) -> tuple[np.ndarray, np.ndarray, pd.DataFrame]:
        """Return mean, raw standard deviation, and process diagnostics."""

        self._require_fitted()
        if history_frame is None:
            raise ValueError("Adaptive Kalman prediction requires causal feature history")
        prepared = (
            history_frame
            if isinstance(history_frame, PreparedKalmanHistory)
            else prepare_kalman_history(history_frame)
        )
        means: list[float] = []
        sigmas: list[float] = []
        scales: list[float] = []
        q_values: list[float] = []
        history_counts: list[int] = []
        for row in dataset.frame.itertuples(index=False):
            point = prepared.points.get(str(row.point_id))
            fallback = _finite_or(row.last_rate_mm_y, float(self.fallback_rate_))
            if point is None:
                mean, sigma, process_scale, q_value, history_count = self._fallback_distribution(
                    row, fallback
                )
            else:
                cutoff = int(
                    np.searchsorted(
                        point.dates,
                        np.datetime64(pd.Timestamp(row.current_date), "ns"),
                        side="right",
                    )
                )
                mean, sigma, process_scale, q_value, history_count = self._filter_distribution(
                    point, cutoff, row, fallback
                )
            means.append(mean)
            sigmas.append(sigma)
            scales.append(process_scale)
            q_values.append(q_value)
            history_counts.append(history_count)
        diagnostics = pd.DataFrame(
            {
                "process_scale": scales,
                "adaptive_q": q_values,
                "causal_history_rows": history_counts,
            }
        )
        return np.asarray(means), np.asarray(sigmas), diagnostics

    def state_dict(self) -> dict[str, Any]:
        self._require_fitted()
        return {
            "model_id": self.model_id,
            "family": self.family,
            "parameters": dict(self.parameters),
            "fallback_rate": self.fallback_rate_,
            "acceleration_scale": self.acceleration_scale_,
            "volatility_scale": self.volatility_scale_,
            "train_sample_ids_sha256": self.train_sample_ids_sha256_,
            "parameter_count": 2,
        }

    def _filter_distribution(
        self,
        point: PointHistory,
        cutoff: int,
        origin: Any,
        fallback_rate: float,
    ) -> tuple[float, float, float, float, int]:
        if cutoff <= 0:
            return self._fallback_distribution(origin, fallback_rate)
        finite = np.isfinite(point.settlement[:cutoff])
        indices = np.flatnonzero(finite)
        if len(indices) < 2:
            return self._fallback_distribution(origin, fallback_rate, history_count=len(indices))

        first_index = int(indices[0])
        first_sigma = _finite_or(
            point.sigma[first_index],
            float(self.parameters["minimum_measurement_variance"]) ** 0.5,
        )
        first_rate = _finite_or(point.rate[first_index], fallback_rate)
        state = np.asarray([point.settlement[first_index], first_rate], dtype=float)
        covariance = np.diag(
            [
                max(
                    first_sigma**2,
                    float(self.parameters["minimum_measurement_variance"]),
                ),
                float(self.parameters["initial_velocity_variance"]),
            ]
        )
        previous_date = point.dates[first_index]
        previous_sigma = first_sigma
        for index in indices[1:]:
            index = int(index)
            delta_days = float((point.dates[index] - previous_date) / np.timedelta64(1, "D"))
            delta_years = max(delta_days / 365.25, 1.0 / 365.25)
            process_scale = self._process_scale(
                point.acceleration[index],
                point.volatility[index],
                point.missing_campaigns[index],
            )
            q_value = float(self.parameters["q_base"]) * process_scale
            transition = np.asarray([[1.0, delta_years], [0.0, 1.0]])
            process = q_value * np.asarray(
                [
                    [delta_years**3 / 3.0, delta_years**2 / 2.0],
                    [delta_years**2 / 2.0, delta_years],
                ]
            )
            state = transition @ state
            covariance = transition @ covariance @ transition.T + process

            sigma = _finite_or(point.sigma[index], previous_sigma)
            measurement_variance = max(
                sigma**2, float(self.parameters["minimum_measurement_variance"])
            )
            state, covariance = _scalar_update(
                state,
                covariance,
                np.asarray([1.0, 0.0]),
                float(point.settlement[index]),
                measurement_variance,
            )

            rate = point.rate[index]
            if np.isfinite(rate):
                rate_variance = (
                    float(self.parameters["rate_measurement_variance_multiplier"])
                    * max(
                        (sigma**2 + previous_sigma**2) / delta_years**2,
                        float(self.parameters["minimum_rate_measurement_variance"]),
                    )
                )
                state, covariance = _scalar_update(
                    state,
                    covariance,
                    np.asarray([0.0, 1.0]),
                    float(rate),
                    rate_variance,
                )
            previous_date = point.dates[index]
            previous_sigma = sigma

        horizon_years = self._horizon_years(origin)
        origin_acceleration = _finite_or(origin.recent_acceleration_mm_y2, 0.0)
        acceleration_limit = (
            float(self.parameters["acceleration_clip_ratio"])
            * float(self.acceleration_scale_)
        )
        projected_acceleration = float(
            np.clip(origin_acceleration, -acceleration_limit, acceleration_limit)
        )
        mean = float(state[1]) + (
            float(self.parameters["acceleration_gain"])
            * projected_acceleration
            * horizon_years
        )
        process_scale = self._process_scale(
            origin.recent_acceleration_mm_y2,
            origin.std_last_3_rates_mm_y,
            origin.missing_campaigns_since_previous,
        )
        q_value = float(self.parameters["q_base"]) * process_scale
        origin_sigma = _finite_or(
            origin.current_standard_uncertainty_mm,
            float(self.parameters["minimum_measurement_variance"]) ** 0.5,
        )
        average_rate_measurement_variance = 2.0 * origin_sigma**2 / horizon_years**2
        raw_variance = (
            max(float(covariance[1, 1]), 0.0)
            + q_value * horizon_years / 3.0
            + average_rate_measurement_variance
        )
        raw_floor = float(self.parameters["raw_sigma_floor_mm_y"])
        sigma_rate = float(np.sqrt(max(raw_variance, raw_floor**2)))
        if not np.isfinite(mean):
            mean = fallback_rate
        return mean, sigma_rate, process_scale, q_value, len(indices)

    def _fallback_distribution(
        self,
        origin: Any,
        fallback_rate: float,
        *,
        history_count: int = 0,
    ) -> tuple[float, float, float, float, int]:
        horizon_years = self._horizon_years(origin)
        process_scale = self._process_scale(
            origin.recent_acceleration_mm_y2,
            origin.std_last_3_rates_mm_y,
            origin.missing_campaigns_since_previous,
        )
        q_value = float(self.parameters["q_base"]) * process_scale
        sigma = _finite_or(
            origin.current_standard_uncertainty_mm,
            float(self.parameters["minimum_measurement_variance"]) ** 0.5,
        )
        variance = (
            float(self.parameters["initial_velocity_variance"])
            + q_value * horizon_years / 3.0
            + 2.0 * sigma**2 / horizon_years**2
        )
        raw_floor = float(self.parameters["raw_sigma_floor_mm_y"])
        return (
            float(fallback_rate),
            float(np.sqrt(max(variance, raw_floor**2))),
            process_scale,
            q_value,
            history_count,
        )

    def _process_scale(self, acceleration: Any, volatility: Any, gap: Any) -> float:
        acceleration_ratio = min(
            abs(_finite_or(acceleration, 0.0)) / float(self.acceleration_scale_),
            float(self.parameters["maximum_feature_ratio"]),
        )
        volatility_ratio = min(
            max(_finite_or(volatility, 0.0), 0.0) / float(self.volatility_scale_),
            float(self.parameters["maximum_feature_ratio"]),
        )
        gap_value = min(
            max(_finite_or(gap, 0.0) - 1.0, 0.0),
            float(self.parameters["maximum_feature_ratio"]),
        )
        raw = (
            1.0
            + float(self.parameters["acceleration_weight"]) * acceleration_ratio
            + float(self.parameters["volatility_weight"]) * volatility_ratio
            + float(self.parameters["gap_weight"]) * gap_value
        )
        return float(
            np.clip(
                raw,
                float(self.parameters["process_scale_min"]),
                float(self.parameters["process_scale_max"]),
            )
        )

    def _horizon_years(self, origin: Any) -> float:
        days = _finite_or(origin.forecast_horizon_days, np.nan)
        if not np.isfinite(days) or days <= 0:
            raise ValueError("forecast_horizon_days must be positive")
        return max(days / 365.25, 1.0 / 365.25)

    def _require_fitted(self) -> None:
        values = (
            self.fallback_rate_,
            self.acceleration_scale_,
            self.volatility_scale_,
            self.train_sample_ids_sha256_,
        )
        if any(value is None for value in values):
            raise RuntimeError(f"Model is not fitted: {self.model_id}")

    def _validate_parameters(self) -> None:
        positive = (
            "q_base",
            "initial_velocity_variance",
            "minimum_measurement_variance",
            "minimum_rate_measurement_variance",
            "rate_measurement_variance_multiplier",
            "maximum_feature_ratio",
            "process_scale_min",
            "process_scale_max",
            "acceleration_clip_ratio",
            "raw_sigma_floor_mm_y",
        )
        for name in positive:
            if float(self.parameters[name]) <= 0:
                raise ValueError(f"Adaptive Kalman parameter must be positive: {name}")
        if float(self.parameters["process_scale_min"]) > float(
            self.parameters["process_scale_max"]
        ):
            raise ValueError("process_scale_min cannot exceed process_scale_max")
        if float(self.parameters["acceleration_gain"]) < 0:
            raise ValueError("acceleration_gain must be non-negative")


def _scalar_update(
    state: np.ndarray,
    covariance: np.ndarray,
    observation: np.ndarray,
    value: float,
    variance: float,
) -> tuple[np.ndarray, np.ndarray]:
    innovation = value - float(observation @ state)
    innovation_variance = float(observation @ covariance @ observation + variance)
    if not np.isfinite(innovation_variance) or innovation_variance <= 0:
        raise FloatingPointError("Kalman innovation variance is not positive")
    gain = covariance @ observation / innovation_variance
    updated_state = state + gain * innovation
    identity = np.eye(len(state))
    residual = identity - np.outer(gain, observation)
    updated_covariance = residual @ covariance @ residual.T + np.outer(gain, gain) * variance
    updated_covariance = 0.5 * (updated_covariance + updated_covariance.T)
    return updated_state, updated_covariance


def _positive_scale(values: pd.Series, quantile: float) -> float:
    finite = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    finite = finite.loc[finite.ge(0)]
    if finite.empty:
        return 1.0
    scale = float(finite.quantile(quantile))
    if not np.isfinite(scale) or scale <= 1e-12:
        positive = finite.loc[finite.gt(1e-12)]
        scale = float(positive.median()) if not positive.empty else 1.0
    return scale


def _finite_or(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return number if np.isfinite(number) else float(fallback)
