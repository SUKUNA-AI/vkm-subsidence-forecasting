"""Exact coefficient history for a fixed small-strain Maxwell material group.

A group has one shear modulus and one deviatoric fluidity. Its memory is the
deviatoric strain of a coefficient vector in the same displacement space.
This representation follows by linearity at the group's material points,
including when the group occupies only part of an element. Changing basis or
group parameters requires a new replay or a qualified state transfer.
"""
from dataclasses import dataclass, field
from numbers import Real

import numpy as np
from scipy.sparse import csr_matrix


def _finite_scalar(value, message):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(message)
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(message) from exc
    if not np.isfinite(value):
        raise ValueError(message)
    return value


def _real_array(values, ndim, message):
    values = np.asarray(values)
    if values.ndim != ndim or values.dtype.kind not in "iuf" or not np.isfinite(values).all():
        raise ValueError(message)
    return values


def _check_csr(matrix):
    message = "finite canonical square CSR required"
    if not isinstance(matrix, csr_matrix) or matrix.shape[0] != matrix.shape[1]:
        raise ValueError(message)

    data, indices, indptr = matrix.data, matrix.indices, matrix.indptr
    n = matrix.shape[0]
    if not all(isinstance(a, np.ndarray) for a in (data, indices, indptr)):
        raise ValueError(message)
    if (
        data.ndim != 1 or data.dtype.kind not in "iuf" or not np.isfinite(data).all()
        or indices.ndim != 1 or indices.dtype.kind != "i" or len(indices) != len(data)
        or indptr.shape != (n + 1,) or indptr.dtype.kind != "i"
        or indptr[0] != 0 or indptr[-1] != len(data) or np.any(indptr[1:] < indptr[:-1])
        or np.any(indices < 0) or np.any(indices >= n)
    ):
        raise ValueError(message)

    # Cached SciPy flags survive direct edits to indices. A nonincreasing pair
    # is valid only at a row boundary. Repeated pointers allow empty rows.
    resets = np.flatnonzero(indices[1:] <= indices[:-1]) + 1
    if np.any(indptr[np.searchsorted(indptr, resets)] != resets):
        raise ValueError(message)


def _immutable_copy(array):
    return np.frombuffer(array.tobytes(), dtype=array.dtype)


@dataclass(frozen=True)
class MaxwellGroup:
    """Rock deviatoric stiffness, including support; rate is intrinsic G/eta."""

    rate_per_s: float
    stiffness: csr_matrix
    _shape: tuple[int, int] = field(init=False, repr=False)
    _buffers: tuple[np.ndarray, ...] = field(init=False, repr=False, compare=False)
    _buffer_layout: tuple = field(init=False, repr=False, compare=False)
    _identity: object = field(default_factory=object, init=False, repr=False, compare=False)

    def __post_init__(self):
        message = "positive finite real Maxwell rate required"
        rate = _finite_scalar(self.rate_per_s, message)
        if rate <= 0:
            raise ValueError(message)
        _check_csr(self.stiffness)
        data, indices, indptr = (
            _immutable_copy(array)
            for array in (self.stiffness.data, self.stiffness.indices, self.stiffness.indptr)
        )
        # Keep the exact index width and values from the qualified input.
        owned = csr_matrix(self.stiffness.shape, dtype=self.stiffness.dtype)
        owned.data, owned.indices, owned.indptr = data, indices, indptr
        owned.has_canonical_format = True
        object.__setattr__(self, "rate_per_s", rate)
        object.__setattr__(self, "stiffness", owned)
        object.__setattr__(self, "_shape", owned.shape)
        buffers = (data, indices, indptr)
        object.__setattr__(self, "_buffers", buffers)
        object.__setattr__(self, "_buffer_layout", self._layout(buffers))

    @staticmethod
    def _layout(buffers):
        return tuple((a.dtype, a.shape, a.strides) for a in buffers)

    def _checked_stiffness(self, basis_size):
        matrix = self.stiffness
        arrays = (matrix.data, matrix.indices, matrix.indptr)
        if (matrix.shape != self._shape
                or any(a is not b for a, b in zip(arrays, self._buffers, strict=True))
                or self._layout(arrays) != self._buffer_layout):
            raise ValueError("Maxwell group CSR shape or buffers changed")
        if matrix.shape != (basis_size, basis_size):
            raise ValueError("Maxwell group and displacement basis mismatch")
        return matrix


class _BoundRatios(np.ndarray):
    """Numeric ratios with the ordered identities used to form them."""

    def __array_finalize__(self, source):
        self._group_ids = getattr(source, "_group_ids", None)
        self._original = getattr(source, "_original", None)


def _read_ratios(r, groups=None):
    values = _real_array(r, 1, "finite nonnegative real Maxwell ratios vector required")
    if np.any(values < 0):
        raise ValueError("finite nonnegative real Maxwell ratios vector required")
    if groups is not None and len(groups) != len(values):
        raise ValueError("Maxwell group identity or ordering mismatch")
    if isinstance(r, _BoundRatios):
        if r._original != (values.dtype, values.tobytes()):
            raise ValueError("bound Maxwell ratios changed or reordered")
        if groups is not None and r._group_ids != tuple(group._identity for group in groups):
            raise ValueError("Maxwell group identity or ordering mismatch")
    return values


