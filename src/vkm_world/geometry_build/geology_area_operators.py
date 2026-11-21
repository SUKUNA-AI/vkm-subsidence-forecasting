"""Analytic area averages for common P1 geological fields."""

from __future__ import annotations

import numpy as np
from shapely import STRtree
from shapely.geometry import Polygon


class AreaOperators:
    """Integrate affine nodal basis functions over clipped triangular cells."""

    def __init__(self, xy, triangles):
        self.xy = np.asarray(xy, dtype=np.float64)
        self.triangles = np.asarray(triangles, dtype=np.int64)
        self.cells = [Polygon(self.xy[face]) for face in self.triangles]
        self.index = STRtree(self.cells)

    def stencil(self, polygon, relative_tolerance=1e-8):
        if not polygon.is_valid or polygon.is_empty or polygon.area <= 0:
            raise ValueError("Areal operator requires qualified nonempty input geometry")
        cells = sorted(self.index.query(polygon, predicate="intersects"))
        weights = np.zeros(len(self.xy), dtype=np.float64)
        included = 0.
        for index in cells:
            intersection = polygon.intersection(self.cells[index])
            area = intersection.area
            if area <= 0.:
                continue
            face = self.triangles[index]
            points = self.xy[face]
            a, b = np.linalg.solve((points[1:] - points[0]).T,
                                   np.asarray(intersection.centroid.coords[0]) - points[0])
            basis = np.array([1. - a - b, a, b])
            if np.any(basis < -1e-10):
                raise ValueError("Areal weight outside the child affine simplex")
            np.add.at(weights, face, np.where(basis < 0., 0., basis) * area)
            included += area
        if not np.isclose(included, polygon.area, rtol=relative_tolerance, atol=1e-8):
            return None, included
        np.testing.assert_allclose(weights.sum(), included, rtol=relative_tolerance, atol=1e-8)
        keep = np.flatnonzero(weights)
        return (keep.tolist(), (weights[keep] / weights.sum()).tolist()), included
