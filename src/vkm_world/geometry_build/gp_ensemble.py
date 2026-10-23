"""Conditional Gaussian ensemble of layered contacts (top depth + interval thicknesses).

Everything here is a declared MODEL_CHOICE, not evidence:

* the top contact is modelled through ``Y0 = ln(depth below the reference collar)``;
* each interval thickness is a clipped Gaussian ``H = max(Y, 0)``: zero thickness is the
  event ``Y <= 0``, an observed absence is the censored datum ``Y <= 0``, and the contact
  order is exact by construction;
* latent fields are independent and stationary, with a constant unknown mean (flat prior),
  a sill with the Jeffreys prior and (practical range, nugget ratio) on a declared grid; every
  realization draws its own parameters from the grid posterior of the readings it is given;
* the nugget is micro-scale variation of the field, not measurement error, so point readings
  are reproduced exactly at their own support; one-sided readings are checks, not constraints.

Realizations are synthetic. They are never observations, and coverage of withheld readings is
an internal consistency check of the uncertainty model, not field validation.

Small problems (cross-validation) run in NumPy. The grid simulator accepts a NumPy or a torch
backend; torch is imported lazily and only when requested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np

COVARIANCE_TYPES = ("exponential", "spherical", "gaussian")


def correlation(h, kind, range_m, xp=np):
    """Unit-sill correlation at distance ``h`` for a practical range ``range_m``."""
    r = h / range_m
    if kind == "exponential":
        return xp.exp(-3.0 * r)
    if kind == "gaussian":
        return xp.exp(-3.0 * r * r)
    if kind == "spherical":
        c = 1.0 - 1.5 * r + 0.5 * r * r * r
        return xp.where(r < 1.0, c, xp.zeros_like(c))
    raise ValueError("unknown covariance type: %r" % (kind,))


def pairwise_distance(a, b):
    a = np.asarray(a, float).reshape(-1, 2)
    b = np.asarray(b, float).reshape(-1, 2)
    return np.sqrt(((a[:, None, :] - b[None, :, :]) ** 2).sum(-1))


@dataclass(frozen=True)
class ParameterGrid:
    """Declared grid for (practical range, nugget ratio); uniform prior on the grid."""

    ranges_m: tuple = tuple(float(v) for v in np.geomspace(250.0, 25000.0, 13))
    nugget_ratios: tuple = (0.0, 0.05, 0.1, 0.2, 0.3, 0.45, 0.6, 0.8)
    jitter: float = 1e-4

    def __post_init__(self):
        if not self.ranges_m or min(self.ranges_m) <= 0:
            raise ValueError("positive ranges required")
        if not self.nugget_ratios or min(self.nugget_ratios) < 0 or max(self.nugget_ratios) >= 1:
            raise ValueError("nugget ratios must lie in [0, 1)")
        if not 0 <= self.jitter < 1e-2:
            raise ValueError("jitter is a numerical regulariser, keep it below 1e-2")

    def tau(self, v):
        """White part of the unit-sill covariance: nugget plus numerical jitter."""
        nu = self.nugget_ratios[v]
        return nu + (1.0 - nu) * self.jitter

    def describe(self):
        return {"ranges_m": list(self.ranges_m), "nugget_ratios": list(self.nugget_ratios), "jitter": self.jitter,
                "prior": "uniform over the grid (log-uniform range); flat mean; Jeffreys sill"}


def _logsumexp(a, axis=None):
    m = np.max(a, axis=axis, keepdims=True)
    out = m + np.log(np.sum(np.exp(a - m), axis=axis, keepdims=True))
    return np.squeeze(out, axis=axis) if axis is not None else float(out.squeeze())


class GridPosterior:
    """Posterior over (range, nugget) of one stationary field from readings at ``xy``.

    Mean (flat prior) and sill (Jeffreys) are integrated analytically, so the marginal
    likelihood on the grid is exact. ``evaluate`` and ``draw`` accept a batch of reading
    vectors (one row per realization). ``xy`` is (n, 2) for fixed support positions or
    (R, n, 2) when every realization places the supports differently (plan-registration
    jitter); then row r of the readings belongs to positions r. Nothing outside the given
    supports enters the posterior.
    """

    def __init__(self, xy, kind, grid=ParameterGrid()):
        xy = np.asarray(xy, float)
        self.xy = xy[None] if xy.ndim == 2 else xy
        if self.xy.ndim != 3 or self.xy.shape[2] != 2:
            raise ValueError("xy must be (n, 2) or (R, n, 2)")
        self.kind, self.grid = kind, grid
        B, n = self.xy.shape[:2]
        if n < 2:
            raise ValueError("at least two readings are needed for a sill posterior")
        if kind not in COVARIANCE_TYPES:
            raise ValueError("unknown covariance type")
        self.n, self.batch = n, B
        d = np.sqrt(((self.xy[:, :, None, :] - self.xy[:, None, :, :]) ** 2).sum(-1))
        same = d <= 1e-9
        A, V = len(grid.ranges_m), len(grid.nugget_ratios)
        self.precision = np.empty((B, A, V, n, n))
        self.logdet = np.empty((B, A, V))
        for a, rng in enumerate(grid.ranges_m):
            rho = correlation(d, kind, rng)
            for v, nu in enumerate(grid.nugget_ratios):
                r = (1.0 - nu) * rho + grid.tau(v) * same
                c = np.linalg.cholesky(r)
                ci = np.linalg.inv(c)
                self.precision[:, a, v] = np.swapaxes(ci, 1, 2) @ ci
                self.logdet[:, a, v] = 2.0 * np.log(np.diagonal(c, axis1=1, axis2=2)).sum(axis=1)
        self.q = self.precision.sum(axis=(3, 4))            # 1' R^-1 1, (B, A, V)

    def _rows(self, R):
        if self.batch not in (1, R):
            raise ValueError("readings must have one row per position batch")
        return np.zeros(R, int) if self.batch == 1 else np.arange(R)

    def evaluate(self, y):
        """Return log marginal likelihood (R, A, V), GLS mean (R, A, V) and S^2 (R, A, V)."""
        y = np.atleast_2d(np.asarray(y, float))
        if y.shape[1] != self.n or not np.isfinite(y).all():
            raise ValueError("one finite reading per training support required")
        B, A, V, n = self.precision.shape[:4]
        if B == 1:
            py = (y @ self.precision[0].reshape(A * V * n, n).T).reshape(len(y), A, V, n)
        else:
            self._rows(len(y))
            py = np.einsum("ravij,rj->ravi", self.precision, y)
        ypy = np.einsum("rj,ravj->rav", y, py)
        q = self.q[self._rows(len(y))]
        beta = py.sum(axis=3) / q
        s2 = np.maximum(ypy - q * beta * beta, 1e-12 * np.maximum(ypy, 1.0))
        logml = -0.5 * self.logdet[self._rows(len(y))] - 0.5 * np.log(q) - 0.5 * (n - 1) * np.log(s2)
        return logml, beta, s2

    def log_evidence(self, y):
        """Grid-averaged log marginal likelihood (used by the type-selection rule)."""
        logml, _, _ = self.evaluate(y)
        A, V = logml.shape[1:]
        return _logsumexp(logml.reshape(len(logml), -1), axis=1) - math.log(A * V)

    def draw(self, rng, y):
        """One parameter draw per reading row: dict of index and value arrays."""
        y = np.atleast_2d(np.asarray(y, float))
        logml, beta, s2 = self.evaluate(y)
        R, A, V = logml.shape
        flat = logml.reshape(R, -1)
        p = np.exp(flat - flat.max(axis=1, keepdims=True))
        cdf = np.cumsum(p, axis=1)
        u = rng.random(R) * cdf[:, -1]
        idx = np.minimum((cdf < u[:, None]).sum(axis=1), A * V - 1)
        a_idx, v_idx = np.divmod(idx, V)
        rows = np.arange(R)
        s2_sel = s2[rows, a_idx, v_idx]
        sigma2 = 0.5 * s2_sel / rng.gamma(0.5 * (self.n - 1), 1.0, R)
        q_sel = self.q[self._rows(R), a_idx, v_idx]
        mean = beta[rows, a_idx, v_idx] + np.sqrt(sigma2 / q_sel) * rng.standard_normal(R)
        return {"a_idx": a_idx, "v_idx": v_idx, "sigma2": sigma2, "mean": mean}

    def selected_precision(self, params):
        R = len(params["a_idx"])
        return self.precision[self._rows(R), params["a_idx"], params["v_idx"]]       # (R, n, n)


def _trunc_normal_upper(rng, mu, sd, upper):
    """Draw from N(mu, sd^2) truncated to (-inf, upper], stable in the far tail."""
    from scipy.special import log_ndtr, ndtri_exp

    z = (upper - mu) / sd
    logp = log_ndtr(z) + np.log(np.maximum(rng.random(np.shape(mu)), 1e-300))
    return np.minimum(mu + sd * ndtri_exp(logp), upper)


def draw_parameters(rng, posterior, y, censored=None, upper=None, sweeps=30):
    """Parameter draws per row of ``y``; censored entries are imputed by data augmentation.

    ``censored`` is a boolean mask over the readings, ``upper`` their upper bounds. The
    returned readings are the imputed latent values the realization is conditioned on.
    """
    y = np.array(np.atleast_2d(np.asarray(y, float)), dtype=float)
    if posterior.batch > 1 and len(y) == 1:
        y = np.repeat(y, posterior.batch, axis=0)
    censored = np.zeros(posterior.n, bool) if censored is None else np.asarray(censored, bool)
    if not censored.any():
        return posterior.draw(rng, y), y
    upper = np.asarray(upper, float)
    cols = np.flatnonzero(censored)
    y[:, cols] = np.minimum(y[:, cols], upper[cols])
    for _ in range(sweeps):
        params = posterior.draw(rng, y)
        prec = posterior.selected_precision(params)
        for c in cols:
            pcc = prec[:, c, c]
            resid = y - params["mean"][:, None]
            resid[:, c] = 0.0
            mu = params["mean"] - (prec[:, c, :] * resid).sum(axis=1) / pcc
            sd = np.sqrt(params["sigma2"] / pcc)
            y[:, c] = _trunc_normal_upper(rng, mu, sd, upper[c])
    params = posterior.draw(rng, y)
    return params, y


def kriging_draws(rng, posterior, y, params, targets):
    """Marginal predictive draws (R, m) of the latent field at ``targets`` ((m, 2) or (R, m, 2))."""
    y = np.atleast_2d(np.asarray(y, float))
    R = len(y)
    t = np.asarray(targets, float)
    t = t.reshape(1, -1, 2) if t.ndim <= 2 else t
    grid = posterior.grid
    xy = posterior.xy
    d = np.sqrt(((t[:, :, None, :] - xy[:, None, :, :]) ** 2).sum(-1))           # (1|R, m, n)
    d = np.broadcast_to(d, (R,) + d.shape[1:])
    same = d <= 1e-9
    nu = np.asarray(grid.nugget_ratios)[params["v_idx"]][:, None, None]
    tau = nu + (1.0 - nu) * grid.jitter
    rng_m = np.asarray(grid.ranges_m)[params["a_idx"]][:, None, None]
    k = (1.0 - nu) * correlation(d, posterior.kind, rng_m) + tau * same
    w = k @ posterior.selected_precision(params)                                    # (R, m, n)
    var = np.maximum((1.0 - nu[:, :, 0]) + tau[:, :, 0] - (w * k).sum(axis=2), 0.0)
    mean = params["mean"][:, None]
    mu = mean + np.einsum("rmn,rn->rm", w, y - mean)
    return mu + np.sqrt(params["sigma2"][:, None] * var) * rng.standard_normal(mu.shape)


def select_covariance_type(xy, y, types=COVARIANCE_TYPES, grid=ParameterGrid()):
    """Declared rule: largest grid-averaged log marginal likelihood; ties -> listed order."""
    evidence = {kind: float(GridPosterior(xy, kind, grid).log_evidence(y)[0]) for kind in types}
    best = max(types, key=lambda k: (evidence[k], -types.index(k)))
    return best, evidence


# --------------------------------------------------------------------------- layered readings

@dataclass
class LayeredReadings:
    """Point depths below each support's own collar, absences and one-sided checks.

    ``depth``: (n, K) depths of K ordered contacts, NaN where there is no point reading.
    ``absent``: (row, top, bottom) observed zero thickness of every interval top..bottom.
    ``checks``: dict(row, contact, deeper_than_m): contact not reached down to that depth.
    """

    ids: list
    xy: np.ndarray
    depth: np.ndarray
    contacts: list
    absent: list = field(default_factory=list)
    checks: list = field(default_factory=list)

    def __post_init__(self):
        self.xy = np.asarray(self.xy, float).reshape(-1, 2)
        self.depth = np.array(self.depth, dtype=float)
        n, k = self.depth.shape
        if len(self.ids) != n or len(self.xy) != n or len(self.contacts) != k:
            raise ValueError("ids, xy and depth rows must match")
        if len(set(self.ids)) != n:
            raise ValueError("one row per physical borehole")
        self.filled = np.zeros_like(self.depth, dtype=bool)
        self.zero_intervals = np.zeros((n, k - 1), bool)
        for row, top, bottom in self.absent:
            if not 0 <= top < bottom < k:
                raise ValueError("absence must name two ordered contacts")
            span = self.depth[row, top:bottom + 1]
            known = span[np.isfinite(span)]
            if len(known) and np.ptp(known) > 1e-9:
                raise ValueError("absence contradicts point readings of %s" % self.ids[row])
            if len(known):
                missing = ~np.isfinite(span)
                self.filled[row, top:bottom + 1] = missing
                self.depth[row, top:bottom + 1] = known[0]
            self.zero_intervals[row, top:bottom] = True
        for row in range(n):
            finite = np.flatnonzero(np.isfinite(self.depth[row]))
            if not len(finite):
                raise ValueError("support without point readings: %s" % self.ids[row])
            if finite[0] != 0:
                raise ValueError("top-contact reading required (log-depth field): %s" % self.ids[row])
            if np.any(np.diff(finite) > 1):
                raise ValueError("non-adjacent readings (sum of thicknesses) are not supported: %s" % self.ids[row])
            if np.any(np.diff(self.depth[row, finite]) < -1e-9):
                raise ValueError("crossing contacts: record the conflict upstream: %s" % self.ids[row])
            if self.depth[row, 0] <= 0:
                raise ValueError("top depth must be positive below the collar")

    @property
    def n_fields(self):
        return self.depth.shape[1]

    def field_readings(self, f, rows=None):
        """Readings of latent field ``f`` restricted to support rows ``rows``.

        Returns (row indices, latent values, censored mask). Field 0 is the top log-depth
        (without collar shift), field k >= 1 the thickness of interval k-1 -> k.
        """
        rows = np.arange(len(self.ids)) if rows is None else np.asarray(rows)
        if f == 0:
            keep = rows[np.isfinite(self.depth[rows, 0])]
            return keep, np.log(self.depth[keep, 0]), np.zeros(len(keep), bool)
        both = np.isfinite(self.depth[rows, f - 1]) & np.isfinite(self.depth[rows, f])
        keep = rows[both]
        value = self.depth[keep, f] - self.depth[keep, f - 1]
        censored = self.zero_intervals[keep, f - 1] | (value <= 1e-9)
        value = np.where(censored, 0.0, value)
        return keep, value, censored

    def subset(self, rows):
        rows = np.asarray(rows)
        index = {int(r): i for i, r in enumerate(rows)}
        raw = self.depth[rows].copy()
        raw[self.filled[rows]] = np.nan
        return LayeredReadings([self.ids[r] for r in rows], self.xy[rows], raw, list(self.contacts),
                               [(index[r], t, b) for r, t, b in self.absent if r in index],
                               [dict(c, row=index[c["row"]]) for c in self.checks if c["row"] in index])


TOP_FLOOR_M = 0.1


def top_latent(depth0, shift):
    """Latent top reading under a collar shift (true collar = reference + shift)."""
    d = np.asarray(depth0, float) - np.asarray(shift, float)
    return np.log(np.maximum(d, TOP_FLOOR_M)), int(np.sum(d < TOP_FLOOR_M))


def draw_field(rng, xy, kind, grid, y, censored, sweeps=30, chunk=400):
    """Parameter draws and conditioning readings for one field.

    ``xy`` is (n, 2), or (R, n, 2) for per-realization support positions; then the posterior
    is built per realization in chunks (memory) and the result is identical to one batch.
    Returns (posterior or list of (rows, posterior), params, readings used).
    """
    y = np.atleast_2d(np.asarray(y, float))
    xy = np.asarray(xy, float)
    upper = np.zeros(y.shape[1])
    if xy.ndim == 2:
        post = GridPosterior(xy, kind, grid)
        params, used = draw_parameters(rng, post, y, censored, upper, sweeps)
        return [(np.arange(len(used)), post)], params, used
    R = len(xy)
    y = np.broadcast_to(y, (R, y.shape[1])).copy()
    parts, params, used = [], {}, np.empty_like(y)
    for s in range(0, R, chunk):
        rows = np.arange(s, min(s + chunk, R))
        post = GridPosterior(xy[rows], kind, grid)
        p, u = draw_parameters(rng, post, y[rows], censored, upper, sweeps)
        parts.append((rows, post))
        used[rows] = u
        for key, val in p.items():
            params.setdefault(key, []).append(val)
    return parts, {k: np.concatenate(v) for k, v in params.items()}, used


def _kriging_parts(rng, parts, used, params, targets):
    R = len(used)
    t = np.asarray(targets, float)
    out = None
    for rows, post in parts:
        sub = {k: v[rows] for k, v in params.items()}
        tt = t[rows] if t.ndim == 3 else t
        draws = kriging_draws(rng, post, used[rows], sub, tt)
        if out is None:
            out = np.empty((R, draws.shape[1]))
        out[rows] = draws
    return out


def layered_predictive(rng, readings, train_rows, targets, kinds, draws, grid=ParameterGrid(),
                       train_shift=None, sweeps=30, train_xy=None, target_xy=None):
    """Predictive draws of contact depths below the reference collar at ``targets``.

    ``kinds``: covariance type per latent field. ``train_shift``: (draws, n) vertical collar
    shifts of all supports (true collar = reference + shift; only the top reading changes).
    ``train_xy``: (draws, n, 2) per-realization support positions (plan registration) and
    ``target_xy``: (draws, m, 2) per-realization target positions; None = as placed.
    Returns dict with ``depth`` (draws, m, K) and ``thickness`` (draws, m, K-1).
    """
    t = np.asarray(targets, float).reshape(-1, 2) if target_xy is None else np.asarray(target_xy, float)
    m = t.shape[-2]
    K = readings.n_fields
    latent = np.empty((draws, m, K))
    info = []
    for f in range(K):
        rows, value, censored = readings.field_readings(f, train_rows)
        if len(rows) < 2:
            raise ValueError("field %d has fewer than two training readings" % f)
        y = np.broadcast_to(value, (draws, len(rows))).copy()
        floored = 0
        if f == 0 and train_shift is not None:
            y, floored = top_latent(readings.depth[rows, 0][None, :], train_shift[:, rows])
        xy = readings.xy[rows] if train_xy is None else np.asarray(train_xy)[:, rows]
        parts, params, used = draw_field(rng, xy, kinds[f], grid, y, censored, sweeps)
        latent[:, :, f] = _kriging_parts(rng, parts, used, params, t)
        info.append({"field": f, "kind": kinds[f], "readings": int(len(rows)), "censored": int(censored.sum()),
                     "top_floor_hits": floored})
    top = np.exp(latent[:, :, 0])
    thickness = np.maximum(latent[:, :, 1:], 0.0)
    depth = np.concatenate([top[:, :, None], top[:, :, None] + np.cumsum(thickness, axis=2)], axis=2)
    return {"depth": depth, "thickness": thickness, "fields": info}


# --------------------------------------------------------------------------- scores

def crps_ensemble(samples, observed):
    """Ensemble CRPS per column: mean|X - y| - 0.5 mean|X - X'| (sorted-sample identity)."""
    x = np.sort(np.asarray(samples, float), axis=0)
    m = x.shape[0]
    y = np.asarray(observed, float)
    term1 = np.abs(x - y).mean(axis=0)
    weights = (2.0 * np.arange(1, m + 1) - m - 1)
    term2 = (weights[:, None] * x.reshape(m, -1)).sum(axis=0).reshape(x.shape[1:]) / (m * m)
    return term1 - term2


