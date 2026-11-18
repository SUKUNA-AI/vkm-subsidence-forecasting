"""Exact coefficient history for a fixed small-strain Maxwell material group.

A group has one shear modulus and one deviatoric fluidity. Its memory is the
deviatoric strain of a coefficient vector in the same displacement space.
This representation follows by linearity at the group's material points,
including when the group occupies only part of an element. Changing basis or
group parameters requires a new replay or a qualified state transfer.
"""
from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_matrix


@dataclass(frozen=True)
class MaxwellGroup:
    """Rock deviatoric stiffness, including support; rate is intrinsic G/eta."""

    rate_per_s: float
    stiffness: csr_matrix

    def __post_init__(self):
        if not np.isfinite(self.rate_per_s) or self.rate_per_s <= 0:
            raise ValueError("positive finite Maxwell rate required")
        if (not isinstance(self.stiffness, csr_matrix)
                or self.stiffness.shape[0] != self.stiffness.shape[1]
                or not self.stiffness.has_canonical_format
                or not np.isfinite(self.stiffness.data).all()):
            raise ValueError("finite canonical square CSR required")


def ratios(groups, dt_s):
    """Backward-Euler ratios for deviatoric Maxwell in unchanged groups."""
    if not np.isfinite(dt_s) or dt_s < 0:
        raise ValueError("finite nonnegative Maxwell time increment required")
    r = np.array([g.rate_per_s for g in groups]) * dt_s
    if not np.isfinite(r).all():
        raise ValueError("nonfinite Maxwell increment")
    return r


def tangent(elastic, groups, r):
    """Assemble a tangent at fixed support and time step."""
    if len(groups) != len(r):
        raise ValueError("Maxwell group identity or ordering mismatch")
    change = csr_matrix(elastic.shape, dtype=elastic.dtype)
    for group, ratio in zip(groups, r, strict=True):
        if ratio:
            change += group.stiffness * (ratio / (1. + ratio))
    return elastic - change


def memory_load(groups, q, r):
    """The load due to old memory, with the sign for the equilibrium RHS."""
    result = np.zeros(q.shape[1])
    for group, old, ratio in zip(groups, q, r, strict=True):
        result += (group.stiffness @ old) / (1. + ratio)
    return result


def update(q, displacement, r):
    """Return trial coefficients without mutating the accepted state."""
    if q.shape != (len(r), displacement.size):
        raise ValueError("Maxwell state does not cover the exact groups and basis")
    if not np.isfinite(displacement).all() or not np.isfinite(q).all():
        raise ValueError("finite Maxwell state and displacement required")
    delta = (r / (1. + r))[:, None] * (displacement[None, :] - q)
    trial = q + delta
    # An instantaneous event must preserve even the bit patterns of old memory.
    trial[r == 0.] = q[r == 0.]
    return trial


def work_terms(elastic, groups, r, u0, u1, q0, q1):
    """Integrate elastic change, viscous dissipation and endpoint excess.

    The input geometry/support and all operators are fixed over this material
    step. This ledger is separate from the work of changing event support.
    """
    du = u1-u0
    um = .5*(u1+u0)
    stored = float(du @ (elastic @ um))
    excess = .5*float(du @ (elastic @ du))
    dissipated = 0.
    for group, ratio, old, new in zip(groups, r, q0, q1, strict=True):
        dq = new-old
        S = group.stiffness
        S_dq = S @ dq
        cross = float(du @ S_dq)
        dq2 = float(dq @ S_dq)
        stored -= float(du @ (S @ (.5*(new+old)))) + float((um-.5*(new+old)) @ S_dq)
        excess += .5*dq2-cross
        if ratio:
            dissipated += dq2/ratio
    return stored, dissipated, excess
