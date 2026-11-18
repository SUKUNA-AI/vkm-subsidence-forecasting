"""Conforming WEDGE6 materialization of the versioned L080 P1 overlay.

The carrier triangles do not approximate the physical contacts. Each numerical
prism is restricted to one original affine face. Quality is measured after
materialization, before admission to a consuming solver.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import numpy as np
import shapely

from vkm_world.geometry_build.mesh_audit import audit_plan_triangles


AREA_TOLERANCE_M2 = 1e-7
XY_TOLERANCE_M = 1e-7
P1_TOLERANCE_M = 1e-6
MINIMUM_PLAN_QUALITY = .10
REPRESENTATION = 'L080_QUALITY_CARRIER_WITH_EXACT_P1_AND_DRAIN_OVERLAY'
DRAIN_ROLE = 'ORIGINAL_SCENARIO_MASK_RESTORED_NOT_OBSERVED'


def _read_sha(stream):
    actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    stream.seek(0)
    return actual


def _load(path, expected, label):
    with Path(path).open('rb') as stream:
        actual = _read_sha(stream)
        if actual != expected:
            raise ValueError(f'{label} digest mismatch')
        with np.load(stream, allow_pickle=False) as archive:
            return dict(archive)


def _text(data, key):
    item = data[key]
    if item.shape != () or item.dtype.kind != 'U' or not item.item():
        raise ValueError(f'invalid {key}')
    return item.item()


def _index(values, length, upper, label):
    if (values.shape != (length,) or values.dtype.kind not in 'iu'
            or np.any(values < 0) or np.any(values >= upper)):
        raise ValueError(f'invalid {label} indices')
    return values.astype(np.int64, copy=False)


def _faces(xy, tri):
    if xy.ndim != 2 or xy.shape[1] != 2 or not np.isfinite(xy).all():
        raise ValueError('invalid planar coordinates')
    if tri.ndim != 2 or tri.shape[1] != 3 or len(tri) == 0:
        raise ValueError('expected nonempty planar triangles')
    _index(tri.ravel(), tri.size, len(xy), 'triangle vertex')
    faces = xy[tri]
    if np.any(np.linalg.det(faces[:, 1:] - faces[:, :1]) <= 0):
        raise ValueError('nonpositive planar triangle')
    return faces


def _lineage(path, overlay, source_sha):
    try:
        with Path(path).open('rb') as stream:
            if _read_sha(stream) != _text(overlay, 'drain_lineage_receipt_sha256'):
                raise ValueError('drain lineage receipt digest mismatch')
            receipt = json.load(stream)
        rows = [r for r in receipt['nested_representations'] if r['source_sha256'] == source_sha]
        if (len(rows) != 1 or _text(overlay, 'source_drain_named37_sha256') not in receipt['source_inputs'].values()
                or _text(overlay, 'drain_binding') != DRAIN_ROLE):
            raise ValueError('drain lineage is incompatible with source TIN')
        return np.asarray(rows[0]['restored']['indices'])
    except (TypeError, KeyError) as exc:
        raise ValueError('invalid drain lineage receipt') from exc


def _subdivide(pieces):
    parts, piece_ids = shapely.get_parts(shapely.constrained_delaunay_triangles(pieces), return_index=True)
    if len(parts) == 0 or not np.all(shapely.get_num_coordinates(parts) == 4):
        raise ValueError('overlay subdivision did not produce planar triangles')
    coordinates = shapely.get_coordinates(parts).reshape(-1, 4, 2)[:, :3].copy()
    negative = np.linalg.det(coordinates[:, 1:] - coordinates[:, :1]) < 0
    coordinates[negative] = coordinates[negative][:, [0, 2, 1]]
    xy, index = np.unique(coordinates.reshape(-1, 2), axis=0, return_inverse=True)
    return xy, index.reshape(-1, 3), piece_ids


def _edges(triangles):
    directed = triangles[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    edges, inverse, counts = np.unique(np.sort(directed, axis=1), axis=0, return_inverse=True, return_counts=True)
    if np.any(counts > 2):
        raise ValueError('nonmanifold planar face')
    owners = np.full((len(edges), 2), -1, dtype=np.int64)
    direction = (directed[:, 0] > directed[:, 1]).astype(int)
    owners[inverse, direction] = np.repeat(np.arange(len(triangles)), 3)
    if np.any(np.count_nonzero(owners >= 0, axis=1) != counts):
        raise ValueError('inconsistent planar face orientation')
    return edges, owners


def _heights(xy, triangles, source, faces, heights):
    child = xy[triangles]
    parent = faces[source]
    basis = np.swapaxes(parent[:, 1:] - parent[:, :1], 1, 2)
    delta = child - parent[:, :1]
    uv = delta @ np.linalg.inv(basis).swapaxes(1, 2)
    bary = np.concatenate((1 - uv.sum(axis=2, keepdims=True), uv), axis=2)
    values = (bary @ heights[source]).reshape(-1, heights.shape[2])
    vertices = triangles.ravel()
    _, first = np.unique(vertices, return_index=True)
    shared = values[first]
    if np.max(np.abs(values - shared[vertices])) > P1_TOLERANCE_M:
        raise ValueError('discontinuous source P1 contact height at a shared edge')
    if not np.isfinite(shared).all() or np.any(np.diff(shared, axis=1) >= 0):
        raise ValueError('nonpositive source interval thickness')
    return shared


def _same_partition(ids, area, expected, label):
    restored = np.bincount(ids, weights=area, minlength=len(expected))
    if not np.allclose(restored, expected, rtol=0, atol=AREA_TOLERANCE_M2):
        raise ValueError(f'overlay does not cover each {label} face exactly once')


def _readonly(value):
    result = np.array(value, copy=True)
    result.setflags(write=False)
    return result


@dataclass(frozen=True)
class ExactP1Wedges:
    """Exact physical geometry and stable cell/face lineage, with no solver state."""

    nodes: np.ndarray
    cells: np.ndarray
    layer_index: np.ndarray
    xy: np.ndarray
    plan_triangles: np.ndarray
    source_face: np.ndarray
    carrier_face: np.ndarray
    patch_index: np.ndarray
    patch_ids: np.ndarray
    layer_names: np.ndarray
    horizon_names: np.ndarray
    plan_edges: np.ndarray
    plan_edge_owners: np.ndarray
    pressure_cell_ids: np.ndarray
    drain_pressure_cells: np.ndarray
    drain_face_nodes: np.ndarray
    layer_density: np.ndarray
    layer_young: np.ndarray
    layer_poisson: np.ndarray
    quality: np.ndarray
    overlay_sha256: str
    source_tin_sha256: str
    drain_lineage_sha256: str
    world_id: str
    mesh_identity: str
    horizontal_frame: str = 'SH1_LOCAL_METRES'
    vertical_datum: str = 'UNKNOWN'

    def require_quality(self):
        below = int(np.count_nonzero(self.quality < MINIMUM_PLAN_QUALITY))
        if below:
            raise ValueError(f'WEDGE6 PLAN_QUALITY_HOLD: {below} plan cells below {MINIMUM_PLAN_QUALITY:g}')

    def lateral_faces(self, layer):
        """Return oriented quad vertices and their cell pair, with -1 outside.

        A face normal points from the first owner to the second. No boundary
        condition, contact law or Darcy conductance is inferred from adjacency.
        """
        n, count = len(self.xy), len(self.plan_triangles)
        if not isinstance(layer, (int, np.integer)) or not 0 <= layer < len(self.layer_names):
            raise ValueError('unknown layer index')
        side = self.plan_edges
        vertices = np.c_[side+(layer+1)*n, side[:, ::-1]+layer*n]
        pair = np.where(self.plan_edge_owners >= 0, self.plan_edge_owners + layer*count, -1)
        return vertices, pair

    def horizon_faces(self, horizon):
        """Return upward triangles and their cell pair (below, above)."""
        if not isinstance(horizon, (int, np.integer)) or not 0 <= horizon < len(self.horizon_names):
            raise ValueError('unknown horizon index')
        n, count = len(self.xy), len(self.plan_triangles)
        faces = np.arange(count)
        pair = np.c_[faces+horizon*count if horizon < len(self.layer_names) else np.full(count, -1),
                     faces+(horizon-1)*count if horizon > 0 else np.full(count, -1)]
        return self.plan_triangles+horizon*n, pair


def read_exact_p1_wedges(overlay_path, source_path, drain_lineage_path, expected_overlay_sha256):
    """Read a pinned archive and materialize reference cells for geometry/CPU audit.

    Assembly consumers must call ``require_quality`` before admitting the mesh.
    Layer order follows the source from top to bottom, while every cell lists
    its lower three nodes before its upper three nodes in CCW XY order.
    """
    try:
        return _read(overlay_path, source_path, drain_lineage_path, expected_overlay_sha256)
    except KeyError as exc:
        raise ValueError(f'missing exact P1 input field: {exc}') from exc
    except shapely.errors.GEOSException as exc:
        raise ValueError('invalid exact P1 polygon geometry') from exc


def _read(overlay_path, source_path, lineage_path, expected_sha):
    overlay = _load(overlay_path, expected_sha, 'overlay')
    if _text(overlay, 'representation') != REPRESENTATION:
        raise ValueError('unsupported exact P1 representation')
    source_sha = _text(overlay, 'source_tin_sha256')
    tin = _load(source_path, source_sha, 'source TIN')
    world = _text(overlay, 'source_world_id')
    if world != _text(tin, 'world_id'):
        raise ValueError('source world mismatch')
    if _text(tin, 'wave4_active_C1_geometry_sha256') != _text(overlay, 'source_active_geometry_sha256'):
        raise ValueError('source active parent geometry mismatch')
    layers, horizons, patches = (tin[key] for key in ('layer_names', 'horizon_names', 'patch_ids'))
    for key, ids in zip(('layer_names', 'horizon_names', 'patch_ids'), (layers, horizons, patches), strict=True):
        if ids.ndim != 1 or ids.dtype.kind != 'U' or len(np.unique(ids)) != len(ids) or not np.array_equal(overlay[key], ids):
            raise ValueError(f'invalid {key} in source binding')
    carrier = _faces(overlay['carrier_xy_m'], overlay['carrier_triangles'])
    source = _faces(tin['xy'], tin['triangles'])
    heights = tin['horizons_z_m']
    if heights.shape != (len(tin['xy']), len(horizons)) or len(horizons) != len(layers) + 1:
        raise ValueError('source contact/layer dimensions')
    contacts = heights[tin['triangles']]
    if not (np.array_equal(source, overlay['source_triangle_xy_m'])
            and np.array_equal(contacts, overlay['source_triangle_contact_z_m'])):
        raise ValueError('source affine geometry changed')
    source_patch = _index(tin['triangle_patch_id'], len(source), len(patches), 'source patch')
    if not np.array_equal(source_patch, overlay['source_affine_patch_id']):
        raise ValueError('source patch mapping changed')
    carrier_patch = _index(overlay['carrier_patch_id'], len(carrier), len(patches), 'carrier patch')
    try:
        pieces = shapely.from_wkb([bytes.fromhex(s) for s in overlay['overlay_wkb_hex']])
    except (TypeError, ValueError) as exc:
        raise ValueError('malformed overlay polygon encoding') from exc
    piece_source = _index(overlay['overlay_source_affine_face'], len(pieces), len(source), 'piece source')
    piece_carrier = _index(overlay['overlay_carrier_face'], len(pieces), len(carrier), 'piece carrier')
    if not np.array_equal(source_patch[piece_source], carrier_patch[piece_carrier]):
        raise ValueError('overlay crosses a logical patch boundary')
    if not len(pieces) or not all(p.geom_type == 'Polygon' and not p.interiors and p.is_valid and p.area > 0 for p in pieces):
        raise ValueError('invalid exact P1 overlay polygons')
    source_poly = shapely.polygons(source)
    carrier_poly = shapely.polygons(carrier)
    for region, parent in ((source_poly, piece_source), (carrier_poly, piece_carrier)):
        if not np.all(shapely.covers(shapely.buffer(region[parent], XY_TOLERANCE_M), pieces)):
            raise ValueError('overlay polygon outside the bonded source or carrier face')
    area = shapely.area(pieces)
    _same_partition(piece_source, area, shapely.area(source_poly), 'source')
    _same_partition(piece_carrier, area, shapely.area(carrier_poly), 'carrier')
    restored = _lineage(lineage_path, overlay, source_sha)
    restored = _index(restored, len(restored), len(source), 'restored drain')
    if len(np.unique(restored)) != len(restored):
        raise ValueError('duplicate restored drain facet')
    drain = overlay['overlay_drain_class']
    if drain.dtype.kind != 'b' or not np.array_equal(drain, np.isin(piece_source, restored)):
        raise ValueError('overlay drain lineage changed')

    xy, triangles, piece_ids = _subdivide(pieces)
    facts = audit_plan_triangles(xy[triangles], shapely.union_all(carrier_poly), tolerance=XY_TOLERANCE_M)
    if not facts['conforming']:
        raise ValueError('overlay is not a conforming planar mesh')
    points = xy[triangles]
    _same_partition(piece_ids, shapely.area(shapely.polygons(points)), area, 'overlay piece')
    source_id, carrier_id = piece_source[piece_ids], piece_carrier[piece_ids]
    z = _heights(xy, triangles, source_id, source, contacts)
    a, b = np.roll(points, -1, axis=1)-points, np.roll(points, -2, axis=1)-points
    quality = ((a[:, :, 0]*b[:, :, 1]-a[:, :, 1]*b[:, :, 0]) /
               (np.linalg.norm(a, axis=2)*np.linalg.norm(b, axis=2))).min(axis=1)
    if not np.isfinite(quality).all() or np.any(quality <= 0):
        raise ValueError('degenerate overlay triangle')
    return _extrude(tin, overlay, xy, z, triangles, source_id, carrier_id,
                    drain[piece_ids], quality, expected_sha, source_sha, world)


def _extrude(tin, overlay, xy, z, tri, source, carrier, drain, quality, overlay_sha, source_sha, world):
    n, nt, nl = len(xy), len(tri), z.shape[1]-1
    nodes = np.column_stack((np.tile(xy, (nl+1, 1)), z.T.ravel()))
    cells = np.vstack([np.c_[tri+(i+1)*n, tri+i*n] for i in range(nl)])
    layers = np.repeat(np.arange(nl), nt)
    active = tin['active_pressure_layers']
    if active.dtype.kind != 'b' or active.shape != (nl,):
        raise ValueError('invalid source pressure layers')
    pressure = np.flatnonzero(active[layers])
    pks = np.flatnonzero(tin['horizon_names'] == 'PKS')
    if len(pks) != 1 or pks[0] == 0 or pks[0] >= nl or not active[pks[0]-1] or active[pks[0]]:
        raise ValueError('PKS roof drain must face the declared pressure layer above')
    roof = int(pks[0])
    density, young, poisson = (tin[key] for key in ('layer_rho_kg_m3', 'layer_young_pa', 'layer_nu'))
    if (any(np.shape(x) != (nl,) or not np.isfinite(x).all() for x in (density, young, poisson))
            or np.any(density <= 0) or np.any(young <= 0) or np.any((poisson <= -1) | (poisson >= .5))):
        raise ValueError('invalid bound layer material values')
    drain_sha = _text(overlay, 'drain_lineage_receipt_sha256')
    edges, owners = _edges(tri)
    h = hashlib.sha256(f'EXACT_P1_WEDGE6/1:{overlay_sha}:{source_sha}:{drain_sha}'.encode())
    for value in (nodes, cells, source, carrier, pressure, drain):
        h.update(value.tobytes())
    data = (nodes, cells, layers, xy, tri, source, carrier, tin['triangle_patch_id'][source],
            tin['patch_ids'], tin['layer_names'], tin['horizon_names'], edges, owners, pressure,
            (roof-1)*nt + np.flatnonzero(drain), tri[drain][:, [0, 2, 1]]+roof*n,
            density, young, poisson, quality)
    return ExactP1Wedges(*map(_readonly, data), overlay_sha, source_sha, drain_sha, world, h.hexdigest())