def wilson_interval(k, n, z=1.959963984540054):
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (centre - half, centre + half)


def coverage_verdict(k, n, nominal):
    lo, hi = wilson_interval(k, n)
    if n == 0:
        return "NO_READINGS"
    if lo <= nominal <= hi:
        return "CONSISTENT"
    return "UNDER_COVERS" if hi < nominal else "OVER_COVERS"


def score_draws(draws, observed):
    """Interval scores of one withheld reading from its predictive draws."""
    q05, q10, q50, q90, q95 = np.quantile(draws, [0.05, 0.10, 0.50, 0.90, 0.95])
    return {"observed": float(observed), "p05": float(q05), "p10": float(q10), "p50": float(q50), "p90": float(q90),
            "p95": float(q95), "in80": bool(q10 <= observed <= q90), "in90": bool(q05 <= observed <= q95),
            "crps": float(crps_ensemble(np.asarray(draws)[:, None], np.array([observed]))[0]),
            "abs_error_p50": float(abs(q50 - observed))}


# --------------------------------------------------------------------------- grid simulation

class _NumpyBackend:
    name = "numpy"

    def __init__(self, seed, dtype=np.float64):
        self.rng = np.random.default_rng(seed)
        self.dtype = dtype
        self.xp = np

    def asarray(self, a):
        return np.asarray(a, dtype=self.dtype)

    def to_numpy(self, a):
        return np.asarray(a)

    def correlation_matrix(self, nodes, kind, range_m, jitter):
        c = correlation(pairwise_distance(nodes, nodes), kind, range_m).astype(self.dtype)
        c[np.diag_indices_from(c)] += jitter
        return c

    def cholesky(self, c):
        try:
            return np.linalg.cholesky(c), 0
        except np.linalg.LinAlgError:
            return None, 1

    def randn(self, shape):
        return self.rng.standard_normal(shape).astype(self.dtype)

    def empty(self, shape):
        return np.empty(shape, dtype=self.dtype)

    def index(self, a):
        return np.asarray(a, dtype=np.int64)

    def take_rows(self, field, idx):
        return np.take_along_axis(field, np.asarray(idx, np.int64), axis=1)

    def matvec64(self, mats, vecs):
        return np.einsum("bij,bj->bi", mats, np.asarray(vecs, float)).astype(self.dtype)

    def cross_kriging(self, nodes_c, x, idx, kind, range_m, nu, tau, coef, chunk=4096):
        b, N = len(x), len(nodes_c)
        out = np.empty((b, N), dtype=self.dtype)
        coef = np.asarray(coef, float)
        for s in range(0, N, chunk):
            d = np.sqrt(((nodes_c[None, s:s + chunk, None, :] - x[:, None, :, :]) ** 2).sum(-1))
            rho = correlation(d, kind, range_m)
            out[:, s:s + chunk] = (1.0 - nu)[:, None] * np.einsum("bmn,bn->bm", rho, coef)
        np.add.at(out, (np.repeat(np.arange(b), idx.shape[1]), np.asarray(idx).ravel()), (tau[:, None] * coef).ravel())
        return out

    def synchronize(self):
        pass


