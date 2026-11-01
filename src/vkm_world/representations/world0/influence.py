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


def _square_finite(matrix: np.ndarray, name: str) -> np.ndarray:
    if np.iscomplexobj(matrix):
        raise ValueError(f"{name} must be real")
    result = np.asarray(matrix, dtype=float)
    if result.ndim != 2 or result.shape[0] != result.shape[1] or not result.shape[0]:
        raise ValueError(f"{name} must be a nonempty square matrix")
    if not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must contain only finite values")
    return result


def accepted_compliance(C: np.ndarray, symmetrize: bool = True, *, areas: np.ndarray | None = None,
                        reciprocity_tol: float = 1e-6, condition_limit: float = 1e12) -> tuple[np.ndarray, float]:
    """Accept a reciprocal positive-energy compliance for mean traction/convergence ports.

    Work is ``t.T @ diag(areas) @ dv``; hence ``A C = C.T A``, not ``C A = A C.T``.
    ``areas=None`` explicitly selects the historical **equal-area port contract** (unit common metric),
    not a rule for unknown or unequal areas. In 2D the metric is width times unit out-of-plane thickness.
    Symmetrization, when requested, is performed on ``sqrt(A) C inv(sqrt(A))`` and is refused above
    ``reciprocity_tol``. The returned asymmetry is measured in this energy normalization before correction.
    No historical input is overwritten. Numerical tolerances are acceptance choices, not site measurements.
    """
    C = _square_finite(C, "C")
    if not np.isfinite(reciprocity_tol) or reciprocity_tol < 0:
        raise ValueError("reciprocity_tol must be finite and nonnegative")
    if not np.isfinite(condition_limit) or condition_limit < 1:
        raise ValueError("condition_limit must be finite and at least one")
    a = np.ones(C.shape[0]) if areas is None else np.asarray(areas, dtype=float)
    if a.shape != (C.shape[0],) or not np.all(np.isfinite(a)) or np.any(a <= 0):
        raise ValueError("areas must have one finite strictly positive value per port")
    # Normalize the metric by a common factor; its physical units cancel in the similarity transform.
    w = np.sqrt(a / np.max(a))
    if np.any(w == 0):
        raise ValueError("port-area ratio is outside the floating-point range")
    energy = w[:, None] * C / w[None, :]
    scale = float(np.max(np.abs(energy)))
    if not np.isfinite(scale) or scale == 0:
        raise ValueError("C has zero or nonfinite energy scale")
    normalized = energy / scale
    asym = float(np.max(np.abs(normalized - normalized.T)))
    if asym > reciprocity_tol:
        raise ValueError(f"C violates port-work reciprocity: {asym:.6g} > {reciprocity_tol:.6g}")
    symmetric = 0.5 * (normalized + normalized.T)
    if np.min(np.linalg.eigvalsh(symmetric)) <= 0:
        raise ValueError("C must have strictly positive energy; nullspaces require an explicit gauge")
    accepted = symmetric if symmetrize else normalized
    condition = float(np.linalg.cond(accepted))
    if not np.isfinite(condition) or condition > condition_limit:
        raise ValueError(f"C is ill-conditioned in the port-work metric: {condition:.6g}")
    return accepted * scale * w[None, :] / w[:, None], asym


def stiffness_from_compliance(C: np.ndarray, symmetrize: bool = True, *, areas: np.ndarray | None = None,
                              reciprocity_tol: float = 1e-6,
                              condition_limit: float = 1e12) -> tuple[np.ndarray, float]:
    """K = accepted(C)⁻¹ and pre-correction energy asymmetry; see ``accepted_compliance``."""
    accepted, asym = accepted_compliance(C, symmetrize, areas=areas, reciprocity_tol=reciprocity_tol,
                                         condition_limit=condition_limit)
    return np.linalg.solve(accepted, np.eye(accepted.shape[0])), asym


