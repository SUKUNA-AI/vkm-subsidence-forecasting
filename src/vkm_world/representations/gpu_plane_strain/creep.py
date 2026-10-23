"""Norton (power-law) steady-state creep with Prandtl–Reuss flow: implicit (backward Euler) radial return and
the consistent tangent, vectorised over Gauss points.

Law (form of VKM-SRC-045, Ermashov 2015, eq. 2.1.5–2.1.6, transferred in TR-CREEP-ERMASHOV2015-SKRU1-SLICE-V6_2):

    ε̇_eq = A · (q / p0)^n,     A = 1/ηs  [1/yr],  p0 = 1 MPa,   ε̇_c = (3/2) ε̇_eq · s / q,

q = von Mises equivalent stress, s = deviator. This is MAPDL TB,CREEP TBOPT=10 and OGS CreepBGRa with Q = 0.

Backward Euler at a Gauss point: q = q_tr − 3G·Δε, Δε = Δt·A·(q/p0)^n. With x = 3GΔε/q_tr ∈ [0, 1) this is
x = c·(1 − x)^n, c = 3G·Δt·A·(q_tr/p0)^(n−1)/p0, solved by Newton from x0 = max(0, 1 − c^(−1/n)) — the function is
increasing and concave, so the iterates increase monotonically to the root (no overshoot, no bracketing needed).
"""
from __future__ import annotations

import math

import torch

from .fem import MANDEL_M

SQ32 = math.sqrt(1.5)


def deviator(sig: torch.Tensor):
    m = torch.tensor(MANDEL_M, dtype=sig.dtype, device=sig.device)
    p = (sig[..., 0] + sig[..., 1] + sig[..., 2]) / 3.0
    s = sig - p[..., None] * m
    return p, s


def von_mises(sig: torch.Tensor) -> torch.Tensor:
    _, s = deviator(sig)
    return SQ32 * torch.linalg.vector_norm(s, dim=-1)


def norton_return(sig_tr: torch.Tensor, K: torch.Tensor, G: torch.Tensor, A: torch.Tensor, n: float, dt: float,
                  p0: float = 1.0e6, iters: int = 30, need_tangent: bool = True):
    """Implicit Norton step at every Gauss point.

    sig_tr: trial stress (..., 4) Mandel [Pa]; K, G, A broadcastable to sig_tr[..., 0]; A = 0 -> elastic.
    Returns (sig, d_eps_c (..., 4) Mandel, d_eps_eq (...), D_t (..., 4, 4) or None).
    """
    p, s_tr = deviator(sig_tr)
    snorm = torch.linalg.vector_norm(s_tr, dim=-1)
    q_tr = SQ32 * snorm
    c = 3.0 * G * dt * A * (q_tr / p0) ** (n - 1.0) / p0
    pos = c > 0
    cs = torch.where(pos, c, torch.ones_like(c))
    x = torch.where(pos, torch.clamp(1.0 - cs ** (-1.0 / n), min=0.0), torch.zeros_like(c))
    for _ in range(iters):
        om = 1.0 - x
        f = x - c * om ** n
        fp = 1.0 + c * n * om ** (n - 1.0)
        x = torch.maximum(x, x - f / fp)  # monotone from the left
        x = torch.clamp(x, max=1.0 - 1e-15)
    beta = 1.0 - x
    d_eq = x * q_tr / (3.0 * G)
    nhat = torch.where(snorm[..., None] > 0, s_tr / torch.clamp(snorm, min=1e-300)[..., None], torch.zeros_like(s_tr))
    m = torch.tensor(MANDEL_M, dtype=sig_tr.dtype, device=sig_tr.device)
    sig = p[..., None] * m + beta[..., None] * s_tr
    d_eps_c = (SQ32 * d_eq)[..., None] * nhat
    if not need_tangent:
        return sig, d_eps_c, d_eq, None
    q = beta * q_tr
    gp = dt * A * n * (q / p0) ** (n - 1.0) / p0  # dΔε/dq
    h = gp / (1.0 + 3.0 * G * gp)                  # dΔε/dq_tr
    I = torch.eye(4, dtype=sig_tr.dtype, device=sig_tr.device)
    mm = torch.outer(m, m)
    Pdev = I - mm / 3.0
    coef = 2.0 * G * (1.0 - beta - 3.0 * G * h)
    Dt = (K[..., None, None] * mm + (2.0 * G * beta)[..., None, None] * Pdev
          + coef[..., None, None] * nhat[..., :, None] * nhat[..., None, :])
    return sig, d_eps_c, d_eq, Dt