class _TorchBackend:
    name = "torch"

    def __init__(self, seed, device="cuda", dtype="float32", row_chunk=2048):
        import torch

        self.torch = torch
        self.xp = torch
        self.device = torch.device(device)
        self.dtype = getattr(torch, dtype)
        self.gen = torch.Generator(device=self.device)
        self.gen.manual_seed(int(seed))
        self.row_chunk = row_chunk

    def asarray(self, a):
        return self.torch.as_tensor(np.asarray(a), dtype=self.dtype, device=self.device)

    def to_numpy(self, a):
        return a.detach().cpu().numpy()

    def correlation_matrix(self, nodes, kind, range_m, jitter):
        torch = self.torch
        x = torch.as_tensor(np.asarray(nodes, float), dtype=torch.float64, device=self.device)
        x = (x - x.mean(dim=0)).to(self.dtype)
        n = len(x)
        c = torch.empty((n, n), dtype=self.dtype, device=self.device)
        for s in range(0, n, self.row_chunk):
            d = torch.cdist(x[s:s + self.row_chunk], x, compute_mode="donot_use_mm_for_euclid_dist")
            c[s:s + self.row_chunk] = correlation(d, kind, float(range_m), xp=torch)
            del d
        c.diagonal().add_(jitter)
        return c

    def cholesky(self, c):
        torch = self.torch
        info = torch.empty((), dtype=torch.int32, device=self.device)
        torch.linalg.cholesky_ex(c, out=(c, info))
        return (c, 0) if int(info) == 0 else (None, int(info))

    def randn(self, shape):
        return self.torch.randn(shape, generator=self.gen, dtype=self.dtype, device=self.device)

    def empty(self, shape):
        return self.torch.empty(shape, dtype=self.dtype, device=self.device)

    def index(self, a):
        return self.torch.as_tensor(np.asarray(a, dtype=np.int64), device=self.device)

    def take_rows(self, field, idx):
        return self.torch.gather(field, 1, self.index(idx))

    def matvec64(self, mats, vecs):
        torch = self.torch
        m = torch.as_tensor(np.asarray(mats, float), dtype=torch.float64, device=self.device)
        return torch.einsum("bij,bj->bi", m, vecs.to(torch.float64))

    def cross_kriging(self, nodes_c, x, idx, kind, range_m, nu, tau, coef, chunk=4096):
        torch = self.torch
        f64 = torch.float64
        nodes_t = torch.as_tensor(np.asarray(nodes_c, float), dtype=f64, device=self.device)
        x_t = torch.as_tensor(np.asarray(x, float), dtype=f64, device=self.device)
        coef = coef.to(f64)
        nu_t = torch.as_tensor(np.asarray(nu, float), dtype=f64, device=self.device)
        tau_t = torch.as_tensor(np.asarray(tau, float), dtype=f64, device=self.device)
        b, N = len(x_t), len(nodes_t)
        out = torch.empty((b, N), dtype=self.dtype, device=self.device)
        for s in range(0, N, chunk):
            d = torch.cdist(nodes_t[s:s + chunk].unsqueeze(0).expand(b, -1, -1), x_t, compute_mode="donot_use_mm_for_euclid_dist")
            rho = correlation(d, kind, float(range_m), xp=torch)
            out[:, s:s + chunk] = ((1.0 - nu_t)[:, None] * torch.einsum("bmn,bn->bm", rho, coef)).to(self.dtype)
            del d, rho
        out.scatter_add_(1, self.index(idx), (tau_t[:, None] * coef).to(self.dtype))
        return out

    def synchronize(self):
        if self.device.type == "cuda":
            self.torch.cuda.synchronize(self.device)