def surface_operator(S: np.ndarray, C: np.ndarray, symmetrize: bool = True, *, areas: np.ndarray | None = None,
                     reciprocity_tol: float = 1e-6, condition_limit: float = 1e12) -> np.ndarray:
    """G = S·accepted(C)⁻¹ using exactly the stiffness acceptance policy, not the raw C."""
    accepted, _ = accepted_compliance(C, symmetrize, areas=areas, reciprocity_tol=reciprocity_tol,
                                     condition_limit=condition_limit)
    S = np.asarray(S, dtype=float)
    if S.ndim != 2 or S.shape[1] != accepted.shape[0] or not np.all(np.isfinite(S)):
        raise ValueError("S must be a finite (surface targets, ports) matrix")
    return np.linalg.solve(accepted.T, S.T).T


def lag_profile(K: np.ndarray, interior: int | None = None) -> np.ndarray:
    """Mean normalised off-diagonal K[i, i+l]/K[i, i] over interior rows, l = 0 … n−1."""
    K = np.asarray(K, float)
    n = K.shape[0]
    m = interior if interior is not None else max(1, n // 4)
    rows = range(m, n - m) if n - 2 * m > 0 else range(n)
    prof = np.zeros(n)
    cnt = np.zeros(n)
    for i in rows:
        if K[i, i] == 0:
            continue  # A normalized row is undefined; do not turn this into a finite lag statistic.
        for j in range(n):
            lag = abs(j - i)
            prof[lag] += K[i, j] / K[i, i]
            cnt[lag] += 1
    with np.errstate(invalid="ignore"):
        return np.where(cnt > 0, prof / np.maximum(cnt, 1), np.nan)


def sign_structure(K: np.ndarray, rel_tol: float = 1e-6) -> dict:
    """Classify a general real matrix, keeping M- and energy-positivity tests distinct.

    A real Z-matrix is a nonsingular M-matrix iff all its eigenvalues have positive real parts.
    Positive definiteness of sym(K) is neither used nor necessary. Near singular/poorly resolved cases
    return ``m_matrix=None`` (INDETERMINATE), never a false mathematical disproof. Cooperative dynamics
    additionally require the monotone local law stated in RC-T1; the structural flag alone proves no physics.
    """
    K = _square_finite(K, "K")
    if not np.isfinite(rel_tol) or not 0 < rel_tol < 1:
        raise ValueError("rel_tol must be finite and between zero and one")
    n = K.shape[0]
    d = np.diag(K)
    off = K - np.diag(d)
    scale = float(np.max(np.abs(K)))
    diag_scale = float(np.max(np.abs(d)))
    tol = rel_tol * scale
    pos_off = off > tol
    z = bool(not np.any(pos_off))
    z_exact = bool(not np.any(off > 0))
    sym = 0.5 * (K + K.T)
    eig_min = float(np.min(np.linalg.eigvalsh(sym)))
    real_min = float(np.min(np.linalg.eigvals(K).real))
    condition = float(np.linalg.cond(K))
    try:
        inverse = np.linalg.solve(K, np.eye(n))
        inv_nonneg = bool(np.all(inverse >= -rel_tol * np.max(np.abs(inverse))))
    except np.linalg.LinAlgError:
        inv_nonneg = None
    if not z or real_min < -tol:
        m_matrix, status = False, "FAIL"
    elif not z_exact or real_min <= tol or not np.isfinite(condition) or condition > 1 / rel_tol:
        m_matrix, status = None, "INDETERMINATE"
    else:
        m_matrix, status = True, "PASS"
    prof = lag_profile(K)
    first_pos = next((l for l in range(1, n) if np.isfinite(prof[l]) and prof[l] > rel_tol), None)
    return {
        "n": n, "diag_positive": bool(np.all(d > 0)), "z_matrix": z, "z_matrix_exact": z_exact,
        "positive_offdiag_fraction": float(pos_off.sum() / max(1, n * (n - 1))),
        "max_positive_offdiag_rel": float(np.max(np.where(pos_off, off, 0.0)) / max(diag_scale, scale * rel_tol)) if np.any(pos_off) else 0.0,
        "min_eig_sym": eig_min, "inverse_nonnegative": inv_nonneg,
        "min_eig_real": real_min, "condition_number": condition,
        "m_matrix": m_matrix, "m_matrix_status": status,
        # Exact sign is required for the theorem. A tolerance-only Z classification
        # is numerical uncertainty, not permission to delete a positive coupling.
        "cooperative_world0": True if z_exact else (None if z else False),
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
