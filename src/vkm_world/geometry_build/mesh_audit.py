"""Independent topology checks for plan triangles and vertical wedge exports.

These checks establish numerical representation properties, not geological
accuracy or solver readiness. Tolerances are coordinate-unit distances, not
field uncertainty. The wedge audit deliberately rejects nonvertical prisms.
"""
from __future__ import annotations

from collections import Counter

import numpy as np
from shapely import STRtree, points as geometry_points, union_all
from shapely.geometry import LineString, Polygon, box


def _array(values, shape, name):
    result = np.asarray(values, dtype=float)
    if result.ndim != len(shape) or result.shape[1:] != shape[1:] or not len(result):
        raise ValueError(f"nonempty {name} with shape {shape} required")
    if not np.isfinite(result).all():
        raise ValueError(f"finite {name} required")
    return result


def audit_plan_triangles(triangles, polygon, tolerance=1e-7):
    """Count hanging nodes, false boundaries, overlaps, gaps and bad winding.

    An indexed geometric test checks every unique edge for vertices strictly
    inside it. Area and edge-incidence alone cannot detect a T-junction.
    """
    triangles = _array(triangles, (None, 3, 2), "triangles")
    if not np.isfinite(tolerance) or tolerance <= 0 or not polygon.is_valid or polygon.is_empty:
        raise ValueError("valid footprint and finite positive tolerance required")
    xy, inverse = np.unique(triangles.reshape(-1, 2), axis=0, return_inverse=True)
    cells = inverse.reshape(-1, 3)
    raw_edges = cells[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    edges, counts = np.unique(np.sort(raw_edges, axis=1), axis=0, return_counts=True)
    a = triangles[:, 1]-triangles[:, 0]
    b = triangles[:, 2]-triangles[:, 0]
    twice_area = a[:, 0]*b[:, 1]-a[:, 1]*b[:, 0]
    tree = STRtree(geometry_points(xy))
    hanging = 0
    for i, j in edges:
        start, end = xy[[i, j]]
        vector = end-start
        length = float(np.linalg.norm(vector))
        if length <= tolerance:
            continue
        lo, hi = np.minimum(start, end)-tolerance, np.maximum(start, end)+tolerance
        candidates = tree.query(box(*lo, *hi))
        candidates = candidates[(candidates != i) & (candidates != j)]
        offset = xy[candidates]-start
        along = offset@vector/length
        perpendicular = np.abs(offset[:, 0]*vector[1]-offset[:, 1]*vector[0])/length
        hanging += int(np.sum((along > tolerance) & (along < length-tolerance) &
                              (perpendicular <= tolerance)))
    boundary_band = polygon.boundary.buffer(tolerance)
    false_boundary = sum(LineString(xy[e]).difference(boundary_band).length > tolerance
                         for e in edges[counts == 1])
    polygons = [Polygon(t) for t in triangles]
    rebuilt = union_all(polygons)
    summed_area = float(np.abs(twice_area).sum()/2)
    area_tolerance = max(tolerance*float(polygon.length), tolerance**2)
    overlap_area = max(0., summed_area-float(rebuilt.area))
    missing_area = float(polygon.difference(rebuilt).area)
    outside_area = float(rebuilt.difference(polygon).area)
    duplicate_triangles = len(cells)-len(np.unique(np.sort(cells, axis=1), axis=0))
    result = {
        "triangles": len(triangles), "vertices": len(xy), "edges": len(edges),
        "boundary_edges": int(np.sum(counts == 1)), "hanging_node_edge_pairs": hanging,
        "false_boundary_edges": int(false_boundary), "nonmanifold_edges": int(np.sum(counts > 2)),
        "duplicate_triangles": duplicate_triangles, "nonpositive_triangles": int(np.sum(twice_area <= 0)),
        "summed_area": summed_area, "source_area": float(polygon.area),
        "overlap_area": overlap_area, "missing_area": missing_area, "outside_area": outside_area,
        "coordinate_tolerance": tolerance, "area_tolerance": area_tolerance,
    }
    result["conforming"] = bool(not any(result[key] for key in (
        "hanging_node_edge_pairs", "false_boundary_edges", "nonmanifold_edges",
        "duplicate_triangles", "nonpositive_triangles")) and
        max(overlap_area, missing_area, outside_area) <= area_tolerance)
    return result


def _wedge_arrays(points, wedges):
    points = _array(points, (None, 3), "points")
    cells = np.asarray(wedges)
    if cells.ndim != 2 or cells.shape[1] != 6 or not len(cells) or cells.dtype.kind not in "iu":
        raise ValueError("nonempty integer wedge connectivity required")
    if cells.min() < 0 or cells.max() >= len(points):
        raise ValueError("wedge connectivity outside points")
    return points, cells


def _oriented_key(face):
    face = tuple(int(x) for x in face)
    return min(face[index:]+face[:index] for index in range(len(face)))


def audit_vertical_wedges(points, wedges, polygon, *, winding="ccw", faces=None, tolerance=1e-7):
    """Audit one vertical wedge layer in mathematical or exported OGS order.

    ``winding='ogs'`` explicitly selects clockwise lower triangles used by the
    existing writer. No absolute-volume fallback conceals inverted cells.
    """
    points, cells = _wedge_arrays(points, wedges)
    if winding not in ("ccw", "ogs"):
        raise ValueError("winding must be ccw or ogs")
    if winding == "ogs":
        cells = cells[:, [0, 2, 1, 3, 5, 4]]
    vertices = points[cells]
    vertical = np.max(np.abs(vertices[:, :3, :2]-vertices[:, 3:, :2]), axis=(1, 2)) <= tolerance
    heights = vertices[:, 3:, 2]-vertices[:, :3, 2]
    a = vertices[:, 1, :2]-vertices[:, 0, :2]
    b = vertices[:, 2, :2]-vertices[:, 0, :2]
    signed_area = (a[:, 0]*b[:, 1]-a[:, 1]*b[:, 0])/2
    volumes = signed_area*heights.mean(axis=1)
    plan = vertices[:, :3, :2]
    # Audit each material layer separately, then audit_layer_interface between
    # them. Repeated XY columns within one layer are ambiguous and fail closed.
    xy, planar_ids = np.unique(plan.reshape(-1, 2), axis=0, return_inverse=True)
    _, first = np.unique(np.sort(planar_ids.reshape(-1, 3), axis=1), axis=0, return_index=True)
    plan_audit = audit_plan_triangles(plan[np.sort(first)], polygon, tolerance)
    _, inverse = np.unique(points, axis=0, return_inverse=True)
    welded = inverse[cells]
    duplicates = len(cells)-len(np.unique(np.sort(welded, axis=1), axis=0))
    incidence = {}
    for cell in welded:
        for ids in ((0, 2, 1), (3, 4, 5), (0, 1, 4, 3), (1, 2, 5, 4), (2, 0, 3, 5)):
            face = tuple(cell[list(ids)])
            incidence.setdefault(tuple(sorted(face)), []).append(_oriented_key(face))
    inconsistent = sum(len(items) == 2 and items[0] == items[1] for items in incidence.values())
    nonmanifold = sum(len(items) > 2 for items in incidence.values())
    result = {
        "wedges": len(cells), "points": len(points), "winding": winding,
        "nonvertical_wedges": int(np.sum(~vertical)),
        "nonpositive_vertex_thickness": int(np.sum(np.any(heights <= 0, axis=1))),
        "nonpositive_oriented_volume": int(np.sum(volumes <= 0)),
        "minimum_oriented_volume": float(volumes.min()), "total_oriented_volume": float(volumes.sum()),
        "duplicate_wedges": duplicates, "nonmanifold_faces": nonmanifold,
        "repeated_plan_triangles": len(cells)-len(first),
        "same_orientation_internal_faces": inconsistent,
        "internal_faces": sum(len(items) == 2 for items in incidence.values()),
        "boundary_faces": sum(len(items) == 1 for items in incidence.values()),
        "plan": plan_audit,
    }
    result["conforming"] = bool(plan_audit["conforming"] and not any(result[key] for key in (
        "nonvertical_wedges", "nonpositive_vertex_thickness", "nonpositive_oriented_volume",
        "duplicate_wedges", "repeated_plan_triangles", "nonmanifold_faces", "same_orientation_internal_faces")))
    if faces is not None:
        faces = np.asarray(faces, dtype=int)
        if faces.ndim != 2 or faces.shape[1] != 3 or not len(faces) or faces.min() < 0 or faces.max() >= len(points):
            raise ValueError("valid nonempty triangle surface connectivity required")
        shell = inverse[faces]
        shell_edges = shell[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
        edge_counts = Counter(map(tuple, np.sort(shell_edges, axis=1)))
        oriented_edges = Counter(map(tuple, shell_edges))
        bad_winding = sum(oriented_edges[(a, b)] != oriented_edges[(b, a)] for a, b in edge_counts)
        duplicate_faces = len(shell)-len(np.unique(np.sort(shell, axis=1), axis=0))
        # Translate to a local origin to avoid cancellation at large mine XY.
        p = points[faces]-points[0]
        shell_volume = float(np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum()/6)
        result["shell"] = {
            "nonmanifold_or_open_edges": sum(count != 2 for count in edge_counts.values()),
            "inconsistent_edge_winding": bad_winding,
            "duplicate_faces": duplicate_faces, "oriented_volume": shell_volume,
            "volume_difference": abs(shell_volume-float(volumes.sum())),
        }
        result["conforming"] &= bool(not result["shell"]["nonmanifold_or_open_edges"] and not bad_winding and not duplicate_faces and
                                      np.isclose(shell_volume, volumes.sum(), rtol=1e-9, atol=tolerance**3))
    return result


def audit_layer_interface(lower_points, lower_wedges, upper_points, upper_wedges, tolerance=1e-7):
    """Compare the complete top/bottom triangulations of adjacent layer meshes.

    This is an exact XY topology contract for vertical wedge layers. Unequal
    domains or subdivisions fail; a geometric area-only match does not suffice.
    """
    lower_points, lower_wedges = _wedge_arrays(lower_points, lower_wedges)
    upper_points, upper_wedges = _wedge_arrays(upper_points, upper_wedges)

    def interface(points, cells, indices):
        result = {}
        duplicates = 0
        for triangle in points[cells[:, indices]]:
            ordered = triangle[np.lexsort((triangle[:, 1], triangle[:, 0]))]
            key = tuple(ordered[:, :2].ravel())
            duplicates += key in result
            result[key] = ordered[:, 2]
        return result, duplicates

    lower, dup_lower = interface(lower_points, lower_wedges, [3, 4, 5])
    upper, dup_upper = interface(upper_points, upper_wedges, [0, 1, 2])
    shared = lower.keys() & upper.keys()
    gaps = [float(np.max(np.abs(lower[key]-upper[key]))) for key in shared]
    maximum = max(gaps) if gaps else None
    result = {"matched_triangles": len(shared), "unmatched_lower_triangles": len(lower.keys()-upper.keys()),
              "unmatched_upper_triangles": len(upper.keys()-lower.keys()),
              "duplicate_lower_triangles": dup_lower, "duplicate_upper_triangles": dup_upper,
              "maximum_vertical_gap": maximum, "coordinate_tolerance": tolerance}
    result["conforming"] = bool(shared and not any(result[key] for key in (
        "unmatched_lower_triangles", "unmatched_upper_triangles", "duplicate_lower_triangles",
        "duplicate_upper_triangles")) and maximum <= tolerance)
    return result