def make_backend(name="numpy", seed=0, **kw):
    if name == "numpy":
        return _NumpyBackend(seed, **kw)
    if name == "torch":
        return _TorchBackend(seed, **kw)
    raise ValueError("backend must be numpy or torch")


def data_node_index(nodes, xy, tolerance_m=1e-6):
    """Index of the grid node that coincides with every support (supports are mesh vertices)."""
    d = pairwise_distance(xy, nodes)
    idx = d.argmin(axis=1)
    if np.any(d[np.arange(len(xy)), idx] > tolerance_m):
        raise ValueError("every support must be a node of the grid")
    return idx


def simulate_on_nodes(backend, nodes, data_idx, kind, grid, params, y_data, batch=200, log=None):
    """Conditional realizations (R, N) of one latent field on ``nodes``.

    Unconditional simulation by the Cholesky factor of the continuous correlation (plus the
    numerical jitter) and an independent white nugget, then kriging of the residuals at the
    data nodes. ``data_idx`` is (n,) or (R, n): per-realization data nodes (plan-registration
    jitter by candidate points appended to ``nodes``). Kriging weights are solved in float64
    per realization, so data nodes reproduce ``y_data`` up to float32 rounding.
    """
    nodes = np.asarray(nodes, float)
    y_data = np.atleast_2d(np.asarray(y_data, float))
    R, N = len(y_data), len(nodes)
    data_idx = np.asarray(data_idx)
    data_idx = np.broadcast_to(data_idx, (R, data_idx.shape[-1])) if data_idx.ndim == 1 else data_idx
    n = data_idx.shape[1]
    out = backend.empty((R, N))
    centre = nodes.mean(axis=0)
    nodes_c = nodes - centre
    log = [] if log is None else log
    nu_all = np.asarray(grid.nugget_ratios)[params["v_idx"]]
    for a in np.unique(params["a_idx"]):
        rows_a = np.flatnonzero(params["a_idx"] == a)
        rng_m = grid.ranges_m[int(a)]
        jitter = grid.jitter
        L = None
        while L is None:
            L, info = backend.cholesky(backend.correlation_matrix(nodes, kind, rng_m, jitter))
            if L is None:
                if jitter >= 1e-2:
                    raise RuntimeError("Cholesky failed even with jitter 1e-2")
                jitter *= 10.0
        log.append({"kind": kind, "range_m": rng_m, "jitter_used": jitter, "realizations": int(len(rows_a))})
        for s in range(0, len(rows_a), batch):
            rows = rows_a[s:s + batch]
            b = len(rows)
            idx = data_idx[rows]                                                      # (b, n)
            nu = nu_all[rows]
            tau = nu + (1.0 - nu) * jitter
            x = nodes_c[idx]                                                          # (b, n, 2)
            d_dd = np.sqrt(((x[:, :, None] - x[:, None]) ** 2).sum(-1))
            r_dd = (1.0 - nu)[:, None, None] * correlation(d_dd, kind, rng_m) + tau[:, None, None] * (d_dd <= 1e-9)
            r_inv = np.linalg.inv(r_dd)                                               # float64, small
            zc = L @ backend.randn((N, b))                                            # (N, b)
            eta = backend.randn((N, b))
            sig = np.sqrt(params["sigma2"][rows])
            amp_c = backend.asarray(sig * np.sqrt(1.0 - nu))
            amp_w = backend.asarray(sig * np.sqrt(nu))
            mean = backend.asarray(params["mean"][rows])
            field = (mean[None, :] + amp_c[None, :] * zc + amp_w[None, :] * eta).T      # (b, N)
            del zc, eta
            resid = backend.asarray(y_data[rows]) - backend.take_rows(field, idx)       # (b, n)
            coef = backend.matvec64(r_inv, resid)                                     # R^-1 resid, (b, n)
            field += backend.cross_kriging(nodes_c, x, idx, kind, rng_m, nu, tau, coef)
            out[backend.index(rows)] = field
            del field, resid, coef
        del L
    return out


