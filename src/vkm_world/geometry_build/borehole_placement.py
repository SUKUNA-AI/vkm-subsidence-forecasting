"""Source-bounded borehole placements for the input to a later geology build.

A placement is a conditional realization. This module does not assign identities,
register drawings or infer a common coordinate or height datum.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
import hashlib
from itertools import islice, product
import json
from math import isfinite, prod, sqrt, tau, ulp
from numbers import Real
from time import monotonic
from typing import Any

import numpy as np

VERSION = "vkm.borehole_placement/2"
MAX_CANDIDATES_PER_DOMAIN = 32
DEFAULT_MAX_CASES = 256
DEFAULT_MAX_PAIR_CHECKS = 200_000
DEFAULT_TIME_LIMIT_S = 30.0
DEFAULT_NODE_LIMIT = 50_000


@dataclass(frozen=True)
class Domain:
    constraint_id: str
    site_id: str
    frame_id: str
    geometry: dict[str, Any]
    fixed: bool = False
    alternative_group: str | None = None
    alternative_id: str | None = None


@dataclass(frozen=True)
class Relative:
    """Closed component/distance intervals for subject minus anchor in one frame."""

    constraint_id: str
    site_id: str
    anchor_id: str
    frame_id: str
    dx: tuple[float | None, float | None] = (None, None)
    dy: tuple[float | None, float | None] = (None, None)
    distance: tuple[float | None, float | None] = (None, None)
    alternative_group: str | None = None
    alternative_id: str | None = None

    def covers(self, site, anchor) -> bool:
        a, b = site - anchor
        # Subtraction at a large local coordinate can round a valid exact offset
        # by a few representable steps. This is not a scientific tolerance.
        tx, ty = (2 * ulp(max(abs(float(s)), abs(float(t)))) for s, t in zip(site, anchor, strict=True))
        return all(
            (low is None or value >= low - precision) and
            (high is None or value <= high + precision)
            for value, (low, high), precision in zip(
                (a, b, float(np.hypot(a, b))), (self.dx, self.dy, self.distance),
                (tx, ty, tx + ty + 2 * ulp(float(np.hypot(a, b)))), strict=True
            )
        )


@dataclass
class _SearchLimits:
    deadline: float
    remaining_pairs: int
    node_limit: int

    def remaining_time(self) -> float:
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("TIME_LIMIT")
        return remaining

    def consume_pairs(self, count: int) -> None:
        if count > self.remaining_pairs:
            raise TimeoutError("RELATION_PAIR_LIMIT")
        self.remaining_pairs -= count
        self.remaining_time()


def _active(item: Domain | Relative, choices: dict[str, str]) -> bool:
    return item.alternative_group is None or choices[item.alternative_group] == item.alternative_id


def _seed(seed: int, *keys: str) -> int:
    data = json.dumps([seed, *keys], ensure_ascii=False, separators=(",", ":")).encode()
    return int.from_bytes(hashlib.sha256(data).digest()[:8], "little")


def _components(geometry):
    if geometry.geom_type in {"MultiPoint", "MultiLineString", "MultiPolygon", "GeometryCollection"}:
        for part in geometry.geoms:
            yield from _components(part)
    elif not geometry.is_empty:
        yield geometry


def _polygon_design(polygon, seed):
    from scipy.stats.qmc import Halton
    from shapely.geometry import LineString, Point

    rectangle = np.asarray(polygon.minimum_rotated_rectangle.exterior.coords)[:4]
    sides = np.roll(rectangle, -1, axis=0) - rectangle
    length = np.linalg.norm(sides, axis=1)
    axis = sides[np.argmax(length)] / length.max()
    basis = np.stack((axis, [-axis[1], axis[0]]), axis=1)
    center = rectangle[0]
    uv = (rectangle - center) @ basis
    lower, upper = uv.min(axis=0), uv.max(axis=0)
    selected = {tuple(polygon.representative_point().coords[0])}
    for point in lower + Halton(2, scramble=True, seed=seed).random(256) * (upper - lower):
        candidate = Point(center + basis @ point)
        if polygon.covers(candidate):
            selected.add(tuple(candidate.coords[0]))
            if len(selected) == MAX_CANDIDATES_PER_DOMAIN:
                return selected

    # A straight cross-section finds support even in a thin bent or perforated
    # component occupying little of its own rotated rectangle.
    for u in np.linspace(lower[0], upper[0], MAX_CANDIDATES_PER_DOMAIN + 2)[1:-1]:
        line = LineString([center + basis @ [u, lower[1]], center + basis @ [u, upper[1]]])
        for part in _components(polygon.intersection(line)):
            point = part.interpolate(0.5, normalized=True) if part.geom_type == "LineString" else part
            if polygon.distance(point) <= 1e-9:
                selected.add(tuple(point.coords[0]))
                if len(selected) == MAX_CANDIDATES_PER_DOMAIN:
                    return selected
    return selected


def _domain_points(geometry, seed, limits) -> np.ndarray:
    """Generate a reproducible design inside each distinct geometric component."""
    candidates = set()
    for part in _components(geometry):
        limits.remaining_time()
        if part.geom_type == "Point":
            candidates.add(tuple(part.coords[0]))
        elif part.geom_type == "LineString":
            candidates.update(tuple(part.interpolate(station, normalized=True).coords[0])
                              for station in np.linspace(0, 1, MAX_CANDIDATES_PER_DOMAIN))
        elif part.geom_type == "Polygon":
            candidates.update(_polygon_design(part, seed))
        else:
            raise ValueError(f"unsupported placement geometry: {part.geom_type}")
    return np.asarray(sorted(candidates), dtype=float)


def _window(x0, y0, x1, y1):
    from shapely.geometry import LineString, Point, box

    if x0 == x1 and y0 == y1:
        return Point(x0, y0)
    if x0 == x1 or y0 == y1:
        return LineString([(x0, y0), (x1, y1)])
    return box(x0, y0, x1, y1)


def _clip_at_anchor(domain, anchor, x_range, y_range, distance_upper):
    from shapely.geometry import GeometryCollection

    x0, y0, x1, y1 = domain.bounds

    def clip(padding):
        bounds = []
        for low, high, center, offsets in ((x0, x1, anchor.x, x_range), (y0, y1, anchor.y, y_range)):
            a, b = offsets
            if distance_upper is not None:
                a = -distance_upper if a is None else max(a, -distance_upper)
                b = distance_upper if b is None else min(b, distance_upper)
            guard = padding * ulp(max(abs(low), abs(high), abs(center)))
            low = max(low, center + a - guard) if a is not None else low
            high = min(high, center + b + guard) if b is not None else high
            bounds.append((low, high))
        (ax, bx), (ay, by) = bounds
        if ax > bx or ay > by:
            return GeometryCollection()
        return domain.intersection(_window(ax, ay, bx, by))

    clipped = clip(0)
    return clip(2) if clipped.is_empty else clipped


def _reverse(interval):
    return tuple(-limit if limit is not None else None for limit in reversed(interval))


def _spread(candidates: np.ndarray, previous: list[np.ndarray], target: np.ndarray) -> np.ndarray:
    extent = np.ptp(candidates, axis=0)
    diameter = float(np.linalg.norm(extent))
    if not diameter:
        return np.zeros(len(candidates))
    if previous:
        return np.min(
            [np.linalg.norm(candidates - x, axis=1) for x in previous], axis=0
        ) / diameter
    return -np.linalg.norm(candidates - target, axis=1) / diameter


def _constrained_assignment(candidates, scores, relations, limits):
    """Choose a jointly feasible discrete design point with a max-spread objective."""
    limits.remaining_time()
    if not relations:
        return {key: int(np.argmax(scores[key])) for key in candidates}

    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix

    index = 0
    addressing = {}
    row_addresses = []
    col_addresses = []
    lower = []
    upper = []

    def add(indices, lo, hi):
        row_addresses.extend([len(lower)] * len(indices))
        col_addresses.extend(indices)
        lower.append(lo)
        upper.append(hi)

    for site, points in candidates.items():
        addresses = list(range(index, index + len(points)))
        addressing[site] = addresses
        add(addresses, 1, 1)
        index += len(addresses)

    for relation in relations:
        a, b = relation.site_id, relation.anchor_id
        limits.consume_pairs(len(candidates[a]) * len(candidates[b]))
        for i, site in enumerate(candidates[a]):
            for j, anchor in enumerate(candidates[b]):
                if not relation.covers(site, anchor):
                    add([addressing[a][i], addressing[b][j]], 0, 1)

    a_matrix = coo_matrix(
        (np.ones(len(row_addresses)), (row_addresses, col_addresses)),
        shape=(len(lower), index),
    ).tocsc()
    fit = milp(
        -np.concatenate([scores[key] for key in candidates]),
        integrality=np.ones(index, dtype=int),
        bounds=Bounds(np.zeros(index), np.ones(index)),
        constraints=LinearConstraint(a_matrix, lower, upper),
        options={"time_limit": limits.remaining_time(), "node_limit": limits.node_limit},
    )
    if fit.status == 2:
        return None
    if fit.status == 1:
        raise TimeoutError("OPTIMIZER_LIMIT")
    if not fit.success:
        raise RuntimeError(f"placement optimizer did not return a decision: {fit.message}")
    return {key: int(np.argmax(fit.x[addresses])) for key, addresses in addressing.items()}


def _validate_horizontal_coordinates(geometry):
    if geometry.get("type") == "GeometryCollection":
        for member in geometry["geometries"]:
            _validate_horizontal_coordinates(member)
        return
    remaining = [geometry["coordinates"]]
    while remaining:
        values = remaining.pop()
        if isinstance(values, (tuple, list, np.ndarray)):
            remaining.extend(values)
        elif not isinstance(values, Real) or type(values) is bool or not isfinite(values):
            raise ValueError("horizontal coordinates must be finite real numbers")


def _validate(domains, relations):
    from shapely.geometry import shape

    ids = set()
    geometry = {}
    for item in (*domains, *relations):
        if not item.constraint_id or item.constraint_id in ids or not item.site_id or not item.frame_id:
            raise ValueError("constraints require unique IDs, explicit subjects and coordinate frames")
        ids.add(item.constraint_id)
        if bool(item.alternative_group) != bool(item.alternative_id):
            raise ValueError(f"incomplete alternative {item.constraint_id}")
        if isinstance(item, Relative):
            if item.site_id == item.anchor_id or not item.anchor_id:
                raise ValueError(f"invalid relative anchor {item.constraint_id}")
            for low, high in (item.dx, item.dy, item.distance):
                if not all(value is None or (isinstance(value, Real) and type(value) is not bool
                                               and isfinite(value)) for value in (low, high)):
                    raise ValueError(f"invalid relation magnitude {item.constraint_id}")
                if low is not None and high is not None and low > high:
                    raise ValueError(f"backwards relation interval {item.constraint_id}")
            if any(x is not None and x < 0 for x in item.distance):
                raise ValueError(f"negative distance {item.constraint_id}")
        else:
            if type(item.fixed) is not bool:
                raise ValueError(f"fixed must be a boolean: {item.constraint_id}")
            _validate_horizontal_coordinates(item.geometry)
            obj = shape(item.geometry)
            if obj.is_empty or not obj.is_valid or obj.has_z or not all(map(isfinite, obj.bounds)):
                raise ValueError(f"invalid native horizontal domain {item.constraint_id}")
            if item.fixed and obj.geom_type != "Point":
                raise ValueError(f"fixed domain is not a point {item.constraint_id}")
            if not item.fixed and all(part.geom_type == "Point" for part in _components(obj)):
                raise ValueError(f"unbounded approximate point {item.constraint_id}")
            geometry[item.constraint_id] = obj
    return geometry


def _radial_points(region, anchor, interval, limits):
    """Sample a bounded annulus and its exact intersections with source segments."""
    from shapely.geometry import Point

    low, high = interval
    radii = {value for value in interval if value is not None}
    if low is not None and high is not None:
        radii.add(low + (high - low) / 2)
    center = np.asarray(anchor.coords[0])
    angles = np.linspace(0, tau, MAX_CANDIDATES_PER_DOMAIN, endpoint=False)
    directions = np.column_stack((np.cos(angles), np.sin(angles)))
    results = set()
    for part in _components(region):
        limits.remaining_time()
        if part.geom_type == "Point":
            continue
        boundaries = [part] if part.geom_type == "LineString" else [part.exterior, *part.interiors]
        for radius in sorted(radii):
            for target in center + radius * directions:
                if part.covers(Point(*target)):
                    results.add(tuple(target))
            for boundary in boundaries:
                vertices = np.asarray(boundary.coords)
                for a, b in zip(vertices[:-1], vertices[1:], strict=True):
                    edge = b - a
                    length_squared = float(edge @ edge)
                    if not length_squared:
                        continue
                    t = float((center - a) @ edge) / length_squared
                    nearest = a + t * edge
                    residual = radius**2 - float((nearest - center) @ (nearest - center))
                    if residual < -4 * ulp(radius**2):
                        continue
                    dt = sqrt(max(0.0, residual) / length_squared)
                    for root in (t - dt, t + dt):
                        if 0 <= root <= 1:
                            results.add(tuple(a + root * edge))
    return results


def _linear_feasibility(domains, relations, limits, count):
    """Find a continuous witness or a contradiction in an outer linear model."""
    from scipy.optimize import linprog
    from shapely.geometry import Point
    from shapely.geometry.polygon import orient

    sites = sorted(domains)
    offsets = {site: 2 * i for i, site in enumerate(sites)}
    origins = {site: np.asarray(domains[site].representative_point().coords[0]) for site in sites}
    low_high = []
    rows, rhs = [], []

    def add(terms, upper):
        row = np.zeros(2 * len(sites))
        for key, vector in terms:
            i = offsets[key]
            row[i:i + 2] += vector
        rows.append(row)
        rhs.append(upper)

    for site in sites:
        limits.remaining_time()
        hull = domains[site].convex_hull
        ox, oy = origins[site]
        x0, y0, x1, y1 = hull.bounds
        low_high.extend(((x0 - ox, x1 - ox), (y0 - oy, y1 - oy)))
        if hull.geom_type == "Point":
            continue
        if hull.geom_type == "LineString":
            a, b = np.asarray(hull.coords) - origins[site]
            tang = b - a
            normal = np.asarray([tang[1], -tang[0]]) / np.linalg.norm(tang)
            bound = float(normal @ a)
            add([(site, normal)], bound)
            add([(site, -normal)], -bound)
            continue
        ring = np.asarray(orient(hull, sign=1).exterior.coords) - origins[site]
        for a, b in zip(ring[:-1], ring[1:], strict=True):
            tang = b - a
            normal = np.asarray([tang[1], -tang[0]]) / np.linalg.norm(tang)
            add([(site, normal)], float(normal @ a))
    for relation in relations:
        a, b = relation.site_id, relation.anchor_id
        for i, (lower, upper) in enumerate((relation.dx, relation.dy)):
            axis = np.eye(2)[i]
            shift = (origins[a] - origins[b])[i]
            if upper is not None:
                add([(a, axis), (b, -axis)], upper - shift)
            if lower is not None:
                add([(a, -axis), (b, axis)], shift - lower)
    def solve(objective):
        result = linprog(objective, A_ub=rows or None, b_ub=rhs or None, bounds=low_high,
                         method="highs", options={"time_limit": limits.remaining_time()})
        if result.status == 1:
            raise TimeoutError("OPTIMIZER_LIMIT")
        return result

    def positions(vector):
        return {site: vector[i:i + 2] + origins[site] for site, i in offsets.items()}

    def valid(witness):
        return (all(domains[site].distance(Point(*xy)) <= 2 * ulp(max(1., float(np.abs(xy).max())))
                    for site, xy in witness.items())
                and all(r.covers(witness[r.site_id], witness[r.anchor_id]) for r in relations))

    initial = solve(np.zeros(2 * len(sites)))
    if initial.status == 2:
        return "CONTINUOUS_INFEASIBLE_LINEAR_OUTER_BOUND", []
    if not initial.success:
        return "UNRESOLVED_CONTINUOUS_SEARCH", []

    extremes = [initial.x]
    widths = [high - low for low, high in low_high]
    if count > 1 or not valid(positions(initial.x)):
        for axis in sorted(range(len(widths)), key=lambda i: (-widths[i], i))[:8]:
            if not widths[axis]:
                break
            objective = np.eye(len(widths))[axis]
            results = [solve(objective), solve(-objective)]
            if not all(result.success for result in results):
                continue
            a, b = (result.x for result in results)
            extremes.extend((1 - t) * a + t * b for t in np.linspace(0., 1., min(count + 2, 10)))
            if len({tuple(v) for v in extremes if valid(positions(v))}) >= count:
                break

    unique = {}
    for vector in extremes:
        proposal = positions(vector)
        if valid(proposal):
            unique[tuple(float(x) for xy in proposal.values() for x in xy)] = proposal
    if not unique:
        return "UNRESOLVED_CONTINUOUS_SEARCH", []
    return "CONTINUOUS_WITNESS", [value for _, value in sorted(unique.items())]


def _frame_case(frame, domains, relations, shapes, seed, count, limits):
    from shapely.geometry import Point

    by_site: dict[str, list[Domain]] = defaultdict(list)
    for domain in domains:
        by_site[domain.site_id].append(domain)
    common = {}
    for site, records in sorted(by_site.items()):
        allowed = shapes[records[0].constraint_id]
        for record in records[1:]:
            allowed = allowed.intersection(shapes[record.constraint_id])
        if allowed.is_empty:
            return {"status": "CONFLICT_EMPTY_INTERSECTION", "site_id": site,
                    "feasibility": "CONTINUOUS_INFEASIBLE_DOMAIN_INTERSECTION", "realizations": []}
        common[site] = allowed

    # A purely relative subject can be sampled once an existing physical
    # anchor and finite bounds supply a domain. Its coordinates remain a choice.
    for _ in range(len(relations)):
        limits.remaining_time()
        new_sites = 0
        for relation in relations:
            if relation.site_id in common or relation.anchor_id not in common:
                continue
            anchor = common[relation.anchor_id]
            radius = relation.distance[1]
            dx = tuple(value if value is not None else fallback
                       for value, fallback in zip(relation.dx, (None, None) if radius is None else (-radius, radius), strict=True))
            dy = tuple(value if value is not None else fallback
                       for value, fallback in zip(relation.dy, (None, None) if radius is None else (-radius, radius), strict=True))
            if None in (*dx, *dy):
                continue
            x0, y0, x1, y1 = anchor.bounds
            common[relation.site_id] = _window(x0 + dx[0], y0 + dy[0], x1 + dx[1], y1 + dy[1])
            new_sites += 1
        if not new_sites:
            break
    if any(r.site_id not in common or r.anchor_id not in common for r in relations):
        return {"status": "UNRESOLVED_RELATION_ANCHOR", "realizations": []}

    for _ in range(len(common)):
        limits.remaining_time()
        reduced = False
        for relation in relations:
            directions = (
                (relation.site_id, relation.anchor_id, relation.dx, relation.dy),
                (relation.anchor_id, relation.site_id, _reverse(relation.dx), _reverse(relation.dy)),
            )
            for subject, anchor, dx, dy in directions:
                if common[anchor].geom_type != "Point":
                    continue
                if common[subject].geom_type == "Point":
                    continue
                clipped = _clip_at_anchor(common[subject], common[anchor], dx, dy, relation.distance[1])
                if clipped.is_empty:
                    return {"status": "NO_FEASIBLE_SAMPLED_REALIZATION",
                            "feasibility": "CONTINUOUS_INFEASIBLE_ANCHORED_WINDOW",
                            "constraint_id": relation.constraint_id, "realizations": []}
                reduced |= clipped.geom_type == "Point"
                common[subject] = clipped
        if not reduced:
            break

    by_coords = {}
    origins = {}
    summaries = {}
    for site, region in sorted(common.items()):
        records = by_site.get(site, [])
        points = set(map(tuple, _domain_points(region, _seed(seed, frame, site), limits)))
        for relation in relations:
            if site not in (relation.site_id, relation.anchor_id) or relation.distance == (None, None):
                continue
            other = relation.anchor_id if site == relation.site_id else relation.site_id
            if common[other].geom_type == "Point":
                points.update(_radial_points(region, common[other], relation.distance, limits))
        points = np.asarray(sorted(points), dtype=float)
        target = region.representative_point()
        origins[site] = np.asarray([target.x, target.y])
        by_coords[site] = points
        relational = [r.constraint_id for r in relations if site in (r.site_id, r.anchor_id)]
        summaries[site] = {
            "constraints": [record.constraint_id for record in records] or relational,
            "relative_constraints": relational,
            "fixed": any(record.fixed for record in records),
            "candidate_count": len(points),
            "bounds": list(region.bounds),
            "area_native_squared": region.area,
            "length_native": region.length if region.geom_type in {"LineString", "MultiLineString"} else None,
            "component_count": sum(1 for _ in _components(region)),
        }

    realizations = []
    last: dict[str, list[np.ndarray]] = defaultdict(list)
    feasibility = "VERIFIED_REALIZATION"
    interruption = None
    try:
        for _ in range(count):
            scores = {key: _spread(points, last.get(key, []), origins[key]) for key, points in by_coords.items()}
            assignment = _constrained_assignment(by_coords, scores, relations, limits)
            if assignment is None:
                if all(common[key].geom_type == "Point" for key in common):
                    feasibility = "CONTINUOUS_INFEASIBLE_FIXED_POSITIONS"
                    break
                feasibility, witnesses = _linear_feasibility(common, relations, limits, count)
                if not witnesses:
                    break
                for key in by_coords:
                    points = np.vstack([by_coords[key], *[w[key] for w in witnesses]])
                    by_coords[key] = np.unique(points, axis=0)
                    summaries[key]["candidate_count"] = len(by_coords[key])
                witness = witnesses[0]
                assignment = {key: int(np.flatnonzero(np.all(by_coords[key] == xy, axis=1))[0])
                              for key, xy in witness.items()}
            proposal = []
            for key, i in assignment.items():
                selected = by_coords[key][i]
                records = by_site.get(key, [])
                point = Point(*selected)
                if not all(shapes[record.constraint_id].distance(point) <= 1e-9 for record in records):
                    raise RuntimeError(f"chosen placement outside the source domain for {key}")
                proposal.append({
                    "site_id": key,
                    "frame_id": frame,
                    "xy": selected.tolist(),
                    "status": "DERIVATION" if summaries[key]["fixed"] else "MODEL_CHOICE",
                    "coordinate_role": "SOURCE_FIXED" if summaries[key]["fixed"] else "SCENARIO",
                    "constraints": summaries[key]["constraints"],
                })
            positions = {item["site_id"]: np.asarray(item["xy"]) for item in proposal}
            if not all(r.covers(positions[r.site_id], positions[r.anchor_id]) for r in relations):
                raise RuntimeError("optimizer proposed a placement outside a relative constraint")
            if proposal in realizations:
                break
            for key, selected in positions.items():
                last[key].append(selected)
            realizations.append(proposal)
    except TimeoutError as exc:
        interruption = str(exc)
    for key, samples in last.items():
        summaries[key]["max_shift_from_first_native"] = max(
            float(np.linalg.norm(sample - samples[0])) for sample in samples
        )
        summaries[key]["distinct_returned_positions"] = len({tuple(sample) for sample in samples})
    if interruption:
        status = "INCOMPLETE_RESOURCE_LIMIT"
    else:
        status = "PLACED" if realizations else "NO_FEASIBLE_SAMPLED_REALIZATION"
    return {"status": status, "feasibility": feasibility if realizations or not interruption else "UNRESOLVED",
            "resource_limit": interruption, "domains": summaries, "realizations": realizations}


def realize(domains, relations=(), *, seed: int, count: int = 8,
            max_cases: int = DEFAULT_MAX_CASES, time_limit_s: float = DEFAULT_TIME_LIMIT_S,
            max_pair_checks: int = DEFAULT_MAX_PAIR_CHECKS, node_limit: int = DEFAULT_NODE_LIMIT) -> dict:
    """Report one native-frame ensemble for each coherent assignment of alternatives."""
    if type(seed) is not int or type(count) is not int or count < 1:
        raise ValueError("an integer seed and positive realization count are required")
    if any(type(value) is not int or value < 1 for value in (max_cases, max_pair_checks, node_limit)):
        raise ValueError("case, relation-pair and optimizer-node budgets must be positive integers")
    if (not isinstance(time_limit_s, Real) or type(time_limit_s) is bool
            or not isfinite(time_limit_s) or time_limit_s <= 0):
        raise ValueError("time_limit_s must be a finite positive number")
    limits = _SearchLimits(monotonic() + time_limit_s, max_pair_checks, node_limit)
    domains = sorted(domains, key=lambda item: item.constraint_id)
    relations = sorted(relations, key=lambda item: item.constraint_id)
    shapes = _validate(domains, relations)
    cases = []
    frame_coverage = []
    for frame in sorted({item.frame_id for item in (*domains, *relations)}):
        frame_domains = [item for item in domains if item.frame_id == frame]
        frame_relations = [item for item in relations if item.frame_id == frame]
        groups = defaultdict(set)
        participants = defaultdict(lambda: defaultdict(set))
        for item in (*frame_domains, *frame_relations):
            if item.alternative_group is not None:
                groups[item.alternative_group].add(item.alternative_id)
                if isinstance(item, Domain):
                    participants[item.alternative_group][item.alternative_id].add(item.site_id)
        expected = {key: set().union(*alternatives.values()) for key, alternatives in participants.items()}
        keys = sorted(groups)
        combinations = prod(len(group) for group in groups.values())
        processed = 0
        alternatives = product(*(sorted(groups[key]) for key in keys))
        for options in islice(alternatives, max(0, max_cases - len(cases))):
            if monotonic() >= limits.deadline:
                break
            processed += 1
            choice = dict(zip(keys, options, strict=True))
            dd = [item for item in frame_domains if _active(item, choice)]
            rr = [item for item in frame_relations if _active(item, choice)]
            ids = [item.constraint_id for item in (*dd, *rr)]
            missing = sorted({
                site for key, sites in expected.items() for site in sites - participants[key][choice[key]]
            })
            if missing:
                result = {"status": "INCOMPLETE_ALTERNATIVE_PARTICIPANTS",
                          "missing_site_ids": missing, "realizations": []}
            else:
                try:
                    result = _frame_case(frame, dd, rr, shapes, seed, count, limits)
                except TimeoutError as exc:
                    result = {"status": "INCOMPLETE_RESOURCE_LIMIT", "feasibility": "UNRESOLVED",
                              "resource_limit": str(exc), "realizations": []}
            binding = json.dumps([asdict(item) for item in (*dd, *rr)], ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":"), allow_nan=False)
            binding_sha = hashlib.sha256(binding.encode()).hexdigest()
            case_key = _seed(seed, VERSION, frame, json.dumps(choice, sort_keys=True), binding_sha)
            case_id = f"BHP-{case_key:016x}"
            result["realizations"] = [
                {"id": f"{case_id}-{i:02d}-{_seed(seed, json.dumps(positions, sort_keys=True)):016x}",
                 "seed": seed, "positions": positions}
                for i, positions in enumerate(result["realizations"], start=1)
            ]
            generated = len(result["realizations"])
            coverage = "COMPLETE" if generated == count else "PARTIAL" if generated else "EMPTY"
            cases.append({
                "requested_realizations": count, "generated_realizations": generated,
                "design_coverage": coverage,
                "id": case_id,
                "frame_id": frame,
                "alternatives": choice,
                "input_constraints": ids,
                "input_content_sha256": binding_sha,
                "seed": seed,
                **result,
            })
        frame_coverage.append({"frame_id": frame, "total_combinations": combinations,
                               "explored_combinations": processed, "omitted_combinations": combinations - processed,
                               "omission_reason": ("TIME_LIMIT" if monotonic() >= limits.deadline else "CASE_LIMIT")
                               if processed < combinations else None})
    unfinished = (any(frame["omitted_combinations"] for frame in frame_coverage)
                  or any(case["status"] == "INCOMPLETE_RESOURCE_LIMIT" for case in cases))
    return {
        "version": VERSION,
        "status": "INCOMPLETE_RESOURCE_LIMIT" if unfinished else "SEARCH_COMPLETED",
        "frame_coverage": frame_coverage,
        "method": "domain clipping, oriented Halton and transects, bounded discrete search, anchored radial witnesses and linear continuous witnesses",
        "interpretation": "conditional coverage, no probability or transformation inferred",
        "server_seed": seed,
        "cases": cases,
    }
