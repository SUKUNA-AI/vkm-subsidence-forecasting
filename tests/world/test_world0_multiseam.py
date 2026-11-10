"""Analytic checks for the common vector representation and port signs."""
import math

import numpy as np
import pytest

from vkm_world.representations.world0.multiseam import ElasticMesh, elastic_matrix, port_force
from vkm_world.representations.world0.multiseam_schur import SchurOperator
from vkm_world.representations.world0.multiseam_stages import TensorStages


def box(kind):
    xy = np.array([[0.,0.], [2.,0.], [0.,3.]]) if kind == "wedge" else np.array([[0.,0.], [2.,0.], [2.,3.], [0.,3.]])
    n = len(xy)
    xyz = np.column_stack((np.tile(xy, (3, 1)), np.repeat([0., 1., 3.], n)))
    cells = np.stack((np.arange(2*n), np.arange(n, 3*n)))
    return xyz, cells, np.arange(n)


@pytest.mark.parametrize("kind", ["wedge", "hex"])
@pytest.mark.parametrize("bbar", [False, True])
def test_two_layers_exact_free_contraction(kind, bbar):
    xyz, cells, base = box(kind)
    n = len(base)
    mesh = ElasticMesh(xyz, cells, [2e9, 8e9], [.35, .25], base, volumetric_average=bbar)
    ezz = np.array([-0.002, -0.001])
    rhs = mesh.eigenstrain_rhs(ezz)
    u = mesh.solve(rhs)
    eps, stress = mesh.fields(u, ezz)
    expected = np.repeat([0., -.002, -.004], n)
    assert np.max(np.abs(u.reshape(-1, 3)[:, 2] - expected)) <= 1e-13
    assert np.max(np.abs(stress)) <= 1e-5
    assert eps[:, 2] == pytest.approx(ezz)
    assert mesh.volume_m3 == pytest.approx([3., 6.] if kind == "wedge" else [6., 12.])


@pytest.mark.parametrize("kind", ["wedge", "hex"])
def test_exact_affine_tensor_and_rigid_translation(kind):
    xyz, cells, base = box(kind)
    mesh = ElasticMesh(xyz, cells, 3e9, .25, base)
    a = np.array([[0.0001, .0002, .0003], [0., .0004, .0001], [.0002, 0., -.0003]])
    u = (xyz @ a.T + np.array([.01, -.03, .05])).ravel()
    strains, stress = mesh.fields(u, np.zeros(len(cells)))
    oracle = np.array([a[0,0],a[1,1],a[2,2],a[0,1]+a[1,0],a[1,2]+a[2,1],a[0,2]+a[2,0]])
    assert strains == pytest.approx(np.tile(oracle, (len(cells), 1)), abs=1e-12)
    assert stress == pytest.approx(np.tile(elastic_matrix(3e9,.25) @ oracle, (len(cells), 1)), abs=1e-5)
    assert np.max(np.abs(mesh.matrix @ np.tile([.01, -.03, .05], len(xyz)))) < 1e-5


@pytest.mark.parametrize("kind", ["wedge", "hex"])
def test_inverted_cell_is_rejected(kind):
    xyz, cells, base = box(kind)
    n = len(base)
    inverted = cells[:, np.r_[np.arange(n, 2*n), np.arange(n)]]
    with pytest.raises(ValueError, match="Jacobian"):
        ElasticMesh(xyz, inverted, 3e9, .25, base)


def test_rotated_port_virtual_work_and_wrong_basis_rejection():
    angle = .53
    c, s = math.cos(angle), math.sin(angle)
    R = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    traction = np.array([-3e6, 5e5, 2e5])
    displacement = np.array([-.002, .003, .004])
    area = .19
    force = port_force(traction, R, area)
    assert np.dot(force, displacement) == pytest.approx(area * traction @ (R.T @ displacement))
    assert np.allclose(port_force(-traction, R, area), -force)
    with pytest.raises(ValueError, match="right-handed"):
        port_force(traction, R * np.array([-1, 1, 1]), area)
    with pytest.raises(ValueError, match="positive area"):
        port_force(traction, R, 0)


@pytest.mark.parametrize("kind", ["wedge", "hex"])
def test_consistent_body_force_preserves_weight_and_first_moment(kind):
    xyz, cells, base = box(kind)
    density = np.array([1000., 3000.])
    mesh = ElasticMesh(xyz, cells, 3e9, .25, base)
    forces = mesh.body_force_rhs(density, [0, 0, -10]).reshape(-1, 3)
    weight = 10 * density * mesh.volume_m3
    assert forces.sum(axis=0) == pytest.approx([0, 0, -sum(weight)])
    assert forces[:, 2] @ xyz[:, 2] == pytest.approx(-weight @ [0.5, 2.0])


def test_tensor_equilibrium_and_events_keep_the_accepted_history():
    xyz, cells, base = box("hex")
    mesh = ElasticMesh(xyz, cells, [2e9, 8e9], [0.25, 0.25], base)
    model = TensorStages(mesh, young_pa=[2e9, 8e9], poisson=[0.25, 0.25],
                         density=[2200, 2400], rock_cells=[True, False], patch_index=[0, 0],
                         excavation_fraction=[0.25],
                         events=[(3e6, 0, "excavate"), (5e6, 0, "backfill")],
                         viscosity_pa_s=1e16, fill_young_pa=28e6,
                         fill_density=1800, fill_fraction=0.7)
    initial, ref = model.initial()
    assert ref["global_relative_balance"] < 1e-12
    pre, data = model.advance(initial, 3e6)
    assert data["creep_dissipation_j"] > 0
    assert np.max(np.abs(pre.creep)) > 0
    assert np.max(np.abs(pre.creep[..., :3].sum(axis=-1))) < 1e-15
    assert np.all(initial.creep == 0)
    with pytest.raises(ValueError, match="next event"):
        model.advance(initial, 4e6)
    post, data = model.apply_events(pre)
    np.testing.assert_array_equal(post.creep, pre.creep)
    assert data["body_force_n"][2] > ref["body_force_n"][2]
    before_fill, _ = model.advance(post, 5e6)
    filled, data = model.apply_events(before_fill)
    np.testing.assert_array_equal(filled.creep, before_fill.creep)
    np.testing.assert_allclose(filled.fill_ref, model._strain(before_fill.displacement_m)[..., 2], atol=0)
    assert (data["fill_stress_pa"] <= 0).all()
    assert data["global_relative_balance"] < 1e-12


def test_static_condensation_matches_full_solve_and_rejects_stale_matrix():
    from scipy.sparse import csc_matrix

    matrix = csc_matrix([[5., -1., 0.], [-1., 4., -2.], [0., -2., 3.]])
    context = {"world": "synthetic", "boundary": "fixed exterior"}
    operator = SchurOperator(matrix, [1], context)
    force = np.array([1., 2., 3.])
    result, _ = operator.solve(force)
    np.testing.assert_allclose(matrix @ result, force, rtol=1e-12)
    operator.assert_applicable(matrix, [1], context)
    with pytest.raises(ValueError, match="stale"):
        operator.assert_applicable(matrix * 2, [1], context)
