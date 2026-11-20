"""Source-bounded borehole placements for the input to a later geology build.

A placement is a conditional realization. This module does not assign identities,
register drawings or infer a common coordinate or height datum.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
import hashlib
from itertools import product
import json
from math import isfinite
from typing import Any

import numpy as np

VERSION = "vkm.borehole_placement/1"
MAX_CANDIDATES_PER_DOMAIN = 32


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
        return all(
            (lo is None or value >= lo) and (hi is None or value <= hi)
            for value, (lo, hi) in zip(
                (a, b, float(np.hypot(a, b))), (self.dx, self.dy, self.distance), strict=True
            )
        )


def _active(item: Domain | Relative, choices: dict[str, str]) -> bool:
    return item.alternative_group is None or choices[item.alternative_group] == item.alternative_id


def _seed(seed: int, *keys: str) -> int:
    data = json.dumps([seed, *keys], ensure_ascii=False, separators=(",", ":")).encode()
    return int.from_bytes(hashlib.sha256(data).digest()[:8], "little")


def _domain_points(geometry, seed: int) -> np.ndarray:
    """Cover each connected part using line stations or a seeded Halton design."""
    from scipy.stats.qmc import Halton
    from shapely.geometry import Point

    candidates: set[tuple[float, float]] = set()
    pieces = [geometry]
    while pieces:
        part = pieces.pop()
        if part.geom_type in {"MultiPoint", "MultiLineString", "MultiPolygon", "GeometryCollection"}:
            pieces.extend(part.geoms)
        elif part.geom_type == "Point":
            candidates.add((part.x, part.y))
        elif part.geom_type == "LineString":
            for station in np.linspace(0, 1, MAX_CANDIDATES_PER_DOMAIN):
                p = part.interpolate(station, normalized=True)
                candidates.add((p.x, p.y))
        elif part.geom_type == "Polygon":
            center = part.representative_point()
            candidates.add((center.x, center.y))
            bounds = np.asarray(part.bounds)
            proposals = Halton(2, scramble=True, seed=seed).random(MAX_CANDIDATES_PER_DOMAIN * 8)
            covered = 0
            for x, y in bounds[:2] + proposals * (bounds[2:] - bounds[:2]):
                if part.covers(Point(x, y)):
                    candidates.add((float(x), float(y)))
                    covered += 1
                    if covered >= MAX_CANDIDATES_PER_DOMAIN - 1:
                        break
        else:
            raise ValueError(f"unsupported placement geometry: {part.geom_type}")
    return np.asarray(sorted(candidates), dtype=float)


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


def _constrained_assignment(candidates, scores, relations):
    """Choose a jointly feasible discrete design point with a max-spread objective."""
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
    )
    if fit.status == 2:
        return None
    if not fit.success:
        raise RuntimeError(f"placement optimizer did not return a decision: {fit.message}")
    return {key: int(np.argmax(fit.x[addresses])) for key, addresses in addressing.items()}


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
                if (low is not None and not isfinite(low)) or (high is not None and not isfinite(high)):
                    raise ValueError(f"invalid relation magnitude {item.constraint_id}")
                if low is not None and high is not None and low > high:
                    raise ValueError(f"backwards relation interval {item.constraint_id}")
            if any(x is not None and x < 0 for x in item.distance):
                raise ValueError(f"negative distance {item.constraint_id}")
        else:
            obj = shape(item.geometry)
            if obj.is_empty or not obj.is_valid or obj.has_z or not all(map(isfinite, obj.bounds)):
                raise ValueError(f"invalid native horizontal domain {item.constraint_id}")
            if item.fixed and obj.geom_type != "Point":
                raise ValueError(f"fixed domain is not a point {item.constraint_id}")
            if not item.fixed and obj.geom_type in {"Point", "MultiPoint"}:
                raise ValueError(f"unbounded approximate point {item.constraint_id}")
            geometry[item.constraint_id] = obj
    return geometry


def _frame_case(frame, domains, relations, shapes, seed, count):
    from shapely.geometry import Point
    by_site: dict[str, list[Domain]] = defaultdict(list)
    for domain in domains:
        by_site[domain.site_id].append(domain)
    if any(r.site_id not in by_site or r.anchor_id not in by_site for r in relations):
        return {"status": "UNRESOLVED_RELATION_ANCHOR", "realizations": []}

    by_coords = {}
    origins = {}
    summaries = {}
    for site, records in sorted(by_site.items()):
        common = shapes[records[0].constraint_id]
        for record in records[1:]:
            common = common.intersection(shapes[record.constraint_id])
        if common.is_empty:
            return {"status": "CONFLICT_EMPTY_INTERSECTION", "site_id": site, "realizations": []}
        points = _domain_points(common, _seed(seed, frame, site))
        target = common.representative_point()
        origins[site] = np.asarray([target.x, target.y])
        by_coords[site] = points
        summaries[site] = {
            "constraints": [record.constraint_id for record in records],
            "fixed": any(record.fixed for record in records),
            "candidate_count": len(points),
            "bounds": list(common.bounds),
            "area_native_squared": common.area,
            "length_native": common.length if common.geom_type in {"LineString", "MultiLineString"} else None,
        }

    realizations = []
    last: dict[str, list[np.ndarray]] = defaultdict(list)
    for _ in range(count):
        scores = {key: _spread(points, last[key], origins[key]) for key, points in by_coords.items()}
        assignment = _constrained_assignment(by_coords, scores, relations)
        if assignment is None:
            return {"status": "NO_FEASIBLE_SAMPLED_REALIZATION", "realizations": []}
        proposal = []
        for key, i in assignment.items():
            selected = by_coords[key][i]
            records = by_site[key]
            location_refs = summaries[key]["constraints"]
            point = Point(*selected)
            if not all(shapes[record.constraint_id].distance(point) <= 1e-9 for record in records):
                raise RuntimeError(f"chosen placement outside the source domain for {key}")
            proposal.append({
                "site_id": key,
                "frame_id": frame,
                "xy": selected.tolist(),
                "status": "DERIVATION" if summaries[key]["fixed"] else "MODEL_CHOICE",
                "coordinate_role": "SOURCE_FIXED" if summaries[key]["fixed"] else "SCENARIO",
                "constraints": location_refs,
            })
            last[key].append(selected)
        positions = {r["site_id"]: np.asarray(r["xy"]) for r in proposal}
        if not all(r.covers(positions[r.site_id], positions[r.anchor_id]) for r in relations):
            raise RuntimeError("optimizer proposed a placement outside a relative constraint")
        if proposal in realizations:
            break
        realizations.append(proposal)
    for key, samples in last.items():
        summaries[key]["max_shift_from_first_native"] = max(
            float(np.linalg.norm(sample - samples[0])) for sample in samples
        )
    return {"status": "PLACED", "domains": summaries, "realizations": realizations}


def realize(domains, relations=(), *, seed: int, count: int = 8) -> dict:
    """Report one native-frame ensemble for each coherent assignment of alternatives."""
    if type(seed) is not int or type(count) is not int or count < 1:
        raise ValueError("an integer seed and positive realization count are required")
    domains = sorted(domains, key=lambda item: item.constraint_id)
    relations = sorted(relations, key=lambda item: item.constraint_id)
    shapes = _validate(domains, relations)
    cases = []
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
        for options in product(*(sorted(groups[key]) for key in keys)):
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
                result = _frame_case(frame, dd, rr, shapes, seed, count)
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
            cases.append({
                "id": case_id,
                "frame_id": frame,
                "alternatives": choice,
                "input_constraints": ids,
                "input_content_sha256": binding_sha,
                "seed": seed,
                **result,
            })
    return {
        "version": VERSION,
        "method": "geometry intersection, seeded Halton/line coverage, max-spread discrete constrained design",
        "interpretation": "conditional coverage, no probability or transformation inferred",
        "server_seed": seed,
        "cases": cases,
    }
