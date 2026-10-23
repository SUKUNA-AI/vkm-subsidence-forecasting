"""Response kernels of World-0 from high-fidelity runs (FW §20, §27; AG §49; G5).

For a slot (mined layer) whose faces carry kernel tractions σ_i, linear elasticity of the surrounding rock gives
    v = C·(σ⁰ − σ)          (C — compliance: convergence of cell i per unit support deficit on cell j),
    s = S·(σ⁰ − σ)          (S — surface displacement per unit deficit),
hence the World-0 relations σ = σ⁰ − K·v with K = C⁻¹ and s = G·v with G = S·C⁻¹ (exact under the stated
assumptions: linear surroundings, piecewise-constant tractions per cell). The SIGN STRUCTURE of K is a result of
the computation, not a postulate (G5): ``sign_structure`` reports whether K is a Z-/M-matrix (cooperative class of
RC-T1) and how off-diagonal terms change sign with distance.
"""
from __future__ import annotations

import math

import numpy as np


def stiffness_from_compliance(C: np.ndarray, symmetrize: bool = True) -> tuple[np.ndarray, float]:
    """K = C⁻¹; returns (K, relative asymmetry of C) — Betti reciprocity demands C·diag(a) symmetric."""
    C = np.asarray(C, float)
    asym = float(np.max(np.abs(C - C.T)) / np.max(np.abs(C)))
    Cs = 0.5 * (C + C.T) if symmetrize else C
    return np.linalg.inv(Cs), asym


def surface_operator(S: np.ndarray, C: np.ndarray) -> np.ndarray:
    """G = S·C⁻¹: surface displacement per unit cell convergence (columns = cells)."""
    return np.asarray(S, float) @ np.linalg.inv(np.asarray(C, float))


def lag_profile(K: np.ndarray, interior: int | None = None) -> np.ndarray:
    """Mean normalised off-diagonal K[i, i+l]/K[i, i] over interior rows, l = 0 … n−1."""
    K = np.asarray(K, float)
    n = K.shape[0]
    m = interior if interior is not None else max(1, n // 4)
    rows = range(m, n - m) if n - 2 * m > 0 else range(n)
    prof = np.zeros(n)
    cnt = np.zeros(n)
    for i in rows:
        for j in range(n):
            lag = abs(j - i)
            prof[lag] += K[i, j] / K[i, i]
            cnt[lag] += 1
    with np.errstate(invalid="ignore"):
        return np.where(cnt > 0, prof / np.maximum(cnt, 1), np.nan)


def sign_structure(K: np.ndarray, rel_tol: float = 1e-6) -> dict:
    """Classify the influence matrix: Z-matrix, M-matrix, cooperative class, sign changes by distance."""
    K = np.asarray(K, float)
    n = K.shape[0]
    d = np.diag(K)
    off = K - np.diag(d)
    scale = np.max(np.abs(d))
    pos_off = off > rel_tol * scale
    z = bool(not np.any(pos_off))
    sym = 0.5 * (K + K.T)
    eig_min = float(np.min(np.linalg.eigvalsh(sym)))
    try:
        inv_nonneg = bool(np.all(np.linalg.inv(K) >= -rel_tol * np.max(np.abs(np.linalg.inv(K)))))
    except np.linalg.LinAlgError:
        inv_nonneg = False
    prof = lag_profile(K)
    first_pos = next((l for l in range(1, n) if np.isfinite(prof[l]) and prof[l] > rel_tol), None)
    return {
        "n": n, "diag_positive": bool(np.all(d > 0)), "z_matrix": z,
        "positive_offdiag_fraction": float(pos_off.sum() / max(1, n * (n - 1))),
        "max_positive_offdiag_rel": float(np.max(np.where(pos_off, off, 0.0)) / scale) if np.any(pos_off) else 0.0,
        "min_eig_sym": eig_min, "inverse_nonnegative": inv_nonneg,
        "m_matrix": bool(z and eig_min > 0),
        "cooperative_world0": z,   # ∂v̇_i/∂v_j = −φ'·K_ij ≥ 0 for i ≠ j  ⇔  K_ij ≤ 0 (Kamke condition, RC-T1)
        "first_positive_lag": first_pos, "lag_profile": prof,
    }


def knothe_fit(x: np.ndarray, s: np.ndarray, depth: float | None = None) -> dict:
    """Least-squares fit of s(x) ≈ A·exp(−π(x−x0)²/r²)/r (Knothe comparator) + moment-based radius.

    Returns A (area), x0, r, tanβ = depth/r (if depth given), rms residual relative to max|s|, and the radius from
    the second moment r_m = √(2π)·σ_x (identical for an exact Gaussian).
    """
    x = np.asarray(x, float)
    s = np.asarray(s, float)
    w = np.clip(s, 0.0, None)
    area = float(np.trapezoid(w, x))
    x0 = float(np.trapezoid(w * x, x) / area)
    var = float(np.trapezoid(w * (x - x0) ** 2, x) / area)
    r_m = math.sqrt(2.0 * math.pi * var)
    best = None
    for r in np.geomspace(r_m / 4, r_m * 4, 400):
        g = np.exp(-math.pi * (x - x0) ** 2 / r ** 2) / r
        A = float(g @ s / (g @ g))
        res = float(np.sqrt(np.mean((s - A * g) ** 2)))
        if best is None or res < best[0]:
            best = (res, r, A)
    res, r, A = best
    out = {"A": A, "x0": x0, "r": r, "r_moment": r_m, "rel_rms": res / float(np.max(np.abs(s))), "area": area}
    if depth is not None:
        out["tan_beta"] = depth / r
        out["tan_beta_moment"] = depth / r_m
    return out


def width_at_fraction(x: np.ndarray, s: np.ndarray, frac: float) -> float:
    """Width of the region where s ≥ frac·max(s) (linear interpolation at both ends)."""
    x = np.asarray(x, float)
    s = np.asarray(s, float)
    thr = frac * np.max(s)
    idx = np.where(s >= thr)[0]
    if len(idx) == 0:
        return 0.0
    i0, i1 = idx[0], idx[-1]
    xl = x[i0] if i0 == 0 else np.interp(thr, [s[i0 - 1], s[i0]], [x[i0 - 1], x[i0]])
    xr = x[i1] if i1 == len(x) - 1 else np.interp(thr, [s[i1 + 1], s[i1]], [x[i1 + 1], x[i1]])
    return float(xr - xl)
