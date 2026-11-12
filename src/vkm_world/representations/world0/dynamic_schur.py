"""Linear first-order condensation, with explicit reduction memory.

Exact elimination retains n states: it is not a reduced-order speedup.
Modal projection is a separate approximation, never silently selected.
All coefficients must already use a consistent work-conjugate scaling.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import numpy as np
from scipy.linalg import eigh, expm
from vkm_world.representations.world0.binding import representation_snapshot, assert_applicable


def _array(value, ndim, name):
    if np.iscomplexobj(value):
        raise ValueError(f"{name} must be real")
    a = np.array(value, dtype=float, copy=True)
    if a.ndim != ndim or not np.isfinite(a).all():
        raise ValueError(f"invalid {name}")
    return a


def _freeze(a):
    a = np.ascontiguousarray(a)
    # A bytes-owned buffer cannot have WRITEABLE re-enabled by numpy.
    return np.frombuffer(a.tobytes(), dtype=a.dtype).reshape(a.shape)


@dataclass(frozen=True, slots=True)
class CondensedState:
    time_s: float
    retained: np.ndarray
    hidden: np.ndarray
    signature: str

    def __post_init__(self):
        if not np.isfinite(self.time_s) or not isinstance(self.signature, str) or not re.fullmatch(r"[0-9a-f]{64}", self.signature):
            raise ValueError("invalid state time or signature")
        object.__setattr__(self, "time_s", float(self.time_s))
        for name in ("retained", "hidden"):
            object.__setattr__(self, name, _freeze(_array(getattr(self, name), 1, name)))

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        return (type(self), (self.time_s, self.retained, self.hidden, self.signature))


class DynamicCondensation:
    """Exact BE elimination of C xdot + K x = f, including nonzero C_ri.

    K, C need not be symmetric/SPD. The eliminated block and Schur matrix
    of each step must be invertible; singular systems fail without a gauge fix.
    Dense direct solves target small qualification systems, not whole-mine FEM.
    """
    __slots__ = ("_c", "_k", "_r", "_i", "_binding_json", "_sealed")

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("immutable operator; construct and bind a new operator")
        object.__setattr__(self, name, value)

    @property
    def c(self):
        return self._c

    @property
    def k(self):
        return self._k

    @property
    def r(self):
        return self._r

    @property
    def i(self):
        return self._i

    @property
    def binding(self):
        # Expose a detached record, never the operator's dependency storage.
        return json.loads(self._binding_json)

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        return (type(self), (self.c, self.k, self.r, self.binding["components"]))

    def __init__(self, capacity, stiffness, retained, components):
        self._c = _freeze(_array(capacity, 2, "capacity"))
        self._k = _freeze(_array(stiffness, 2, "stiffness"))
        if self.k.shape != self.c.shape or self.k.shape[0] != self.k.shape[1]:
            raise ValueError("C and K must be square and equally sized")
        n = len(self.k)
        raw = np.asarray(retained)
        if raw.ndim != 1 or raw.dtype.kind not in "iu":
            raise ValueError("retained indices must be integers")
        if not 0 < len(raw) < n or len(np.unique(raw)) != len(raw) or np.any(raw < 0) or np.any(raw >= n):
            raise ValueError("invalid partition")
        self._r = _freeze(raw.astype(int))
        self._i = _freeze(np.setdiff1d(np.arange(n), self.r))
        self._binding_json = json.dumps(self._binding(self.c, self.k, components), sort_keys=True)
        self._sealed = True

    def _binding(self, c, k, components):
        c, k = _array(c, 2, "capacity"), _array(k, 2, "stiffness")
        if c.shape != self.c.shape or k.shape != self.k.shape:
            raise ValueError("operator dimensions changed")
        dependencies = dict(components)
        dependencies["dynamic_capacity_sha256"] = hashlib.sha256(np.asarray(c, dtype="<f8").tobytes()).hexdigest()
        dependencies["dynamic_retained_order"] = self.r.tolist()
        return representation_snapshot(np.asarray(k), np.arange(len(k)), dependencies)

    def assert_applicable(self, capacity, stiffness, components):
        assert_applicable(self.binding, self._binding(capacity, stiffness, components))

    def initial_state(self, full_state, time_s=0.0):
        x = _array(full_state, 1, "full state")
        if x.shape != (len(self.k),) or not np.isfinite(time_s):
            raise ValueError("state dimensions/time")
        return CondensedState(float(time_s), _freeze(x[self.r]), _freeze(x[self.i]), self.binding["signature"])

    def reconstruct(self, state):
        if state.signature != self.binding["signature"]:
            raise ValueError("state belongs to a different operator")
        if state.retained.shape != self.r.shape or state.hidden.shape != self.i.shape:
            raise ValueError("state dimensions")
        x = np.empty(len(self.k))
        x[self.r], x[self.i] = state.retained, state.hidden
        if not np.isfinite(x).all():
            raise ValueError("nonfinite state")
        return x

    def _eliminate(self, a, rhs):
        ii, ir = a[np.ix_(self.i, self.i)], a[np.ix_(self.i, self.r)]
        ri, rr = a[np.ix_(self.r, self.i)], a[np.ix_(self.r, self.r)]
        lift = np.linalg.solve(ii, ir)
        offset = np.linalg.solve(ii, rhs[self.i])
        q = np.linalg.solve(rr - ri @ lift, rhs[self.r] - ri @ offset)
        x = np.empty(len(a), dtype=np.result_type(a, rhs))
        x[self.r], x[self.i] = q, offset - lift @ q
        return x

    def trial_step(self, accepted, force, dt_s):
        if not np.isfinite(dt_s) or dt_s <= 0:
            raise ValueError("positive finite dt required")
        f = _array(force, 1, "force")
        if f.shape != (len(self.k),):
            raise ValueError("force dimensions")
        previous = self.reconstruct(accepted)
        a = self.k + self.c / dt_s
        rhs = f + self.c @ previous / dt_s
        x = self._eliminate(a, rhs)
        residual = a @ x - rhs
        scale = max(1.0, np.linalg.norm(a, np.inf)*np.linalg.norm(x, np.inf), np.linalg.norm(rhs, np.inf))
        if np.linalg.norm(residual, np.inf) > 1e-11 * scale:
            raise RuntimeError("condensed balance failed")
        return self.initial_state(x, accepted.time_s + dt_s)

    def static_solve(self, force):
        f = _array(force, 1, "force")
        if f.shape != (len(self.k),):
            raise ValueError("force dimensions")
        return self._eliminate(self.k, f)

    def laplace_operator(self, s):
        if not np.isfinite(s):
            raise ValueError("finite Laplace variable required")
        a = self.k + s*self.c
        return a[np.ix_(self.r,self.r)] - a[np.ix_(self.r,self.i)] @ np.linalg.solve(a[np.ix_(self.i,self.i)], a[np.ix_(self.i,self.r)])

    def laplace_rhs(self, s, force_transform, initial_full):
        """RHS includes C*x(0); force_transform has force*time units."""
        if not np.isfinite(s):
            raise ValueError("finite Laplace variable required")
        x = self.reconstruct(self.initial_state(initial_full))
        f = np.asarray(force_transform)
        if f.shape != x.shape or not np.isfinite(f).all():
            raise ValueError("invalid force transform")
        a, rhs = self.k + s*self.c, f + self.c @ x
        return rhs[self.r] - a[np.ix_(self.r,self.i)] @ np.linalg.solve(a[np.ix_(self.i,self.i)], rhs[self.i])

    def memory_kernel(self, time_s):
        """K_ri exp(-C_ii^-1 K_ii t) C_ii^-1 K_ir, C_ri=C_ir=0 only.

        The retained equation subtracts its convolution with x_r. Initial
        hidden forcing is separately -K_ri exp(-A*t) x_i(0).
        """
        if not np.isfinite(time_s) or time_s < 0:
            raise ValueError("nonnegative time required")
        if np.any(self.c[np.ix_(self.r,self.i)]) or np.any(self.c[np.ix_(self.i,self.r)]):
            raise ValueError("this kernel realization requires block-diagonal C")
        ci = self.c[np.ix_(self.i,self.i)]
        a = np.linalg.solve(ci, self.k[np.ix_(self.i,self.i)])
        return self.k[np.ix_(self.r,self.i)] @ expm(-a*time_s) @ np.linalg.solve(ci,self.k[np.ix_(self.i,self.r)])


def modal_projection(capacity, stiffness, retained, n_hidden_modes):
    """Static constraint modes plus lowest generalized internal modes.

    C and K must be symmetric positive definite. Returns T, T^T C T,
    T^T K T. At zero modes static response to retained-only loads is exact;
    arbitrary internal loads and dynamics require omitted-mode error checks.
    """
    c, k = _array(capacity, 2, "capacity"), _array(stiffness, 2, "stiffness")
    if c.shape != k.shape or c.shape[0] != c.shape[1]:
        raise ValueError("matrix dimensions")
    for a in (c,k):
        if not np.allclose(a,a.T,rtol=0,atol=1e-12):
            raise ValueError("symmetric matrices required")
        np.linalg.cholesky(a)
    r = np.asarray(retained)
    if r.ndim != 1 or r.dtype.kind not in "iu" or len(np.unique(r)) != len(r) or not 0 < len(r) < len(k) or np.any(r<0) or np.any(r>=len(k)):
        raise ValueError("invalid partition")
    i = np.setdiff1d(np.arange(len(k)),r)
    if not isinstance(n_hidden_modes,(int,np.integer)) or not 0 <= n_hidden_modes <= len(i):
        raise ValueError("invalid mode count")
    kii, cii = k[np.ix_(i,i)], c[np.ix_(i,i)]
    _, v = eigh(kii, cii)
    t = np.zeros((len(k),len(r)+n_hidden_modes))
    t[r,:len(r)] = np.eye(len(r))
    t[i,:len(r)] = -np.linalg.solve(kii,k[np.ix_(i,r)])
    t[i,len(r):] = v[:,:n_hidden_modes]
    return t, t.T@c@t, t.T@k@t


def project_with_detail(full, prolongation, metric):
    """Lossless split/merge coordinates q and detail z; neither is discarded.

    x=Pq+z and P^T M z=0. If 1 lies in range(P), 1^T M x is conserved
    even by Pq. Full energy/history recovery additionally requires z.
    """
    x,p,m = _array(full,1,"state"),_array(prolongation,2,"prolongation"),_array(metric,2,"metric")
    if m.shape != (len(x),len(x)) or p.shape[0] != len(x):
        raise ValueError("projection dimensions")
    if not np.allclose(m,m.T,rtol=0,atol=1e-12):
        raise ValueError("symmetric metric required")
    np.linalg.cholesky(m)
    q = np.linalg.solve(p.T@m@p,p.T@m@x)
    return q, x-p@q
