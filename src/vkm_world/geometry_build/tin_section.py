"""Exact sections of an exported P1 contact TIN along a polyline trace.

Review finding R2-03: a section recomputed with the continuous interpolator
may differ from the exported piecewise-linear geometry between nodes. This
module intersects the trace with the exported triangles themselves (no new
triangulation) and keeps every edge crossing, so the section is the exact
P1 restriction of the exported surfaces. Outside the mesh the section is NaN.
"""
from __future__ import annotations

import numpy as np


def _edges(triangles):
    e = np.concatenate([triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]])
    return np.unique(np.sort(e, axis=1), axis=0)


def _segment_parameters(a, b, nodes, edges, eps=1e-12):
    """Parameters t in [0,1] where segment a->b crosses mesh edges or passes vertices."""
    d = b - a
    p, q = nodes[edges[:, 0]], nodes[edges[:, 1]]
    e = q - p
    denom = d[0]*e[:, 1] - d[1]*e[:, 0]
    ap = p - a
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (ap[:, 0]*e[:, 1] - ap[:, 1]*e[:, 0]) / denom
        s = (ap[:, 0]*d[1] - ap[:, 1]*d[0]) / denom
    hit = (np.abs(denom) > eps) & (t >= -1e-12) & (t <= 1+1e-12) & (s >= -1e-12) & (s <= 1+1e-12)
    ts = list(np.clip(t[hit], 0, 1))
    # collinear edges: add their endpoints projected on the segment
    length2 = float(d @ d)
    if length2 > 0:
        col = (np.abs(denom) <= eps) & (np.abs(ap[:, 0]*d[1]-ap[:, 1]*d[0]) <= 1e-9*np.sqrt(length2))
        for pt in np.concatenate([p[col], q[col]]):
            tt = float((pt-a) @ d / length2)
            if -1e-12 <= tt <= 1+1e-12:
                ts.append(min(max(tt, 0.), 1.))
    ts += [0.0, 1.0]
    return np.unique(np.round(np.asarray(ts), 12))


def _locate(nodes, triangles, points):
    from matplotlib.tri import Triangulation
    finder = Triangulation(nodes[:, 0], nodes[:, 1], triangles).get_trifinder()
    return finder(points[:, 0], points[:, 1])


def _barycentric(nodes, tri, points):
    p = nodes[tri]
    v0, v1 = p[:, 1]-p[:, 0], p[:, 2]-p[:, 0]
    v2 = points-p[:, 0]
    det = v0[:, 0]*v1[:, 1]-v0[:, 1]*v1[:, 0]
    u = (v2[:, 0]*v1[:, 1]-v2[:, 1]*v1[:, 0])/det
    v = (v0[:, 0]*v2[:, 1]-v0[:, 1]*v2[:, 0])/det
    return np.column_stack([1-u-v, u, v])


