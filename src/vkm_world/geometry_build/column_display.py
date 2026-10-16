"""Explicit column translation for a joint display with an unresolved Z tie.

This is an engineering display assumption, not a datum transformation. The
same translation applies to every contact and nested object at a given XY.
"""
from __future__ import annotations

import numpy as np


def column_translation(native_top, surface):
    native_top = np.asarray(native_top, dtype=np.float64)
    surface = np.asarray(surface, dtype=np.float64)
    if native_top.shape != surface.shape or not np.isfinite(native_top).all() or not np.isfinite(surface).all():
        raise ValueError("matching finite native top and display surface required")
    return surface - native_top


def translate_contacts(contacts, delta):
    contacts = np.asarray(contacts, dtype=np.float64)
    delta = np.asarray(delta, dtype=np.float64)
    if contacts.ndim != 2 or delta.shape != contacts.shape[1:]:
        raise ValueError("one translation per column required")
    if not np.isfinite(contacts).all() or not np.isfinite(delta).all() or np.any(np.diff(contacts, axis=0) <= 0):
        raise ValueError("finite ordered contacts required")
    return contacts + delta[None, :]


class TINSurface:
    """Piecewise linear interpolation on the supplied TIN, not a new triangulation."""

    def __init__(self, points, triangles):
        from matplotlib.tri import Triangulation

        self.points = np.asarray(points, dtype=np.float64)
        self.triangles = np.asarray(triangles, dtype=np.int64).copy()
        if self.points.ndim != 2 or self.points.shape[1] != 3 or not np.isfinite(self.points).all():
            raise ValueError("finite XYZ vertices required")
        if self.triangles.ndim != 2 or self.triangles.shape[1] != 3 or self.triangles.min() < 0 or self.triangles.max() >= len(self.points):
            raise ValueError("valid triangular connectivity required")
        p = self.points[self.triangles]
        cross = (p[:, 1, 0]-p[:, 0, 0])*(p[:, 2, 1]-p[:, 0, 1])-(p[:, 2, 0]-p[:, 0, 0])*(p[:, 1, 1]-p[:, 0, 1])
        if np.any(cross == 0):
            raise ValueError("degenerate plan triangle")
        self.triangles[cross < 0] = self.triangles[cross < 0][:, [0, 2, 1]]
        self.finder = Triangulation(self.points[:, 0], self.points[:, 1], self.triangles).get_trifinder()

    def sample(self, xy, boundary_tolerance_m=0.002, z_values=None):
        """Return NaN outside the source support; only tiny boundary roundoff snaps."""
        xy = np.asarray(xy, dtype=np.float64)
        if xy.ndim != 2 or xy.shape[1] != 2 or not np.isfinite(xy).all():
            raise ValueError("finite query XY required")
        ids = self.finder(xy[:, 0], xy[:, 1])
        inside = ids >= 0
        result = np.full(len(xy), np.nan)
        p = self.points[self.triangles[ids[inside]]].copy()
        if z_values is not None:
            z_values = np.asarray(z_values, dtype=np.float64)
            if z_values.shape != (len(self.points),) or not np.isfinite(z_values).all():
                raise ValueError("one finite scalar per source vertex required")
            p[:, :, 2] = z_values[self.triangles[ids[inside]]]
        q = xy[inside]-p[:, 0, :2]
        ab = p[:, 1, :2]-p[:, 0, :2]
        ac = p[:, 2, :2]-p[:, 0, :2]
        det = ab[:, 0]*ac[:, 1]-ab[:, 1]*ac[:, 0]
        u = (q[:, 0]*ac[:, 1]-q[:, 1]*ac[:, 0])/det
        v = (ab[:, 0]*q[:, 1]-ab[:, 1]*q[:, 0])/det
        result[inside] = p[:, 0, 2]+u*(p[:, 1, 2]-p[:, 0, 2])+v*(p[:, 2, 2]-p[:, 0, 2])
        if np.any(~inside) and boundary_tolerance_m > 0:
            # A vertex-only nearest-neighbour fallback would miss roundoff at
            # the middle of a long boundary edge. Project onto boundary edges
            # and accept only a sub-millimetre/numerical deviation.
            if not hasattr(self, "boundary"):
                edges = np.sort(np.concatenate([self.triangles[:, [0, 1]], self.triangles[:, [1, 2]], self.triangles[:, [2, 0]]]), axis=1)
                edges, counts = np.unique(edges, axis=0, return_counts=True)
                self.boundary = edges[counts == 1]
            a = self.points[self.boundary[:, 0]]
            b = self.points[self.boundary[:, 1]]
            vector = b[:, :2]-a[:, :2]
            denominator = np.sum(vector*vector, axis=1)
            outside_ids = np.flatnonzero(~inside)
            az = a[:, 2] if z_values is None else z_values[self.boundary[:, 0]]
            bz = b[:, 2] if z_values is None else z_values[self.boundary[:, 1]]
            for start in range(0, len(outside_ids), 128):
                query_ids = outside_ids[start:start+128]
                projection = np.clip(np.sum((xy[query_ids, None, :]-a[None, :, :2])*vector[None, :, :], axis=2)/denominator, 0, 1)
                projected = a[None, :, :2]+projection[:, :, None]*vector[None, :, :]
                distance2 = np.sum((xy[query_ids, None, :]-projected)**2, axis=2)
                nearest = distance2.argmin(axis=1)
                row = np.arange(len(query_ids))
                use = distance2[row, nearest] <= boundary_tolerance_m**2
                result[query_ids[use]] = (az[nearest]+projection[row, nearest]*(bz[nearest]-az[nearest]))[use]
        return result
