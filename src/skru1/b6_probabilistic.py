"""Gate B6-only probabilistic scoring helpers.

This module is intentionally separate from the hash-frozen Gate B5 metric
interface.  Extending B6 scoring must not mutate B5 benchmark provenance.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np


def quantile_crps_approximation(
    y_true: Iterable[float],
    levels: Iterable[float],
    quantiles: np.ndarray,
) -> np.ndarray:
    """Approximate CRPS by integrating pinball loss over quantile levels.

    The native quantile grid is not evenly spaced, so an arithmetic mean would
    over-weight its densely sampled tails.  Nearest-level quadrature assigns
    each supplied quantile the width between adjacent level midpoints, with
    the edge cells extended to zero and one.
    """

    truth = np.asarray(list(y_true), dtype=float)
    taus = np.asarray(list(levels), dtype=float)
    values = np.asarray(quantiles, dtype=float)
    if truth.ndim != 1 or taus.ndim != 1 or values.shape != (len(truth), len(taus)):
        raise ValueError("Quantile CRPS arrays have incompatible shapes")
    if len(taus) < 2 or not np.all(np.diff(taus) > 0.0) or taus[0] <= 0.0 or taus[-1] >= 1.0:
        raise ValueError("Quantile levels must be strictly increasing inside (0, 1)")
    boundaries = np.concatenate(([0.0], (taus[:-1] + taus[1:]) / 2.0, [1.0]))
    weights = np.diff(boundaries)
    error = truth[:, None] - values
    pinball = np.maximum(taus[None, :] * error, (taus[None, :] - 1.0) * error)
    return 2.0 * np.sum(pinball * weights[None, :], axis=1)
