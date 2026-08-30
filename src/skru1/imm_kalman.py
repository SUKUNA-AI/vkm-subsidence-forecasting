"""Two-regime interacting multiple-model filter for Gate B3.

The model is deliberately narrow: both regimes share a causal
``[settlement, velocity, acceleration]`` state, while acceleration retention
and jerk/process noise differ.  Regime probabilities are updated from
origin-known measurement innovations only.  No label outside the supplied
training fold is used by ``fit``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .adaptive_kalman import (
    PointHistory,
    PreparedKalmanHistory,
    prepare_kalman_history,
)
from .baselines import TARGET_COLUMN
from .leakage import LeakageViolation
from .splits import ManifestDataset


_POSITION_OBSERVATION = np.asarray([1.0, 0.0, 0.0])
_VELOCITY_OBSERVATION = np.asarray([0.0, 1.0, 0.0])
_ACCELERATION_OBSERVATION = np.asarray([0.0, 0.0, 1.0])


@dataclass
class TwoRegimeIMMRate:
    """Damped-acceleration IMM with stable and transition regimes."""

    model_id: str
    parameters: Mapping[str, Any]
    family: str = "imm_damped_acceleration"
    fallback_rate_: float | None = None
    acceleration_scale_: float | None = None
    train_sample_ids_sha256_: str | None = None

    def fit(self, dataset: ManifestDataset) -> "TwoRegimeIMMRate":
        if not isinstance(dataset, ManifestDataset) or dataset.provenance.split != "train":
            raise LeakageViolation("Two-regime IMM fit requires train provenance")
        target = pd.to_numeric(dataset.frame[TARGET_COLUMN], errors="coerce").dropna()
        if target.empty:
            raise ValueError("Two-regime IMM training target is empty")
        self._validate_parameters()
        self.fallback_rate_ = float(target.median())
        acceleration = pd.to_numeric(
            dataset.frame["recent_acceleration_mm_y2"], errors="coerce"
        ).abs()
        self.acceleration_scale_ = _positive_scale(
            acceleration,
            float(self.parameters["acceleration_scale_quantile"]),
        )
        self.train_sample_ids_sha256_ = dataset.provenance.sample_ids_sha256
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
        """Return predictive mean, raw sigma, and inspectable regime diagnostics."""

        self._require_fitted()
        if history_frame is None:
            raise ValueError("Two-regime IMM prediction requires causal feature history")
        prepared = (
            history_frame
            if isinstance(history_frame, PreparedKalmanHistory)
            else prepare_kalman_history(history_frame)
        )
        means: list[float] = []
        sigmas: list[float] = []
        diagnostics: list[dict[str, Any]] = []
        for origin in dataset.frame.itertuples(index=False):
            point = prepared.points.get(str(origin.point_id))
            fallback = _finite_or(origin.last_rate_mm_y, float(self.fallback_rate_))
            if point is None:
                mean, sigma, row = self._fallback_distribution(origin, fallback)
            else:
                cutoff = int(
                    np.searchsorted(
                        point.dates,
                        np.datetime64(pd.Timestamp(origin.current_date), "ns"),
                        side="right",
                    )
                )
                mean, sigma, row = self._filter_distribution(
                    point,
                    cutoff,
                    origin,
                    fallback,
                )
            means.append(mean)
            sigmas.append(sigma)
            diagnostics.append(row)
        return np.asarray(means), np.asarray(sigmas), pd.DataFrame(diagnostics)

    def state_dict(self) -> dict[str, Any]:
        self._require_fitted()
        return {
            "model_id": self.model_id,
            "family": self.family,
            "parameters": dict(self.parameters),
            "fallback_rate": self.fallback_rate_,
            "acceleration_scale": self.acceleration_scale_,
            "train_sample_ids_sha256": self.train_sample_ids_sha256_,
            "tuned_parameter_count": 4,
            "state_dimension": 3,
            "regime_count": 2,
        }

    def _filter_distribution(
        self,
        point: PointHistory,
        cutoff: int,
        origin: Any,
        fallback_rate: float,
    ) -> tuple[float, float, dict[str, Any]]:
        if cutoff <= 0:
            return self._fallback_distribution(origin, fallback_rate)
        indices = np.flatnonzero(np.isfinite(point.settlement[:cutoff]))
        if len(indices) < 2:
            return self._fallback_distribution(
                origin,
                fallback_rate,
                history_count=len(indices),
            )

        first = int(indices[0])
        first_sigma = _finite_or(
            point.sigma[first],
            float(self.parameters["minimum_measurement_variance"]) ** 0.5,
        )
        first_rate = _finite_or(point.rate[first], fallback_rate)
        first_acceleration = self._clip_acceleration(point.acceleration[first])
        initial_state = np.asarray(
            [point.settlement[first], first_rate, first_acceleration],
            dtype=float,
        )
        initial_covariance = np.diag(
            [
                max(
                    first_sigma**2,
                    float(self.parameters["initial_position_variance"]),
                ),
                float(self.parameters["initial_velocity_variance"]),
                float(self.parameters["initial_acceleration_variance"]),
            ]
        )
        states = np.stack([initial_state.copy(), initial_state.copy()])
        covariances = np.stack([initial_covariance.copy(), initial_covariance.copy()])
        transition_probability = float(
            self.parameters["initial_transition_probability"]
        )
        probabilities = np.asarray(
            [1.0 - transition_probability, transition_probability], dtype=float
        )
        previous_date = point.dates[first]
        previous_sigma = first_sigma

        for raw_index in indices[1:]:
            index = int(raw_index)
            delta_days = float(
                (point.dates[index] - previous_date) / np.timedelta64(1, "D")
            )
            delta_years = max(delta_days / 365.25, 1.0 / 365.25)
            states, covariances, probabilities = self._imm_step(
                states,
                covariances,
                probabilities,
                delta_years=delta_years,
                settlement=float(point.settlement[index]),
                rate=point.rate[index],
                acceleration=point.acceleration[index],
                sigma=_finite_or(point.sigma[index], previous_sigma),
                previous_sigma=previous_sigma,
            )
            previous_date = point.dates[index]
            previous_sigma = _finite_or(point.sigma[index], previous_sigma)

        horizon_years = self._horizon_years(origin)
        regime_means: list[float] = []
        regime_variances: list[float] = []
        origin_sigma = _finite_or(
            origin.current_standard_uncertainty_mm,
            float(self.parameters["minimum_measurement_variance"]) ** 0.5,
        )
        average_rate_measurement_variance = 2.0 * origin_sigma**2 / horizon_years**2
        forecast_vector = np.asarray([0.0, 1.0, 0.5 * horizon_years])
        future_jerk_factor = horizon_years**4 / 36.0
        q_values = self._q_values()
        for regime in range(2):
            state = states[regime].copy()
            state[2] = self._clip_acceleration(state[2])
            mean = float(forecast_vector @ state)
            variance = (
                float(forecast_vector @ covariances[regime] @ forecast_vector)
                + float(q_values[regime]) * future_jerk_factor
                + average_rate_measurement_variance
            )
            regime_means.append(mean)
            regime_variances.append(max(variance, 0.0))

        means = np.asarray(regime_means, dtype=float)
        variances = np.asarray(regime_variances, dtype=float)
        mean = float(probabilities @ means)
        mixture_variance = float(
            probabilities @ (variances + np.square(means - mean))
        )
        raw_floor = float(self.parameters["raw_sigma_floor_mm_y"])
        sigma_rate = float(np.sqrt(max(mixture_variance, raw_floor**2)))
        combined_state = probabilities @ states
        if not np.isfinite(mean) or not np.isfinite(sigma_rate):
            return self._fallback_distribution(
                origin,
                fallback_rate,
                history_count=len(indices),
                numerical_fallback=True,
            )
        return mean, sigma_rate, self._diagnostics(
            probabilities,
            combined_state,
            means,
            history_count=len(indices),
            numerical_fallback=False,
        )

    def _imm_step(
        self,
        states: np.ndarray,
        covariances: np.ndarray,
        probabilities: np.ndarray,
        *,
        delta_years: float,
        settlement: float,
        rate: float,
        acceleration: float,
        sigma: float,
        previous_sigma: float,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        transition_probabilities = self._regime_transition_matrix()
        predicted_regime_probability = probabilities @ transition_probabilities
        probability_floor = np.finfo(float).tiny
        predicted_regime_probability = np.maximum(
            predicted_regime_probability, probability_floor
        )

        mixed_states = np.zeros_like(states)
        mixed_covariances = np.zeros_like(covariances)
        for destination in range(2):
            weights = (
                probabilities * transition_probabilities[:, destination]
            ) / predicted_regime_probability[destination]
            mixed_state = weights @ states
            mixed_covariance = np.zeros((3, 3), dtype=float)
            for source in range(2):
                delta = states[source] - mixed_state
                mixed_covariance += weights[source] * (
                    covariances[source] + np.outer(delta, delta)
                )
            mixed_states[destination] = mixed_state
            mixed_covariances[destination] = mixed_covariance

        q_values = self._q_values()
        retentions = self._retentions()
        updated_states = np.zeros_like(states)
        updated_covariances = np.zeros_like(covariances)
        log_likelihoods = np.zeros(2, dtype=float)
        for regime in range(2):
            transition = _state_transition(delta_years, retentions[regime])
            process = _process_covariance(
                delta_years,
                q_values[regime],
                float(self.parameters["covariance_jitter"]),
            )
            state = transition @ mixed_states[regime]
            covariance = (
                transition @ mixed_covariances[regime] @ transition.T + process
            )
            measurement_variance = max(
                sigma**2,
                float(self.parameters["minimum_measurement_variance"]),
            )
            state, covariance, log_likelihood = _scalar_update_with_log_likelihood(
                state,
                covariance,
                _POSITION_OBSERVATION,
                settlement,
                measurement_variance,
                variance_floor=float(self.parameters["likelihood_variance_floor"]),
            )
            if np.isfinite(rate):
                rate_variance = float(
                    self.parameters["rate_measurement_variance_multiplier"]
                ) * max(
                    (sigma**2 + previous_sigma**2) / delta_years**2,
                    float(self.parameters["minimum_rate_measurement_variance"]),
                )
                state, covariance, rate_log_likelihood = (
                    _scalar_update_with_log_likelihood(
                        state,
                        covariance,
                        _VELOCITY_OBSERVATION,
                        float(rate),
                        rate_variance,
                        variance_floor=float(
                            self.parameters["likelihood_variance_floor"]
                        ),
                    )
                )
                log_likelihood += rate_log_likelihood
            if np.isfinite(acceleration):
                acceleration_variance = max(
                    (
                        float(self.acceleration_scale_)
                        * float(
                            self.parameters[
                                "acceleration_measurement_scale_multiplier"
                            ]
                        )
                    )
                    ** 2,
                    float(
                        self.parameters["minimum_acceleration_measurement_variance"]
                    ),
                )
                state, covariance, acceleration_log_likelihood = (
                    _scalar_update_with_log_likelihood(
                        state,
                        covariance,
                        _ACCELERATION_OBSERVATION,
                        self._clip_acceleration(acceleration),
                        acceleration_variance,
                        variance_floor=float(
                            self.parameters["likelihood_variance_floor"]
                        ),
                    )
                )
                log_likelihood += acceleration_log_likelihood
            updated_states[regime] = state
            updated_covariances[regime] = _stabilize_covariance(
                covariance,
                float(self.parameters["covariance_jitter"]),
            )
            log_likelihoods[regime] = log_likelihood

        log_weights = np.log(predicted_regime_probability) + log_likelihoods
        updated_probabilities = _normalize_log_weights(log_weights)
        return updated_states, updated_covariances, updated_probabilities

    def _fallback_distribution(
        self,
        origin: Any,
        fallback_rate: float,
        *,
        history_count: int = 0,
        numerical_fallback: bool = False,
    ) -> tuple[float, float, dict[str, Any]]:
        horizon_years = self._horizon_years(origin)
        probabilities = np.asarray(
            [
                1.0 - float(self.parameters["initial_transition_probability"]),
                float(self.parameters["initial_transition_probability"]),
            ]
        )
        acceleration = self._clip_acceleration(origin.recent_acceleration_mm_y2)
        state = np.asarray([0.0, fallback_rate, acceleration], dtype=float)
        means = np.asarray(
            [fallback_rate + 0.5 * acceleration * horizon_years] * 2,
            dtype=float,
        )
        origin_sigma = _finite_or(
            origin.current_standard_uncertainty_mm,
            float(self.parameters["minimum_measurement_variance"]) ** 0.5,
        )
        q_average = float(probabilities @ self._q_values())
        variance = (
            float(self.parameters["initial_velocity_variance"])
            + 0.25
            * horizon_years**2
            * float(self.parameters["initial_acceleration_variance"])
            + q_average * horizon_years**4 / 36.0
            + 2.0 * origin_sigma**2 / horizon_years**2
        )
        raw_floor = float(self.parameters["raw_sigma_floor_mm_y"])
        return (
            float(means[0]),
            float(np.sqrt(max(variance, raw_floor**2))),
            self._diagnostics(
                probabilities,
                state,
                means,
                history_count=history_count,
                numerical_fallback=numerical_fallback,
            ),
        )

    def _diagnostics(
        self,
        probabilities: np.ndarray,
        combined_state: np.ndarray,
        regime_means: np.ndarray,
        *,
        history_count: int,
        numerical_fallback: bool,
    ) -> dict[str, Any]:
        safe = np.clip(probabilities, np.finfo(float).tiny, 1.0)
        entropy = float(-np.sum(safe * np.log(safe)) / np.log(2.0))
        return {
            "stable_probability": float(probabilities[0]),
            "transition_probability": float(probabilities[1]),
            "regime_entropy": entropy,
            "posterior_velocity_mm_y": float(combined_state[1]),
            "posterior_acceleration_mm_y2": float(combined_state[2]),
            "stable_forecast_mean_mm_y": float(regime_means[0]),
            "transition_forecast_mean_mm_y": float(regime_means[1]),
            "stable_q": float(self.parameters["q_stable"]),
            "transition_q": float(self.parameters["q_transition"]),
            "causal_history_rows": int(history_count),
            "numerical_fallback_used": bool(numerical_fallback),
        }

    def _regime_transition_matrix(self) -> np.ndarray:
        stable_stay = float(self.parameters["p_stable_stay"])
        transition_stay = float(self.parameters["p_transition_stay"])
        return np.asarray(
            [
                [stable_stay, 1.0 - stable_stay],
                [1.0 - transition_stay, transition_stay],
            ],
            dtype=float,
        )

    def _q_values(self) -> np.ndarray:
        return np.asarray(
            [self.parameters["q_stable"], self.parameters["q_transition"]],
            dtype=float,
        )

    def _retentions(self) -> np.ndarray:
        return np.asarray(
            [
                self.parameters["stable_acceleration_retention_per_year"],
                self.parameters["transition_acceleration_retention_per_year"],
            ],
            dtype=float,
        )

    def _clip_acceleration(self, value: Any) -> float:
        limit = float(self.parameters["acceleration_clip_ratio"]) * float(
            self.acceleration_scale_
        )
        return float(np.clip(_finite_or(value, 0.0), -limit, limit))

    @staticmethod
    def _horizon_years(origin: Any) -> float:
        days = _finite_or(origin.forecast_horizon_days, np.nan)
        if not np.isfinite(days) or days <= 0:
            raise ValueError("forecast_horizon_days must be positive")
        return max(days / 365.25, 1.0 / 365.25)

    def _validate_parameters(self) -> None:
        positive = (
            "q_stable",
            "q_transition",
            "initial_position_variance",
            "initial_velocity_variance",
            "initial_acceleration_variance",
            "minimum_measurement_variance",
            "minimum_rate_measurement_variance",
            "rate_measurement_variance_multiplier",
            "acceleration_scale_quantile",
            "acceleration_measurement_scale_multiplier",
            "minimum_acceleration_measurement_variance",
            "acceleration_clip_ratio",
            "raw_sigma_floor_mm_y",
            "covariance_jitter",
            "likelihood_variance_floor",
        )
        for name in positive:
            if float(self.parameters[name]) <= 0:
                raise ValueError(f"{name} must be positive")
        if float(self.parameters["q_transition"]) <= float(
            self.parameters["q_stable"]
        ):
            raise ValueError("q_transition must exceed q_stable")
        for name in ("p_stable_stay", "p_transition_stay"):
            if not 0.5 < float(self.parameters[name]) < 1.0:
                raise ValueError(f"{name} must be strictly between 0.5 and 1")
        for name in (
            "stable_acceleration_retention_per_year",
            "transition_acceleration_retention_per_year",
        ):
            if not 0.0 < float(self.parameters[name]) <= 1.0:
                raise ValueError(f"{name} must be in (0, 1]")
        initial_transition = float(self.parameters["initial_transition_probability"])
        if not 0.0 < initial_transition < 1.0:
            raise ValueError("initial_transition_probability must be in (0, 1)")
        quantile = float(self.parameters["acceleration_scale_quantile"])
        if not 0.5 <= quantile < 1.0:
            raise ValueError("acceleration_scale_quantile must be in [0.5, 1)")

    def _require_fitted(self) -> None:
        if (
            self.fallback_rate_ is None
            or self.acceleration_scale_ is None
            or self.train_sample_ids_sha256_ is None
        ):
            raise RuntimeError("Two-regime IMM is not fitted")


def _state_transition(delta_years: float, retention_per_year: float) -> np.ndarray:
    retention = float(retention_per_year) ** float(delta_years)
    return np.asarray(
        [
            [1.0, delta_years, 0.5 * delta_years**2],
            [0.0, 1.0, delta_years],
            [0.0, 0.0, retention],
        ],
        dtype=float,
    )


def _process_covariance(
    delta_years: float,
    q_value: float,
    jitter: float,
) -> np.ndarray:
    influence = np.asarray(
        [delta_years**3 / 6.0, delta_years**2 / 2.0, delta_years],
        dtype=float,
    )
    return float(q_value) * np.outer(influence, influence) + np.eye(3) * jitter


def _scalar_update_with_log_likelihood(
    state: np.ndarray,
    covariance: np.ndarray,
    observation: np.ndarray,
    measurement: float,
    measurement_variance: float,
    *,
    variance_floor: float,
) -> tuple[np.ndarray, np.ndarray, float]:
    innovation = float(measurement - observation @ state)
    innovation_variance = max(
        float(observation @ covariance @ observation + measurement_variance),
        variance_floor,
    )
    gain = covariance @ observation / innovation_variance
    updated_state = state + gain * innovation
    identity = np.eye(len(state))
    residual = identity - np.outer(gain, observation)
    updated_covariance = (
        residual @ covariance @ residual.T
        + np.outer(gain, gain) * measurement_variance
    )
    log_likelihood = -0.5 * (
        np.log(2.0 * np.pi * innovation_variance)
        + innovation**2 / innovation_variance
    )
    return updated_state, updated_covariance, float(log_likelihood)


def _normalize_log_weights(log_weights: np.ndarray) -> np.ndarray:
    maximum = float(np.max(log_weights))
    weights = np.exp(log_weights - maximum)
    total = float(np.sum(weights))
    if not np.isfinite(total) or total <= 0:
        return np.asarray([0.5, 0.5], dtype=float)
    return weights / total


def _stabilize_covariance(covariance: np.ndarray, jitter: float) -> np.ndarray:
    symmetric = 0.5 * (covariance + covariance.T)
    eigenvalues, eigenvectors = np.linalg.eigh(symmetric)
    eigenvalues = np.maximum(eigenvalues, jitter)
    return (eigenvectors * eigenvalues) @ eigenvectors.T


def _positive_scale(values: pd.Series, quantile: float) -> float:
    finite = pd.to_numeric(values, errors="coerce")
    finite = finite[np.isfinite(finite)]
    if finite.empty:
        return 1.0
    scale = float(finite.quantile(quantile))
    if not np.isfinite(scale) or scale <= 0:
        positive = finite[finite > 0]
        scale = float(positive.median()) if not positive.empty else 1.0
    return max(scale, np.finfo(float).eps)


def _finite_or(value: Any, fallback: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return numeric if np.isfinite(numeric) else float(fallback)
