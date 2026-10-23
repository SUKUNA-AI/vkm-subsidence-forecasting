"""Representation checks: external equivalence (AG §16–18), state closure (MS §2, §29), dissipation (AG §22).

These criteria measure the quality of a REPRESENTATION of a given world, never the truth of the world (FW §19).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np


def external_equivalence(y_fine, y_coarse, metric: str = "sup", R: np.ndarray | None = None,
                         relative_to: float | None = None) -> float:
    """E_ext = d_Y(Y_f, Y_c).

    ``sup`` — max |Δy|; ``rms`` — root mean square; ``mahalanobis`` — sqrt(Δyᵀ R⁻¹ Δy) with the observation
    covariance R (AG §18); ``relative_to`` divides sup/rms by a scale (e.g. the fine maximum).
    """
    dy = np.asarray(y_fine, float).ravel() - np.asarray(y_coarse, float).ravel()
    if metric == "sup":
        e = float(np.max(np.abs(dy)))
    elif metric == "rms":
        e = float(np.sqrt(np.mean(dy ** 2)))
    elif metric == "mahalanobis":
        if R is None:
            raise ValueError("Mahalanobis distance needs the covariance R")
        e = float(np.sqrt(dy @ np.linalg.solve(np.asarray(R, float), dy)))
    else:
        raise ValueError(metric)
    if relative_to is not None and metric != "mahalanobis":
        e /= relative_to
    return e


@dataclass(frozen=True)
class ClosureResult:
    verdict: str            # STATE_SUFFICIENT | STATE_INSUFFICIENT
    spread: float           # max pairwise E_ext of the future responses
    tolerance: float
    reduced_states: tuple
    detail: str = ""


def state_closure_test(fine_states: Sequence, reduce: Callable, future_response: Callable, tolerance: float,
                       reduced_atol: float = 1e-9, metric: str = "sup", relative_to: float | None = None) -> ClosureResult:
    """MS §29: fine states with the same reduced state must give the same future response within tolerance.

    ``reduce(X) -> x`` (array-like), ``future_response(X) -> Y`` (same future inputs for every X).
    Raises if the supplied fine states do not share one reduced state (the test would be meaningless).
    """
    xs = [np.atleast_1d(np.asarray(reduce(X), float)) for X in fine_states]
    for x in xs[1:]:
        if x.shape != xs[0].shape or np.max(np.abs(x - xs[0])) > reduced_atol * max(1.0, np.max(np.abs(xs[0]))):
            raise ValueError("fine states do not map to one reduced state")
    ys = [np.asarray(future_response(X), float) for X in fine_states]
    spread = 0.0
    for i in range(len(ys)):
        for j in range(i + 1, len(ys)):
            spread = max(spread, external_equivalence(ys[i], ys[j], metric, relative_to=relative_to))
    verdict = "STATE_SUFFICIENT" if spread <= tolerance else "STATE_INSUFFICIENT"
    return ClosureResult(verdict, spread, tolerance, tuple(tuple(x) for x in xs))


def markov_residual(step: Callable, x0, inputs, n_steps: int, dt: float, x_ref: Sequence) -> float:
    """Max deviation of a reduced Markov step x(t+Δt) = F(x(t), u) from a reference trajectory (MS §10)."""
    x = np.asarray(x0, float)
    worst = 0.0
    for k in range(n_steps):
        x = np.asarray(step(x, inputs, dt), float)
        worst = max(worst, float(np.max(np.abs(x - np.asarray(x_ref[k + 1], float)))))
    return worst


def dissipation_nonnegative(stress, strain_rate_inelastic, atol: float = 0.0) -> bool:
    """D = σ : ε̇_in ≥ 0 at every sample (AG §22; for the dissipative laws used here)."""
    d = np.sum(np.asarray(stress, float) * np.asarray(strain_rate_inelastic, float), axis=-1)
    return bool(np.all(d >= -atol))
