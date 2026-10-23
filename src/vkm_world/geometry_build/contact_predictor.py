"""One contact predictor for the model build and every validation fold.

Review finding R2-01 (17.10.2026): the full-context build interpolated depths
and added the surface at the target, while cross-validation interpolated
absolute levels. The two operators differ by H(q) - sum w_i H(x_i). Here the
build and each fold call the same ``ContactPredictor.predict``; a fold differs
only by the supports it receives.

Observations keep their source meaning (depths below the recorded collar):

* point depth: lower == upper;
* depth interval: contact not reached / bounded reading, lower or upper may be
  NaN (unbounded);
* thickness between two contacts, including observed absence (0, 0).

Parameterization:

* ``LEVEL``: p = Z, absolute level in the build height frame;
* ``SURFACE``: p = Z - S(x), with S = sampled surface operator + site offset.

Prediction: Z(q) = S(q) + p(q). With weights w_i(q) from the declared method,
p(q) minimises the weighted squared misfit of every support's shallowest
level reading and of the gaps between its consecutive readings (increment or
isopach form), subject to the contact order p_0 >= p_1 >= ... (zero thickness
allowed). For complete point columns this is exactly the common-weight convex
combination of ``joint_predict``; missing readings contribute nothing and a
missing contact inherits the local thickness pattern. Interval readings contribute only when
violated. The projection is a declared MODEL_CHOICE; its magnitude is
reported per target, not hidden. No offset regulariser is added: a contact
without a level reading in the weighted support is returned as NaN.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib

import numpy as np

from .constrained_geology import support_weights

PARAMETERIZATIONS = ("LEVEL", "SURFACE")


@dataclass
class SupportSet:
    """Observations of physical boreholes in one height frame.

    ``depth_lower``/``depth_upper`` are depths below the collar (m, positive
    down); NaN means unbounded; both NaN means no reading. ``groups`` joins
    rows that are copies of one physical object: a fold withholds the group.
    """

    ids: list
    groups: list
    xy: np.ndarray
    collar: np.ndarray
    depth_lower: np.ndarray
    depth_upper: np.ndarray
    contacts: list
    thickness: list = field(default_factory=list)  # dict(row, top, bottom, lower, upper)
    independent_collar: np.ndarray | None = None

    def __post_init__(self):
        self.xy = np.asarray(self.xy, float).reshape(-1, 2)
        self.collar = np.asarray(self.collar, float).ravel()
        self.depth_lower = np.asarray(self.depth_lower, float)
        self.depth_upper = np.asarray(self.depth_upper, float)
        n, k = len(self.xy), len(self.contacts)
        if self.depth_lower.shape != (n, k) or self.depth_upper.shape != (n, k):
            raise ValueError("one bound pair per support and contact is required")
        if len(self.ids) != n or len(self.groups) != n or self.collar.shape != (n,):
            raise ValueError("ids, groups and collars must match supports")
        if len(set(self.ids)) != n:
            raise ValueError("collapse physical duplicate readings before prediction")
        if not np.isfinite(self.xy).all() or not np.isfinite(self.collar).all():
            raise ValueError("finite placement and collar required")
        both = np.isfinite(self.depth_lower) & np.isfinite(self.depth_upper)
        if np.any(self.depth_lower[both] > self.depth_upper[both] + 1e-9):
            raise ValueError("reversed depth interval")
        point = self.point_mask()
        for row in range(n):
            d = self.depth_lower[row, point[row]]
            if np.any(np.diff(d) < -1e-9):
                raise ValueError("crossing source contacts: retain the conflict before interpolation")
        for t in self.thickness:
            if not (0 <= t["row"] < n and 0 <= t["top"] < t["bottom"] < k):
                raise ValueError("thickness reading must name one support and two ordered contacts")
        if self.independent_collar is not None:
            self.independent_collar = np.asarray(self.independent_collar, float).ravel()
            if self.independent_collar.shape != (n,):
                raise ValueError("one independent collar value (or NaN) per support")

    @classmethod
    def from_depths(cls, ids, xy, collar, depths, contacts, groups=None, **kw):
        depths = np.asarray([[np.nan if v is None else v for v in row] for row in depths], float)
        return cls(list(ids), list(groups or ids), xy, collar, depths, depths.copy(), list(contacts), **kw)

    def point_mask(self):
        return np.isfinite(self.depth_lower) & np.isfinite(self.depth_upper) & (np.abs(self.depth_upper-self.depth_lower) <= 1e-9)

    def complete_rows(self):
        return self.point_mask().all(axis=1)

    def has_partial_information(self):
        point = self.point_mask()
        interval = (np.isfinite(self.depth_lower) | np.isfinite(self.depth_upper)) & ~point
        return bool((~point).any() and (interval.any() or point.any())) or bool(self.thickness)

    def levels(self):
        """Observed point levels collar - depth; NaN where no point reading."""
        out = np.full(self.depth_lower.shape, np.nan)
        point = self.point_mask()
        out[point] = (self.collar[:, None] - self.depth_lower)[point]
        return out

    def subset(self, mask):
        mask = np.asarray(mask, bool)
        index = np.full(len(mask), -1)
        index[mask] = np.arange(int(mask.sum()))
        thickness = [dict(t, row=int(index[t["row"]])) for t in self.thickness if mask[t["row"]]]
        return SupportSet([i for i, m in zip(self.ids, mask) if m], [g for g, m in zip(self.groups, mask) if m],
                          self.xy[mask], self.collar[mask], self.depth_lower[mask], self.depth_upper[mask],
                          list(self.contacts), thickness,
                          None if self.independent_collar is None else self.independent_collar[mask])


@dataclass(frozen=True)
class PredictorSpec:
    method: str
    parameterization: str
    site_offset_m: float = 0.0
    extrapolate: bool = False
    allow_partial: bool = False

    def __post_init__(self):
        if self.parameterization not in PARAMETERIZATIONS:
            raise ValueError("parameterization must be LEVEL or SURFACE")
        if self.method not in ("nearest", "idw2", "linear"):
            raise ValueError("unsupported method")
        if self.extrapolate and self.method == "linear":
            raise ValueError("linear extrapolation can invert layers")
        if self.allow_partial and self.method != "idw2":
            # Nearest/linear weights concentrate on one or three supports; a missing
            # reading there leaves the contact undetermined, not interpolated.
            raise ValueError("partial observations are supported with common IDW2 weights only")

    def describe(self):
        return {"method": self.method, "parameterization": self.parameterization, "site_offset_m": self.site_offset_m,
                "extrapolate": self.extrapolate, "allow_partial": self.allow_partial,
                "operator": "Z(q)=S(q)+argmin_p sum_i w_i(q) sum_obs dist(p,obs_i)^2, p ordered" if self.allow_partial
                else "Z(q)=S(q)+sum_i w_i(q) p_i"}


class ContactPredictor:
    """Declared predictor: method, parameterization, surface operator, support and extrapolation."""

    def __init__(self, spec, surface=None, support=None):
        if spec.parameterization == "SURFACE" and surface is None:
            raise ValueError("SURFACE parameterization requires a surface operator")
        self.spec, self.surface, self.support = spec, surface, support

    def surface_at(self, xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        if self.spec.parameterization == "LEVEL":
            return np.zeros(len(xy))
        return np.asarray(self.surface(xy), float) + self.spec.site_offset_m

    def parameter_bounds(self, supports):
        s = self.surface_at(supports.xy)[:, None]
        # deeper bound -> lower level
        lo = supports.collar[:, None] - supports.depth_upper - s
        hi = supports.collar[:, None] - supports.depth_lower - s
        lo[~np.isfinite(supports.depth_upper)] = -np.inf
        hi[~np.isfinite(supports.depth_lower)] = np.inf
        return lo, hi

    def predict(self, supports, targets):
        q = np.asarray(targets, float).reshape(-1, 2)
        point = supports.point_mask()
        complete = bool(point.all()) and not supports.thickness
        if not complete and not self.spec.allow_partial:
            raise ValueError("predictor declared for complete columns received partial observations")
        s_sup = self.surface_at(supports.xy)
        if not np.isfinite(s_sup).all():
            raise ValueError("surface operator undefined at a support")
        lo, hi = self.parameter_bounds(supports)
        weights, masks = support_weights(supports.xy, q, self.spec.method, self.support, extrapolate=self.spec.extrapolate)
        k = len(supports.contacts)
        p = np.full((len(q), k), np.nan)
        adjusted = np.zeros(len(q))
        known = masks != "UNKNOWN"
        if complete:
            p[known] = weights[known] @ lo
        else:
            # At a support its own readings dominate (they are reproduced), while
            # contacts it did not record come from the other supports: the limit
            # of the IDW estimate as q approaches the support.
            for i in np.flatnonzero(masks == "DIRECT_CONSTRAINT"):
                j = int(np.argmax(weights[i]))
                others = np.ones(len(supports.ids), bool); others[j] = False
                if others.any():
                    # The target is the support itself (inside the full support hull), so the
                    # limit uses the other supports' IDW weights even where x_j lies outside
                    # their own hull; the support-polygon visibility rule still applies.
                    w_other, m_other = support_weights(supports.xy[others], q[[i]], self.spec.method, self.support,
                                                       extrapolate=self.spec.method != "linear")
                    weights[i] = 0.0
                    if m_other[0] != "UNKNOWN":
                        weights[i, others] = w_other[0]
                weights[i, j] = 1e12
            for i in np.flatnonzero(known):
                p[i], adjusted[i] = _solve_node(weights[i], lo, hi, supports.thickness, k)
        s_q = self.surface_at(q)
        z = s_q[:, None] + p
        undetermined = known & ~np.isfinite(z).all(axis=1)
        return {"levels": z, "parameter": p, "surface": s_q, "masks": masks, "weights": weights,
                "constraint_adjustment_m": adjusted, "undetermined_contact_rows": int(undetermined.sum())}


def _solve_node(w, lo, hi, thickness, k):
    """Ordered weighted least squares at one target in increment (isopach) form.

    Each support contributes the level misfit of its shallowest point reading
    and the misfit of every gap between consecutive point readings (a gap may
    span unrecorded contacts: only their sum is observed). For a complete
    column this metric is an invertible transform of the level misfit, so the
    minimiser is the same common-weight mean; for partial columns a missing
    contact inherits the local thickness pattern instead of being interpolated
    independently. Variables (p_0, t_j >= 0), so zero thickness is allowed and
    order is exact. Interval readings enter only while violated; thickness
    readings (incl. observed absence) likewise, through bounded auxiliary
    variables in one exact bounded least-squares call. A contact outside the row
    space of the weighted readings is returned as NaN (no regulariser).
    """
    from scipy.optimize import lsq_linear

    a = np.zeros((k, k))
    a[:, 0] = 1
    for c in range(1, k):
        a[c, 1:c+1] = -1
    use = w > 0
    point = use[:, None] & np.isfinite(lo) & np.isfinite(hi) & (np.abs(hi-lo) <= 1e-9)
    interval = use[:, None] & (np.isfinite(lo) | np.isfinite(hi)) & ~point
    thick = [t for t in thickness if w[t["row"]] > 0]
    base_rows, base_b = [], []
    for r in np.flatnonzero(point.any(axis=1)):
        obs = np.flatnonzero(point[r])
        sw = np.sqrt(w[r])
        base_rows.append(sw*a[obs[0]]); base_b.append(sw*lo[r, obs[0]])
        for ca, cb in zip(obs[:-1], obs[1:]):
            base_rows.append(sw*(a[ca]-a[cb])); base_b.append(sw*(lo[r, ca]-lo[r, cb]))
    if not base_rows:
        return np.full(k, np.nan), 0.0
    exact_thick = [t for t in thick if t.get("lower") is not None and t.get("lower") == t.get("upper")]
    for t in exact_thick:
        sw = np.sqrt(w[t["row"]])
        base_rows.append(sw*(a[t["top"]]-a[t["bottom"]])); base_b.append(sw*t["lower"])
    # Determined contacts: a[k] lies in the row space of the equality readings.
    r_eq = np.asarray(base_rows)/np.maximum(np.abs(np.asarray(base_rows)).max(axis=1, keepdims=True), 1e-300)
    projector = np.linalg.pinv(r_eq) @ r_eq
    determined = np.linalg.norm(a @ projector - a, axis=1) <= 1e-8*np.sqrt(k)
    lower = np.r_[-np.inf, np.zeros(k-1)]
    upper = np.full(k, np.inf)
    raw = np.linalg.lstsq(np.asarray(base_rows), np.asarray(base_b), rcond=None)[0]
    raw_z = a @ raw
    # Exact formulation (audit F, 23.10): dist(v, [lo, hi])^2 = min_{y in [lo, hi]} (v - y)^2.
    # Every interval/soft-thickness reading gets one auxiliary bounded variable y, so the whole
    # convex problem is a single bounded least-squares call; no active-set iteration.
    soft = [t for t in thick if t not in exact_thick]
    terms = []  # (sqrt weight, coefficient row over contacts, lo, hi)
    for r, c in zip(*np.nonzero(interval)):
        terms.append((np.sqrt(w[r]), a[c], lo[r, c], hi[r, c]))
    for t in soft:
        lo_t = -np.inf if t.get("lower") is None else t["lower"]
        hi_t = np.inf if t.get("upper") is None else t["upper"]
        terms.append((np.sqrt(w[t["row"]]), a[t["top"]]-a[t["bottom"]], lo_t, hi_t))
    m = len(terms)
    if m:
        base = np.hstack([np.asarray(base_rows), np.zeros((len(base_rows), m))])
        extra = np.zeros((m, k+m))
        for j, (sw, row, _, _) in enumerate(terms):
            extra[j, :k] = sw*row
            extra[j, k+j] = -sw
        matrix = np.vstack([base, extra])
        target = np.r_[np.asarray(base_b), np.zeros(m)]
        lower_all = np.r_[lower, [tl for _, _, tl, _ in terms]]
        upper_all = np.r_[upper, [th for _, _, _, th in terms]]
        fit = lsq_linear(matrix, target, bounds=(lower_all, upper_all), method="bvls")
        z = a @ fit.x[:k]
    else:
        fit = lsq_linear(np.asarray(base_rows), np.asarray(base_b), bounds=(lower, upper), method="bvls")
        z = a @ fit.x
    if not fit.success:
        raise RuntimeError("ordered least squares failed: "+str(fit.message))
    # Order constraints can determine a contact the equality rows leave free: if the nearest determined
    # contacts above and below coincide, every ordered solution puts it there (audit Sol, 23.10.2026).
    for c in np.flatnonzero(~determined):
        above = [i for i in range(c) if determined[i]]
        below = [i for i in range(c+1, k) if determined[i]]
        if above and below and abs(z[above[-1]]-z[below[0]]) <= 1e-9:
            determined[c] = True
    z = np.where(determined, z, np.nan)
    shift = float(np.nanmax(np.abs(z-raw_z))) if np.isfinite(z).any() else 0.0
    return z, shift


def geometry_digest(nodes, triangles, contacts):
    """Version hash shared by plans, sections and exports of one geometry."""
    h = hashlib.sha256()
    for name, array, dtype in (("nodes", nodes, "<f8"), ("triangles", triangles, "<i8"), ("contacts", contacts, "<f8")):
        a = np.ascontiguousarray(np.asarray(array, dtype=dtype))
        h.update(name.encode()); h.update(str(a.shape).encode()); h.update(a.tobytes())
    return h.hexdigest()


def cv_blocks(xy, block_size_m, origin=None):
    """Fixed square blocks from coordinates only (formed before any error is seen)."""
    xy = np.asarray(xy, float)
    if not np.isfinite(block_size_m) or block_size_m <= 0:
        raise ValueError("positive block size required")
    origin = np.floor(xy.min(axis=0)/block_size_m)*block_size_m if origin is None else np.asarray(origin, float)
    cell = np.floor((xy-origin)/block_size_m).astype(int)
    return ["B%d_%d" % (a, b) for a, b in cell]


def cross_validate(predictor_factory, supports, methods, block_size_m=None, groups_for_blocks=None):
    """Same predictor (per method) for every fold; whole identity groups withheld.

    Fold kinds: leave-one-physical-borehole-out (identity group), and fixed
    spatial blocks. Each held support is classed INTERPOLATION when inside the
    training convex hull and EXTRAPOLATION otherwise; the production policy
    of the method decides whether it predicts there. Errors are evaluated at
    the held support's own readings: point levels (= minus depth error under
    the assumed collar), thicknesses (collar invariant) and interval readings.
    Independent absolute heights are scored only when supplied.
    """
    from shapely.geometry import MultiPoint, Point

    folds = [("leave_one_physical_borehole_out", g, [i for i, x in enumerate(supports.groups) if x == g])
             for g in sorted(set(supports.groups))]
    if block_size_m:
        blocks = groups_for_blocks or cv_blocks(supports.xy, block_size_m)
        for b in sorted(set(blocks)):
            members = {supports.groups[i] for i, x in enumerate(blocks) if x == b}
            held = [i for i, g in enumerate(supports.groups) if g in members]
            folds.append(("spatial_block", b, held))
    point = supports.point_mask()
    levels = supports.levels()
    records = []
    for kind, label, held in folds:
        train = np.ones(len(supports.ids), bool); train[held] = False
        if train.sum() < 1:
            continue
        training = supports.subset(train)
        hull = MultiPoint(training.xy).convex_hull if train.sum() >= 3 else None
        for method in methods:
            predictor = predictor_factory(method, training)
            try:
                result = predictor.predict(training, supports.xy[held])
            except ValueError as error:
                for j in held:
                    inside = hull is not None and hull.buffer(1e-6).covers(Point(supports.xy[j]))
                    records.append({"fold": kind, "label": label, "method": method, "well": supports.ids[j],
                                    "training_wells": int(train.sum()), "position": "INTERPOLATION" if inside else "EXTRAPOLATION",
                                    "status": "NOT_APPLICABLE", "reason": str(error), "predicted_contacts": 0})
                continue
            for r, j in enumerate(held):
                z = result["levels"][r]
                inside = hull is not None and hull.buffer(1e-6).covers(Point(supports.xy[j]))
                obs = point[j]
                ok = np.isfinite(z)
                rec = {"fold": kind, "label": label, "method": method, "well": supports.ids[j],
                       "training_wells": int(train.sum()), "position": "INTERPOLATION" if inside else "EXTRAPOLATION",
                       "status": str(result["masks"][r]), "constraint_adjustment_m": float(result["constraint_adjustment_m"][r])}
                comp = obs & ok
                rec["predicted_contacts"] = int(comp.sum()); rec["observed_contacts"] = int(obs.sum())
                # A held support with only interval/thickness readings is still a prediction (review Codex, 23.10):
                # it counts as predicted and is scored through its interval and thickness checks.
                rec["prediction_finite"] = bool(ok.any())
                rec["level_error_m"] = [float(z[c]-levels[j, c]) if comp[c] else None for c in range(len(z))]
                thick = []
                for c in range(len(z)-1):
                    if obs[c] and obs[c+1] and ok[c] and ok[c+1]:
                        thick.append(float((z[c]-z[c+1])-(levels[j, c]-levels[j, c+1])))
                    else:
                        thick.append(None)
                rec["thickness_error_m"] = thick
                # Held interval readings: violation (m) of the prediction against the reading, 0 when satisfied.
                lo_j, hi_j = supports.depth_lower[j], supports.depth_upper[j]
                interval = (np.isfinite(lo_j) | np.isfinite(hi_j)) & ~obs
                depth_pred = supports.collar[j] - z
                viol = []
                for c in np.flatnonzero(interval):
                    if not ok[c]:
                        viol.append({"contact": supports.contacts[c], "violation_m": None})
                        continue
                    v = max((lo_j[c]-depth_pred[c]) if np.isfinite(lo_j[c]) else 0.0,
                            (depth_pred[c]-hi_j[c]) if np.isfinite(hi_j[c]) else 0.0, 0.0)
                    viol.append({"contact": supports.contacts[c], "violation_m": float(v)})
                rec["interval_violations"] = viol
                thick_read = []
                for t in supports.thickness:
                    if t["row"] != j:
                        continue
                    if ok[t["top"]] and ok[t["bottom"]]:
                        val = float(z[t["top"]]-z[t["bottom"]])
                        lo_t = t.get("lower"); hi_t = t.get("upper")
                        v = max((lo_t-val) if lo_t is not None else 0.0, (val-hi_t) if hi_t is not None else 0.0, 0.0)
                        thick_read.append({"top": supports.contacts[t["top"]], "bottom": supports.contacts[t["bottom"]],
                                           "predicted_m": val, "violation_m": float(v)})
                    else:
                        thick_read.append({"top": supports.contacts[t["top"]], "bottom": supports.contacts[t["bottom"]],
                                           "predicted_m": None, "violation_m": None})
                rec["thickness_reading_checks"] = thick_read
                if supports.independent_collar is not None and np.isfinite(supports.independent_collar[j]):
                    rec["independent_level_error_m"] = [float(z[c]-(supports.independent_collar[j]-supports.depth_lower[j, c]))
                                                        if comp[c] else None for c in range(len(z))]
                records.append(rec)
    return {"records": records, "summary": summarize_cv(records, methods, len(supports.contacts)),
            "independent_absolute_check_wells": int(np.isfinite(supports.independent_collar).sum()) if supports.independent_collar is not None else 0,
            "independent_field_validation": False}


def summarize_cv(records, methods, k):
    def scores(rows, key, width):
        cols = []
        for c in range(width):
            e = np.array([r[key][c] for r in rows if r.get(key) and r[key][c] is not None])
            cols.append(None if not len(e) else {"n": int(len(e)), "bias_m": float(e.mean()), "mae_m": float(np.abs(e).mean()),
                                                 "rmse_m": float(np.sqrt(np.mean(e**2))), "max_abs_m": float(np.abs(e).max())})
        return cols
    summary = {}
    for kind in sorted({r["fold"] for r in records}):
        for position in ("INTERPOLATION", "EXTRAPOLATION"):
            subset = [r for r in records if r["fold"] == kind and r.get("position") == position]
            if not subset:
                continue
            own = {m: {r["well"] for r in subset if r["method"] == m and r.get("prediction_finite")} for m in methods}
            common = set.intersection(*own.values()) if own else set()
            for m in methods:
                rows = [r for r in subset if r["method"] == m]
                predicted = [r for r in rows if r.get("prediction_finite")]
                checks = [v["violation_m"] for r in predicted for v in r.get("interval_violations", []) + r.get("thickness_reading_checks", [])
                          if v.get("violation_m") is not None]
                summary[f"{kind}/{position}/{m}"] = {
                    "attempted": len(rows), "predicted": len(predicted), "refused": len(rows)-len(predicted),
                    "own_coverage_wells": sorted(own[m]), "common_coverage_wells": sorted(common),
                    "own_level_scores": scores(predicted, "level_error_m", k),
                    "common_level_scores": scores([r for r in predicted if r["well"] in common], "level_error_m", k),
                    "common_thickness_scores": scores([r for r in predicted if r["well"] in common], "thickness_error_m", k-1),
                    "independent_collar_level_scores": scores([r for r in predicted if r.get("independent_level_error_m")], "independent_level_error_m", k),
                    "interval_and_thickness_checks": {"n": len(checks), "violated": int(sum(v > 1e-6 for v in checks)),
                                                      "max_violation_m": float(max(checks)) if checks else None}}
    return summary
