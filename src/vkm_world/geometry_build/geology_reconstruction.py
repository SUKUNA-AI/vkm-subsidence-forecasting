"""Source-constrained geological contact reconstruction on a shared P1 carrier.

Coordinates and topology are fixed inputs. Latent collars, native datum
offsets, scenario assumptions and measured constraints have separate roles.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Real
from time import monotonic

import numpy as np
from scipy.optimize import LinearConstraint, linprog, minimize
from scipy.sparse import block_diag, coo_matrix, csr_matrix, diags, eye, vstack
from shapely import points, polygons
from shapely.strtree import STRtree


@dataclass(frozen=True)
class Row:
    id: str
    indices: tuple
    coefficients: tuple
    lower: float | None
    upper: float | None
    role: str
    group: str
    scale: float | None = None


@dataclass
class Problem:
    nodes: np.ndarray
    triangles: np.ndarray
    contact_ids: tuple
    frame_id: str
    configuration_id: str
    rows: tuple
    variable_ids: tuple
    dispositions: tuple
    gradient: csr_matrix
    nodal_area: np.ndarray
    scenario_basis: str | None


def _number(value, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite real number")
    try:
        if isfinite(value):
            return float(value)
    except OverflowError:
        pass
    raise ValueError(f"{name} must be a finite real number")


def _bounds(value, name):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} needs a lower and upper bound")
    lo, hi = (None if number is None else _number(number, name) for number in value)
    if lo is None and hi is None or lo is not None and hi is not None and lo > hi:
        raise ValueError(f"{name} has empty bounds")
    return lo, hi


def _xy(value, name):
    if not isinstance(value, (list, tuple, np.ndarray)) or np.shape(value) != (2,):
        raise ValueError(f"{name} must be an XY pair")
    return np.asarray([_number(item, name) for item in value], dtype=np.float64)


def _string(value, name):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a stable non-empty string")
    return value


def _carrier(nodes, triangles):
    raw_nodes, raw_triangles = np.asarray(nodes, dtype=object), np.asarray(triangles, dtype=object)
    if any(isinstance(value, (bool, np.bool_)) for value in raw_nodes.ravel()) or any(
        isinstance(value, (bool, np.bool_)) for value in raw_triangles.ravel()
    ):
        raise ValueError("carrier coordinates and incidence cannot contain booleans")
    nodes, triangles = np.asarray(nodes), np.asarray(triangles)
    if (nodes.ndim != 2 or nodes.shape[1] != 2 or len(nodes) < 3
            or nodes.dtype.kind not in "iuf" or not np.isfinite(nodes).all()):
        raise ValueError("carrier nodes must be finite Nx2 real coordinates")
    if (triangles.ndim != 2 or triangles.shape[1] != 3 or not len(triangles)
            or triangles.dtype.kind not in "iu" or triangles.min() < 0 or triangles.max() >= len(nodes)):
        raise ValueError("carrier triangles must be Mx3 valid node IDs")
    nodes, triangles = nodes.astype(np.float64), triangles.astype(np.int64)
    if len(np.unique(nodes, axis=0)) != len(nodes) or len(np.unique(np.sort(triangles), axis=0)) != len(triangles):
        raise ValueError("carrier has duplicate nodes or triangles")
    x = nodes[triangles]
    ab, ac = x[:, 1] - x[:, 0], x[:, 2] - x[:, 0]
    cross = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
    if np.any(cross == 0.):
        raise ValueError("carrier has a zero-area triangle")
    area = .5 * np.abs(cross)
    nodal_area = np.bincount(triangles.ravel(), weights=np.repeat(area / 3., 3), minlength=len(nodes))
    if np.any(nodal_area == 0.):
        raise ValueError("carrier has an unused node")
    d2 = np.column_stack((ac[:, 1], -ac[:, 0])) / cross[:, None]
    d3 = np.column_stack((-ab[:, 1], ab[:, 0])) / cross[:, None]
    grad = np.stack((-d2-d3, d2, d3), axis=1) * np.sqrt(area / area.sum())[:, None, None]
    gradient = coo_matrix((grad.transpose(0, 2, 1).ravel(),
                           (np.repeat(np.arange(2 * len(triangles)), 3), np.repeat(triangles, 2, axis=0).ravel())),
                          shape=(2 * len(triangles), len(nodes))).tocsr()
    return nodes, triangles, cross, gradient, nodal_area / area.sum()


class _Support:
    def __init__(self, nodes, triangles, cross):
        self.nodes = nodes
        self.triangles = triangles
        self.cross = cross
        self.index = STRtree(polygons(nodes[triangles]))

    def stencil(self, xy):
        for i in sorted(self.index.query(points(xy))):
            indexes = self.triangles[i]
            a, b, c = self.nodes[indexes]
            x, y = xy - a
            dx, dy = b - a
            ex, ey = c - a
            u = (x * ey - y * ex) / self.cross[i]
            v = (dx * y - dy * x) / self.cross[i]
            weights = np.asarray((1. - u - v, u, v))
            if min(weights) >= -1e-13:
                # Normalize only representable roundoff on a shared edge.
                # Geological contact values are never clipped or adjusted.
                weights = np.maximum(weights, 0.)
                return tuple(map(int, indexes)), tuple(weights / weights.sum())
        return None


def _add_row(rows, identity, pairs, limits, role, group, scale=None):
    accum = {}
    for index, coefficient in pairs:
        accum[index] = accum.get(index, 0.) + coefficient
    ordered = [(i, v) for i, v in sorted(accum.items()) if v != 0.]
    if not ordered:
        raise ValueError(f"{identity} is an identically zero operator")
    rows.append(Row(identity, tuple(i for i, v in ordered), tuple(v for i, v in ordered),
                    *limits, role, group, scale))


_CONTEXT_ADMISSIONS = {"CONTEXT_RETAINED", "HOLD", "DEPENDENT_COPY", "REJECTED", "VERTICAL_ONLY"}
_ACTIVE_ADMISSIONS = {"INTERVAL", "HARD", "SOFT", "CONDITIONAL", "HARD_INTERVAL", "SOFT_TARGET", "ADMITTED"}
_CONTACT_COUNTS = {
    "DEPTH_TVD": 1, "DEPTH_MD": 1, "ELEVATION": 1, "CONTACT_ELEVATION": 1,
    "THICKNESS_VERTICAL": 2, "CONTACT_DIFFERENCE": 2, "BOTTOM_INSIDE_UNIT": 2,
    "NOT_REACHED": 1, "LAYER_ABSENT": 2, "COLLAR_ELEVATION": 0, "CONTACT_ORDER": 2,
}
_FACTOR_ROLES = {"SOFT", "SOFT_TARGET"}


def compile_model(nodes, triangles, contact_ids, factors, *, frame_id, configuration_id,
                  priors=(), pinchouts=(), scenario_basis=None):
    """Compile one immutable geological hypothesis into sparse, typed operators."""
    nodes, triangles, cross, gradient, nodal_area = _carrier(nodes, triangles)
    frame_id = _string(frame_id, "frame_id")
    configuration_id = _string(configuration_id, "configuration_id")
    contacts = tuple(_string(contact, "contact") for contact in contact_ids)
    if len(contacts) < 2 or len(set(contacts)) != len(contacts):
        raise ValueError("model needs two or more unique top-down contacts")
    contact_index = {identity: i for i, identity in enumerate(contacts)}
    count = len(nodes)
    variables = [f"CONTACT:{h}:{index}" for h in contacts for index in range(count)]
    extras = {}
    geometry = _Support(nodes, triangles, cross)
    rows = []
    dispositions = []
    ids = set()

    def variable(category, key):
        identity = f"{category}:{key}"
        if identity not in extras:
            extras[identity] = len(variables)
            variables.append(identity)
        return extras[identity]

    def contact(h, stencil, sign=1.):
        indexes, weights = stencil
        offset = contact_index[h] * count
        return [(offset + i, sign * ratio) for i, ratio in zip(indexes, weights, strict=True)]

    def datum(factor):
        mode = factor.get("datum_mode")
        if mode == "COMMON_WORLD":
            return []
        key = factor.get("datum_group")
        if mode == "LATENT_SOURCE_OFFSET" and key:
            return [(variable("DATUM", _string(key, "datum_group")), -1.)]
        return None

    def disposition(key, reason, row_ids=()):
        dispositions.append({"factor_id": key, "reason": reason, "operator_rows": list(row_ids)})

    ordered = sorted(factors, key=lambda item: item["factor_id"])
    for factor in ordered:
        identity = _string(factor.get("factor_id"), "factor_id")
        if identity in ids:
            raise ValueError(f"duplicate source factor {identity}")
        ids.add(identity)
        admission = factor.get("admission")
        quantity = factor.get("quantity")
        if quantity == "ELEVATION_LOCAL":
            quantity = "CONTACT_ELEVATION"
        if admission in _CONTEXT_ADMISSIONS:
            disposition(identity, admission)
            continue
        if admission not in _ACTIVE_ADMISSIONS:
            raise ValueError(f"{identity} uses unsupported admission {admission!r}")
        if quantity not in _CONTACT_COUNTS:
            disposition(identity, "QUANTITY_NOT_SUPPORTED_BY_2_5D_OPERATOR")
            continue
        if not factor.get("source_refs"):
            raise ValueError(f"{identity} lacks source references")
        group = _string(factor.get("independence_group"), f"{identity}.independence_group")
        limits = factor["bounds"]
        if limits.get("unit") != "m":
            raise ValueError(f"{identity} requires bound units m")
        lo, hi = _bounds((limits.get("lower"), limits.get("upper")), identity)
        selected = tuple(factor.get("contact_ids", ()))
        if len(selected) != _CONTACT_COUNTS[quantity] or len(set(selected)) != len(selected):
            raise ValueError(f"{identity} has invalid contact count")
        if any(h not in contact_index for h in selected):
            disposition(identity, "CONTACT_OUTSIDE_HYPOTHESIS")
            continue
        if len(selected) == 2 and contact_index[selected[0]] >= contact_index[selected[1]]:
            raise ValueError(f"{identity} reverses the declared top-down contacts")
        if quantity == "DEPTH_MD" and factor.get("trajectory_basis") != "DECLARED_VERTICAL_SCENARIO":
            disposition(identity, "MD_NEEDS_TRAJECTORY_OR_EXPLICIT_VERTICAL_SCENARIO")
            continue
        if quantity in {"BOTTOM_INSIDE_UNIT", "NOT_REACHED"} and factor.get("depth_basis") not in {
            "TVD", "DECLARED_VERTICAL_SCENARIO",
        }:
            disposition(identity, "CENSORING_NEEDS_QUALIFIED_DEPTH_BASIS")
            continue
        support = factor.get("support") or {}
        if support.get("kind") not in {"POINT", "AREA_MEAN"} or (support["kind"] == "POINT" and support.get("xy") is None):
            disposition(identity, "OWN_SPATIAL_SUPPORT_UNAVAILABLE")
            continue
        if support.get("frame_id") != frame_id or support.get("configuration_id") != configuration_id:
            disposition(identity, "SUPPORT_FRAME_OR_CONFIGURATION_MISMATCH")
            continue
        if support["kind"] == "POINT":
            xy = _xy(support["xy"], identity)
            stencil = geometry.stencil(xy)
            if stencil is None:
                disposition(identity, "OUTSIDE_FIXED_CARRIER")
                continue
        else:
            if quantity not in {"CONTACT_ELEVATION", "ELEVATION", "THICKNESS_VERTICAL", "CONTACT_DIFFERENCE"}:
                raise ValueError(f"{identity} unsupported areal factor quantity")
            indices = tuple(support.get("node_indices", ()))
            weights = tuple(_number(value, f"{identity}.area_weight") for value in support.get("weights", ()))
            if not indices or len(indices) != len(weights) or len(indices) != len(set(indices)):
                raise ValueError(f"{identity} invalid areal support shape")
            if any(type(i) is not int or not 0 <= i < count for i in indices) or (
                any(weight < 0 for weight in weights) or abs(sum(weights) - 1.) > 1e-12
            ):
                raise ValueError(f"{identity} invalid normalized areal support")
            _string(support.get("basis"), f"{identity}.area_basis")
            stencil = indices, weights
        expected = factor.get("epistemic_status")
        if expected == "ENGINEERING_ASSUMPTION":
            role = "SCENARIO_HARD"
        elif expected == "MODEL_CHOICE" or admission == "CONDITIONAL":
            role = "CONDITIONAL_DATA_HARD"
        else:
            role = "DATA_HARD"
        scale = None
        if admission in _FACTOR_ROLES:
            if lo is None or hi is None or lo != hi:
                raise ValueError(f"{identity} soft target needs explicit scalar bounds")
            scale = _number(factor.get("scale_m"), f"{identity}.scale_m")
            if scale <= 0.:
                raise ValueError(f"{identity} needs positive soft normalization")
            role = role.replace("_HARD", "_SOFT")
        block = len(rows)
        if quantity in {"DEPTH_TVD", "DEPTH_MD", "BOTTOM_INSIDE_UNIT", "NOT_REACHED", "COLLAR_ELEVATION"}:
            owner = _string(factor.get("physical_id"), f"{identity}.physical_id")
            if quantity != "COLLAR_ELEVATION" and any(d < 0. for d in (lo, hi) if d is not None):
                raise ValueError(f"{identity} has negative depth")
            own_collar = [(variable("COLLAR", owner), 1.)]
        if quantity in {"ELEVATION", "CONTACT_ELEVATION", "COLLAR_ELEVATION"}:
            operation = datum(factor)
            if operation is None:
                disposition(identity, "HEIGHT_DATUM_OPERATION_UNQUALIFIED")
                continue
            operator = own_collar if quantity == "COLLAR_ELEVATION" else contact(selected[0], stencil)
            _add_row(rows, identity, operator + operation, (lo, hi), role, group, scale)
        elif quantity in {"DEPTH_TVD", "DEPTH_MD"}:
            _add_row(rows, identity, own_collar + contact(selected[0], stencil, -1.), (lo, hi), role, group, scale)
        elif quantity == "BOTTOM_INSIDE_UNIT":
            if admission in _FACTOR_ROLES:
                raise ValueError(f"{identity} has soft censoring without a likelihood")
            # For this quantity a sole lower value is the recorded bottom
            # position, encoded historically as maximum penetration. With a
            # reported two-sided endpoint interval there must exist one bottom
            # inside both the source interval and the already ordered unit.
            roof_bound = lo if hi is None else hi
            _add_row(rows, identity + ":REACHED_ROOF", own_collar + contact(selected[0], stencil, -1.),
                     (None, roof_bound), role, group)
            if lo is not None:
                _add_row(rows, identity + ":NOT_REACHED_BASE", own_collar + contact(selected[1], stencil, -1.),
                         (lo, None), role, group)
        elif quantity == "NOT_REACHED":
            if lo is None or admission in _FACTOR_ROLES:
                raise ValueError(f"{identity} not-reached contact requires a lower hard depth")
            _add_row(rows, identity, own_collar + contact(selected[0], stencil, -1.), (lo, None), role, group)
        else:
            if quantity == "LAYER_ABSENT" and (lo != 0. or hi != 0. or not factor.get("pinchout_basis")):
                raise ValueError(f"{identity} layer absence needs explicit authority and exact zero")
            _add_row(rows, identity, contact(selected[0], stencil) + contact(selected[1], stencil, -1.),
                     (lo, hi), role, group, scale)
        disposition(identity, role, [row.id for row in rows[block:]])

    for layer, (upper, lower) in enumerate(zip(contacts, contacts[1:])):
        for vertex in range(count):
            _add_row(rows, f"ORDER:{upper}:{lower}:{vertex}",
                     [(layer * count + vertex, 1.), ((layer + 1) * count + vertex, -1.)],
                     (0., None), "TOPOLOGY", "SHARED_P1_NODAL_ORDER")

    for pinchout in pinchouts:
        key = _string(pinchout["id"], "pinchout.id")
        _string(pinchout["basis"], "pinchout.basis")
        upper, lower = tuple(pinchout["contact_ids"])
        if contact_index[lower] != contact_index[upper] + 1:
            raise ValueError(f"{key} pinchout requires adjacent contacts")
        vertices = set()
        for vertex in pinchout["node_ids"]:
            if isinstance(vertex, (bool, np.bool_)) or not isinstance(vertex, (int, np.integer)) or not 0 <= vertex < count:
                raise ValueError(f"{key} pinchout vertex is invalid")
            vertices.add(int(vertex))
        for vertex in sorted(vertices):
            stencil = ((vertex,), (1.,))
            _add_row(rows, f"{key}:{vertex}", contact(upper, stencil) + contact(lower, stencil, -1.),
                     (0., 0.), "SCENARIO_HARD", key)

    if priors and not scenario_basis:
        raise ValueError("priors require a declared scenario basis")
    for prior in sorted(priors, key=lambda row: row["id"]):
        key = _string(prior["id"], "prior.id")
        _string(prior["basis"], "prior.basis")
        if key in ids:
            raise ValueError(f"duplicate prior {key}")
        ids.add(key)
        scale = _number(prior.get("scale_m"), f"{key}.scale_m")
        if scale <= 0.:
            raise ValueError(f"{key} needs positive scale")
        kind = prior.get("kind")
        if kind == "roof_plane":
            h = prior["contact_id"]
            origin = _xy(prior["origin_xy"], key)
            slope = _xy(prior["gradient"], key)
            height = _number(prior["intercept_m"], key) + (nodes - origin) @ slope
            band = prior.get("residual_range_m")
            band = _bounds(band, key) if band is not None else None
            for i, target in enumerate(height):
                op = contact(h, ((i,), (1.,)))
                if band:
                    limits = tuple(None if d is None else float(target + d) for d in band)
                    _add_row(rows, f"{key}:BOUND:{i}", op, limits, "SCENARIO_HARD", key)
                _add_row(rows, f"{key}:{i}", op, (float(target), float(target)), "SCENARIO_SOFT", key,
                         scale / np.sqrt(nodal_area[i]))
        elif kind in {"thickness", "collar"}:
            target = _number(prior["center_m"], key)
            band = _bounds(prior["range_m"], key) if prior.get("range_m") is not None else None
            if band and (band[0] is not None and target < band[0] or band[1] is not None and target > band[1]):
                raise ValueError(f"{key} center is outside scenario range")
            if kind == "thickness":
                upper, lower = tuple(prior["contact_ids"])
                if contact_index[lower] <= contact_index[upper]:
                    raise ValueError(f"{key} thickness prior reverses the declared contact order")
                if target < 0. or band is not None and band[0] is not None and band[0] < 0.:
                    raise ValueError(f"{key} has negative layer thickness")
                entries = ((i, contact(upper, ((i,), (1.,))) + contact(lower, ((i,), (1.,)), -1.),
                            scale / np.sqrt(nodal_area[i])) for i in range(count))
            else:
                owner = _string(prior["physical_id"], f"{key}.physical_id")
                entries = ((0, [(variable("COLLAR", owner), 1.)], scale),)
            for i, operator, weight in entries:
                if band:
                    _add_row(rows, f"{key}:BOUND:{i}", operator, band, "SCENARIO_HARD", key)
                _add_row(rows, f"{key}:{i}", operator, (target, target), "SCENARIO_SOFT", key, weight)
        else:
            raise ValueError(f"{key} has unsupported prior kind")

    names = [row.id for row in rows]
    if len(names) != len(set(names)):
        raise ValueError("generated constraint IDs are not unique")
    return Problem(nodes, triangles, contacts, frame_id, configuration_id, tuple(rows), tuple(variables),
                   tuple(dispositions), gradient, nodal_area, scenario_basis)


def _matrix(rows, n):
    data, row_indices, col_indices = [], [], []
    for index, row in enumerate(rows):
        data.extend(row.coefficients)
        row_indices.extend([index] * len(row.indices))
        col_indices.extend(row.indices)
    return coo_matrix((data, (row_indices, col_indices)), shape=(len(rows), n)).tocsr()


def _row_bounds(rows):
    low = np.asarray([-np.inf if row.lower is None else row.lower for row in rows])
    high = np.asarray([np.inf if row.upper is None else row.upper for row in rows])
    return low, high


def _violation(values, lo, hi):
    return np.maximum.reduce((np.zeros_like(values), lo - values, values - hi))


def _split_constraints(matrix, low, high):
    equal = low == high
    up = np.isfinite(high) & ~equal
    down = np.isfinite(low) & ~equal
    return matrix[equal], low[equal], vstack((matrix[up], -matrix[down]), format="csr"), np.r_[high[up], -low[down]]


def _lp(matrix, lo, hi, deadline, tolerance):
    remaining = deadline - monotonic()
    if remaining <= 0.:
        return None
    eq, b_eq, ub, b_ub = _split_constraints(matrix, lo, hi)
    n = matrix.shape[1]
    return linprog(np.zeros(n), A_eq=eq, b_eq=b_eq, A_ub=ub, b_ub=b_ub,
                   bounds=(None, None), method="highs",
                   options={"time_limit": remaining, "primal_feasibility_tolerance": tolerance,
                            "dual_feasibility_tolerance": tolerance})


def _phase_one(matrix, lo, hi, rows, deadline, tolerance):
    """Diagnostic minimum sum of per-row slack in metres. Bounds stay frozen."""
    from scipy.sparse import hstack

    remaining = deadline - monotonic()
    if remaining <= 0.:
        return {"status": "SEARCH_INCOMPLETE"}
    lower, upper = np.isfinite(lo), np.isfinite(hi)
    identity = eye(matrix.shape[0], format="csr")
    inequalities = vstack((
        hstack((matrix[upper], -identity[upper]), format="csr"),
        hstack((-matrix[lower], -identity[lower]), format="csr"),
    ), format="csr")
    n, m = matrix.shape[1], matrix.shape[0]
    result = linprog(np.r_[np.zeros(n), np.ones(m)], A_ub=inequalities,
                     b_ub=np.r_[hi[upper], -lo[lower]], bounds=[(None, None)] * n + [(0., None)] * m,
                     method="highs", options={"time_limit": remaining, "primal_feasibility_tolerance": tolerance})
    if not result.success:
        return {"status": "SEARCH_INCOMPLETE" if result.status == 1 else "NUMERIC_FAILURE",
                "optimizer_status": int(result.status)}
    values = np.asarray(matrix @ result.x[:n])
    conflicts = [{"id": row.id, "role": row.role, "group": row.group, "value_m": float(value),
                  "lower_m": row.lower, "upper_m": row.upper, "minimum_diagnostic_slack_m": float(slack)}
                 for row, value, slack in zip(rows, values, _violation(values, lo, hi), strict=True)
                 if slack > tolerance]
    return {"status": "DIAGNOSTIC_ONLY", "minimum_total_slack_m": float(result.fun),
            "rows_with_slack": conflicts, "proof_scope": "HiGHS numerical LP; not an exact certificate"}


def _rank(matrix, rtol, max_dense_elements):
    from scipy.sparse.csgraph import structural_rank

    m, n = matrix.shape
    if m == 0 or matrix.nnz == 0:
        return {"rank": 0, "nullity": n, "rows": m, "columns": n, "method": "EMPTY_OPERATOR",
                "threshold": None, "singular_values": []}
    # Normalize dimensions before rank. Observation row norms are algebraic
    # scales, separate from measurement accuracy and regularization.
    length = np.sqrt(np.asarray(matrix.multiply(matrix).sum(axis=1)).ravel())
    normalized = diags(np.divide(1., length, out=np.zeros_like(length), where=length > 0.)) @ matrix
    if m * n > max_dense_elements:
        return {"rank": None, "nullity": None, "structural_rank_upper_bound": int(structural_rank(normalized)),
                "rows": m, "columns": n, "method": "STRUCTURAL_UPPER_BOUND",
                "threshold": None, "singular_values": None}
    sigma = np.linalg.svd(normalized.toarray(), compute_uv=False)
    threshold = float(rtol * sigma[0])
    rank = int(np.count_nonzero(sigma > threshold))
    return {"rank": rank, "nullity": n - rank, "rows": m, "columns": n, "method": "ROW_L2_SCALED_SVD",
            "threshold": threshold, "singular_values": sigma.tolist()}


def _regularizer(problem, length, contact_weight, thickness_weight):
    k, nodes = len(problem.contact_ids), len(problem.nodes)
    n = len(problem.variable_ids)
    g = problem.gradient
    components = []
    if contact_weight > 0.:
        components.append(block_diag([np.sqrt(contact_weight) * length * g] * k, format="csr"))
    if thickness_weight > 0.:
        # A discrete difference between neighboring layers uses the same
        # support and finite-element gradient, with physical area weights.
        from scipy.sparse import kron
        difference = diags((np.ones(k - 1), -np.ones(k - 1)), (0, 1), shape=(k - 1, k))
        components.append(np.sqrt(thickness_weight) * length * kron(difference, g, format="csr"))
    regularization = vstack(components, format="csr") if components else csr_matrix((0, k * nodes))
    if n > k * nodes:
        from scipy.sparse import hstack
        regularization = hstack((regularization, csr_matrix((regularization.shape[0], n - k * nodes))),
                                format="csr")
    return regularization


def _closure_report(problem, hard_rows, soft_rows, hard_matrix, soft_matrix, contact_weight,
                    rtol, max_dense_elements):
    """Check unresolved contact motions remaining under declared scenario closure."""
    from scipy.sparse.csgraph import connected_components

    n, k, nodes = len(problem.variable_ids), len(problem.contact_ids), len(problem.nodes)
    if contact_weight > 0.:
        triangles = problem.triangles
        edge_from, edge_to = triangles.ravel(), triangles[:, [1, 2, 0]].ravel()
        adjacency = coo_matrix((np.ones(len(edge_from)), (edge_from, edge_to)), shape=(nodes, nodes))
        components, labels = connected_components(adjacency, directed=False)
        count = components * k + n - k * nodes
        columns = np.r_[(labels[None, :] + components * np.arange(k)[:, None]).ravel(),
                        np.arange(components * k, count)]
        basis = coo_matrix((np.ones(n), (np.arange(n), columns)), shape=(n, count)).tocsr()
        ncontact = components * k
    else:
        basis = eye(n, format="csr")
        components, count, ncontact = None, n, k * nodes
    bounded = np.asarray([row.lower is not None and row.upper is not None for row in hard_rows])
    projection = vstack((hard_matrix[bounded], soft_matrix), format="csr") @ basis
    report = _rank(projection, rtol, max_dense_elements)
    if report["rank"] is None:
        bounded_contact = None
    else:
        nuisance = _rank(projection[:, ncontact:], rtol, max_dense_elements)["rank"]
        # The difference in ranks is the dimension controlled among contact
        # directions after every free nuisance direction is removed.
        bounded_contact = report["rank"] - nuisance == ncontact
    return report | {"gradient_null_contact_dimensions": ncontact, "horizontal_components": components,
                     "declared_contact_closure": bounded_contact, "basis_columns": count,
                     "basis": "CONNECTED_CONSTANT_CONTACT_MODES" if contact_weight > 0. else "FULL_VARIABLE_SPACE"}


def _field_values(problem, x):
    k, n = len(problem.contact_ids), len(problem.nodes)
    return x[:k*n].reshape(k, n)


def evaluate_contacts(problem, contacts, sample_xy):
    """Read all contact fields on fixed support. Outside coverage stays NaN."""
    nodes, triangles, cross, _, _ = _carrier(problem.nodes, problem.triangles)
    support = _Support(nodes, triangles, cross)
    fields = np.asarray(contacts, dtype=np.float64)
    if fields.shape != (len(problem.contact_ids), len(nodes)) or not np.isfinite(fields).all():
        raise ValueError("invalid reconstructed contact coefficient field")
    values = []
    for point in sample_xy:
        stencil = support.stencil(_xy(point, "sample"))
        values.append(np.full(len(fields), np.nan) if stencil is None else
                      fields[:, list(stencil[0])] @ np.asarray(stencil[1]))
    return np.asarray(values).reshape((-1, len(fields)))


def _raw_readback(rows, x):
    # Independent of sparse matrix multiplication and optimizer cache.
    from math import fsum

    output = []
    for row in rows:
        v = fsum(float(x[i]) * w for i, w in zip(row.indices, row.coefficients, strict=True))
        deviation = max(0., row.lower - v if row.lower is not None else 0.,
                        v - row.upper if row.upper is not None else 0.)
        output.append({"id": row.id, "role": row.role, "group": row.group, "value_m": v,
                       "lower_m": row.lower, "upper_m": row.upper, "violation_m": deviation})
    return output


def _equality_qp(P, rhs, constraints, exact):
    """Equality-restricted optimum and original-unit Lagrange multipliers."""
    from scipy.sparse import bmat
    from scipy.sparse.linalg import MatrixRankWarning, spsolve
    from warnings import catch_warnings, simplefilter

    n = P.shape[0]
    row_scale = np.sqrt(np.asarray(constraints.multiply(constraints).sum(axis=1)).ravel())
    if np.any(row_scale == 0.):
        return None
    normalized = diags(1. / row_scale, format="csr") @ constraints if len(row_scale) else constraints
    system = bmat(((P, normalized.T), (normalized, None)), format="csc") if len(exact) else P.tocsc()
    target = np.r_[rhs, exact / row_scale]
    try:
        with catch_warnings():
            simplefilter("error", MatrixRankWarning)
            vector = spsolve(system, target)
    except MatrixRankWarning:
        return None
    if not np.isfinite(vector).all():
        return None
    residual = np.abs(system @ vector - target)
    optimality = float(residual[:n].max())
    equality_residual = float(residual[n:].max(initial=0.))
    if (optimality > 1e-10 * max(1., float(np.max(np.abs(rhs))))
            or equality_residual > 1e-9):
        return None
    return vector[:n], vector[n:] / row_scale, optimality


def _active_set_qp(P, rhs, eq, exact, inequalities, upper, feasible, tolerance, deadline, iteration_limit):
    """Primal feasible active set with sparse KKT solves and explicit KKT check.

    The direction goes from a feasible iterate to the working-set minimizer.
    Add its first blocking bound, or drop a bound with a negative multiplier at
    a feasible stationary point. All scientific hard rows stay in the system.
    """
    x = feasible.copy()
    working = set()
    n_eq = len(exact)
    dual_atol = 1e-10 * max(1., float(np.max(np.abs(rhs))))
    for iteration in range(1, iteration_limit + 1):
        if monotonic() >= deadline:
            return None
        active = sorted(working)
        matrix = vstack((eq, inequalities[active]), format="csr") if active else eq
        limits = np.r_[exact, upper[active]]
        solution = _equality_qp(P, rhs, matrix, limits)
        if solution is None:
            return None
        candidate, multipliers, optimality = solution
        value = np.asarray(inequalities @ candidate)
        exceeded = (value - upper > tolerance / 2.)
        if active:
            exceeded[active] = False
        if np.any(exceeded):
            slack = upper[exceeded] - np.asarray(inequalities[exceeded] @ x)
            target = value[exceeded] - upper[exceeded]
            # Valid even if a feasible witness is numerically on a bound.
            slack = np.maximum(slack, 0.)
            steps = slack / (slack + target)
            selected = int(np.argmin(steps))
            limit = int(np.flatnonzero(exceeded)[selected])
            x += float(steps[selected]) * (candidate - x)
            working.add(limit)
            continue
        if len(exact) and np.max(np.abs(eq @ candidate - exact)) > tolerance / 2.:
            return None
        if np.max(value - upper, initial=0.) > tolerance / 2.:
            return None
        x = candidate
        if active:
            dual = multipliers[n_eq:]
            if np.min(dual) < -dual_atol:
                working.remove(active[int(np.argmin(dual))])
                continue
        return x, optimality, iteration, len(active)
    return None


def solve_model(problem, policy):
    """Feasibility first, then a sparse constrained quadratic or block-IRLS fit."""
    start = monotonic()
    hard_atol = _number(policy.get("hard_atol_m", 1e-7), "hard_atol_m")
    rank_rtol = _number(policy.get("rank_rtol", 1e-10), "rank_rtol")
    length = _number(policy["slope_length_m"], "slope_length_m")
    contact_weight = _number(policy["contact_weight"], "contact_weight")
    thickness_weight = _number(policy["thickness_weight"], "thickness_weight")
    wall = _number(policy.get("time_limit_s", 60.), "time_limit_s")
    max_iterations = policy.get("max_iterations", 300)
    max_dense = policy.get("max_dense_rank_elements", 2_000_000)
    robust_scale = _number(policy.get("block_huber_delta", 2.5), "block_huber_delta")
    irls = policy.get("max_irls_iterations", 8)
    if (hard_atol < 1e-10 or min(rank_rtol, length, wall, robust_scale) <= 0.
            or min(contact_weight, thickness_weight) < 0.
            or any(type(x) is not int or x < 1 for x in (max_iterations, max_dense, irls))):
        raise ValueError("invalid numerical policy")
    deadline = start + wall
    primal = max(1e-10, min(1e-9, hard_atol / 4))
    n = len(problem.variable_ids)
    hard = [row for row in problem.rows if row.scale is None]
    soft = [row for row in problem.rows if row.scale is not None]
    observed = [row for row in problem.rows if row.role in {"DATA_HARD", "DATA_SOFT"}]
    source_conditioned = [row for row in problem.rows if row.role in {
        "CONDITIONAL_DATA_HARD", "CONDITIONAL_DATA_SOFT",
    }]
    A, S = _matrix(hard, n), _matrix(soft, n)
    lo, hi = _row_bounds(hard)
    observed_rank = _rank(_matrix(observed, n), rank_rtol, max_dense)
    result = {
        "schema": "vkm.geology_reconstruction.fit/1",
        "contact_ids": list(problem.contact_ids), "frame_id": problem.frame_id,
        "configuration_id": problem.configuration_id, "variable_count": n,
        "hard_count": len(hard), "soft_count": len(soft), "data_count": len(observed),
        "data_rank_before_prior": observed_rank,
        "conditional_source_count": len(source_conditioned),
        "conditional_source_rank_before_prior": _rank(_matrix(observed + source_conditioned, n), rank_rtol, max_dense),
        "scenario_basis": problem.scenario_basis, "dispositions": list(problem.dispositions),
        "precision": "float64", "hard_atol_m": hard_atol,
    }

    def finish(status, **fields):
        return result | fields | {"status": status, "wall_time_s": monotonic() - start}

    feasibility = _lp(A, lo, hi, deadline, primal)
    if feasibility is None or feasibility.status == 1:
        return finish("SEARCH_INCOMPLETE", hard_feasibility="UNTESTED_WITHIN_BUDGET")
    if feasibility.status == 2:
        return finish("INFEASIBLE", hard_feasibility="INFEASIBLE_NUMERICAL_LP",
                      conflict=_phase_one(A, lo, hi, hard, deadline, primal))
    if not feasibility.success:
        return finish("NUMERIC_FAILURE", hard_feasibility="INCOMPLETE",
                      optimizer_message=str(feasibility.message))
    verified = _raw_readback(hard, feasibility.x)
    if any(row["violation_m"] > hard_atol for row in verified):
        return finish("NUMERIC_FAILURE", hard_feasibility="LP_WITNESS_NOT_VERIFIED",
                      witness_max_violation_m=max(x["violation_m"] for x in verified))
    result["hard_feasibility"] = "VERIFIED_CONTINUOUS_WITNESS"
    closure = _closure_report(problem, hard, soft, A, S, contact_weight, rank_rtol, max_dense)
    result["scenario_conditioned_rank"] = closure
    if closure["declared_contact_closure"] is not True:
        return finish("UNBOUNDED" if closure["declared_contact_closure"] is False else "SEARCH_INCOMPLETE",
                      unclosed_reason="SCENARIO_HAS_UNRESOLVED_CONTACT_DIRECTIONS" if
                      closure["declared_contact_closure"] is False else "CLOSURE_RANK_NOT_ESTABLISHED")
    if monotonic() >= deadline:
        return finish("SEARCH_INCOMPLETE")
    regularizer = _regularizer(problem, length, contact_weight, thickness_weight)
    R = (regularizer.T @ regularizer).tocsr()
    mean = np.asarray([row.lower for row in soft], dtype=np.float64)
    normalization = np.asarray([1. / row.scale for row in soft], dtype=np.float64)
    source_groups = {}
    for i, row in enumerate(soft):
        if row.role in {"DATA_SOFT", "CONDITIONAL_DATA_SOFT"}:
            source_groups.setdefault(row.group, []).append(i)
    for indices in source_groups.values():
        normalization[indices] /= np.sqrt(len(indices))
    weights = np.ones(len(soft), dtype=np.float64)
    optimization = []
    x = feasibility.x.copy()
    # Exact/equality and inequality sets are distinct so SciPy does not need
    # to estimate structure from a mixed constraint matrix.
    eq, b_eq, ub, b_ub = _split_constraints(A, lo, hi)
    constraints = []
    if eq.shape[0]:
        constraints.append(LinearConstraint(eq, b_eq, b_eq))
    if ub.shape[0]:
        constraints.append(LinearConstraint(ub, -np.inf, b_ub))
    for reweight in range(irls if source_groups else 1):
        if monotonic() >= deadline:
            return finish("SEARCH_INCOMPLETE", optimization=optimization)
        W = diags(normalization * np.sqrt(weights), format="csr") if soft else csr_matrix((0, 0))
        B = W @ S
        target = W @ mean
        P = (R + B.T @ B).tocsr()
        rhs = B.T @ target

        def objective(value):
            residual = B @ value - target
            return .5 * (residual @ residual + value @ (R @ value))

        def gradient(value):
            return P @ value - rhs

        def callback(value, state=None):
            return monotonic() >= deadline

        direct = _active_set_qp(P, rhs, eq, b_eq, ub, b_ub, x, hard_atol, deadline, max_iterations)
        if direct is not None:
            from scipy.optimize import OptimizeResult

            vector, optimality, active_iterations, active_count = direct
            fitted = OptimizeResult(x=vector, success=True, status=0, nit=active_iterations, fun=objective(vector),
                                    message=f"Sparse primal active-set optimum ({active_count} active bounds)",
                                    optimality=optimality)
        else:
            options = {"maxiter": max_iterations, "gtol": min(1e-9, hard_atol / 4),
                   "xtol": 1e-10, "barrier_tol": min(1e-10, hard_atol / 8), "sparse_jacobian": True}
            fitted = minimize(objective, x, jac=gradient, hess=lambda value: P,
                              method="trust-constr", constraints=constraints, callback=callback, options=options)
        optimization.append({"status": int(fitted.status), "message": str(fitted.message),
                             "iterations": int(fitted.nit), "objective": float(fitted.fun),
                             "optimality": float(fitted.optimality)})
        if not np.isfinite(fitted.x).all():
            return finish("NUMERIC_FAILURE", optimization=optimization)
        verified = _raw_readback(hard, fitted.x)
        worst = max((r["violation_m"] for r in verified), default=0.)
        if worst > hard_atol:
            return finish("NUMERIC_FAILURE", optimization=optimization, worst_hard_residual_m=worst)
        x = fitted.x
        if not fitted.success:
            return finish("SEARCH_INCOMPLETE", optimization=optimization, verified_incumbent=True,
                          incumbent_contacts=_field_values(problem, x), worst_hard_residual_m=worst)
        residual = normalization * (S @ x - mean)
        updated = weights.copy()
        for indices in source_groups.values():
            norm = np.linalg.norm(residual[indices])
            updated[indices] = min(1., robust_scale / norm) if norm else 1.
        if not source_groups or np.max(np.abs(updated - weights)) <= 1e-6:
            weights = updated
            break
        weights = updated
    else:
        return finish("SEARCH_INCOMPLETE", optimization=optimization, verified_incumbent=True,
                      incumbent_contacts=_field_values(problem, x), worst_hard_residual_m=worst)

    fields = _field_values(problem, x)
    thickness = fields[:-1] - fields[1:]
    if np.any(thickness < 0.):
        # Exact representation property required downstream; do not clip
        # slightly negative thickness to win a representation check.
        return finish("NUMERIC_FAILURE", optimization=optimization, worst_hard_residual_m=worst,
                      minimum_nodal_thickness_m=float(thickness.min()), diagnostic="NEGATIVE_THICKNESS")
    nuisance = [{"id": name, "model_value_m": float(val), "status": "CONDITIONAL_MODEL_VARIABLE"}
                for name, val in zip(problem.variable_ids[fields.size:], x[fields.size:], strict=True)]
    return finish("MODEL_CLOSED_WITH_ASSUMPTIONS", contacts=fields, nuisance=nuisance,
                  minimum_nodal_thickness_m=float(thickness.min()), worst_hard_residual_m=worst,
                  source_and_prior_checks=_raw_readback(problem.rows, x), optimization=optimization,
                  robust_weights_by_group={group: float(weights[indices[0]]) for group, indices in source_groups.items()},
                  source_soft_normalization="L2_PER_INDEPENDENCE_GROUP",
                  spatial_regularization={"slope_length_m": length, "contact_weight": contact_weight,
                                          "thickness_weight": thickness_weight, "basis": "AREA_NORMALIZED_P1_GRADIENT"})
