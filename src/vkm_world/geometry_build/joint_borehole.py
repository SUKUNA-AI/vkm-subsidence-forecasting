"""Shared-frame horizontal adjustment of boreholes and source-map registrations."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix
from scipy.stats import qmc


VERSION = "vkm.borehole_joint_adjustment/1"


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"Expected a finite JSON number, got {type(value).__name__}")
    return float(value)


def _vector(value, size):
    if type(value) is not list or len(value) != size:
        raise ValueError(f"Expected a JSON array of length {size}")
    return tuple(_number(item) for item in value)


def _id(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected a non-empty identifier")
    return value


@dataclass(frozen=True)
class Mark:
    object_id: str
    xy: tuple[float, float]
    scale: float
    group: str
    record_id: str


@dataclass(frozen=True)
class Map:
    id: str
    frame: str
    output_frame: str
    initial: tuple[float, ...]
    bounds: tuple[tuple[float, float], ...] | None
    marks: tuple[Mark, ...]


@dataclass(frozen=True)
class Problem:
    target_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    maps: tuple[Map, ...]
    anchors: tuple[tuple[str, Mark], ...]
    input_sha256: str


def native_to_plane(xy, transform):
    tx, ty, a, b = transform
    u, v = xy
    return tx + a * u - b * v, ty + b * u + a * v


def plane_to_native(xy, transform):
    tx, ty, a, b = transform
    x, y = xy[0] - tx, xy[1] - ty
    scale_squared = a * a + b * b
    return (a * x + b * y) / scale_squared, (-b * x + a * y) / scale_squared


def _mark(row):
    scale = _number(row["scale"])
    if scale <= 0:
        raise ValueError("Residual normalization must be positive")
    return Mark(_id(row["object_id"]), _vector(row["xy"], 2), scale,
                _id(row["group"]), _id(row["record_id"]))


def _ids(rows):
    result = tuple(sorted(_id(item) for item in rows))
    if len(result) != len(set(result)):
        raise ValueError("Duplicate object ID")
    return result


def compile_problem(payload):
    """Validate the JSON boundary before constructing numeric arrays."""
    targets = _ids(payload["target_ids"])
    supports = _ids(payload["support_ids"])
    if not targets or set(targets) & set(supports):
        raise ValueError("Target and support sets must be nonempty/disjoint (support may be empty)")
    allowed_ids = set(targets + supports)
    maps = []
    seen_frames = set()
    seen_map_ids = set()
    used_records = set()
    lineage_cells = set()
    for row in sorted(payload.get("maps", ()), key=lambda item: item["id"]):
        map_id = _id(row["id"])
        frame = _id(row["frame"])
        plane = _id(row["output_frame"])
        if map_id in seen_map_ids or frame in seen_frames:
            raise ValueError("Each native map frame has one shared registration")
        seen_frames.add(frame)
        seen_map_ids.add(map_id)
        initial = _vector(row["initial"], 4)
        if math.hypot(*initial[2:]) <= 0:
            raise ValueError("Similarity registration must have positive scale")
        bounds = None
        if "limits" in row and row["limits"] is not None:
            if len(row["limits"]) != 4:
                raise ValueError("Registration limits require four pairs")
            bounds = tuple(_vector(pair, 2) for pair in row["limits"])
            if any(lo > hi or not lo <= value <= hi
                   for (lo, hi), value in zip(bounds, initial, strict=True)):
                raise ValueError("Reversed limits or initial pose outside limits")
            if all(lo <= 0 <= hi for lo, hi in bounds[2:]):
                raise ValueError("Registration domain includes a zero scale")
        marks = []
        for item in sorted(row["marks"], key=lambda value: value["record_id"]):
            if type(item.get("primary")) is not bool:
                raise ValueError("primary must be a boolean")
            mark = _mark(item)
            if mark.object_id not in allowed_ids:
                raise ValueError("Map mark outside declared object network")
            if not item["primary"]:
                continue
            cell = mark.object_id, mark.group
            if cell in lineage_cells or mark.record_id in used_records:
                raise ValueError(f"Dependent observation reintroduced as primary: {mark.record_id}")
            used_records.add(mark.record_id)
            lineage_cells.add(cell)
            marks.append(mark)
        maps.append(Map(map_id, frame, plane, initial, bounds, tuple(marks)))
    anchors = []
    for item in sorted(payload.get("anchors", ()), key=lambda value: value["record_id"]):
        mark = _mark(item)
        cell = mark.object_id, mark.group
        if mark.object_id not in allowed_ids or cell in lineage_cells or mark.record_id in used_records:
            raise ValueError(f"Undeclared or dependent anchor: {mark.record_id}")
        lineage_cells.add(cell)
        used_records.add(mark.record_id)
        anchors.append((_id(item["frame"]), mark))
    semantic = dict(payload)
    for key in ("target_ids", "support_ids"):
        semantic[key] = sorted(payload[key])
    for key in ("anchors", "relations", "profiles", "modes"):
        if key in semantic:
            semantic[key] = sorted(semantic[key], key=lambda item: item.get("id", item.get("record_id")))
    semantic["maps"] = [
        {**row, "marks": sorted(row["marks"], key=lambda item: item["record_id"])}
        for row in sorted(payload.get("maps", ()), key=lambda item: item["id"])
    ]
    return Problem(targets, supports, tuple(maps), tuple(anchors), digest(semantic))


def block_huber_residual(residual, delta):
    """Vector embedding whose squared norm equals twice block Huber loss."""
    norm = math.hypot(*residual)
    if norm <= delta:
        return residual
    return residual * (math.sqrt(2 * delta * norm - delta * delta) / norm)


class FrameFit:
    """One documented output plane, with shared map poses and joint landmark variables."""

    def __init__(self, maps, anchors, *, frozen=False, excluded=(), poses=None):
        self.maps = tuple(maps)
        self.anchors = tuple(anchors)
        self.excluded = frozenset(excluded)
        self.fixed = {map_.id: map_.initial for map_ in maps}
        self.fixed.update(poses or {})
        self.measurements = []
        initial_points = {}
        for map_ in maps:
            for mark in map_.marks:
                if mark.record_id not in self.excluded:
                    self.measurements.append((map_.id, mark))
                    initial_points.setdefault(mark.object_id, []).append(
                        native_to_plane(mark.xy, self.fixed[map_.id])
                    )
        for mark in anchors:
            if mark.record_id not in self.excluded:
                self.measurements.append((None, mark))
                initial_points.setdefault(mark.object_id, []).append(mark.xy)

        keys = sorted(initial_points)
        self.point_columns = {key: 2 * index for index, key in enumerate(keys)}
        initial = [value for key in keys for value in np.mean(initial_points[key], axis=0)]
        lower = [-np.inf] * len(initial)
        upper = [np.inf] * len(initial)
        self.pose_columns = {}
        for map_ in maps:
            columns = {}
            if map_.bounds is not None and not frozen and map_.id not in (poses or {}):
                for index, (lo, hi) in enumerate(map_.bounds):
                    if lo < hi:
                        columns[index] = len(initial)
                        initial.append(map_.initial[index])
                        lower.append(lo)
                        upper.append(hi)
            self.pose_columns[map_.id] = columns
        self.initial = np.asarray(initial, dtype=np.float64)
        self.lower = np.asarray(lower)
        self.upper = np.asarray(upper)

        locations = [pair for pairs in initial_points.values() for pair in pairs]
        span = max(1., float(np.max(np.ptp(locations, axis=0)))) if locations else 1.
        self.scales = np.ones(len(initial))
        self.scales[:2 * len(keys)] = span
        for map_ in maps:
            for index, column in self.pose_columns[map_.id].items():
                self.scales[column] = span if index < 2 else math.hypot(*map_.initial[2:])
        self.pattern = lil_matrix((2 * len(self.measurements), len(initial)), dtype=np.int8)
        for index, (map_id, observation) in enumerate(self.measurements):
            point_column = self.point_columns[observation.object_id]
            rows = slice(2 * index, 2 * index + 2)
            self.pattern[rows, point_column:point_column + 2] = 1
            if map_id is not None:
                for column in self.pose_columns[map_id].values():
                    self.pattern[rows, column] = 1
        self.pattern = self.pattern.tocsr()

    def pose(self, vector, map_id):
        result = list(self.fixed[map_id])
        for index, column in self.pose_columns[map_id].items():
            result[index] = vector[column]
        return result

    def residuals(self, vector, delta=None):
        rows = []
        for map_id, mark in self.measurements:
            column = self.point_columns[mark.object_id]
            point = vector[column:column + 2]
            expected = point if map_id is None else plane_to_native(point, self.pose(vector, map_id))
            residual = (np.asarray(mark.xy) - expected) / mark.scale
            rows.append(residual if delta is None else block_huber_residual(residual, delta))
        return np.asarray(rows).reshape(-1)

    def jacobian(self, vector):
        """Derivative of normalized native residuals before the robust loss."""
        matrix = lil_matrix(self.pattern.shape)
        for index, (map_id, mark) in enumerate(self.measurements):
            column = self.point_columns[mark.object_id]
            rows = slice(2 * index, 2 * index + 2)
            if map_id is None:
                matrix[rows, column:column + 2] = -np.eye(2) / mark.scale
                continue
            tx, ty, a, b = self.pose(vector, map_id)
            scale_squared = a * a + b * b
            inverse = np.array([[a, b], [-b, a]]) / scale_squared
            u, v = inverse @ (vector[column:column + 2] - (tx, ty))
            matrix[rows, column:column + 2] = -inverse / mark.scale
            pose_derivatives = inverse @ np.array([[1., 0., u, -v], [0., 1., v, u]])
            for parameter, pose_col in self.pose_columns[map_id].items():
                matrix[rows, pose_col] = (pose_derivatives[:, parameter] / mark.scale).reshape(2, 1)
        return matrix.tocsr()

    def _rank(self, vector, rtol):
        jac = self.jacobian(vector).toarray() * self.scales
        n = len(vector)
        _, values, right = np.linalg.svd(jac, full_matrices=(jac.shape[0] < n))
        rank = int(np.sum(values > (values[0] * rtol if len(values) else rtol)))
        nullspace = right[rank:]
        identified = {
            key: int(np.linalg.matrix_rank(nullspace[:, col:col + 2], tol=rtol)) == 0
            if nullspace.shape[0] else True
            for key, col in self.point_columns.items()
        }
        return {"rank": rank, "variables": n, "nullity": n - rank,
                "singular_values": values.tolist(), "scaled_parameter_units": self.scales.tolist(),
                "rank_in_declared_gauge": rank, "rank_with_prior": rank,
                "prior_factors": 0, "null_directions": nullspace.tolist()}, identified

    def run(self, *, delta=2.5, max_evaluations=500, rank_tolerance=1e-9, diagnostic=True):
        if not len(self.initial):
            return {"positions": {}, "transforms": self.fixed, "residuals": [], "diagnostics": None}
        if len(self.measurements):
            result = least_squares(
                self.residuals, self.initial, args=(delta,), jac_sparsity=self.pattern,
                bounds=(self.lower, self.upper), x_scale=self.scales,
                max_nfev=max_evaluations, tr_options={"atol": 1e-12, "btol": 1e-12},
                ftol=1e-11, xtol=1e-11, gtol=1e-11,
            )
            vector = result.x
            solver = {"success": bool(result.success), "termination": int(result.status),
                      "evaluations": result.nfev, "objective": float(result.cost),
                      "optimality": float(result.optimality)}
        else:
            vector = self.initial
            solver = {"success": False, "termination": None, "evaluations": 0, "objective": None}
        residual = self.residuals(vector).reshape(-1, 2)
        rank, identified = self._rank(vector, rank_tolerance) if diagnostic else (None, {})
        return {
            "positions": {key: vector[column:column + 2].tolist()
                          for key, column in self.point_columns.items()},
            "transforms": {map_.id: self.pose(vector, map_.id) for map_ in self.maps},
            "residuals": [
                {"record_id": obs.record_id, "object_id": obs.object_id, "map_id": map_id,
                 "group": obs.group, "native_delta": (error * obs.scale).tolist(),
                 "norm_scaled": norm, "weight": 1. if norm <= delta else delta / norm}
                for (map_id, obs), error in zip(self.measurements, residual, strict=True)
                for norm in [math.hypot(*error)]
            ],
            "diagnostics": rank, "identified": identified, "solver": solver,
        }


def solve_maps(problem, *, frozen=False, excluded=(), poses=None, max_evaluations=500,
               rank_tolerance=1e-9, diagnostic=True):
    """Fit only explicitly linked maps; the output frame key never bridges local components."""
    planes = sorted({item.output_frame for item in problem.maps} |
                    {frame for frame, _ in problem.anchors})
    fits = {}
    for plane in planes:
        maps = [item for item in problem.maps if item.output_frame == plane]
        anchors = [mark for frame, mark in problem.anchors if frame == plane]
        frame_poses = {item.id: poses[item.id] for item in maps if item.id in (poses or {})}
        fit = FrameFit(maps, anchors, frozen=frozen, excluded=excluded, poses=frame_poses)
        fits[plane] = fit.run(max_evaluations=max_evaluations, rank_tolerance=rank_tolerance,
                              diagnostic=diagnostic)
    return fits


def registration_design(problem, fitted, count, seed):
    """Explore common poses over the declared domains, including coordinate directions."""
    free = [(map_, index, lo, hi)
            for map_ in problem.maps if map_.bounds is not None
            for index, (lo, hi) in enumerate(map_.bounds) if lo < hi]
    center = {key: value for fit in fitted.values() for key, value in fit["transforms"].items()}
    yield "FIT", {}
    if not free or count <= 1:
        return
    samples = qmc.Halton(d=len(free), scramble=True, rng=seed).random(count - 1)
    free_index = {map_.id for map_, _, _, _ in free}
    for sample_index, sample in enumerate(samples):
        props = {key: list(pose) for key, pose in center.items() if key in free_index}
        for k, ((map_, index, lo, hi), weight) in enumerate(zip(free, sample, strict=True)):
            if sample_index < 2 * len(free):
                weight = 0. if sample_index % 2 == 0 else 1.
                value = lo + weight * (hi - lo) if k == sample_index // 2 else center[map_.id][index]
            else:
                value = lo + weight * (hi - lo)
            props[map_.id][index] = value
        yield f"COMMON_POSE_{sample_index + 1:03d}", props


def write_json(path, value):
    """Close each completed result atomically in its existing staging directory."""
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("wb") as stream:
        stream.write(canonical_bytes(value) + b"\n")
    temp.replace(path)
