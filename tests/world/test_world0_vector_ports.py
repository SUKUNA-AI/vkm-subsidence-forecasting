"""Mechanical port power must include both tangential components."""

import numpy as np
import pytest

from vkm_world.representations.world0.ports import (
    PortKind,
    mechanical_port_basis,
    port_power_density,
)


def test_three_component_power_preserves_tangential_work():
    traction = np.array([2.0, -3.0, 4.0])
    velocity = np.array([5.0, 6.0, -7.0])
    assert port_power_density(PortKind.MECHANICAL, traction, velocity) == -36.0
    assert port_power_density(PortKind.MECHANICAL, 2.0, 3.0) == 6.0


def test_oriented_basis_preserves_power_and_opposite_tractions():
    normal = np.array([1.0, 1.0, 1.0]) / np.sqrt(3.0)
    tangent = np.array([1.0, -1.0, 0.0]) / np.sqrt(2.0)
    basis = mechanical_port_basis(normal, tangent, -normal)
    stress = np.array([[8.0, 1.1, -0.7], [1.1, 11.0, 0.9], [-0.7, 0.9, 17.0]])
    velocity = np.array([2.0, -3.0, 5.0])
    traction = stress @ normal

    np.testing.assert_allclose(basis @ basis.T, np.eye(3), atol=1e-14)
    assert np.linalg.det(basis) == pytest.approx(1.0)
    np.testing.assert_array_equal(stress @ -normal, -traction)
    assert port_power_density(PortKind.MECHANICAL, basis @ traction, basis @ velocity) == pytest.approx(
        port_power_density(PortKind.MECHANICAL, traction, velocity), rel=1e-14
    )


@pytest.mark.parametrize(
    "normal,tangent,opposite",
    [
        ([0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]),
        ([0.0, 0.0, 2.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0]),
        ([0.0, 0.0, 1.0], [0.0, 0.0, 1.0], [0.0, 0.0, -1.0]),
    ],
)
def test_invalid_face_orientation_rejected(normal, tangent, opposite):
    with pytest.raises(ValueError):
        mechanical_port_basis(normal, tangent, opposite)


@pytest.mark.parametrize(
    "traction,velocity",
    [([1.0, 2.0], [3.0, 4.0]), ([1.0, 2.0, 3.0], 4.0), ([1.0, np.nan, 3.0], [4.0, 5.0, 6.0])],
)
def test_invalid_vector_power_rejected(traction, velocity):
    with pytest.raises(ValueError):
        port_power_density(PortKind.MECHANICAL, traction, velocity)