def sorted_quantiles(backend, realizations, probs, chunk=2048):
    """Quantiles along axis 0 of (R, N) realizations, by sorting node chunks."""
    R, N = realizations.shape
    probs = np.asarray(probs, float)
    pos = probs * (R - 1)
    lo = np.floor(pos).astype(int)
    hi = np.minimum(lo + 1, R - 1)
    frac = pos - lo
    out = np.empty((len(probs), N))
    for s in range(0, N, chunk):
        block = realizations[:, s:s + chunk]
        srt = backend.torch.sort(block, dim=0).values if backend.name == "torch" else np.sort(block, axis=0)
        srt = backend.to_numpy(srt).astype(float)
        out[:, s:s + chunk] = srt[lo] * (1 - frac)[:, None] + srt[hi] * frac[:, None]
    return out


def tin_sampling_operator(nodes, triangles, points):
    """Exact P1 interpolation weights of ``points`` on the given TIN: (index (m,3), weight (m,3))."""
    from matplotlib.tri import Triangulation

    nodes = np.asarray(nodes, float)
    tri = np.asarray(triangles, int)
    pts = np.asarray(points, float).reshape(-1, 2)
    finder = Triangulation(nodes[:, 0], nodes[:, 1], tri).get_trifinder()
    ids = finder(pts[:, 0], pts[:, 1])
    idx = np.zeros((len(pts), 3), int)
    w = np.full((len(pts), 3), np.nan)
    inside = ids >= 0
    t = tri[ids[inside]]
    p = nodes[t]
    q = pts[inside] - p[:, 0]
    ab = p[:, 1] - p[:, 0]
    ac = p[:, 2] - p[:, 0]
    det = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
    u = (q[:, 0] * ac[:, 1] - q[:, 1] * ac[:, 0]) / det
    v = (ab[:, 0] * q[:, 1] - ab[:, 1] * q[:, 0]) / det
    idx[inside] = t
    w[inside] = np.column_stack([1 - u - v, u, v])
    return idx, w
