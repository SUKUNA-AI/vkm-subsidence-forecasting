"""Torch-free Student-t scoring utilities for Gate C1."""

from __future__ import annotations

import numpy as np
from scipy import stats

from .data_contracts import ContractViolation


def student_t_quantiles(
    loc: np.ndarray,
    scale: np.ndarray,
    df: np.ndarray,
    probabilities: np.ndarray,
) -> np.ndarray:
    loc = np.asarray(loc, dtype=float)
    scale = np.asarray(scale, dtype=float)
    df = np.asarray(df, dtype=float)
    probabilities = np.asarray(probabilities, dtype=float)
    if (scale <= 0).any() or (df <= 2.01).any():
        raise ContractViolation("Student-t quantiles require scale > 0 and df > 2.01")
    quantiles = stats.t.ppf(
        probabilities.reshape(1, -1),
        df=df.reshape(-1, 1),
        loc=loc.reshape(-1, 1),
        scale=scale.reshape(-1, 1),
    )
    if not np.isfinite(quantiles).all() or (np.diff(quantiles, axis=1) < 0).any():
        raise ContractViolation("Student-t quantile calculation failed")
    return quantiles


def quantile_grid_crps(
    truth: np.ndarray,
    loc: np.ndarray,
    scale: np.ndarray,
    df: np.ndarray,
    *,
    probabilities: np.ndarray | None = None,
) -> np.ndarray:
    """Fixed 0.01...0.99 approximation of CRPS via pinball integration."""

    tau = np.asarray(
        probabilities if probabilities is not None else np.arange(0.01, 1.00, 0.01),
        dtype=float,
    )
    if tau.shape != (99,) or not np.allclose(tau, np.arange(0.01, 1.00, 0.01)):
        raise ContractViolation("Gate C1 CRPS grid must be exactly 0.01...0.99")
    quantiles = student_t_quantiles(loc, scale, df, tau)
    residual = np.asarray(truth, dtype=float).reshape(-1, 1) - quantiles
    pinball = np.maximum(tau.reshape(1, -1) * residual, (tau.reshape(1, -1) - 1.0) * residual)
    return 2.0 * np.trapezoid(pinball, tau, axis=1)


def student_t_nll(
    truth: np.ndarray,
    loc: np.ndarray,
    scale: np.ndarray,
    df: np.ndarray,
) -> np.ndarray:
    return -stats.t.logpdf(
        np.asarray(truth, dtype=float),
        df=np.asarray(df, dtype=float),
        loc=np.asarray(loc, dtype=float),
        scale=np.asarray(scale, dtype=float),
    )
