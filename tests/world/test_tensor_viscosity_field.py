"""Meaningful regression for spatial Maxwell/J2 creep viscosity inputs."""
import numpy as np
import pytest

from vkm_world.representations.world0.multiseam import ElasticMesh
from vkm_world.representations.world0.multiseam_stages import TensorStages


def model(viscosity):
    xy = np.array([[0., 0.], [2., 0.], [2., 3.], [0., 3.]])
    xyz = np.column_stack((np.tile(xy, (3, 1)), np.repeat([0., 1., 3.], 4)))
    cells = np.stack((np.arange(8), np.arange(4, 12)))
    young = np.array([2.e9, 8.e9])
    poisson = np.array([.25, .25])
    mesh = ElasticMesh(xyz, cells, young, poisson, np.arange(4))
    return TensorStages(mesh, young_pa=young, poisson=poisson, density=[2200., 2400.],
        rock_cells=[True, True], patch_index=[0, 1], excavation_fraction=[.2, .2],
        events=[], viscosity_pa_s=viscosity, fill_young_pa=28.e6, fill_density=1800., fill_fraction=.7)


def test_scalar_and_uniform_field_have_identical_trajectory():
    scalar = model(1.e16)
    field = model([1.e16, 1.e16])
    a, _ = scalar.initial()
    b, _ = field.initial()
    a, _ = scalar.advance(a, 1.e7)
    b, _ = field.advance(b, 1.e7)
    np.testing.assert_array_equal(a.creep, b.creep)
    np.testing.assert_array_equal(a.displacement_m, b.displacement_m)


def test_changed_viscosity_changes_memory_and_equilibrium():
    stiff = model([1.e16, 1.e16])
    softer = model([1.e16, 1.e14])
    a, _ = stiff.initial()
    b, _ = softer.initial()
    a, _ = stiff.advance(a, 1.e7)
    b, _ = softer.advance(b, 1.e7)
    assert np.linalg.norm(b.creep[1]) > np.linalg.norm(a.creep[1])
    assert np.max(np.abs(a.displacement_m - b.displacement_m)) > 1.e-7


@pytest.mark.parametrize("value", [[1.e16], [1.e16, 0.], [1.e16, -1.], [1.e16, np.inf], [1.e16, np.nan], [[1.e16, 1.e16]]])
def test_invalid_viscosity_field_rejected(value):
    with pytest.raises(ValueError, match="viscosity"):
        model(value)
