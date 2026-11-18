import hashlib
import json
from dataclasses import replace

import numpy as np
import pytest
import shapely

from vkm_world.representations.world0.exact_p1_wedges import read_exact_p1_wedges
from vkm_world.representations.world0.wedge_adapter import WedgeMesh


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_case(root, alter=None, scale_y=1.):
    xy = np.array([[0., 0.], [2., 0.], [2., 2.], [0., 2.]])
    xy[:, 1] *= scale_y
    source_tris = np.array([[0, 1, 2], [0, 2, 3]])
    carrier_tris = np.array([[0, 1, 3], [1, 2, 3]])
    roof = np.array([2., 2., 4., 2.])
    z = np.c_[roof, roof - 1., roof - 2.]
    source = root / 'source.npz'
    common = dict(horizon_names=['TOP', 'PKS', 'BASE'], layer_names=['A', 'B'], patch_ids=['patch'])
    np.savez(source, xy=xy, triangles=source_tris, horizons_z_m=z, world_id='test-world',
             triangle_patch_id=[0, 0], triangle_area_m2=[2.*scale_y]*2, layer_rho_kg_m3=[2100., 2300.],
             layer_young_pa=[2e9, 3e9], layer_nu=[.23, .26], active_pressure_layers=[True, False],
             wave4_active_C1_geometry_sha256='a' * 64, **common)
    s_sha = sha(source)
    receipt = root / 'lineage.json'
    receipt.write_text(json.dumps(dict(source_inputs={'named37/full_stack/wholemine_named37.npz': 'b' * 64,
                                                     'hydro/hydro_parameter_pack.json': 'e' * 64},
        nested_representations=[dict(source_sha256=s_sha, restored=dict(indices=[0]))])), encoding='utf-8')
    wkb, parents, carriers = [], [], []
    for i, s in enumerate(shapely.polygons(xy[source_tris])):
        for j, c in enumerate(shapely.polygons(xy[carrier_tris])):
            parents.append(i); carriers.append(j); wkb.append(s.intersection(c).wkb_hex)
    overlay = root / 'overlay.npz'
    data = dict(carrier_xy_m=xy, carrier_triangles=carrier_tris, carrier_patch_id=[0, 0],
        source_triangle_xy_m=xy[source_tris], source_triangle_contact_z_m=z[source_tris],
        overlay_source_affine_face=parents, overlay_carrier_face=carriers, overlay_wkb_hex=wkb,
        overlay_drain_class=[True, True, False, False], source_affine_patch_id=[0, 0],
        source_world_id='test-world', source_active_geometry_sha256='a' * 64, source_tin_sha256=s_sha,
        quality_carrier_sha256='c' * 64, representation='L080_QUALITY_CARRIER_WITH_EXACT_P1_AND_DRAIN_OVERLAY',
        drain_lineage_receipt_sha256=sha(receipt), drain_binding='ORIGINAL_SCENARIO_MASK_RESTORED_NOT_OBSERVED',
        source_drain_named37_sha256='b' * 64, **common)
    if alter:
        alter(data)
    np.savez(overlay, **data)
    return overlay, source, receipt, sha(overlay)


