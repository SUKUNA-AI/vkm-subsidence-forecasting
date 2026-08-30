"""Student-t robust observation model for the frozen Gate B3 IMM dynamics.

Gate B4 changes exactly one modelling assumption: scalar observation
innovations use a Student-t likelihood and a bounded influence weight.  The
two-regime state dynamics, process noises, transition matrix, and causal
history rules remain those of the frozen B7 comparator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import lgamma
from typing import Any

import numpy as np

from .imm_kalman import (
    TwoRegimeIMMRate,
    _ACCELERATION_OBSERVATION,
    _POSITION_OBSERVATION,
    _VELOCITY_OBSERVATION,
    _normalize_log_weights,
    _process_covariance,
    _stabilize_covariance,
    _state_transition,
)


@dataclass
class RobustInnovationIMMRate(TwoRegimeIMMRate):
    """B7 dynamics with a Student-t scalar observation channel.

    ``student_t_df`` is the only selected Gate B4 hyperparameter.  For a
    standardized squared innovation ``z2`` the influence weight is
    ``min(1, (nu + 1) / (nu + z2))`` and the effective measurement variance is
    inflated by the inverse weight.  ``minimum_robust_weight`` is fixed by the
    protocol, not tuned.
    """

    family: str = "imm_student_t_robust_observation"
    _robust_updates: list[tuple[float, float, str]] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    def state_dict(self) -> dict[str, Any]:
        state = super().state_dict()
        state["family"] = self.family
        state["selected_parameter_count"] = 1
        state["frozen_dynamic_parameter_count"] = 4
        state["tuned_parameter_count"] = 1
        state["observation_likelihood"] = "student_t"
        return state

    def _validate_parameters(self) -> None:
        super()._validate_parameters()
        degrees_of_freedom = float(self.parameters["student_t_df"])
        if not np.isfinite(degrees_of_freedom) or degrees_of_freedom <= 2.0:
            raise ValueError("student_t_df must be finite and greater than 2")
        minimum_weight = float(self.parameters["minimum_robust_weight"])
        if not 0.0 < minimum_weight <= 1.0:
            raise ValueError("minimum_robust_weight must be in (0, 1]")

    def _filter_distribution(
        self,
        point: Any,
        cutoff: int,
        origin: Any,
        fallback_rate: float,
    ) -> tuple[float, float, dict[str, Any]]:
        self._robust_updates = []
        mean, sigma, diagnostics = super()._filter_distribution(
            point,
            cutoff,
            origin,
            fallback_rate,
        )
        return mean, sigma, self._augment_diagnostics(diagnostics)

    def _fallback_distribution(
        self,
        origin: Any,
        fallback_rate: float,
        *,
        history_count: int = 0,
        numerical_fallback: bool = False,
    ) -> tuple[float, float, dict[str, Any]]:
        mean, sigma, diagnostics = super()._fallback_distribution(
            origin,
            fallback_rate,
            history_count=history_count,
            numerical_fallback=numerical_fallback,
        )
        return mean, sigma, self._augment_diagnostics(diagnostics)

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
        predicted_regime_probability = np.maximum(
            predicted_regime_probability,
            np.finfo(float).tiny,
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
            covariance = transition @ mixed_covariances[regime] @ transition.T + process

            measurement_variance = max(
                sigma**2,
                float(self.parameters["minimum_measurement_variance"]),
            )
            state, covariance, log_likelihood, weight, z2 = (
                _student_t_scalar_update_with_log_likelihood(
                    state,
                    covariance,
                    _POSITION_OBSERVATION,
                    settlement,
                    measurement_variance,
                    degrees_of_freedom=float(self.parameters["student_t_df"]),
                    minimum_weight=float(self.parameters["minimum_robust_weight"]),
                    variance_floor=float(self.parameters["likelihood_variance_floor"]),
                )
            )
            self._robust_updates.append((weight, z2, "settlement"))

            if np.isfinite(rate):
                rate_variance = float(
                    self.parameters["rate_measurement_variance_multiplier"]
                ) * max(
                    (sigma**2 + previous_sigma**2) / delta_years**2,
                    float(self.parameters["minimum_rate_measurement_variance"]),
                )
                state, covariance, component, weight, z2 = (
                    _student_t_scalar_update_with_log_likelihood(
                        state,
                        covariance,
                        _VELOCITY_OBSERVATION,
                        float(rate),
                        rate_variance,
                        degrees_of_freedom=float(self.parameters["student_t_df"]),
                        minimum_weight=float(self.parameters["minimum_robust_weight"]),
                        variance_floor=float(
                            self.parameters["likelihood_variance_floor"]
                        ),
                    )
                )
                log_likelihood += component
                self._robust_updates.append((weight, z2, "rate"))

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
                state, covariance, component, weight, z2 = (
                    _student_t_scalar_update_with_log_likelihood(
                        state,
                        covariance,
                        _ACCELERATION_OBSERVATION,
                        self._clip_acceleration(acceleration),
                        acceleration_variance,
                        degrees_of_freedom=float(self.parameters["student_t_df"]),
                        minimum_weight=float(self.parameters["minimum_robust_weight"]),
                        variance_floor=float(
                            self.parameters["likelihood_variance_floor"]
                        ),
                    )
                )
                log_likelihood += component
                self._robust_updates.append((weight, z2, "acceleration"))

            updated_states[regime] = state
            updated_covariances[regime] = _stabilize_covariance(
                covariance,
                float(self.parameters["covariance_jitter"]),
            )
            log_likelihoods[regime] = log_likelihood

        log_weights = np.log(predicted_regime_probability) + log_likelihoods
        return (
            updated_states,
            updated_covariances,
            _normalize_log_weights(log_weights),
        )

    def _augment_diagnostics(self, diagnostics: dict[str, Any]) -> dict[str, Any]:
        output = dict(diagnostics)
        weights = np.asarray([item[0] for item in self._robust_updates], dtype=float)
        z2 = np.asarray([item[1] for item in self._robust_updates], dtype=float)
        output["student_t_df"] = float(self.parameters["student_t_df"])
        output["robust_update_count"] = int(len(weights))
        output["robust_downweighted_update_count"] = int(np.sum(weights < 0.999))
        output["robust_downweighted_rate"] = (
            float(np.mean(weights < 0.999)) if len(weights) else 0.0
        )
        output["robust_weight_mean"] = float(np.mean(weights)) if len(weights) else 1.0
        output["robust_weight_min"] = float(np.min(weights)) if len(weights) else 1.0
        output["robust_innovation_z2_max"] = float(np.max(z2)) if len(z2) else 0.0
        return output


def _student_t_scalar_update_with_log_likelihood(
    state: np.ndarray,
    covariance: np.ndarray,
    observation: np.ndarray,
    measurement: float,
    measurement_variance: float,
    *,
    degrees_of_freedom: float,
    minimum_weight: float,
    variance_floor: float,
) -> tuple[np.ndarray, np.ndarray, float, float, float]:
    """Perform one bounded-influence update and return Student-t evidence."""

    innovation = float(measurement - observation @ state)
    base_innovation_variance = max(
        float(observation @ covariance @ observation + measurement_variance),
        variance_floor,
    )
    standardized_squared = innovation**2 / base_innovation_variance
    raw_weight = (degrees_of_freedom + 1.0) / (
        degrees_of_freedom + standardized_squared
    )
    weight = float(np.clip(raw_weight, minimum_weight, 1.0))
    effective_measurement_variance = measurement_variance / weight
    effective_innovation_variance = max(
        float(
            observation @ covariance @ observation
            + effective_measurement_variance
        ),
        variance_floor,
    )
    gain = covariance @ observation / effective_innovation_variance
    updated_state = state + gain * innovation
    identity = np.eye(len(state))
    residual = identity - np.outer(gain, observation)
    updated_covariance = (
        residual @ covariance @ residual.T
        + np.outer(gain, gain) * effective_measurement_variance
    )
    log_likelihood = (
        lgamma((degrees_of_freedom + 1.0) / 2.0)
        - lgamma(degrees_of_freedom / 2.0)
        - 0.5
        * np.log(
            degrees_of_freedom * np.pi * base_innovation_variance
        )
        - 0.5
        * (degrees_of_freedom + 1.0)
        * np.log1p(standardized_squared / degrees_of_freedom)
    )
    return (
        updated_state,
        updated_covariance,
        float(log_likelihood),
        weight,
        float(standardized_squared),
    )
