"""Pure-Python fallback when Civil 3D is not used or not available (label: ``engine = PURE_PYTHON_FALLBACK``).

* TIN — unconstrained Delaunay of the XY positions (``scipy.spatial.Delaunay``, Qhull); duplicate XY are merged (the
  first Z wins, conflicts are reported); breakline vertices are added as points but their edges are **not** enforced
  (no constrained triangulation: a MODEL_CHOICE recorded in the result); an outer boundary drops triangles whose
  centroid is outside; ``max_triangle_length`` drops triangles with a longer edge.
* contours — marching triangles on the TIN (linear along edges), chained exactly by shared edges.
* difference — ``dz = compare − base`` sampled at the union of both vertex sets inside both TINs, triangulated again;
  cut/fill volumes integrate the linear dz over each triangle (clipped at dz = 0).
* profile — stations along a polyline at a fixed interval (and its end), elevations by barycentric interpolation.

All results are derived objects (TIN: INTERPOLATION; contours, differences, profiles: DERIVATION).
"""
from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

from vkm_cad.errors import ToolFailure

ENGINE = "PURE_PYTHON_FALLBACK"
TIN_SCHEMA = "vkm-cad.fallback_tin/1"


def _scipy_delaunay():
    try:
        from scipy.spatial import Delaunay
    except ImportError as exc:
        raise ToolFailure("FALLBACK_UNAVAILABLE", "the pure-Python fallback needs scipy (pip install scipy)") from exc
    return Delaunay


def scipy_version() -> str | None:
    try:
        import scipy
    except ImportError:
        return None
    return scipy.__version__


