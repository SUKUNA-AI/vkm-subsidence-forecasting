"""Interval constraints and joint, order-preserving contact diagnostics.

Unknown vertical datums are free variables, never zero-valued observations.
Interpolation is restricted to the supplied support polygon (a geological choice).
All contacts share nonnegative weights; no post-fit crossing clipping is used.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linprog
from scipy.spatial import Delaunay, QhullError


def interval_envelope(n, constraints, ordered=True):
    """Find feasible extrema of upward-positive contacts, listed top to bottom.

    Each constraint is {coefficients: [...], lower: number|None,
    upper: number|None, id: ...}. No midpoint or invented variance is used.
    Unbounded extrema are None. An inconsistent system is reported, not repaired.
    """
    if n < 1:
        raise ValueError("at least one contact is required")
    a, b, rows = [], [], []
    for record in constraints:
        row = np.asarray(record["coefficients"], dtype=float)
        if row.shape != (n,) or not np.isfinite(row).all():
            raise ValueError("finite coefficients must match the contact count")
        lo, hi = record.get("lower"), record.get("upper")
        if any(v is not None and not np.isfinite(v) for v in (lo, hi)):
            raise ValueError("use None for an unbounded constraint")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("reversed interval")
        if lo is None and hi is None:
            continue
        rows.append(row)
        if hi is not None:
            a.append(row); b.append(hi)
        if lo is not None:
            a.append(-row); b.append(-lo)
    if ordered:
        for k in range(n-1):
            row = np.zeros(n); row[k+1] = 1; row[k] = -1
            a.append(row); b.append(0.)  # permits exactly absent layers
    kwargs = dict(A_ub=np.asarray(a) if a else None, b_ub=np.asarray(b) if b else None,
                  bounds=[(None, None)]*n, method="highs")
    feasible = linprog(np.zeros(n), **kwargs)
    rank = int(np.linalg.matrix_rank(np.asarray(rows))) if rows else 0
    translation_free = all(abs(float(np.sum(r))) < 1e-12 for r in rows)
    result = {"feasible": bool(feasible.success), "constraint_rank": rank,
              "contact_count": n, "global_translation_free": translation_free,
              "status": "FEASIBLE" if feasible.success else "INFEASIBLE",
              "bounds": [], "constraint_ids": [r.get("id") for r in constraints]}
    if not feasible.success:
        if feasible.status != 2:
            raise RuntimeError("constraint solver failed: "+feasible.message)
        return result
    for k in range(n):
        row = np.eye(n)[k]
        limits = []
        for direction in (1., -1.):
            fit = linprog(direction*row, **kwargs)
            if fit.status == 3:
                limits.append(None)
            elif fit.success:
                limits.append(float(direction*fit.fun))
            else:
                raise RuntimeError("constraint envelope failed: "+fit.message)
        result["bounds"].append(limits)
    return result


def support_weights(xy, targets, method, support=None, *, extrapolate=False):
    """Physical-support weights and masks; shared by every contact predictor.

    Weights depend only on support locations, the declared support polygon,
    the method and the extrapolation policy, never on contact values.
    support is a Shapely Polygon/MultiPolygon: holes and structural boundaries
    are respected; it must not span an independently established discontinuity.
    """
    xy = np.asarray(xy, dtype=float)
    q = np.asarray(targets, dtype=float).reshape(-1, 2)
    if xy.ndim != 2 or xy.shape[1] != 2 or not len(xy):
        raise ValueError("nonempty support coordinates are required")
    if not (np.isfinite(xy).all() and np.isfinite(q).all()):
        raise ValueError("finite, nonempty supports are required")
    if len(np.unique(xy, axis=0)) != len(xy):
        raise ValueError("duplicate locations must be resolved by physical identity first")
    if method not in {"nearest", "idw2", "linear"}:
        raise ValueError("unsupported joint method")
    if extrapolate and method == "linear":
        raise ValueError("linear extrapolation can invert layers; select an explicit convex extension")
    from shapely.geometry import Point, Polygon, LineString
    if support is not None and support.geom_type == "MultiPolygon":
        weights = np.zeros((len(q), len(xy)))
        masks = np.full(len(q), "UNKNOWN", dtype="U20")
        for part in support.geoms:
            si = np.array([i for i, p in enumerate(xy) if part.buffer(1e-8).covers(Point(p))], dtype=int)
            qi = np.array([i for i, p in enumerate(q) if part.buffer(1e-8).covers(Point(p))], dtype=int)
            if not len(si) or not len(qi):
                continue
            part_weights, part_masks = support_weights(xy[si], q[qi], method, part, extrapolate=extrapolate)
            masks[qi] = part_masks
            weights[np.ix_(qi, si)] = part_weights
        return weights, masks
    weights = np.zeros((len(q), len(xy)))
    masks = np.full(len(q), "UNKNOWN", dtype="U20")
    try:
        triangulation = Delaunay(xy) if len(xy) >= 3 else None
    except QhullError:
        triangulation = None
    from shapely.prepared import prep
    # Prepare once: buffering a detailed field outline for each query/support
    # pair otherwise dominates an engineering grid build without changing it.
    support_region = prep(support.buffer(1e-8)) if support is not None else None
    for i, point in enumerate(q):
        distances = np.linalg.norm(xy-point, axis=1)
        j = int(np.argmin(distances))
        if support_region is not None and not support_region.covers(Point(point)):
            continue
        if distances[j] <= 1e-8:
            weights[i, j] = 1.; masks[i] = "DIRECT_CONSTRAINT"
            continue
        cell = -1 if triangulation is None else int(triangulation.find_simplex(point))
        if cell < 0 and not extrapolate:
            continue
        if cell >= 0 and support_region is not None and not support_region.covers(Polygon(xy[triangulation.simplices[cell]])):
            continue
        if method == "nearest":
            weights[i, j] = 1.
        elif method == "idw2":
            w = distances**-2
            if support_region is not None:
                w *= np.array([support_region.covers(LineString([point, p])) for p in xy])
            if not w.sum():
                continue
            weights[i] = w/w.sum()
        else:
            transform = triangulation.transform[cell]
            uv = transform[:2].dot(point-transform[2])
            bary = np.r_[uv, 1-uv.sum()]
            if np.min(bary) < -1e-10:
                raise RuntimeError("invalid convex interpolation weights")
            # Roundoff of hull arithmetic only; never correct geological Z.
            bary = np.maximum(bary, 0.); bary /= bary.sum()
            weights[i, triangulation.simplices[cell]] = bary
        masks[i] = "EXTRAPOLATION" if cell < 0 else "INTERPOLATION"
    return weights, masks


def joint_predict(xy, contacts, targets, method, support=None, *, extrapolate=False):
    """Return contact vectors, masks and physical-support weights.

    Contacts must be complete and ordered top-to-bottom (Z upwards). An absent
    interval is allowed. Missing contacts require a separate support component
    or the partial-observation predictor in contact_predictor.py.
    """
    xy = np.asarray(xy, dtype=float)
    values = np.asarray(contacts, dtype=float)
    q = np.asarray(targets, dtype=float).reshape(-1, 2)
    if xy.ndim != 2 or xy.shape[1] != 2 or len(xy) != len(values) or values.ndim != 2:
        raise ValueError("one complete contact vector per support coordinate is required")
    if not len(xy) or not all(np.isfinite(v).all() for v in (xy, values, q)):
        raise ValueError("finite, nonempty supports are required")
    if np.any(np.diff(values, axis=1) > 1e-9):
        raise ValueError("crossing source contacts: retain the conflict before interpolation")
    weights, masks = support_weights(xy, q, method, support, extrapolate=extrapolate)
    output = np.full((len(q), values.shape[1]), np.nan)
    known = masks != "UNKNOWN"
    output[known] = weights[known] @ values
    return output, masks, weights


def grouped_contact_cv(xy, contacts, physical_ids, spatial_groups=None):
    """Pre-fixed methods on identical held-out physical groups and common support.

    All rows of a physical well are excluded together. Repeated observations
    must be deduplicated upstream; no source-copy weighting is applied here.
    """
    xy, contacts = np.asarray(xy, float), np.asarray(contacts, float)
    ids = np.asarray(physical_ids)
    if len(set(physical_ids)) != len(physical_ids):
        raise ValueError("collapse physical duplicate readings before cross-validation")
    methods = ("nearest", "idw2", "linear")
    folds = [("leave_one_physical_borehole_out", [name]) for name in sorted(set(ids))]
    if spatial_groups:
        for group in sorted(set(spatial_groups)):
            folds.append(("spatial_holdout:"+str(group), list(ids[np.asarray(spatial_groups)==group])))
    records = []
    for kind, held in folds:
        train = ~np.isin(ids, held)
        if not train.any():
            continue
        test = ~train
        for method in methods:
            prediction, masks, _ = joint_predict(xy[train], contacts[train], xy[test], method)
            for j, well in enumerate(ids[test]):
                actual = contacts[test][j]
                valid = bool(np.isfinite(prediction[j]).all())
                records.append({"fold": kind, "held_group": list(map(str, held)), "well": str(well),
                                "method": method, "training_groups": list(map(str, ids[train])),
                                "status": str(masks[j]), "error_m": (prediction[j]-actual).tolist() if valid else None,
                                "thickness_error_m": (-np.diff(prediction[j])+np.diff(actual)).tolist() if valid else None})
    summary = {}
    for kind in sorted({r["fold"] for r in records}):
        subset = [r for r in records if r["fold"]==kind]
        common = set.intersection(*[{r["well"] for r in subset if r["method"]==m and r["error_m"] is not None} for m in methods])
        for method in methods:
            rows = [r for r in subset if r["method"]==method]
            scores = {}
            for key in ("error_m", "thickness_error_m"):
                errors = np.asarray([r[key] for r in rows if r["well"] in common and r[key] is not None])
                scores[key] = {"bias_m": np.mean(errors,axis=0).tolist(), "mae_m": np.mean(abs(errors),axis=0).tolist(),
                               "rmse_m": np.sqrt(np.mean(errors**2,axis=0)).tolist(), "max_abs_m": np.max(abs(errors),axis=0).tolist()} if errors.size else None
            summary[kind+"/"+method] = {"attempted": len(rows), "predicted": sum(r["error_m"] is not None for r in rows),
                                      "common_support_wells": sorted(common), "scores_on_common_support": scores}
    return {"methods_fixed_before_scoring": list(methods), "independent_field_validation": False,
            "predictions": records, "summary": summary}