def test_exact_P1_preserves_physical_height_displacement_and_drain(tmp_path):
    g = read_exact_p1_wedges(*write_case(tmp_path))
    mesh = WedgeMesh.from_exact_p1_geometry(g)
    assert len(mesh.cells) == 8
    assert len(g.pressure_cell_ids) == 4
    assert len(g.drain_face_nodes) == 2
    assert np.all(g.layer_index[g.drain_pressure_cells] == 0)
    assert max(g.nodes[:, 2]) == 4.
    assert np.isclose(mesh.weights.sum(), 8.)
    assert g.vertical_datum == 'UNKNOWN'
    xyz = g.nodes[g.drain_face_nodes]
    assert np.all(np.cross(xyz[:, 1] - xyz[:, 0], xyz[:, 2] - xyz[:, 0])[:, 2] < 0)
    face, pair = g.horizon_faces(1)
    assert np.array_equal(g.cells[pair[:, 0], 3:], face)
    assert np.array_equal(g.cells[pair[:, 1], :3], face)
    _, lateral = g.lateral_faces(0)
    assert np.count_nonzero(np.sum(lateral >= 0, axis=1) == 2) == 4
    assert mesh.signature != WedgeMesh(mesh.nodes, mesh.cells, mesh.density).signature
    # Physical displacement proportional to z must have globally constant dUz/dz on each side.
    u = np.c_[np.zeros((len(mesh.nodes), 2)), mesh.nodes[:, 2] * 0.03]
    expected = np.diag([1., 1., 1.03])
    np.testing.assert_allclose(mesh.deformation_gradient(u), np.broadcast_to(expected, (8, 6, 3, 3)), atol=1e-13)
    # A six-node basis on the unsplit carrier instead gives Uz=.03*2 at the
    # midpoint of its diagonal. The physical roof has z=3 at that point.
    center = np.flatnonzero(np.all(g.xy == [1., 1.], axis=1)).item()
    assert np.isclose(u[center, 2], .09)
    assert not np.isclose(u[center, 2], .03*2.)


def test_quality_guard_blocks_a_valid_exact_but_slender_geometry(tmp_path):
    g = read_exact_p1_wedges(*write_case(tmp_path, scale_y=.01))
    assert len(g.cells) == 8
    with pytest.raises(ValueError, match='PLAN_QUALITY_HOLD'):
        WedgeMesh.from_exact_p1_geometry(g)


@pytest.mark.parametrize('forgery', ('nan', 'empty', 'padded'))
def test_admission_rejects_untrusted_quality_metadata(tmp_path, forgery):
    g = read_exact_p1_wedges(*write_case(tmp_path, scale_y=.01))
    replacement = {'nan': np.full_like(g.quality, np.nan), 'empty': np.empty(0),
                   'padded': np.ones_like(g.quality)}[forgery]
    with pytest.raises(ValueError, match='PLAN_QUALITY_HOLD|quality metadata'):
        WedgeMesh.from_exact_p1_geometry(replace(g, quality=replacement))


@pytest.mark.parametrize('field', ('nodes', 'cells', 'source_face'))
def test_stale_mesh_identity_rejects_changed_geometry_or_lineage(tmp_path, field):
    g = read_exact_p1_wedges(*write_case(tmp_path))
    changed = getattr(g, field).copy()
    if field == 'nodes':
        changed[:, 2] += 5  # mechanically admissible, but a different geometry and identity
    elif field == 'cells':
        changed[[0, 1]] = changed[[1, 0]]
    else:
        changed[0] = 1 - changed[0]
    with pytest.raises(ValueError, match='mesh identity|extrusion'):
        WedgeMesh.from_exact_p1_geometry(replace(g, **{field: changed}))


def test_exact_geometry_array_cannot_be_made_writeable(tmp_path):
    g = read_exact_p1_wedges(*write_case(tmp_path))
    for value in vars(g).values():
        if isinstance(value, np.ndarray):
            with pytest.raises(ValueError):
                value.setflags(write=True)
    with pytest.raises(ValueError, match='immutable'):
        WedgeMesh.from_exact_p1_geometry(replace(g, nodes=g.nodes.copy()))


@pytest.mark.parametrize(('key','value','message'), [
    ('overlay_drain_class', [False] * 4, 'drain lineage'),
    ('overlay_source_affine_face', [0, 0, 0, 1], 'outside the bonded source'),
    ('source_tin_sha256', 'f' * 64, 'source TIN digest'),
    ('source_world_id', 'different-world', 'source world'),
    ('source_drain_named37_sha256', 'e' * 64, 'drain lineage'),
])
def test_exact_input_fails_closed(tmp_path, key, value, message):
    items = write_case(tmp_path, lambda data: data.update({key: value}))
    with pytest.raises(ValueError, match=message):
        read_exact_p1_wedges(*items)
