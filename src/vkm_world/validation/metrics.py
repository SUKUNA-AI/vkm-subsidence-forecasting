"""Point, interval and probabilistic scores (pure numpy, no fitting).

Conventions: error = prediction − truth (positive bias = over-prediction); all inputs are 1-D and
finite — censored or missing targets are removed explicitly by the caller, never silently here.
Interval score and WIS follow Gneiting & Raftery (2007) and Bracher et al. (2021); the normal CRPS
is the closed form of Gneiting et al. (2005). Lower is better for every score.
"""
from __future__ import annotations

import math
from typing import Iterable, Mapping

import numpy as np

_erf = np.vectorize(math.erf, otypes=[float])


def _arr(values: Iterable[float], name: str) -> np.ndarray:
    a = np.asarray(list(values) if not isinstance(values, np.ndarray) else values, dtype=float)
    if a.ndim != 1 or a.size == 0:
        raise ValueError(f"{name} must be a non-empty 1-D array")
    if not np.isfinite(a).all():
        raise ValueError(f"{name} contains non-finite values (drop censored/missing targets explicitly)")
    return a


def _same(*arrays: np.ndarray) -> None:
    if len({a.shape for a in arrays}) != 1:
        raise ValueError("arrays must have the same shape")


# ---------------------------------------------------------------- point
def point_metrics(y_true: Iterable[float], y_pred: Iterable[float],
                  sample_weight: Iterable[float] | None = None) -> dict[str, float | int]:
    """n, MAE, RMSE and bias (optionally weighted; weights are normalised)."""
    t, p = _arr(y_true, "y_true"), _arr(y_pred, "y_pred")
    _same(t, p)
    w = np.ones_like(t) if sample_weight is None else _arr(sample_weight, "sample_weight")
    _same(t, w)
    if (w < 0).any() or w.sum() <= 0:
        raise ValueError("sample_weight must be non-negative with positive mass")
    w = w / w.sum()
    e = p - t
    return {"n": int(t.size), "mae": float(np.sum(w * np.abs(e))),
            "rmse": float(np.sqrt(np.sum(w * e**2))), "bias": float(np.sum(w * e))}


# ---------------------------------------------------------------- interval
def interval_coverage(y_true: Iterable[float], lower: Iterable[float], upper: Iterable[float]) -> float:
    t, lo, hi = _arr(y_true, "y_true"), _arr(lower, "lower"), _arr(upper, "upper")
    _same(t, lo, hi)
    return float(np.mean((t >= lo) & (t <= hi)))


def interval_score(y_true: Iterable[float], lower: Iterable[float], upper: Iterable[float], *,
                   alpha: float) -> np.ndarray:
    """Interval score of the central (1 − alpha) interval: width + (2/alpha)·distance outside."""
    if not 0 < alpha < 1:
        raise ValueError("alpha must lie in (0, 1)")
    t, lo, hi = _arr(y_true, "y_true"), _arr(lower, "lower"), _arr(upper, "upper")
    _same(t, lo, hi)
    if (lo > hi).any():
        raise ValueError("lower bound exceeds upper bound")
    return (hi - lo) + (2.0 / alpha) * np.maximum(lo - t, 0.0) + (2.0 / alpha) * np.maximum(t - hi, 0.0)


def weighted_interval_score(y_true: Iterable[float], median: Iterable[float],
                            intervals: Mapping[float, tuple[Iterable[float], Iterable[float]]]) -> np.ndarray:
    """WIS = (½|y − m| + Σ_k (α_k/2)·IS_{α_k}) / (K + ½); keys of ``intervals`` are central coverages."""
    if not intervals:
        raise ValueError("at least one interval is required")
    t, m = _arr(y_true, "y_true"), _arr(median, "median")
    _same(t, m)
    total = 0.5 * np.abs(t - m)
    for coverage, (lo, hi) in sorted(intervals.items()):
        alpha = 1.0 - float(coverage)
        total = total + (alpha / 2.0) * interval_score(t, lo, hi, alpha=alpha)
    return total / (len(intervals) + 0.5)


# ---------------------------------------------------------------- normal predictive distribution
def _normal(y_true, mean, std) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    t, mu, sd = _arr(y_true, "y_true"), _arr(mean, "mean"), _arr(std, "std")
    _same(t, mu, sd)
    if (sd <= 0).any():
        raise ValueError("predictive standard deviation must be positive")
    return t, mu, sd


def normal_crps(y_true: Iterable[float], mean: Iterable[float], std: Iterable[float]) -> np.ndarray:
    """CRPS of N(mean, std²): σ·[z(2Φ(z) − 1) + 2φ(z) − 1/√π], z = (y − μ)/σ."""
    t, mu, sd = _normal(y_true, mean, std)
    z = (t - mu) / sd
    pdf = np.exp(-0.5 * z**2) / math.sqrt(2.0 * math.pi)
    cdf = 0.5 * (1.0 + _erf(z / math.sqrt(2.0)))
    return sd * (z * (2.0 * cdf - 1.0) + 2.0 * pdf - 1.0 / math.sqrt(math.pi))


def normal_nll(y_true: Iterable[float], mean: Iterable[float], std: Iterable[float]) -> np.ndarray:
    """Negative log density of N(mean, std²) at y."""
    t, mu, sd = _normal(y_true, mean, std)
    return 0.5 * np.log(2.0 * math.pi * sd**2) + 0.5 * ((t - mu) / sd) ** 2


# ---------------------------------------------------------------- conformal
def conformal_quantile(scores: Iterable[float], coverage: float) -> float:
    """Finite-sample split-conformal quantile: the ⌈(n + 1)·coverage⌉-th smallest score.

    Returns +inf when that rank exceeds n (too few calibration scores for the requested coverage);
    the result is never clipped to the sample maximum.
    """
    if not 0 < coverage < 1:
        raise ValueError("coverage must lie in (0, 1)")
    s = np.sort(_arr(scores, "scores"))
    rank = math.ceil((s.size + 1) * coverage - 1e-12)  # guard against float noise at exact integers
    return float("inf") if rank > s.size else float(s[rank - 1])