def ratios(groups, dt_s):
    """Backward-Euler ratios bound to the given ordered group operators."""
    message = "finite nonnegative real Maxwell time increment required"
    dt_s = _finite_scalar(dt_s, message)
    if dt_s < 0:
        raise ValueError(message)
    groups = tuple(groups)
    with np.errstate(over="ignore", invalid="ignore"):
        values = np.array([g.rate_per_s for g in groups]) * dt_s
    if not np.isfinite(values).all():
        raise ValueError("nonfinite Maxwell increment")
    r = _immutable_copy(values).view(_BoundRatios)
    r._group_ids = tuple(group._identity for group in groups)
    r._original = (r.dtype, r.tobytes())
    return r


def tangent(elastic, groups, r):
    """Assemble a tangent at fixed support and time step."""
    r = _read_ratios(r, groups)
    _check_csr(elastic)
    change = csr_matrix(elastic.shape, dtype=elastic.dtype)
    retained = None
    for group, ratio in zip(groups, r, strict=True):
        stiffness = group._checked_stiffness(elastic.shape[0])
        if ratio > 1.:
            change += stiffness
            remainder = stiffness / (1. + ratio)
            retained = remainder if retained is None else retained + remainder
        elif ratio:
            change += stiffness * (ratio / (1. + ratio))
    result = elastic - change
    if retained is not None:
        # Keep the relaxed modulus when ratio / (1 + ratio) rounds to 1.
        result += retained
    if not np.isfinite(result.data).all():
        raise ValueError("nonfinite Maxwell tangent")
    return result


def memory_load(groups, q, r):
    """The load due to old memory, with the sign for the equilibrium RHS."""
    r = _read_ratios(r, groups)
    q = _real_array(q, 2, "finite Maxwell state matrix required")
    if len(q) != len(groups):
        raise ValueError("Maxwell state does not cover the exact groups and basis")
    result = np.zeros(q.shape[1])
    for group, old, ratio in zip(groups, q, r, strict=True):
        result += (group._checked_stiffness(q.shape[1]) @ old) / (1. + ratio)
    if not np.isfinite(result).all():
        raise ValueError("nonfinite Maxwell memory load")
    return result


def update(q, displacement, r):
    """Return trial coefficients without mutating the accepted state."""
    r = _read_ratios(r)
    displacement = _real_array(
        displacement, 1, "finite Maxwell state and displacement vector required",
    )
    q = _real_array(q, 2, "finite Maxwell state and displacement required")
    if q.shape != (len(r), displacement.size):
        raise ValueError("Maxwell state does not cover the exact groups and basis")
    active = r != 0.
    # Inactive rows preserve even the bit patterns of old memory.
    if not active.any():
        return q.copy()

    weight = r / (1. + r)
    ordinary = (r <= 1.) & active
    with np.errstate(over="ignore", invalid="ignore"):
        if ordinary.all():
            trial = q + weight[:, None] * (displacement[None, :] - q)
        else:
            trial = q.astype(np.result_type(q, weight, displacement), copy=True)
            old = q[ordinary]
            trial[ordinary] = old + weight[ordinary, None] * (displacement[None, :] - old)
            large = r > 1.
            # Add the old-memory term directly when subtractive relaxation
            # would lose it to cancellation.
            trial[large] = q[large] / (1. + r[large, None]) + weight[large, None] * displacement
    if not np.isfinite(trial).all():
        raise ValueError("nonfinite Maxwell trial state")
    return trial


def work_terms(elastic, groups, r, u0, u1, q0, q1):
    """Integrate elastic change, viscous dissipation and endpoint excess.

    The input geometry/support and all operators are fixed over this material
    step. This ledger is separate from the work of changing event support.
    """
    r = _read_ratios(r, groups)
    _check_csr(elastic)
    u0 = _real_array(u0, 1, "finite Maxwell displacement vector required")
    u1 = _real_array(u1, 1, "finite Maxwell displacement vector required")
    n = elastic.shape[0]
    if u0.shape != (n,) or u1.shape != (n,):
        raise ValueError("Maxwell displacement basis mismatch")
    q0 = _real_array(q0, 2, "finite Maxwell state matrix required")
    q1 = _real_array(q1, 2, "finite Maxwell state matrix required")
    if q0.shape != (len(groups), n) or q1.shape != q0.shape:
        raise ValueError("Maxwell state does not cover the exact groups and basis")

    du = u1-u0
    um = .5*(u1+u0)
    stored = float(du @ (elastic @ um))
    excess = .5*float(du @ (elastic @ du))
    dissipated = 0.
    for group, ratio, old, new in zip(groups, r, q0, q1, strict=True):
        if not ratio and not np.array_equal(old, new):
            raise ValueError("Maxwell memory changed at zero ratio")
        dq = new-old
        S = group._checked_stiffness(n)
        S_dq = S @ dq
        cross = float(du @ S_dq)
        dq2 = float(dq @ S_dq)
        stored -= float(du @ (S @ (.5*(new+old)))) + float((um-.5*(new+old)) @ S_dq)
        excess += .5*dq2-cross
        if ratio:
            dissipated += dq2/ratio
    if not np.isfinite([stored, dissipated, excess]).all():
        raise ValueError("nonfinite Maxwell work terms")
    return stored, dissipated, excess
