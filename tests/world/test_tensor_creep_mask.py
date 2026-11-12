"""Elastic support with unknown creep data must retain mass and stiffness."""
from dataclasses import replace
import numpy as np
import pytest

from vkm_world.representations.world0.multiseam import ElasticMesh
from vkm_world.representations.world0.multiseam_stages import TensorStages


def model(viscosity, enabled=None, events=()):
    xy = np.array([[0., 0.], [2., 0.], [2., 3.], [0., 3.]])
    xyz = np.column_stack((np.tile(xy, (3, 1)), np.repeat([0., 1., 3.], 4)))
    cells = np.stack((np.arange(8), np.arange(4, 12)))
    young, poisson = np.array([2.e9, 8.e9]), np.array([.25, .25])
    mesh = ElasticMesh(xyz, cells, young, poisson, np.arange(4))
    return TensorStages(mesh, young_pa=young, poisson=poisson,
        density=[2200., 2400.], rock_cells=[True, True], patch_index=[0, 1],
        excavation_fraction=[.2, .2], events=list(events), viscosity_pa_s=viscosity,
        fill_young_pa=28.e6, fill_density=1800., fill_fraction=.7,
        creep_enabled=enabled)


def test_unknown_creep_support_retains_mass_and_excavation_response():
    case = model([1.e16, np.nan], [True, False], [(1.e7, 1, "excavate")])
    state, _ = case.initial()
    state, before = case.advance(state, 1.e7)
    assert np.linalg.norm(state.creep[0]) > 0
    np.testing.assert_array_equal(state.creep[1], 0.)
    after_state, after = case.apply_events(state)
    assert after_state.support[1] == .8
    np.testing.assert_array_equal(after_state.creep, state.creep)
    removed_weight = .2 * 2400. * case.mesh.volume_m3[1] * 9.80665
    np.testing.assert_allclose(after["body_force_n"][2] - before["body_force_n"][2],
                               removed_weight, rtol=1.e-12)
    assert np.max(np.abs(after_state.displacement_m - state.displacement_m)) > 0
    assert after["global_relative_balance"] < 1.e-8


def test_disabled_memory_is_preserved_instead_of_reset():
    case = model([1.e16, np.nan], [True, False])
    state, _ = case.initial()
    memory = state.creep.copy()
    memory[1, :, :3] = [-.0005, -.0005, .001]
    state = replace(state, creep=memory)
    previous = state.creep.copy()
    updated, _ = case.advance(state, 1.e7)
    np.testing.assert_array_equal(updated.creep[1], previous[1])
    np.testing.assert_array_equal(state.creep, previous)


def test_default_and_explicit_mask_preserve_existing_trajectory():
    default = model(1.e16)
    explicit = model([1.e16, 1.e16], [True, True])
    a, _ = default.initial()
    b, _ = explicit.initial()
    a, _ = default.advance(a, 1.e7)
    b, _ = explicit.advance(b, 1.e7)
    np.testing.assert_array_equal(a.displacement_m, b.displacement_m)
    np.testing.assert_array_equal(a.creep, b.creep)


def test_constructor_copies_mask_and_viscosity_inputs():
    enabled = np.array([True, False])
    viscosity = np.array([1.e16, np.nan])
    case = model(viscosity, enabled)
    enabled[:] = False
    viscosity[:] = 1.
    np.testing.assert_array_equal(case.creep_enabled, [True, False])
    assert case.eta[0] == 1.e16 and np.isnan(case.eta[1])
    with pytest.raises(ValueError):
        case.creep_enabled[0] = False


def test_changed_viscosity_cannot_reuse_a_stale_constitutive_matrix():
    warmed = model([1.e16, 1.e16])
    initial, _ = warmed.initial()
    accepted, _ = warmed.advance(initial, 1.e7)
    previous_memory = accepted.creep.copy()
    warmed.eta[:] = 1.e14
    actual, diagnostics = warmed.advance(accepted, 2.e7)
    fresh = model([1.e14, 1.e14])
    expected, _ = fresh.advance(accepted, 2.e7)
    np.testing.assert_allclose(actual.displacement_m, expected.displacement_m,
                               rtol=1.e-12, atol=1.e-15)
    np.testing.assert_allclose(actual.creep, expected.creep, rtol=1.e-12, atol=1.e-15)
    np.testing.assert_array_equal(accepted.creep, previous_memory)
    assert diagnostics["free_relative_residual"] < 1.e-8


@pytest.mark.parametrize("viscosity,enabled", [
    ([1.e16, np.nan], [True, True]),
    ([1.e16, np.inf], [True, False]),
    ([1.e16, -1.], [True, False]),
    ([1.e16, 1.e16], [True]),
    ([1.e16, 1.e16], [1., 0.]),
])
def test_invalid_mask_or_active_viscosity_rejected(viscosity, enabled):
    with pytest.raises(ValueError):
        model(viscosity, enabled)


@pytest.mark.parametrize("dt", [-1., np.nan, np.inf])
def test_invalid_constitutive_time_rejected(dt):
    case = model([1.e16, np.nan], [True, False])
    with pytest.raises(ValueError, match="time"):
        case.creep_ratio(dt)
