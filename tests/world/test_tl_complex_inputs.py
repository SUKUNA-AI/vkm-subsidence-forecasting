"""Real-valued finite mechanics rejects complex data before lossy coercion."""
import numpy as np
import pytest
from vkm_world.representations.world0 import tl_core as core
from vkm_world.representations.world0 import finite_lubby
from vkm_world.representations.world0.wedge_adapter import ReferenceElastic


@pytest.fixture
def model():
    mesh = core.TetMesh(
        np.array([[0., 0, 0], [1., 0, 0], [0, 1., 0], [0, 0, 1.]]),
        [[0, 1, 2, 3]], 2000.)
    return core.TLModel(mesh, ReferenceElastic(2e9, 1e9))


@pytest.mark.parametrize('imag', [0., 1e-9])
@pytest.mark.parametrize('entry', [
    'green', 'material', 'u', 'state_u', 'external_force', 'fixed_values',
    'body_force', 'mesh_nodes', 'density'])
def test_reject_complex_before_cast(model, entry, imag):
    old = model.initial_state()
    F = np.eye(3)[None]
    u = np.zeros((4, 3))
    b = np.array([0., 0, -9.81])
    fix = np.arange(12)
    vals = np.zeros(12)

    def complex_value(a):
        return np.asarray(a, dtype=complex) + imag * 1j

    calls = {
        'green': lambda: core.green(complex_value(F)),
        'material': lambda: core.evaluate_material(model.law, old.material, complex_value(F), 0.),
        'u': lambda: model.assemble(old, complex_value(u), 0.),
        'state_u': lambda: core.FiniteState(complex_value(u), old.material, model.signature),
        'external_force': lambda: model.solve(old, complex_value(u), fix, vals, 0.),
        'fixed_values': lambda: model.solve(old, u, fix, complex_value(vals), 0.),
        'body_force': lambda: core.reference_body_force(model.mesh, complex_value(b)),
        'mesh_nodes': lambda: core.TetMesh(complex_value(model.mesh.nodes), model.mesh.tets, 2000.),
        'density': lambda: core.TetMesh(model.mesh.nodes, model.mesh.tets, complex_value(2000.)),
    }
    before = old.material.to_dict()
    accepted_u = old.u.tobytes()
    with pytest.raises(ValueError, match='real'):
        calls[entry]()
    assert old.material.to_dict() == before and old.u.tobytes() == accepted_u


@pytest.mark.parametrize('imag', [0., 1e-9])
def test_finite_response_rejects_complex(model, imag):
    with pytest.raises(ValueError, match='real'):
        finite_lubby.finite_response(
            model.law, model.initial_state().material,
            np.eye(3)[None].astype(complex) + imag * 1j, 0.)


@pytest.mark.parametrize('dt', [0., .125])
def test_real_rotation_preserves_material_stress_and_rotates_force(model, dt):
    F = np.array([[[1.01, .003, -.002], [.001, .995, .004], [-.003, .002, 1.002]]])
    ang = .61
    Q = np.array([[np.cos(ang), -np.sin(ang), 0],
                  [np.sin(ang), np.cos(ang), 0], [0, 0, 1.]])
    old = model.initial_state().material
    a = core.evaluate_material(model.law, old, F, dt)
    rotated = core.evaluate_material(model.law, old, Q @ F, dt)
    np.testing.assert_allclose(rotated.P, Q @ a.P, rtol=1e-12, atol=1e-6)
    np.testing.assert_allclose(rotated.S, a.S, rtol=1e-12, atol=1e-6)


def test_real_body_force_and_prescribed_deformation_balance(model):
    # Unit tetrahedron: reference mass rho/6, equally distributed over four nodes.
    old = model.initial_state()
    body = core.reference_body_force(model.mesh, [0, 0, -9.81])
    np.testing.assert_allclose(body[:, 2], -2000. * 9.81 / 24., rtol=1e-14)
    np.testing.assert_array_equal(body[:, :2], 0.)
    u = np.array([[0., 0, 0], [.001, .002, 0], [0, -.001, 0], [.002, 0, .001]])
    result = model.solve(old, body, np.arange(12), u.ravel(), 0.)
    np.testing.assert_array_equal(result.state.u, u)
    np.testing.assert_allclose((body + result.reactions).sum(axis=0), 0., atol=2e-9)
    model.validate_state(result.state)