@dataclass
class Tin:
    name: str
    points: np.ndarray                      # (n, 3)
    triangles: np.ndarray                   # (m, 3) indices into points
    model_choices: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _index: Any = None

    # ------------------------------------------------------------------------------------------------ io
    def to_json(self) -> dict[str, Any]:
        return {"schema": TIN_SCHEMA, "name": self.name, "engine": ENGINE,
                "points": [[round(float(v), 6) for v in p] for p in self.points],
                "triangles": [[int(i) for i in t] for t in self.triangles],
                "model_choices": self.model_choices, "warnings": self.warnings}

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "Tin":
        if obj.get("schema") != TIN_SCHEMA:
            raise ToolFailure("SURFACE_NOT_FOUND", "not a fallback TIN")
        return cls(obj["name"], np.asarray(obj["points"], dtype=float).reshape(-1, 3),
                   np.asarray(obj["triangles"], dtype=int).reshape(-1, 3), list(obj.get("model_choices", [])),
                   list(obj.get("warnings", [])))

    # ------------------------------------------------------------------------------------------------ geometry
    def corners(self) -> np.ndarray:
        return self.points[self.triangles]                  # (m, 3, 3)

    def stats(self) -> dict[str, Any]:
        used = np.unique(self.triangles) if len(self.triangles) else np.arange(0)
        pts = self.points[used] if len(used) else self.points
        c = self.corners()
        area2d = area3d = 0.0
        max_edge = 0.0
        if len(c):
            a, b, d = c[:, 0], c[:, 1], c[:, 2]
            cross2 = (b[:, 0] - a[:, 0]) * (d[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (d[:, 0] - a[:, 0])
            area2d = float(np.abs(cross2).sum() / 2)
            area3d = float(np.linalg.norm(np.cross(b - a, d - a), axis=1).sum() / 2)
            edges = np.concatenate([np.linalg.norm((b - a)[:, :2], axis=1), np.linalg.norm((d - b)[:, :2], axis=1),
                                    np.linalg.norm((a - d)[:, :2], axis=1)])
            max_edge = float(edges.max())
        return {"name": self.name, "engine": ENGINE, "points": int(len(used)), "triangles": int(len(self.triangles)),
                "z_min": _r(pts[:, 2].min()) if len(pts) else None, "z_max": _r(pts[:, 2].max()) if len(pts) else None,
                "z_mean": _r(pts[:, 2].mean()) if len(pts) else None,
                "x_min": _r(pts[:, 0].min()) if len(pts) else None, "y_min": _r(pts[:, 1].min()) if len(pts) else None,
                "x_max": _r(pts[:, 0].max()) if len(pts) else None, "y_max": _r(pts[:, 1].max()) if len(pts) else None,
                "area_2d": _r(area2d), "area_3d": _r(area3d), "max_triangle_edge": _r(max_edge)}

    def _grid(self):
        if self._index is None:
            c = self.corners()[:, :, :2]
            lo, hi = c.min(axis=1), c.max(axis=1)
            x0, y0 = lo.min(axis=0) if len(c) else (0.0, 0.0)
            x1, y1 = hi.max(axis=0) if len(c) else (1.0, 1.0)
            n = max(1, int(math.sqrt(max(1, len(c)))))
            cell = max((x1 - x0) / n, (y1 - y0) / n, 1e-9)
            buckets: dict[tuple[int, int], list[int]] = {}
            for t in range(len(c)):
                for gx in range(int((lo[t, 0] - x0) // cell), int((hi[t, 0] - x0) // cell) + 1):
                    for gy in range(int((lo[t, 1] - y0) // cell), int((hi[t, 1] - y0) // cell) + 1):
                        buckets.setdefault((gx, gy), []).append(t)
            self._index = (x0, y0, cell, buckets)
        return self._index

    def elevation_at(self, x: float, y: float) -> float | None:
        """Linear interpolation inside the TIN; ``None`` outside."""
        if not len(self.triangles):
            return None
        x0, y0, cell, buckets = self._grid()
        cand = buckets.get((int((x - x0) // cell), int((y - y0) // cell)))
        if not cand:
            return None
        c = self.corners()[cand]
        a, b, d = c[:, 0], c[:, 1], c[:, 2]
        det = (b[:, 1] - d[:, 1]) * (a[:, 0] - d[:, 0]) + (d[:, 0] - b[:, 0]) * (a[:, 1] - d[:, 1])
        with np.errstate(divide="ignore", invalid="ignore"):
            l1 = ((b[:, 1] - d[:, 1]) * (x - d[:, 0]) + (d[:, 0] - b[:, 0]) * (y - d[:, 1])) / det
            l2 = ((d[:, 1] - a[:, 1]) * (x - d[:, 0]) + (a[:, 0] - d[:, 0]) * (y - d[:, 1])) / det
        l3 = 1 - l1 - l2
        eps = -1e-9
        inside = np.where((det != 0) & (l1 >= eps) & (l2 >= eps) & (l3 >= eps))[0]
        if not len(inside):
            return None
        k = inside[0]
        return float(l1[k] * a[k, 2] + l2[k] * b[k, 2] + l3[k] * d[k, 2])


def _r(v: float) -> float:
    return round(float(v), 6)


def build_tin(points: Sequence[Sequence[float]], *, name: str, boundary: Sequence[Sequence[float]] | None = None,
              max_edge: float | None = None, breaklines: Sequence[Sequence[Sequence[float]]] | None = None) -> Tin:
    Delaunay = _scipy_delaunay()
    choices = ["triangulation: unconstrained Delaunay of XY (scipy.spatial.Delaunay / Qhull)"]
    warnings: list[str] = []
    rows = [tuple(float(v) for v in p[:3]) for p in points if p is not None and len(p) >= 3 and p[2] is not None]
    if breaklines:
        extra = [tuple(float(v) for v in p[:3]) for line in breaklines for p in line]
        rows += extra
        choices.append(f"breaklines: {len(breaklines)} line(s), vertices added as points; edges NOT enforced "
                       "(no constrained triangulation in the fallback)")
    seen: dict[tuple[float, float], float] = {}
    conflicts = 0
    for x, y, z in rows:
        key = (round(x, 6), round(y, 6))
        if key in seen:
            if abs(seen[key] - z) > 1e-9:
                conflicts += 1
            continue
        seen[key] = z
    if conflicts:
        warnings.append(f"{conflicts} duplicate XY position(s) with a different Z: the first Z was kept")
    pts = np.array([[x, y, z] for (x, y), z in seen.items()], dtype=float)
    if len(pts) < 3:
        raise ToolFailure("INVALID_ARGUMENT", "a TIN needs at least three points with elevations")
    try:
        tri = Delaunay(pts[:, :2] - pts[:, :2].mean(axis=0))      # centred: survey coordinates are large
    except Exception as exc:  # noqa: BLE001 - Qhull errors (collinear input)
        raise ToolFailure("INVALID_ARGUMENT", f"triangulation failed: {type(exc).__name__}") from exc
    simplices = np.asarray(tri.simplices, dtype=int)
    keep = np.ones(len(simplices), dtype=bool)
    if max_edge is not None and max_edge > 0:
        c = pts[simplices][:, :, :2]
        edges = np.stack([np.linalg.norm(c[:, 1] - c[:, 0], axis=1), np.linalg.norm(c[:, 2] - c[:, 1], axis=1),
                          np.linalg.norm(c[:, 0] - c[:, 2], axis=1)], axis=1)
        keep &= edges.max(axis=1) <= max_edge
        choices.append(f"maximum triangle edge {max_edge:g} (longer triangles dropped)")
    if boundary is not None and len(boundary) >= 3:
        poly = [(float(p[0]), float(p[1])) for p in boundary]
        cent = pts[simplices][:, :, :2].mean(axis=1)
        keep &= np.array([_inside(poly, cx, cy) for cx, cy in cent], dtype=bool)
        choices.append(f"outer boundary: {len(poly)} vertices (triangles with the centroid outside dropped)")
    return Tin(name, pts, simplices[keep], choices, warnings)


def _inside(poly: list[tuple[float, float]], x: float, y: float) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi:
            inside = not inside
        j = i
    return inside


def levels(z_min: float, z_max: float, interval: float) -> list[float]:
    if interval <= 0:
        raise ToolFailure("INVALID_ARGUMENT", "interval must be positive")
    first = math.ceil(z_min / interval - 1e-9)
    last = math.floor(z_max / interval + 1e-9)
    if last - first > 100_000:
        raise ToolFailure("PAYLOAD_TOO_LARGE", "more than 100000 contour levels")
    return [round(k * interval, 10) for k in range(first, last + 1)]


def contours(tin: Tin, interval: float, major: float = 0.0) -> list[dict[str, Any]]:
    """Contour polylines (marching triangles); a vertex exactly on a level counts as slightly above it."""
    if not len(tin.triangles):
        return []
    z = tin.points[:, 2]
    used = np.unique(tin.triangles)
    out: list[dict[str, Any]] = []
    span = float(np.ptp(z[used])) or 1.0
    for level in levels(float(z[used].min()), float(z[used].max()), interval):
        d = z - level
        d = np.where(np.abs(d) < 1e-12 * span, 1e-9 * span, d)
        segments: list[tuple[tuple[int, int], tuple[int, int]]] = []
        for tri in tin.triangles:
            crossing = []
            for i, j in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
                if (d[i] > 0) != (d[j] > 0):
                    crossing.append((min(i, j), max(i, j)))
            if len(crossing) == 2:
                segments.append((crossing[0], crossing[1]))
        for chain, closed in _chain(segments):
            pts = []
            for i, j in chain:
                t = d[i] / (d[i] - d[j])
                p = tin.points[i] + t * (tin.points[j] - tin.points[i])
                pts.append([_r(p[0]), _r(p[1])])
            is_major = bool(major) and abs(level / major - round(level / major)) < 1e-6
            out.append({"elevation": _r(level), "closed": closed, "major": is_major, "points": pts})
    return out


def _chain(segments: list[tuple[tuple[int, int], tuple[int, int]]]) -> Iterable[tuple[list[tuple[int, int]], bool]]:
    by_edge: dict[tuple[int, int], list[int]] = {}
    for k, (a, b) in enumerate(segments):
        by_edge.setdefault(a, []).append(k)
        by_edge.setdefault(b, []).append(k)
    used = [False] * len(segments)
    # open chains start at edges used once (the TIN border), then closed loops
    starts = [e for e, ks in by_edge.items() if len(ks) == 1] + list(by_edge)
    for start in starts:
        ks = [k for k in by_edge[start] if not used[k]]
        if not ks:
            continue
        chain = [start]
        edge = start
        k = ks[0]
        while True:
            used[k] = True
            a, b = segments[k]
            edge = b if a == edge else a
            chain.append(edge)
            nxt = [q for q in by_edge[edge] if not used[q]]
            if not nxt:
                break
            k = nxt[0]
        closed = len(chain) > 2 and chain[0] == chain[-1]
        yield chain, closed


def difference(base: Tin, compare: Tin, *, name: str, max_edge: float | None = None) -> tuple[Tin, dict[str, Any]]:
    """``dz = compare − base`` at the union of vertices inside both TINs, triangulated; volumes by exact clipping."""
    pts = []
    outside = 0
    seen = set()
    for source in (base.points[np.unique(base.triangles)], compare.points[np.unique(compare.triangles)]):
        for x, y, _z in source:
            key = (round(float(x), 6), round(float(y), 6))
            if key in seen:
                continue
            seen.add(key)
            zb, zc = base.elevation_at(x, y), compare.elevation_at(x, y)
            if zb is None or zc is None:
                outside += 1
                continue
            pts.append([float(x), float(y), zc - zb])
    dz = build_tin(pts, name=name, max_edge=max_edge)
    dz.model_choices.insert(0, "difference: dz = compare - base sampled at the union of both vertex sets inside both "
                               "TINs (linear interpolation), triangulated again")
    cut = fill = 0.0
    for tri in dz.corners():
        pos, neg = _clip_volumes(tri)
        fill += pos
        cut += neg
    stats = {"dz_surface": dz.stats(), "cut_volume": _r(cut), "fill_volume": _r(fill), "net_volume": _r(fill - cut),
             "vertices_outside_overlap": outside,
             "sign_convention": "dz = compare - base; cut = compare below base (subsidence)"}
    return dz, stats


def _clip_volumes(tri: np.ndarray) -> tuple[float, float]:
    """Volumes of the positive and the negative part of a linear dz over one triangle (exact)."""
    def part(sign: float) -> float:
        poly = []
        n = 3
        for k in range(n):
            p, q = tri[k], tri[(k + 1) % n]
            vp, vq = sign * p[2], sign * q[2]
            if vp >= 0:
                poly.append(p)
            if (vp >= 0) != (vq >= 0):
                t = vp / (vp - vq)
                poly.append(p + t * (q - p))
        if len(poly) < 3:
            return 0.0
        total = 0.0
        a = poly[0]
        for b, c in zip(poly[1:-1], poly[2:]):
            area = abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / 2
            total += area * sign * (a[2] + b[2] + c[2]) / 3
        return max(total, 0.0)

    return part(1.0), part(-1.0)


def stations(polyline: Sequence[Sequence[float]], step: float) -> list[tuple[float, float, float]]:
    if step <= 0:
        raise ToolFailure("INVALID_ARGUMENT", "station_interval must be positive")
    pts = [(float(p[0]), float(p[1])) for p in polyline]
    if len(pts) < 2:
        raise ToolFailure("INVALID_ARGUMENT", "the polyline needs at least two points")
    seg = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    total = sum(seg)
    if total <= 0:
        raise ToolFailure("INVALID_ARGUMENT", "the polyline has zero length")
    count = int(total / step + 1e-9)
    if count > 1_000_000:
        raise ToolFailure("PAYLOAD_TOO_LARGE", "more than 1000000 stations")
    marks = [k * step for k in range(count + 1)]
    if total - marks[-1] > 1e-9:
        marks.append(total)
    out = []
    i, start = 0, 0.0
    for st in marks:
        while i < len(seg) - 1 and st > start + seg[i]:
            start += seg[i]
            i += 1
        t = 0.0 if seg[i] == 0 else min(1.0, max(0.0, (st - start) / seg[i]))
        x = pts[i][0] + t * (pts[i + 1][0] - pts[i][0])
        y = pts[i][1] + t * (pts[i + 1][1] - pts[i][1])
        out.append((_r(st), _r(x), _r(y)))
    return out


def profile(tins: Sequence[Tin], polyline: Sequence[Sequence[float]], step: float) -> list[dict[str, Any]]:
    rows = []
    for st, x, y in stations(polyline, step):
        row: dict[str, Any] = {"station": st, "x": x, "y": y}
        for tin in tins:
            z = tin.elevation_at(x, y)
            row[tin.name] = None if z is None else _r(z)
        rows.append(row)
    return rows


def rows_to_csv(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: "" if v is None else v for k, v in row.items()})
    return buf.getvalue().encode("utf-8")


def tin_json_bytes(tin: Tin) -> bytes:
    return (json.dumps(tin.to_json(), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
