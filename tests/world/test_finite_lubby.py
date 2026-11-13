"""Independent synthetic checks of the restricted reference LUBBY family.

These constants exercise a mathematical law and are not SKRU-1 properties.
"""
import numpy as np
import pytest

from vkm_world.representations.world0.constitutive import Lubby2
from vkm_world.representations.world0.finite_lubby import (
    ReferentialLubby,
    UncertifiedFiniteState,
    UnsupportedClassicalTangent,
    deformation_from_green,
    finite_response,
)


def law(**changes):
    parameters = dict(K0=8e9, G0=4e9, GK0=2e9, etaK0=1e12,
                      etaM0=1e13, mK=-1e-9, mM=-2e-9, mG=0.)
    parameters.update(changes)
    return ReferentialLubby(Lubby2(**parameters))


def loaded_state(model):
    stress = np.array([2e6, -1e6, -1e6, .5e6, 0., 0.])
    return model.commit(model.trial_stress(model.initial_state(), stress, 30.))


def test_full_first_piola_tangent_against_independent_F_differences():
    model = law()
    old = loaded_state(model)
    F = np.array([[1.002, .001, 0.], [0., .999, 0.], [0., 0., 1.]])
    response = finite_response(model, old, F, 20.)
    tangent = np.empty((3, 3, 3, 3))
    step = 1e-7
    for k in range(3):
        for l in range(3):
            delta = np.zeros((3, 3))
            delta[k, l] = step
            plus = finite_response(model, old, F + delta, 20.).P
            minus = finite_response(model, old, F - delta, 20.).P
            tangent[:, :, k, l] = (plus - minus) / (2 * step)
    np.testing.assert_allclose(response.A, tangent, rtol=2e-6, atol=30.)


def test_superposed_spatial_rotation_preserves_material_memory():
    model = law()
    old = loaded_state(model)
    F = np.array([[1.002, .001, 0.], [0., .999, 0.], [0., 0., 1.]])
    angle = .73
    Q = np.array([[np.cos(angle), -np.sin(angle), 0.],
                  [np.sin(angle), np.cos(angle), 0.], [0., 0., 1.]])
    original = finite_response(model, old, F, 20.)
    rotated = finite_response(model, old, Q @ F, 20.)
    np.testing.assert_allclose(rotated.P, Q @ original.P, rtol=1e-11, atol=1e-5)
    transformed_A = np.einsum('ia,ajbl,kb->ijkl', Q, original.A, Q)
    np.testing.assert_allclose(rotated.A, transformed_A, rtol=1e-10, atol=1e-3)
    for branch in ('kelvin', 'maxwell'):
        np.testing.assert_allclose(rotated.material_trial.state.memory[branch],
                                   original.material_trial.state.memory[branch], atol=1e-16)


def test_positive_determinant_is_not_sufficient_for_finite_admission():
    model = law()
    old = model.initial_state()
    F = np.diag([.1, 1., 1.])
    assert np.linalg.det(F) > 0
    with pytest.raises(UncertifiedFiniteState, match='Legendre-Hadamard'):
        finite_response(model, old, F, 0.)
    assert all(not np.any(value) for value in old.memory.values())


def test_nonsmooth_unloading_does_not_commit_trial_memory():
    model = law()
    old = loaded_state(model)
    before = old.to_dict()
    with pytest.raises(UnsupportedClassicalTangent, match='no classical tangent'):
        model.trial_stress(old, np.zeros(6), 1.)
    assert old.to_dict() == before


def test_instantaneous_unloading_has_no_viscosity_derivative_cusp():
    model = law()
    old = loaded_state(model)
    target_E = old.memory['kelvin'] + old.memory['maxwell']
    response = finite_response(model, old, deformation_from_green(target_E), 0.)
    assert np.linalg.norm(response.S) < 1e-4
    for branch in old.memory:
        np.testing.assert_array_equal(response.material_trial.state.memory[branch], old.memory[branch])


def test_reference_anchor_preserves_point_energy_time_and_irreversible_memory():
    original = law()
    stress = np.array([2e6, -1e6, -1e6, .5e6, 0., 0.])
    small_trial = original.inner.trial_stress(original.inner.initial_state(), stress, 30.)
    old = original.inner.commit(small_trial)
    anchored, state = ReferentialLubby.anchored_from_small(original.inner, old)
    assert state.time_s == old.time_s
    np.testing.assert_array_equal(state.stress, old.stress)
    for branch in old.memory:
        np.testing.assert_array_equal(state.memory[branch], old.memory[branch])
    assert anchored.energy(state) == small_trial.diagnostics['stored_energy_diagnostic']
    np.testing.assert_array_equal(state.strain, np.zeros(6))


def test_held_second_piola_branch_work_closes_material_energy():
    model = law()
    old = loaded_state(model)
    stress = np.array([1e6, -.5e6, -.5e6, .25e6, 0., 0.])
    trial = model.trial_stress(old, stress, 120.)
    audit = model.held_stress_audit(old, trial)
    assert audit['physical_branch_dissipation'] >= 0.
    assert abs(audit['residual']) < 1e-8
    restored = model.restore(trial.state.to_dict())
    assert restored.to_dict() == trial.state.to_dict()


def test_variable_Kelvin_modulus_is_outside_potential_family():
    with pytest.raises(ValueError, match='mG exactly zero'):
        law(mG=-1e-9)
