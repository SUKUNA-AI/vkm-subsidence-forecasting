"""Analytic regressions retained from the retired PW-v1 representation checks."""
import numpy as np
import pytest

from vkm_world.materials.elasticity import orthotropic_compliance


@pytest.mark.parametrize("poisson", [-0.7, 0., 0.2, 0.49])
def test_isotropic_limit_matches_kelvin_hooke_tensor(poisson):
    young = 2e8
    shear = young / (2 * (1 + poisson))
    stiffness = np.linalg.inv(orthotropic_compliance([young] * 3, [poisson] * 3, [shear] * 3))
    lame = young * poisson / ((1 + poisson) * (1 - 2 * poisson))
    expected = np.zeros((6, 6))
    expected[:3, :3] = lame
    expected += 2 * shear * np.eye(6)
    np.testing.assert_allclose(stiffness, expected, rtol=1e-12, atol=1e-6)


def test_ogs_reciprocal_poisson_and_shear_order_are_explicit():
    young, poisson, shear = [3e8, 1e8, 2e8], [0.25, 0.1, 0.3], [1e8, 5e7, 7e7]
    compliance = orthotropic_compliance(young, poisson, shear)
    np.testing.assert_array_equal(compliance, compliance.T)
    assert compliance[0, 1] == -poisson[0] / young[0]
    assert compliance[1, 2] == -poisson[1] / young[1]
    assert compliance[0, 2] == -poisson[2] / young[0]
    np.testing.assert_array_equal(np.diag(compliance)[3:], 1 / (2 * np.array(shear)))


@pytest.mark.parametrize("factor", [1., 1.5789, 2.4004, 4.06])
def test_series_layer_keeps_declared_plane_strain_constrained_modulus(factor):
    # Algebraic CASE19 T0 regression. The numbers are synthetic diagnostic
    # inputs, never material parameters or a validated homogenisation law.
    young, poisson, extraction = 8e8, 0.2, 0.462
    equivalent = (1 - extraction) * young * (1 + 2 * poisson) / (1 + poisson) ** 2
    equivalent_nu = poisson / (1 + poisson)
    constrained = (1 - extraction) * young / (1 - poisson ** 2)
    vertical = 1 / (1 / equivalent + (factor - 1) / constrained)
    shear = equivalent / (2 * (1 + equivalent_nu))
    stiffness = np.linalg.inv(orthotropic_compliance(
        [equivalent, vertical, equivalent],
        [equivalent_nu, equivalent_nu * vertical / equivalent, equivalent_nu], [shear] * 3))
    assert stiffness[1, 1] == pytest.approx(constrained / factor, rel=1e-10)
    assert stiffness[0, 0] == pytest.approx(constrained * (1 + (factor - 1) * (1 - poisson ** 2)) / factor,
                                            rel=1e-10)
    assert stiffness[0, 1] == pytest.approx(poisson * constrained / factor, rel=1e-10)


def test_conversion_does_not_hide_an_unstable_material():
    compliance = orthotropic_compliance([3e8, 1e8, 2e8], [0.9] * 3, [1e8] * 3)
    assert np.linalg.eigvalsh(compliance).min() < 0