def section_through_tin(nodes, triangles, values, polyline):
    """Exact P1 section. Returns dict with chainage, xy, values (m, k), segment, triangle.

    Breakpoints: polyline vertices, every crossing of a mesh edge and every
    mesh vertex on the trace. Values between breakpoints are linear (exact P1).
    """
    nodes = np.asarray(nodes, float)
    triangles = np.asarray(triangles, int)
    values = np.asarray(values, float)
    if values.ndim == 1:
        values = values[:, None]
    line = np.asarray(polyline, float)
    if line.ndim != 2 or line.shape[1] != 2 or len(line) < 2:
        raise ValueError("polyline of at least two XY vertices required")
    if values.shape[0] != len(nodes):
        raise ValueError("one value row per mesh node required")
    edges = _edges(triangles)
    xy, chain, seg = [], [], []
    start = 0.0
    for s, (a, b) in enumerate(zip(line[:-1], line[1:])):
        length = float(np.linalg.norm(b-a))
        if length == 0:
            continue
        ts = _segment_parameters(a, b, nodes, edges)
        if s > 0:
            ts = ts[ts > 0]  # shared turning vertex once
        xy.extend(a+(b-a)*t for t in ts)
        chain.extend(start+t*length for t in ts)
        seg.extend([s]*len(ts))
        start += length
    xy = np.asarray(xy)
    chain = np.asarray(chain)
    # Evaluate each breakpoint inside a triangle chosen by the adjacent interval midpoint,
    # so a point exactly on an edge uses a triangle that contains it.
    out = np.full((len(xy), values.shape[1]), np.nan)
    tri_of = _locate(nodes, triangles, xy)
    mids = 0.5*(xy[:-1]+xy[1:])
    tri_mid = _locate(nodes, triangles, mids) if len(mids) else np.array([], int)
    for i in range(len(xy)):
        candidates = [tri_of[i]]
        if i > 0:
            candidates.append(tri_mid[i-1])
        if i < len(mids):
            candidates.append(tri_mid[i])
        for t in candidates:
            if t is not None and t >= 0:
                bary = _barycentric(nodes, triangles[[t]], xy[[i]])[0]
                if bary.min() >= -1e-7:
                    out[i] = bary @ values[triangles[t]]
                    tri_of[i] = t
                    break
    # Roundoff just outside the mesh boundary: accept a containing triangle within 1e-9 relative tolerance.
    missing = np.flatnonzero(~np.isfinite(out).all(axis=1))
    if len(missing):
        scale = max(float(np.ptp(nodes[:, 0])), float(np.ptp(nodes[:, 1])), 1.0)
        for i in missing:
            bary = _barycentric(nodes, triangles, np.repeat(xy[[i]], len(triangles), axis=0))
            t = int(np.argmax(bary.min(axis=1)))
            if bary[t].min() >= -1e-9*scale:
                out[i] = np.clip(bary[t], 0, None)/np.clip(bary[t], 0, None).sum() @ values[triangles[t]]
                tri_of[i] = t
    # An interval whose midpoint lies outside the mesh is a gap (hole/notch): consumers must break the line.
    gap_after = np.zeros(len(xy), bool)
    if len(mids):
        gap_after[:-1] = tri_mid < 0
    return {"chainage_m": chain, "xy": xy, "values": out, "segment": np.asarray(seg), "triangle": tri_of,
            "gap_after": gap_after, "gaps": int(gap_after.sum()),
            "breakpoints": int(len(xy)), "operator": "EXACT_P1_ON_EXPORTED_TIN"}


def project_onto_trace(points, polyline):
    """Distance of each point to the trace and chainage of its orthogonal projection."""
    line = np.asarray(polyline, float)
    pts = np.asarray(points, float).reshape(-1, 2)
    best = np.full(len(pts), np.inf); chain = np.full(len(pts), np.nan); start = 0.
    for a, b in zip(line[:-1], line[1:]):
        d = b-a; length = float(np.linalg.norm(d))
        if length == 0:
            continue
        t = np.clip(((pts-a) @ d)/length**2, 0, 1)
        dist = np.linalg.norm(pts-(a+t[:, None]*d), axis=1)
        better = dist < best
        best[better] = dist[better]; chain[better] = start+t[better]*length
        start += length
    return {"distance_m": best, "chainage_m": chain}


def compare_continuous(section, continuous_values):
    """Difference between the exported P1 section and a continuous model at the same breakpoints.

    This is measured ON THE LISTED SAMPLE POINTS only; it is not a supremum.
    """
    diff = np.asarray(continuous_values, float) - section["values"]
    ok = np.isfinite(diff)
    return {"sampled_points": int(ok.any(axis=1).sum()) if diff.ndim > 1 else int(ok.sum()),
            "max_abs_m_on_samples": float(np.nanmax(np.abs(diff))) if ok.any() else None,
            "mean_abs_m_on_samples": float(np.nanmean(np.abs(diff))) if ok.any() else None,
            "scope": "sampled breakpoints and any extra supplied samples; not a supremum over the mesh"}
