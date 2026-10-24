"""Executable reduced kernels of World-0 (DAE form of MS §26: 0 = g(q, z, u), ż = f(q, z, u)).

Each kernel is a small, explicit model of one kernel type of ``catalogue``: quasi-static generalised coordinates q
are solved from the port balance, memory z is integrated in time. Inputs are port variables (traction or
displacement), outputs are port responses. Time integration is implicit Euler (unconditionally stable for the
monotone creep laws used here) with a scalar Newton/bisection solve; all quantities SI, compression +.

These kernels are representation units, not claims about nature (FW §7). Parameter values are inputs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from . import laws


def _bisect(fun, lo: float, hi: float, tol: float = 1e-13, it: int = 200) -> float:
    flo = fun(lo)
    fhi = fun(hi)
    if flo == 0:
        return lo
    if fhi == 0:
        return hi
    if flo * fhi > 0:
        raise ValueError("root not bracketed")
    for _ in range(it):
        mid = 0.5 * (lo + hi)
        fm = fun(mid)
        if fm == 0 or (hi - lo) <= tol * max(1.0, abs(mid)):
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return 0.5 * (lo + hi)


# ------------------------------------------------------------------------------------------------ pillar
@dataclass
class PillarKernel:
    """MK-PILLAR-IC / MK-PILLAR-BARRIER, families ELASTIC + CREEP_STEADY.

    q = v_p (shortening), z = eps_cr (mean axial creep strain). Reduced law:
        v_p = h·(σ_p/E_m + ε_cr),   ε̇_cr = c_shape·(p0/η)(σ_p/p0)^n,
    E_m = E' (plane strain) or E (uniaxial); c_shape = 1 for a homogeneous uniaxial specimen,
    (√3/2)^(n+1) for a free plane-strain pillar (W0-L-02a), otherwise identified from a fine FE model.
    """

    h: float
    E: float
    nu: float
    eta: float
    n: float
    c_shape: float = 1.0
    plane_strain: bool = True
    p0: float = 1.0e6

    @property
    def E_m(self) -> float:
        return laws.plane_strain_modulus(self.E, self.nu) if self.plane_strain else self.E

    def creep_rate(self, sigma_p: float) -> float:
        return self.c_shape * float(laws.norton_rate(sigma_p, self.eta, self.n, self.p0))

    def shortening(self, sigma_p: float, eps_cr: float) -> float:
        return self.h * (sigma_p / self.E_m + eps_cr)

    def respond_traction(self, times, sigma_fn, eps_cr0: float = 0.0, substeps: int = 20):
        """Traction-controlled port: σ_p(t) given. Returns (v_p(t), ε_cr(t))."""
        t = np.asarray(times, float)
        eps = np.empty_like(t)
        eps[0] = eps_cr0
        for i in range(1, len(t)):
            e = eps[i - 1]
            dt = (t[i] - t[i - 1]) / substeps
            for k in range(substeps):
                s = sigma_fn(t[i - 1] + (k + 1) * dt)
                e = e + dt * self.creep_rate(s)       # traction known → explicit = exact per substep (σ fixed)
            eps[i] = e
        v = np.array([self.shortening(sigma_fn(ti), ei) for ti, ei in zip(t, eps)])
        return v, eps

    def respond_spring(self, times, sigma_star: float, K_sys: float, eps_cr0: float = 0.0, substeps: int = 20):
        """Pillar loaded through a linear surrounding: σ_p = σ* − K_sys·v_p (pair pillar–overburden, K in Pa/m).

        Implicit Euler on ε_cr; returns (v_p, σ_p, ε_cr) on ``times``.
        """
        t = np.asarray(times, float)
        a = 1.0 + K_sys * self.h / self.E_m

        def sig(e):
            return (sigma_star - K_sys * self.h * e) / a

        eps = np.empty_like(t)
        eps[0] = eps_cr0
        for i in range(1, len(t)):
            e = eps[i - 1]
            dt = (t[i] - t[i - 1]) / substeps
            for _ in range(substeps):
                e_old = e
                hi = e_old + dt * self.creep_rate(max(sig(e_old), 0.0)) + 1e-300
                e = _bisect(lambda x: x - e_old - dt * self.creep_rate(max(sig(x), 0.0)), e_old, hi)
            eps[i] = e
        s = np.array([sig(e) for e in eps])
        v = self.h * (s / self.E_m + eps)
        return v, s, eps

    def dissipation_rate(self, sigma_p: float) -> float:
        """Creep dissipation per unit volume σ·ε̇_cr ≥ 0 (AG §22 check)."""
        return sigma_p * self.creep_rate(sigma_p)


# ---------------------------------------------------------------------------------------------- backfill
@dataclass
class BackfillKernel:
    """MK-BACKFILL, family BACKFILL_COMPACTION: unilateral contact + hyperbolic compaction with unloading memory."""

    h_fill: float
    gap0: float
    E0: float
    eps_m: float
    E_u: float | None = None

    def stress(self, v: float, eps_max: float = 0.0) -> tuple[float, float]:
        """Fill stress for roof convergence v (gap closes first). Returns (σ_b, new ε_max)."""
        eps = max(0.0, v - self.gap0) / self.h_fill
        if eps >= eps_max or self.E_u is None:
            return float(laws.backfill_stress(eps, self.E0, self.eps_m)), max(eps, eps_max)
        s_max = float(laws.backfill_stress(eps_max, self.E0, self.eps_m))
        return max(0.0, s_max - self.E_u * (eps_max - eps)), eps_max

    def opening(self, v: float) -> float:
        return max(0.0, self.gap0 - v)


# ------------------------------------------------------------------------------------------- mined cell
@dataclass
class MinedCellKernel:
    """MK-MINED-CELL: pillar (fraction 1−k) and optional backfill (fraction k) sharing one cell convergence v.

    Port balance (traction-controlled cell): (1−k)·σ_p(v, ε_cr) + k·σ_b(v) = σ_cell,
    σ_p = E_m (v/h − ε_cr). With σ_cell = σ* − K_sys·v the same kernel is the pair cell–overburden.
    """

    pillar: PillarKernel
    k: float
    backfill: BackfillKernel | None = None

    def balance(self, v: float, eps_cr: float, eps_max: float) -> tuple[float, float, float]:
        sp = self.pillar.E_m * (v / self.pillar.h - eps_cr)
        sb = 0.0
        if self.backfill is not None:
            sb, _ = self.backfill.stress(v, eps_max)
        return (1.0 - self.k) * sp + self.k * sb, sp, sb

    def solve_v(self, sigma_cell_fn, eps_cr: float, eps_max: float) -> float:
        """Convergence v from the cell balance; sigma_cell_fn(v) may depend on v (spring surroundings)."""
        lo = self.pillar.h * eps_cr - 1e-9
        hi = self.pillar.h * 0.999
        if self.backfill is not None:
            hi = min(hi, self.backfill.gap0 + self.backfill.h_fill * self.backfill.eps_m * 0.999999)
        return _bisect(lambda v: self.balance(v, eps_cr, eps_max)[0] - sigma_cell_fn(v), lo, hi)

    def respond(self, times, sigma_star: float, K_sys: float = 0.0, substeps: int = 20):
        """σ_cell = σ* − K_sys·v. Returns dict of arrays v, sigma_p, sigma_b, eps_cr (implicit Euler)."""
        t = np.asarray(times, float)
        out = {k: np.empty_like(t) for k in ("v", "sigma_p", "sigma_b", "eps_cr")}
        e, em = 0.0, 0.0

        def scell(v):
            return sigma_star - K_sys * v

        def state(e_, em_):
            v = self.solve_v(scell, e_, em_)
            _, sp, sb = self.balance(v, e_, em_)
            return v, sp, sb

        v, sp, sb = state(e, em)
        if self.backfill is not None:
            em = max(em, max(0.0, v - self.backfill.gap0) / self.backfill.h_fill)
        for key, val in zip(("v", "sigma_p", "sigma_b", "eps_cr"), (v, sp, sb, e)):
            out[key][0] = val
        for i in range(1, len(t)):
            dt = (t[i] - t[i - 1]) / substeps
            for _ in range(substeps):
                e_old = e

                def resid(x):
                    _, spx, _ = state(x, em)
                    return x - e_old - dt * self.pillar.creep_rate(max(spx, 0.0))

                inc = dt * self.pillar.creep_rate(max(sp, 0.0)) * (1.0 + 1e-6) + 1e-30
                hi = e_old + inc
                while resid(hi) < 0.0:          # the inner solve is iterative: widen the bracket if needed
                    inc *= 2.0
                    hi = e_old + inc
                e = _bisect(resid, e_old, hi)
                v, sp, sb = state(e, em)
                if self.backfill is not None:
                    em = max(em, max(0.0, v - self.backfill.gap0) / self.backfill.h_fill)
            for key, val in zip(("v", "sigma_p", "sigma_b", "eps_cr"), (v, sp, sb, e)):
                out[key][i] = val
        return out


# ------------------------------------------------------------------------------------------- salt layer
@dataclass
class ConfinedSaltLayerKernel:
    """MK-LAYER-* under laterally uniform loading: oedometric elasticity + Norton relaxation of the deviator.

    Memory z = q_dev (σ_v − σ_h). Under ε_h = 0 and constant σ_v the deviator relaxes (W0-L-12) and the
    vertical strain grows by 2(1−2ν)(q0 − q)/E only: isochoric creep is LOCKED in a confined layer.
    """

    h: float
    E: float
    nu: float
    eta: float
    n: float
    p0: float = 1.0e6

    def deviator(self, t, q0: float):
        return laws.oedometric_norton_q(t, q0, self.E, self.nu, self.eta, self.n, self.p0)

    def extra_shortening(self, t, q0: float):
        return self.h * laws.oedometric_norton_vertical_strain(t, q0, self.E, self.nu, self.eta, self.n, self.p0)

    def deviator_numeric(self, times, q0: float, substeps: int = 50):
        """Implicit-Euler integration of q̇ = −E/(2(1−ν))·(p0/η)(q/p0)^n (check of the closed form)."""
        c = self.E / (2.0 * (1.0 - self.nu))
        t = np.asarray(times, float)
        q = np.empty_like(t)
        q[0] = q0
        for i in range(1, len(t)):
            x = q[i - 1]
            dt = (t[i] - t[i - 1]) / substeps
            for _ in range(substeps):
                x_old = x
                x = _bisect(lambda y: y - x_old + dt * c * float(laws.norton_rate(y, self.eta, self.n, self.p0)),
                            0.0, x_old)
            q[i] = x
        return q


# ---------------------------------------------------------------------------------------------- aquifer
@dataclass
class AquiferKernel:
    """MK-LAYER-TKT, family HYDRO: Terzaghi 1D consolidation in modal form.

    Layer drained at the top (ζ = 0), impermeable at ζ = 1, drainage length H_d. Modes sin(M_m ζ), M_m = π(2m+1)/2;
    the modal amplitudes are the memory state p_k. ``n_modes`` is a REPRESENTATION_CHOICE (W0-L-04a analogue).
    """

    H_d: float
    c_v: float
    n_modes: int = 1

    @property
    def M(self) -> np.ndarray:
        return math.pi * (2 * np.arange(self.n_modes) + 1) / 2.0

    def project(self, zeta: np.ndarray, p: np.ndarray) -> np.ndarray:
        """Fine profile p(ζ) → modal amplitudes (orthogonal projection on the first n_modes modes)."""
        out = np.empty(self.n_modes)
        for i, Mi in enumerate(self.M):
            out[i] = 2.0 * np.trapezoid(p * np.sin(Mi * zeta), zeta)
        return out

    def evolve(self, a0: np.ndarray, t) -> np.ndarray:
        """Amplitudes at times t (exact for the linear modal ODE ȧ_m = −M_m²c_v/H_d² a_m)."""
        t = np.atleast_1d(np.asarray(t, float))
        rate = self.M ** 2 * self.c_v / self.H_d ** 2
        return np.asarray(a0)[None, :] * np.exp(-np.outer(t, rate))

    def mean_pressure(self, a: np.ndarray) -> np.ndarray:
        """Layer-mean pressure Σ a_m/M_m (∫₀¹ sin(Mζ)dζ = 1/M)."""
        return (np.atleast_2d(a) / self.M[None, :]).sum(axis=1)

    def settlement(self, p_mean_drop, m_v: float) -> np.ndarray:
        """1D consolidation settlement s = m_v·H_d·Δp̄ for a drop Δp̄ of the layer-mean pressure (effective-stress
        gain at constant total stress), m_v = 1/M_oed; the layer thickness equals H_d (one-sided drainage)."""
        return self.H_d * m_v * np.asarray(p_mean_drop, float)


# -------------------------------------------------------------------------------------------- VZT barrier
@dataclass
class VZTKernel:
    """MK-VZT, family HYDRO (+ DAMAGE): hydraulic barrier aggregating sublayers in series.

    d_i ∈ [0, 1] mixes intact and fractured conductivity log-linearly, K_i = K_int^(1−d)·K_frac^d
    (MODEL_CHOICE for the coupling damage → permeability, FW §10 example); the effective conductivity is the
    thickness-weighted harmonic mean; leakage by Darcy.
    """

    h: tuple[float, ...]
    K_intact: tuple[float, ...]
    K_frac: tuple[float, ...]
    d: list[float] = field(default_factory=list)

    def __post_init__(self):
        if not self.d:
            self.d = [0.0] * len(self.h)
        if not (len(self.h) == len(self.K_intact) == len(self.K_frac) == len(self.d)):
            raise ValueError("sublayer arrays must have the same length")

    def K_layers(self) -> np.ndarray:
        Ki = np.asarray(self.K_intact, float)
        Kf = np.asarray(self.K_frac, float)
        d = np.clip(np.asarray(self.d, float), 0.0, 1.0)
        return np.exp((1.0 - d) * np.log(Ki) + d * np.log(Kf))

    def K_eff(self) -> float:
        return laws.series_conductivity(self.h, self.K_layers())

    def leakage(self, head_drop: float) -> float:
        return laws.darcy_flux(self.K_eff(), head_drop, float(sum(self.h)))

    def controlling_layer(self) -> int:
        """Index of the sublayer with the largest hydraulic resistance h_i/K_i (the one that keeps the barrier)."""
        return int(np.argmax(np.asarray(self.h) / self.K_layers()))


# --------------------------------------------------------------------------------------------- surface
@dataclass
class SurfaceKernel:
    """MK-SURFACE: traction-free boundary; u_s = G·v (G from the influence identification, response kernel)."""

    x: np.ndarray
    G: np.ndarray

    def displacement(self, v: np.ndarray) -> np.ndarray:
        return self.G @ np.asarray(v, float)
