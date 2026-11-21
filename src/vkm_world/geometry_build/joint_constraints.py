"""Typed spatial relations and continuous feasibility for borehole adjustment."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from itertools import product
from math import hypot, isfinite, prod, sqrt
from numbers import Real
from time import monotonic

import numpy as np
from scipy.optimize import linprog
from shapely.affinity import affine_transform
from shapely.errors import GEOSException
from shapely.geometry import Point, shape
from shapely.geometry.polygon import orient


@dataclass(frozen=True, slots=True)
class Relation:
    id: str
    kind: str
    ids: tuple[str, ...]
    frame: str
    hard: bool
    tolerance: float
    bounds: tuple[tuple[float | None, float | None], ...]
    geometry: object = None
    map_id: str | None = None
    direction: tuple[float, float] | None = None
    min_gap: float = 0.
    side: int | None = None
    alternative_group: str | None = None
    alternative_id: str | None = None


_GEOMETRY_DIMENSIONS = {
    "Point": 0, "MultiPoint": 1, "LineString": 1, "MultiLineString": 2,
    "Polygon": 2, "MultiPolygon": 3,
}
_ARITY = {
    "distance": 2, "offset": 2, "boundary_distance": 1, "region": 1,
    "side": 1, "profile_order": None, "profile_membership": 1, "profile_corridor": 3,
}


def _number(value, name):
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite JSON number")
    try:
        if isfinite(value):
            return float(value)
    except OverflowError:
        pass
    raise ValueError(f"{name} must be a finite JSON number")


def _identifier(value, name):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a nonempty identifier")
    return value


def _array(value, length, name):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{name} must have {length} entries")
    return value


def _xy(value, name):
    return tuple(_number(n, name) for n in _array(value, 2, name))


def _interval(value, name, *, nonnegative=False):
    low, high = (None if x is None else _number(x, name) for x in _array(value, 2, name))
    if low is not None and high is not None and low > high:
        raise ValueError(f"{name} has reversed limits")
    if nonnegative and any(x is not None and x < 0. for x in (low, high)):
        raise ValueError(f"{name} requires nonnegative limits")
    return (0. if nonnegative and low is None else low), high


def _geometry(value, allowed, name):
    if not isinstance(value, dict) or value.get("type") not in allowed:
        raise ValueError(f"{name} has incompatible GeoJSON geometry")
    kind = value["type"]

    def read_coordinates(coordinates, depth):
        if not depth:
            return _xy(coordinates, name)
        if not isinstance(coordinates, (list, tuple)) or not coordinates:
            raise ValueError(f"{name} has malformed coordinates")
        return [read_coordinates(child, depth - 1) for child in coordinates]

    coordinates = read_coordinates(value.get("coordinates"), _GEOMETRY_DIMENSIONS[kind])
    try:
        geometry = shape({"type": kind, "coordinates": coordinates})
    except (ValueError, TypeError, GEOSException) as exc:
        raise ValueError(f"{name} has malformed geometry") from exc
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError(f"{name} has empty or invalid geometry")
    return geometry


def compile_relations(rows, frames_by_object=None):
    """Validate JSON-like rows and return deterministically ordered typed relations.

    The geometry of a relation with map_id belongs to that map's normalized
    native frame. Its bounds remain expressed in the declared evaluation frame.
    """
    result = []
    identifiers = set()
    for row in rows:
        relation_id = _identifier(row.get("id"), "relation.id")
        if relation_id in identifiers:
            raise ValueError(f"duplicate relation id {relation_id}")
        identifiers.add(relation_id)
        kind = row.get("kind")
        if kind not in _ARITY:
            raise ValueError(f"{relation_id} has unsupported relation kind {kind!r}")
        frame = _identifier(row.get("frame"), f"{relation_id}.frame")
        ids = row.get("ids")
        arity = _ARITY[kind]
        if not isinstance(ids, (list, tuple)) or len(ids) < 1 or (
            len(ids) != arity if arity is not None else len(ids) < 2
        ):
            raise ValueError(f"{relation_id} has invalid object arity")
        ids = tuple(_identifier(i, f"{relation_id}.ids") for i in ids)
        if len(set(ids)) != len(ids):
            raise ValueError(f"{relation_id} has repeated object ids")
        if frames_by_object is not None and any(
            frames_by_object.get(object_id) != frame for object_id in ids
        ):
            raise ValueError(f"{relation_id} links incompatible or unknown frames")
        hard = row.get("hard", True)
        if type(hard) is not bool:
            raise ValueError(f"{relation_id}.hard must be bool")
        numeric = _number(row.get("numerical_tolerance", 1e-8), f"{relation_id}.numerical_tolerance")
        if numeric < 0:
            raise ValueError(f"{relation_id} has negative numerical tolerance")
        map_id = row.get("map_id")
        if map_id is not None:
            _identifier(map_id, f"{relation_id}.map_id")
        group, mode = row.get("alternative_group"), row.get("alternative_id")
        if group is not None or mode is not None:
            _identifier(group, f"{relation_id}.alternative_group")
            _identifier(mode, f"{relation_id}.alternative_id")

        geometry = direction = side = None
        gap = 0.
        if kind in {"distance", "boundary_distance", "profile_membership"}:
            bounds = (_interval(row.get("bounds"), f"{relation_id}.bounds", nonnegative=True),)
        elif kind == "offset":
            bounds = tuple(_interval(row.get(axis, (None, None)), f"{relation_id}.{axis}") for axis in ("dx", "dy"))
            if all(interval == (None, None) for interval in bounds):
                raise ValueError(f"{relation_id} has no bounded offset component")
        elif kind == "side":
            name = row.get("side")
            if name not in {"left", "right"}:
                raise ValueError(f"{relation_id} must specify left or right")
            bounds, side = ((0., None),), (1 if name == "left" else -1)
        elif kind == "profile_corridor":
            bounds = tuple(_interval(row.get(name), f"{relation_id}.{name}")
                           for name in ("station", "cross_track"))
        elif kind == "profile_order":
            if row.get("direction") is not None:
                direction = _unit(_xy(row["direction"], f"{relation_id}.direction"))
                if direction is None:
                    raise ValueError(f"{relation_id} profile direction is zero")
            gap = _number(row.get("min_gap", 0.), f"{relation_id}.min_gap")
            if gap < 0:
                raise ValueError(f"{relation_id} has negative station gap")
            bounds = ((gap, None),) * (len(ids) - 1)
        else:
            bounds = ((0., 0.),)

        if kind == "region":
            geometry = _geometry(row.get("geometry"), _GEOMETRY_DIMENSIONS, relation_id)
        if kind in {"boundary_distance", "profile_membership", "side"}:
            if kind == "side":
                allowed = {"LineString"}
            else:
                allowed = {"LineString", "MultiLineString"}
            geometry = _geometry(row.get("geometry"), allowed, relation_id)
            if kind == "side" and len(geometry.coords) != 2:
                raise ValueError(f"{relation_id} requires one oriented segment")
        if map_id is not None and geometry is None and not (
            kind == "profile_order" and direction is not None
        ):
            raise ValueError(f"{relation_id} supplies unrelated map transform")
        result.append(Relation(
            relation_id, kind, ids, frame, hard, numeric, bounds, geometry,
            map_id, direction, gap, side, group, mode,
        ))
    return tuple(sorted(result, key=lambda relation: relation.id))


def _unit(vector):
    magnitude = hypot(*vector)
    return tuple(v / magnitude for v in vector) if magnitude else None


def _pose(relation, transforms):
    if relation.map_id is None:
        return (0., 0., 1., 0.)
    if not transforms or relation.map_id not in transforms:
        raise KeyError(relation.map_id)
    tx, ty, a, b = _working_array(transforms[relation.map_id], 4, f"{relation.id}.transform")
    if a == b == 0.:
        raise ValueError(f"{relation.id} has a nonfinite or degenerate transform")
    return tx, ty, a, b


def _working_geometry(relation, transforms):
    if relation.map_id is None:
        return relation.geometry
    tx, ty, a, b = _pose(relation, transforms)
    return affine_transform(relation.geometry, (a, -b, b, a, tx, ty))


def _working_direction(relation, transforms):
    if relation.direction is None or relation.map_id is None:
        return relation.direction
    _, _, a, b = _pose(relation, transforms)
    dx, dy = relation.direction
    return _unit((a * dx - b * dy, b * dx + a * dy))


def _signed_side(geometry, point):
    (x0, y0), (x1, y1) = geometry.coords
    dx, dy = _unit((x1 - x0, y1 - y0))
    return dx * (point[1] - y0) - dy * (point[0] - x0)


def _working_array(values, length, name):
    if not isinstance(values, (list, tuple, np.ndarray)) or np.shape(values) != (length,):
        raise ValueError(f"{name} has wrong vector shape")
    if not all(isinstance(x, Real) and not isinstance(x, (bool, np.bool_)) and isfinite(x)
               for x in values):
        raise ValueError(f"{name} has nonfinite or nonnumeric values")
    return np.asarray(values, dtype=np.float64)


def _value(relation, positions, transforms):
    points = [_working_array(positions[identity], 2, identity) for identity in relation.ids]
    kind = relation.kind
    if kind == "distance":
        return (hypot(*(points[1] - points[0])),)
    if kind == "offset":
        return tuple(points[1] - points[0])
    if kind == "profile_corridor":
        delta, subject = points[1] - points[0], points[2] - points[0]
        length = hypot(*delta)
        if not length:
            return None
        tangent = delta / length
        normal = np.asarray((-tangent[1], tangent[0]))
        return (float(subject @ tangent) / length, float(subject @ normal))
    if kind == "profile_order":
        direction = _working_direction(relation, transforms)
        if direction is None:
            direction = _unit(points[-1] - points[0])
        if direction is None:
            return None
        return tuple(float((b - a) @ direction) for a, b in zip(points, points[1:]))
    geometry = _working_geometry(relation, transforms)
    if kind == "side":
        return (relation.side * _signed_side(geometry, points[0]),)
    return (float(geometry.distance(Point(*points[0]))),)


def select_relations(relations, choices=None):
    """Return common and chosen hypothesis factors. Refuse an ambiguous choice."""
    choices = choices or {}
    groups = {}
    for relation in relations:
        if relation.alternative_group is not None:
            groups.setdefault(relation.alternative_group, set()).add(relation.alternative_id)
    if not all(choices.get(group) in values for group, values in groups.items()):
        raise ValueError("every alternative factor group needs a valid hypothesis choice")
    return tuple(relation for relation in relations if relation.alternative_group is None
                 or relation.alternative_id == choices[relation.alternative_group])


def relation_residuals(relations, positions, transforms=None):
    """Evaluate unpadded signed quantities and violations in the working frame."""
    result = []
    for relation in relations:
        evidence = {"id": relation.id, "kind": relation.kind, "frame": relation.frame, "ids": relation.ids,
                    "hard": relation.hard, "intervals": relation.bounds, "tolerance": relation.tolerance}
        if any(positions.get(key) is None for key in relation.ids):
            result.append(evidence | {"status": "UNRESOLVED", "reason": "MISSING_POSITION",
                                      "values": None, "violations": None, "max_violation": None})
            continue
        if relation.map_id is not None and relation.map_id not in (transforms or {}):
            result.append(evidence | {"status": "UNRESOLVED", "reason": "MISSING_SHARED_REGISTRATION",
                                      "values": None, "violations": None, "max_violation": None})
            continue
        values = _value(relation, positions, transforms)
        if values is None:
            result.append(evidence | {"status": "UNRESOLVED", "reason": "COINCIDENT_PROFILE_ENDPOINTS",
                                      "values": None, "violations": None, "max_violation": None})
            continue
        if not all(isfinite(x) for x in values):
            raise ValueError(f"{relation.id} produced a nonfinite residual")
        violations = tuple(max(0., 0. if lo is None else lo - x, 0. if hi is None else x - hi)
                           for x, (lo, hi) in zip(values, relation.bounds, strict=True))
        maximum = max(violations)
        result.append(evidence | {
            "status": "SATISFIED" if maximum <= relation.tolerance else "VIOLATED",
            "values": tuple(float(x) for x in values), "violations": violations, "max_violation": maximum,
        })
    return result


def _margins(values, intervals):
    equalities = []
    inequalities = []
    for value, (low, high) in zip(values, intervals, strict=True):
        if low == high and low is not None:
            equalities.append(value - low)
        else:
            if low is not None:
                inequalities.append(value - low)
            if high is not None:
                inequalities.append(high - value)
    return equalities, inequalities


def relation_margins(relations, positions, transforms=None):
    """Return (equality residuals, inequality margins) for one hard hypothesis."""
    equalities, inequalities = [], []
    for relation in relations:
        if not relation.hard:
            continue
        values = _value(relation, positions, transforms)
        if values is None:
            # This candidate cannot certify the profile. Keep constraint arity
            # unchanged across optimizer evaluations.
            values = tuple(-1. - relation.min_gap for _ in relation.bounds)
        exact, interval = _margins(values, relation.bounds)
        equalities.extend(exact)
        inequalities.extend(interval)
    return np.asarray(equalities, dtype=np.float64), np.asarray(inequalities, dtype=np.float64)


def circle_intersections(center_a, radius_a, center_b, radius_b):
    """Return one or two isolated exact candidates. Coincident circles are nonisolated."""
    first = _xy(center_a, "center_a")
    second = _xy(center_b, "center_b")
    ra, rb = _number(radius_a, "radius_a"), _number(radius_b, "radius_b")
    if min(ra, rb) < 0:
        raise ValueError("negative circle radius")
    dx, dy = second[0] - first[0], second[1] - first[1]
    separation = hypot(dx, dy)
    if separation == 0.:
        return (first,) if ra == rb == 0. else ()
    if not abs(ra - rb) <= separation <= ra + rb:
        return ()
    scale = max(ra, rb, separation)
    ra, rb, distance = ra / scale, rb / scale, separation / scale
    along = (distance**2 + (ra + rb) * (ra - rb)) / (2. * distance)
    height = sqrt(max(0., (ra - along) * (ra + along)))
    ux, uy = dx / separation, dy / separation
    base_x, base_y = first[0] + scale * along * ux, first[1] + scale * along * uy
    if height == 0.:
        return ((base_x, base_y),)
    return tuple(sorted(((base_x - scale * height * uy, base_y + scale * height * ux),
                         (base_x + scale * height * uy, base_y - scale * height * ux))))


def line_circle_intersections(start, end, center, radius):
    """Isolated intersections on the closed segment, including tangencies."""
    ax, ay = _xy(start, "start")
    bx, by = _xy(end, "end")
    cx, cy = _xy(center, "center")
    radius = _number(radius, "radius")
    if radius < 0.:
        raise ValueError("negative circle radius")
    dx, dy = bx - ax, by - ay
    length = hypot(dx, dy)
    if not length:
        return ((ax, ay),) if hypot(ax - cx, ay - cy) == radius else ()
    ux, uy = dx / length, dy / length
    along = (cx - ax) * ux + (cy - ay) * uy
    perpendicular = abs((cx - ax) * uy - (cy - ay) * ux)
    if perpendicular > radius:
        return ()
    offset = sqrt(max(0., (radius - perpendicular) * (radius + perpendicular)))
    return tuple((ax + station * ux, ay + station * uy)
                 for station in sorted({along - offset, along + offset}) if 0. <= station <= length)


def _geometric_linear_outer(geometry):
    """Yield normal, reference point, lower/upper bounds for the exact convex hull."""
    hull = geometry.convex_hull
    if hull.geom_type == "Point":
        for axis in np.eye(2):
            yield axis, hull.coords[0], (0., 0.)
    elif hull.geom_type == "LineString":
        a, b = np.asarray(hull.coords, dtype=np.float64)
        direction = _unit(b - a)
        yield direction, a, (0., hypot(*(b - a)))
        yield (-direction[1], direction[0]), a, (0., 0.)
    else:
        ring = list(orient(hull, sign=1).exterior.coords)
        for (ax, ay), (bx, by) in zip(ring, ring[1:]):
            direction = _unit((bx - ax, by - ay))
            if direction is not None:
                yield (-direction[1], direction[0]), (ax, ay), (0., None)


def solve_linear_feasibility(relations, bounds=None, fixed=None, *, initial=None,
                             transforms=None, time_limit=10., max_extrema=0):
    """Solve offsets, profile order with fixed axis, and outer linear geometry.

    A successful outer-model LP only yields a continuous witness if the exact
    geometry and every hard nonlinear condition also verify. The hull never
    replaces a concave domain, disconnected region or a region with holes.
    """
    if not isfinite(time_limit) or time_limit <= 0. or type(max_extrema) is not int or max_extrema < 0:
        raise ValueError("invalid linear feasibility budget")
    hard = tuple(relation for relation in relations if relation.hard)
    fixed, bounds, initial = fixed or {}, bounds or {}, initial or {}
    identities = sorted(set(bounds) | set(fixed) | {identity for r in hard for identity in r.ids})
    if not identities:
        return {"status": "CONTINUOUS_WITNESS", "positions": {}, "witnesses": [{}],
                "equality_rank": 0, "linear_variables": 0, "nonlinear_outer_relaxations": []}
    index = {identity: i for i, identity in enumerate(identities)}
    n = 2 * len(index)
    shift, inequalities, equalities, upper, equal, labels, equal_labels, relaxations = (
        {}, [], [], [], [], [], [], [],
    )
    limits = []
    for identity in identities:
        exact = _xy(fixed[identity], f"fixed.{identity}") if identity in fixed else None
        box = bounds.get(identity, ((None, None), (None, None)))
        intervals = [_interval(axis, f"bounds.{identity}") for axis in _array(box, 2, f"bounds.{identity}")]
        origin = initial.get(identity)
        centers = []
        for j, (lo, hi) in enumerate(intervals):
            if exact is not None:
                chosen = exact[j]
                if (lo is not None and chosen < lo) or (hi is not None and chosen > hi):
                    return {"status": "LINEAR_BOUNDS_CONTRADICTION", "positions": None,
                            "witnesses": [], "conflict": [identity], "nonlinear_outer_relaxations": []}
                centers.append(chosen)
                limits.append((0., 0.))
                continue
            center = (float(origin[j]) if origin is not None else
                      lo / 2 + hi / 2 if lo is not None and hi is not None else
                      lo if lo is not None else hi if hi is not None else 0.)
            centers.append(center)
            limits.append((None if lo is None else lo - center, None if hi is None else hi - center))
        shift[identity] = np.asarray(centers)

    def add(label, terms, interval, *, shift_override=None):
        row, correction = np.zeros(n), 0.
        for identity, vector in terms:
            start = 2 * index[identity]
            row[start:start + 2] += vector
            correction += np.asarray(vector) @ shift[identity]
        if shift_override is not None:
            correction = shift_override
        lo, hi = interval
        if lo == hi and lo is not None:
            equalities.append(row)
            equal.append(lo - correction)
            equal_labels.append(label)
        else:
            if lo is not None:
                inequalities.append(-row)
                upper.append(correction - lo)
                labels.append(label)
            if hi is not None:
                inequalities.append(row)
                upper.append(hi - correction)
                labels.append(label)

    for relation in hard:
        kind = relation.kind
        if relation.map_id is not None and relation.map_id not in (transforms or {}):
            relaxations.append(relation.id)
            continue
        if kind == "offset":
            first, last = relation.ids
            for direction, bound in zip(np.eye(2), relation.bounds, strict=True):
                add(relation.id, [(first, -direction), (last, direction)], bound)
        elif kind == "profile_order" and relation.direction is not None:
            direction = np.asarray(_working_direction(relation, transforms))
            for first, last in zip(relation.ids, relation.ids[1:]):
                add(relation.id, [(first, -direction), (last, direction)], (relation.min_gap, None))
        elif kind == "profile_corridor" and all(i in fixed for i in relation.ids[:2]):
            first, last, subject = relation.ids
            delta = shift[last] - shift[first]
            tangent = _unit(delta)
            if tangent is None:
                relaxations.append(relation.id)
                continue
            tangent = np.asarray(tangent)
            normal = np.asarray((-tangent[1], tangent[0]))
            scale = hypot(*delta)
            for vector, interval in zip((tangent / scale, normal), relation.bounds, strict=True):
                add(relation.id, [(first, -vector), (subject, vector)], interval)
        elif kind in {"region", "side"}:
            geometry = _working_geometry(relation, transforms)
            identity = relation.ids[0]
            if kind == "side":
                start, end = np.asarray(geometry.coords, dtype=np.float64)
                ux, uy = _unit(end - start)
                constraints = [(relation.side * np.asarray((-uy, ux)), start, (0., None))]
            else:
                constraints = _geometric_linear_outer(geometry)
                if not geometry.equals(geometry.convex_hull):
                    relaxations.append(relation.id)
            for direction, origin, interval in constraints:
                delta = shift[identity] - origin
                add(relation.id, [(identity, direction)], interval, shift_override=np.asarray(direction) @ delta)
        else:
            relaxations.append(relation.id)

    deadline = monotonic() + time_limit
    bounds_rank = sum(lo == hi and lo is not None for lo, hi in limits)
    free = [i for i, (lo, hi) in enumerate(limits) if lo != hi or lo is None]
    rank = np.linalg.matrix_rank(np.asarray(equalities)[:, free]) if equalities and free else 0
    metadata = {"linear_variables": n, "equality_rank": int(rank) + bounds_rank,
                "nonlinear_outer_relaxations": sorted(set(relaxations))}

    def lp(objective):
        remaining = deadline - monotonic()
        if remaining <= 0:
            return None
        return linprog(objective, A_ub=inequalities or None, b_ub=upper or None,
                       A_eq=equalities or None, b_eq=equal or None, bounds=limits, method="highs",
                       options={"time_limit": remaining, "primal_feasibility_tolerance": 1e-9,
                                "dual_feasibility_tolerance": 1e-9})

    first = lp(np.zeros(n))
    if first is None or first.status == 1:
        return metadata | {"status": "INCOMPLETE_RESOURCE_LIMIT", "positions": None, "witnesses": []}
    if first.status == 2:
        return metadata | {"status": "LINEAR_OUTER_MODEL_INFEASIBLE", "positions": None, "witnesses": [],
                           "conflict_candidates": sorted(set(labels + equal_labels)),
                           "proof_scope": "HiGHS numerical status at 1e-9, no exact certificate"}
    if not first.success:
        return metadata | {"status": "UNRESOLVED_CONTINUOUS_SEARCH", "positions": None, "witnesses": [],
                           "optimizer_status": int(first.status)}

    def positions(vector):
        return {identity: tuple(float(x) for x in vector[2*i:2*i+2] + shift[identity])
                for identity, i in index.items()}

    witnesses = {}
    proposals = []

    def accept(vector):
        proposal = positions(vector)
        key = tuple(proposal.values())
        if key not in witnesses:
            evaluations = relation_residuals(hard, proposal, transforms)
            proposals.append(proposal)
            if all(row["status"] == "SATISFIED" for row in evaluations):
                witnesses[key] = proposal

    accept(first.x)
    limited = False
    for axis in free[:max_extrema]:
        vector = np.eye(1, n, axis).ravel()
        ends = [lp(sign * vector) for sign in (1., -1.)]
        if any(end is None or end.status == 1 for end in ends):
            limited = True
            break
        if all(end.success for end in ends):
            low, high = (end.x for end in ends)
            for fraction in (.0, .25, .5, .75, 1.):
                accept((1. - fraction) * low + fraction * high)
    status = ("INCOMPLETE_RESOURCE_LIMIT" if limited else "CONTINUOUS_WITNESS" if witnesses
              else "UNRESOLVED_CONTINUOUS_SEARCH")
    return metadata | {"status": status, "positions": next(iter(witnesses.values()), None),
                       "witnesses": list(witnesses.values()), "outer_proposals": proposals}


def _parts(geometry):
    if geometry.geom_type.startswith("Multi"):
        return tuple(geometry.geoms)
    return (geometry,)


def _segment_candidates(path, lower, upper):
    """Analytic parallel offsets and vertex-arc starts for an interval to a line."""
    radii = sorted(set((lower or 0.,) if upper is None
                       else (lower or 0., (lower or 0.) / 2 + upper / 2, upper)))
    for part in _parts(path):
        for a, b in zip(part.coords, list(part.coords)[1:]):
            a, b = np.asarray(a), np.asarray(b)
            direction = _unit(b - a)
            if direction is None:
                continue
            normal = np.asarray((-direction[1], direction[0]))
            for radius in radii:
                for fraction in (.0, .25, .5, .75, 1.):
                    near = a + fraction * (b - a)
                    for sign in (-1, 1):
                        yield tuple(near + sign * radius * normal)
                for angle in np.linspace(0., 2. * np.pi, 8, endpoint=False):
                    yield tuple(a + radius * np.asarray((np.cos(angle), np.sin(angle))))
                    yield tuple(b + radius * np.asarray((np.cos(angle), np.sin(angle))))


def _circle_candidates(identity, relations, fixed, domains):
    radii = []
    for relation in relations:
        if relation.kind != "distance" or identity not in relation.ids:
            continue
        anchor = next(point for point in relation.ids if point != identity)
        if anchor not in fixed:
            continue
        lo, hi = relation.bounds[0]
        for radius in sorted({lo, hi} - {None}):
            radii.append((anchor, fixed[anchor], radius))
            for angle in np.linspace(0., 2. * np.pi, 16, endpoint=False):
                center = fixed[anchor]
                yield (center[0] + radius * np.cos(angle), center[1] + radius * np.sin(angle))
            for geometry in domains:
                for part in _parts(geometry):
                    if part.geom_type == "LineString":
                        curves = (part,)
                    elif part.geom_type == "Polygon":
                        curves = (part.exterior, *part.interiors)
                    else:
                        continue
                    for curve in curves:
                        for a, b in zip(curve.coords, list(curve.coords)[1:]):
                            yield from line_circle_intersections(a, b, fixed[anchor], radius)
    for index, (key_a, point_a, radius_a) in enumerate(radii):
        for key_b, point_b, radius_b in radii[index + 1:]:
            if key_a != key_b:
                yield from circle_intersections(point_a, radius_a, point_b, radius_b)


def _profile_candidates(identity, relations, origins, transforms):
    for relation in relations:
        if relation.kind == "profile_corridor" and identity == relation.ids[-1]:
            first, last = (origins.get(key) for key in relation.ids[:2])
            if first is None or last is None:
                continue
            first, last = np.asarray(first), np.asarray(last)
            delta = last - first
            tangent = _unit(delta)
            if tangent is None or any(x is None for bound in relation.bounds for x in bound):
                continue
            normal = np.asarray((-tangent[1], tangent[0]))
            for station, cross_track in product(*(sorted({lo, .5 * lo + .5 * hi, hi})
                                                     for lo, hi in relation.bounds)):
                yield tuple(first + station * delta + cross_track * normal)
            continue
        if relation.kind != "profile_order" or identity not in relation.ids[1:-1]:
            continue
        lo, hi = (origins.get(key) for key in (relation.ids[0], relation.ids[-1]))
        if lo is None or hi is None:
            continue
        a, b = np.asarray(lo), np.asarray(hi)
        direction = _working_direction(relation, transforms)
        if direction is not None:
            b = a + ((b - a) @ direction) * np.asarray(direction)
        for fraction in (.125, .25, .375, .5, .625, .75, .875):
            yield tuple(a + fraction * (b - a))


def _scenario_components(relations):
    """Only graph-independent components may have designs combined afterwards."""
    adjacency = {}
    for relation in relations:
        for identity in relation.ids:
            adjacency.setdefault(identity, set()).update(relation.ids)
    while adjacency:
        component = set()
        frontier = [min(adjacency)]
        while frontier:
            identity = frontier.pop()
            if identity in component:
                continue
            component.add(identity)
            frontier.extend(adjacency.pop(identity) - component)
        yield tuple(sorted(component))


def _component_candidates(relations, initial, transforms, fixed, bounds, seed, deadline,
                          max_starts, max_realizations, max_function_evaluations):
    """Search one connected component. All retained incumbents verify jointly."""
    from scipy.optimize import minimize
    from vkm_world.geometry_build.borehole_placement import _domain_points, _SearchLimits

    fixed = {key: value for key, value in fixed.items() if value is not None}
    active = {identity for relation in relations for identity in relation.ids}
    hard = tuple(relation for relation in relations if relation.hard)
    verification = {}
    rejected = 0
    exhausted = False
    evaluations = 0
    method_calls = {}
    geometries = {}
    point_designs = {}
    lp = solve_linear_feasibility(
        hard, bounds, fixed, initial=initial, transforms=transforms,
        time_limit=max(1e-6, deadline - monotonic()), max_extrema=min(2, max_starts // 2),
    )
    if lp["status"] in {"LINEAR_OUTER_MODEL_INFEASIBLE", "LINEAR_BOUNDS_CONTRADICTION"}:
        return {"candidates": [], "linear": lp, "status": lp["status"], "rejected": rejected,
                "coverage": {}, "methods": {}}
    if lp["status"] == "INCOMPLETE_RESOURCE_LIMIT":
        exhausted = True

    for relation in hard:
        if relation.kind == "region":
            if relation.map_id is not None and relation.map_id not in transforms:
                continue
            geometry = _working_geometry(relation, transforms)
            geometries.setdefault(relation.ids[0], []).append(geometry)

    def covers_box(key, location):
        return all((lo is None or lo <= value) and (hi is None or value <= hi)
                   for value, (lo, hi) in zip(location, bounds.get(key, ((None, None),) * 2), strict=True))

    def accept(positions, method):
        nonlocal rejected
        if any(positions.get(key) is None or not covers_box(key, positions[key]) for key in active):
            rejected += 1
            return False
        key = tuple((identity, tuple(float(x) for x in coordinates))
                    for identity, coordinates in sorted(positions.items()) if identity in active)
        if key in verification:
            return True
        results = relation_residuals(relations, positions, transforms)
        if all(row["status"] == "SATISFIED" for row in results if row["hard"]):
            verification[key] = {"positions": dict(key), "verification": results, "candidate_method": method}
            method_calls[method] = method_calls.get(method, 0) + 1
            return True
        rejected += 1
        return False

    origins = initial | fixed
    accept(origins, "INPUT")
    for witness in lp.get("witnesses", ()):
        accept(witness, "CONTINUOUS_LP")

    for identity in sorted(active):
        if monotonic() >= deadline:
            exhausted = True
            break
        if identity in fixed:
            point_designs[identity] = [fixed[identity]]
            continue
        local = geometries.get(identity, ())
        raw = []
        for geometry in local:
            for part in _parts(geometry):
                raw.append(tuple(part.representative_point().coords[0]))
        if origins.get(identity) is not None:
            raw.append(tuple(origins[identity]))
        bounded_domains = [bound for bound in hard if bound.ids == (identity,) and bound.kind in {"boundary_distance", "profile_membership"}
                           and (bound.map_id is None or bound.map_id in transforms)]
        for bound in bounded_domains:
            if bound.bounds[0][1] is not None:
                raw.extend(_segment_candidates(_working_geometry(bound, transforms), *bound.bounds[0]))
        raw.extend(_circle_candidates(identity, hard, fixed, local))
        raw.extend(_profile_candidates(identity, [r for r in hard if r.kind == "profile_corridor"
                                                 or origins.get(identity) is not None or local],
                                       origins, transforms))
        try:
            for geometry in local:
                digest = sha256(f"{seed}:{identity}:{geometry.wkb_hex}".encode()).digest()
                raw.extend(map(tuple, _domain_points(geometry, int.from_bytes(digest[:8], "little"),
                                                     _SearchLimits(deadline, 0, 0))))
        except TimeoutError:
            exhausted = True
        if identity in bounds and all(lo is not None and hi is not None for lo, hi in bounds[identity]):
            raw.extend(product(*(sorted({lo, lo / 2 + hi / 2, hi}) for lo, hi in bounds[identity])))
        # Initial coordinates, finite geometry, finite explicit boxes and
        # observed anchored relations can bound an otherwise free object.
        # An outer LP corner is insufficient to place a free component.
        if not raw:
            can_infer = any(identity in relation.ids and (
                relation.kind == "offset" and all(x is not None for bound in relation.bounds for x in bound)
                or relation.kind == "distance" and relation.bounds[0][1] is not None
            ) and all(other in fixed for other in relation.ids if other != identity) for relation in hard)
            if can_infer:
                raw.extend(tuple(x[identity]) for x in lp.get("outer_proposals", ()) if identity in x)
        if not raw:
            return {"candidates": [], "linear": lp, "status": "UNDERIDENTIFIED_NO_FINITE_DESIGN",
                    "missing_design_ids": [identity], "rejected": rejected, "coverage": {}, "methods": {}}

        def local_rank(point):
            point = Point(*point)
            score = 0.
            for geometry in local:
                scale = max(1., float(geometry.length))
                score += float(geometry.distance(point)) / scale
            return score

        unique = list(dict.fromkeys(tuple(float(v) for v in xy) for xy in raw if covers_box(identity, xy)))
        direct = sorted(enumerate(unique), key=lambda row: (local_rank(row[1]), row[0]))
        point_designs[identity] = [xy for _, xy in direct]

    movable = sorted(active - fixed.keys())
    if not movable and verification:
        return {"candidates": list(verification.values()), "linear": lp, "status": "VERIFIED_FEASIBLE",
                "rejected": rejected, "coverage": {}, "methods": method_calls}
    if any(not point_designs.get(identity) for identity in movable):
        return {"candidates": list(verification.values()), "linear": lp,
                "status": "INCOMPLETE_RESOURCE_LIMIT" if exhausted else "UNRESOLVED_CONTINUOUS_SEARCH",
                "rejected": rejected, "coverage": {}, "methods": method_calls}

    def optimizer_margin(x, to_positions):
        nonlocal evaluations
        evaluations += 1
        if monotonic() >= deadline or evaluations > max_function_evaluations:
            raise TimeoutError("nonlinear search budget")
        equalities, inequalities = [], []
        positions = to_positions(x)
        for relation in hard:
            if relation.kind == "region":
                geometry = _working_geometry(relation, transforms)
                position = positions[relation.ids[0]]
                point = Point(*position)
                if geometry.geom_type in {"Polygon", "MultiPolygon"}:
                    inside = geometry.covers(point)
                    inequalities.append(geometry.boundary.distance(point) if inside
                                        else -geometry.distance(point))
                    continue
                if geometry.geom_type == "LineString" and len(geometry.coords) == 2:
                    a, b = np.asarray(geometry.coords)
                    length = hypot(*(b - a))
                    tangent = np.asarray(_unit(b - a))
                    station = (point.x - a[0]) * tangent[0] + (point.y - a[1]) * tangent[1]
                    equalities.append(_signed_side(geometry, position))
                    inequalities.extend((station, length - station))
                    continue
                if geometry.geom_type == "Point":
                    equalities.extend(np.asarray(position) - np.asarray(geometry.coords[0]))
                    continue
            eq, ineq = relation_margins((relation,), positions, transforms)
            equalities.extend(eq)
            inequalities.extend(ineq)
        return np.asarray(equalities), np.asarray(inequalities)

    if any(relation.map_id is not None and relation.map_id not in transforms for relation in hard):
        return {"candidates": [], "linear": lp, "status": "UNRESOLVED_MISSING_SHARED_REGISTRATION",
                "rejected": rejected, "coverage": {}, "methods": method_calls}
    prepared = [dict(candidate) | fixed for candidate in lp.get("outer_proposals", ())]
    # One-coordinate components try exact geometric candidates in native
    # deterministic order before allocating nonlinear starts.
    if len(movable) == 1:
        identity = movable[0]
        for point in point_designs[identity]:
            if monotonic() >= deadline:
                exhausted = True
                break
            probe = fixed | {identity: point}
            if not accept(probe, "GEOMETRIC_DESIGN") and len(prepared) < 2 * max_starts:
                prepared.append(probe)
    elif movable:
        # Coordinates originate from shared graph constraints, not independent
        # accepted jitters. Each assembled start is solved/verified as a vector.
        for k in range(max_starts):
            position = {identity: point_designs[identity][(k * (2 * i + 1) + i) % len(point_designs[identity])]
                        for i, identity in enumerate(movable)}
            prepared.append(position | fixed)

    if movable and not exhausted:
        tried = 0
        n = 2 * len(movable)
        for start in prepared:
            if tried >= max_starts or monotonic() >= deadline:
                # A sampled-domain budget never proves a nonlinear negative.
                exhausted = monotonic() >= deadline
                break
            if not all(start.get(identity) is not None for identity in movable):
                continue
            if accept(start, "GEOMETRIC_DESIGN"):
                continue
            tried += 1
            shift = np.asarray([x for identity in movable for x in start[identity]])
            linear_bounds = [(None if lo is None else lo - shift[i], None if hi is None else hi - shift[i])
                             for i, (lo, hi) in enumerate(
                                 value for identity in movable for value in bounds.get(identity, ((None, None),) * 2))]

            def to_positions(z):
                state = np.asarray(z) + shift
                return fixed | {identity: tuple(state[2*i:2*i+2]) for i, identity in enumerate(movable)}

            try:
                margins = optimizer_margin(np.zeros(n), to_positions)
                constraints = [{"type": mode, "fun": lambda x, k=k: optimizer_margin(x, to_positions)[k]}
                               for k, mode in enumerate(("eq", "ineq")) if margins[k].size]
                solution = minimize(lambda x: .5 * np.dot(x, x), np.zeros(n), jac=lambda x: x,
                                    method="SLSQP", bounds=linear_bounds, constraints=constraints,
                                    options={"maxiter": 120, "ftol": 1e-12})
                accept(to_positions(solution.x), "CONSTRAINED_MULTISTART")
            except TimeoutError:
                exhausted = True
                break

    # Round-robin over exact geometry component signatures before limiting the
    # count. Selection only describes sampled coverage.
    def geometry_signature(entry):
        return tuple((identity, k, tuple(j for j, part in enumerate(_parts(geometry))
                     if part.distance(Point(*entry["positions"][identity])) <= 1e-8))
                     for identity, domains in sorted(geometries.items())
                     for k, geometry in enumerate(domains))

    by_component = {}
    for value in verification.values():
        by_component.setdefault(geometry_signature(value), []).append(value)
    selected = []
    for i in range(max((len(values) for values in by_component.values()), default=0)):
        for values in by_component.values():
            if i < len(values):
                selected.append(values[i])
                if len(selected) == max_realizations:
                    break
        if len(selected) == max_realizations:
            break
    coverage = {str(signature): len(values) for signature, values in by_component.items()}
    status = ("INCOMPLETE_RESOURCE_LIMIT" if exhausted else "VERIFIED_FEASIBLE" if selected
              else "UNRESOLVED_CONTINUOUS_SEARCH")
    return {"status": status, "candidates": selected, "linear": lp, "rejected": rejected,
            "coverage": coverage, "methods": method_calls, "evaluations": evaluations,
            "distinct_joint_count_before_cap": len(verification)}


def generate_mode_candidates(relations, initial, transforms=None, *, fixed=None, bounds=None, seed=7124,
                             max_hypotheses=32, max_starts=16, max_realizations=32,
                             time_limit=30., max_function_evaluations=20_000):
    """Search complete configurations for the declared relation hypotheses.

    Other independent positions remain unchanged. A failed component retains a
    diagnostic, and configurations with its unresolved constraints are exposed
    as partial incumbents rather than accepted complete witnesses.
    """
    for name, value in {"seed": seed, "max_hypotheses": max_hypotheses, "max_starts": max_starts,
                        "max_realizations": max_realizations,
                        "max_function_evaluations": max_function_evaluations}.items():
        if type(value) is not int or value < (0 if name == "seed" else 1):
            raise ValueError(f"invalid search parameter {name}")
    if type(time_limit) is bool or not isfinite(time_limit) or time_limit <= 0.:
        raise ValueError("invalid search duration")
    bounds, fixed, transforms = bounds or {}, fixed or {}, transforms or {}
    initial = {key: None if value is None else _xy(value, f"initial.{key}") for key, value in initial.items()}
    fixed = {key: _xy(value, f"fixed.{key}") for key, value in fixed.items()}
    groups = {}
    for relation in relations:
        if relation.alternative_group is not None:
            groups.setdefault(relation.alternative_group, set()).add(relation.alternative_id)
    hypothesis_count = prod(len(values) for values in groups.values())
    group_keys = sorted(groups)
    branches = []
    complete = []
    partial = []
    deadline = monotonic() + time_limit
    remaining_evaluations = max_function_evaluations
    frontier_reason = None
    for index, key in enumerate(product(*(sorted(groups[group]) for group in group_keys))):
        if (index >= max_hypotheses or monotonic() >= deadline or remaining_evaluations <= 0):
            frontier_reason = ("HYPOTHESIS_LIMIT" if index >= max_hypotheses else
                               "TIME_LIMIT" if monotonic() >= deadline else "FUNCTION_EVALUATION_LIMIT")
            break
        choices = dict(zip(group_keys, key, strict=True))
        active = select_relations(relations, choices)
        components = list(_scenario_components(active))
        reports = []
        for component in components:
            if monotonic() >= deadline or remaining_evaluations <= 0:
                reports.append({"ids": component, "status": "INCOMPLETE_RESOURCE_LIMIT", "candidates": []})
                continue
            keyset = set(component)
            factors = [relation for relation in active if set(relation.ids) <= keyset]
            report = _component_candidates(
                factors, {i: initial[i] for i in component if i in initial}, transforms,
                {i: fixed[i] for i in component if i in fixed},
                {i: bounds[i] for i in component if i in bounds},
                seed, deadline, max_starts, max_realizations, remaining_evaluations,
            )
            remaining_evaluations -= report.get("evaluations", 0)
            reports.append({"ids": component} | report)
        size = max((len(report["candidates"]) for report in reports), default=1)
        seen = set()
        for sample in range(max(1, size)):
            positions = initial | fixed
            methods = []
            for report in reports:
                candidates = report["candidates"]
                if not candidates:
                    positions.update({identity: fixed.get(identity) for identity in report["ids"]})
                    continue
                candidate = candidates[sample % len(candidates)]
                positions.update(candidate["positions"])
                methods.append(candidate["candidate_method"])
            signature = tuple((identity, positions[identity]) for identity in sorted(positions))
            if signature in seen:
                continue
            seen.add(signature)
            checks = relation_residuals(active, positions, transforms)
            passed = all(row["status"] == "SATISFIED" for row in checks if row["hard"])
            target = complete if passed else partial
            target.append({"choices": choices, "positions": positions, "verification": checks,
                           "candidate_method": methods, "status": "VERIFIED_FEASIBLE" if passed
                           else "PARTIAL_UNRESOLVED"})
        summary = [{key: value for key, value in report.items() if key != "candidates"}
                   | {"accepted_count": len(report["candidates"])} for report in reports]
        branches.append({"choices": choices, "components": summary, "configuration_count": len(seen),
                         "candidate_product_upper": prod(len(r["candidates"]) for r in reports)})
    exhausted = frontier_reason is not None or any(
        component["status"] == "INCOMPLETE_RESOURCE_LIMIT"
        for branch in branches for component in branch["components"]
    )
    return {
        "status": ("INCOMPLETE_RESOURCE_LIMIT" if exhausted else "VERIFIED_FEASIBLE" if complete
                   else "UNRESOLVED_CONTINUOUS_SEARCH"),
        "realizations": complete, "partial_incumbents": partial, "branches": branches,
        "hypothesis_count": hypothesis_count, "unvisited_hypothesis_count": hypothesis_count - len(branches),
        "frontier_reason": frontier_reason, "accepted_count": len(complete),
        "requested_per_hypothesis": max_realizations, "function_evaluations":
            max_function_evaluations - max(0, remaining_evaluations),
        "unresolved_branch_count": sum(any(not c["accepted_count"] for c in b["components"])
                                       for b in branches),
        "selection": "DETERMINISTIC_MATCHED_COMPONENT_DESIGN_NO_PROBABILITY",
    }
