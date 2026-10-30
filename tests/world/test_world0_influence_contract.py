"""Analytical port-work/M-matrix regressions (MASTER_SPEC W0-001/002/003; R3-05/06).

Every matrix is synthetic. These checks verify implementation, not a geological world or field prediction.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from vkm_world.representations.world0 import influence as I


def _unequal_ports():
    # Reciprocal force compliance B; F=A t gives C=B A and A C symmetric.
    areas = np.array([1.0, 4.0])
    B = np.array([[2.0, 0.5], [0.5, 1.0]])
    return areas, B @ np.diag(areas)


def test_r3_05_unequal_ports_preserve_reciprocal_operator_and_work():
    areas, C = _unequal_ports()
    accepted, asym = I.accepted_compliance(C, areas=areas)
    K, _ = I.stiffness_from_compliance(C, areas=areas)
    # Independent exact inverse for C=[[2,2],[.5,4]], determinant 7.
    expected = np.array([[4.0, -2.0], [-0.5, 2.0]]) / 7.0
    np.testing.assert_allclose(K, expected, rtol=1e-14)
    np.testing.assert_allclose(accepted, C, rtol=1e-14)
    assert asym < 1e-14
    np.testing.assert_allclose(K @ C, np.eye(2), atol=1e-14)
    for t, q in [(np.array([2., -1.]), np.array([-.5, 3.])),
                 (np.array([0., 1.]), np.array([1., 0.]))]:
        assert t @ np.diag(areas) @ C @ q == pytest.approx(q @ np.diag(areas) @ C @ t)


def test_r3_05_unknown_metric_cannot_accept_unequal_port_example_as_equal_area():
    _, C = _unequal_ports()
    with pytest.raises(ValueError, match="reciprocity"):
        I.stiffness_from_compliance(C)


def test_k_g_use_same_accepted_compliance_after_small_metric_correction():
    areas, C = _unequal_ports()
    C[0, 1] += 1e-7
    S = np.array([[.2, .3], [.5, .1], [-.1, .7]])
    accepted, asym = I.accepted_compliance(C, areas=areas)
    K, _ = I.stiffness_from_compliance(C, areas=areas)
    G = I.surface_operator(S, C, areas=areas)
    assert 0 < asym < 1e-6
    np.testing.assert_allclose(G, S @ K, rtol=1e-14)
    for t in np.random.default_rng(20261030).normal(size=(12, 2)):
        np.testing.assert_allclose(G @ accepted @ t, S @ t, rtol=1e-13, atol=1e-14)
    np.testing.assert_allclose(areas[:, None] * accepted, (areas[:, None] * accepted).T)


def test_equal_area_default_and_common_area_units_preserve_operator():
    C = np.array([[2., .5], [.5, 1.]])
    K, asym = I.stiffness_from_compliance(C)
    assert asym == 0
    expected = np.array([[1., -.5], [-.5, 2.]]) / 1.75
    np.testing.assert_allclose(K, expected)
    areas, C = _unequal_ports()
    K1, _ = I.stiffness_from_compliance(C, areas=areas)
    K2, _ = I.stiffness_from_compliance(C, areas=areas * 1e6)
    np.testing.assert_allclose(K1, K2, rtol=1e-14)


@pytest.mark.parametrize("C,kwargs,reason", [
    ([], {}, "square"),
    ([[1., 0., 0.]], {}, "square"),
    ([[1., np.nan], [0., 1.]], {}, "finite"),
    ([[1. + 1.j]], {}, "real"),
    (np.eye(2), {"areas": [1.]}, "areas"),
    (np.eye(2), {"areas": [1., 0.]}, "areas"),
    (np.eye(2), {"areas": [1., -1.]}, "areas"),
    (np.eye(2), {"areas": [1., np.inf]}, "areas"),
    (np.zeros((2, 2)), {}, "scale"),
    ([[1., 1.], [1., 1.]], {}, "energy"),
    ([[1., 0.], [0., -1.]], {}, "energy"),
    (np.diag([1., 1e-14]), {}, "ill-conditioned"),
    ([[1., .5], [0., 1.]], {}, "reciprocity"),
    (np.eye(2), {"reciprocity_tol": -1.}, "reciprocity_tol"),
    (np.eye(2), {"condition_limit": np.inf}, "condition_limit"),
])
def test_compliance_runtime_rejects_invalid_use_time_inputs(C, kwargs, reason):
    with pytest.raises(ValueError, match=reason):
        I.stiffness_from_compliance(C, **kwargs)


@pytest.mark.parametrize("S", [[1., 2.], [[1.]], [[np.nan, 1.]], [[np.inf, 1.]]])
def test_surface_operator_rejects_invalid_support(S):
    with pytest.raises(ValueError, match="S must"):
        I.surface_operator(S, np.eye(2))


def test_r3_06_nonsymmetric_m_matrix_is_not_rejected_by_symmetric_part():
    K = np.array([[1., -3.], [0., 1.]])
    result = I.sign_structure(K)
    assert result["min_eig_sym"] < 0
    assert result["inverse_nonnegative"]
    assert result["m_matrix"] is True
    assert result["m_matrix_status"] == "PASS"


def test_m_matrix_false_indeterminate_and_zero_diagonal_are_distinct():
    assert I.sign_structure([[1., .1], [0., 1.]])["m_matrix"] is False
    assert I.sign_structure([[1., -2.], [-2., 1.]])["m_matrix"] is False
    for K in [np.zeros((2, 2)), [[1., -1.], [-1., 1.]], np.diag([1., 1e-9]),
              [[1., 1e-9], [0., 1.]]]:
        result = I.sign_structure(K)
        assert result["m_matrix"] is None
        assert result["m_matrix_status"] == "INDETERMINATE"
    assert np.all(np.isnan(I.sign_structure(np.zeros((2, 2)))["lag_profile"]))


@pytest.mark.parametrize("K,rel_tol", [([], 1e-6), ([[np.inf]], 1e-6), (np.eye(2), 0.),
                                      (np.eye(2), np.nan), (np.eye(2), 1.)])
def test_sign_structure_runtime_rejects_invalid_contract(K, rel_tol):
    with pytest.raises(ValueError):
        I.sign_structure(K, rel_tol=rel_tol)


def _analysis_script():
    path = Path(__file__).resolve().parents[2] / "scripts/world0/analyze_influence.py"
    spec = importlib.util.spec_from_file_location("world0_analysis_contract_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_work_conjugate_aggregation_of_unequal_ports_preserves_coarse_work():
    module = _analysis_script()
    areas = np.array([1., 2., 3., 6.])
    B = np.eye(4) + .1 * np.ones((4, 4))
    C = B @ np.diag(areas)
    S = np.array([[1., 2., 3., 4.]])
    Cc, Sc = module.aggregate(C, S, 2, areas=areas)
    ac = np.array([3., 9.])
    t = np.array([2., -1.])
    tf = np.repeat(t, 2)
    v = C @ tf
    vc = Cc @ t
    assert tf @ np.diag(areas) @ v == pytest.approx(t @ np.diag(ac) @ vc)
    np.testing.assert_allclose(ac[:, None] * Cc, (ac[:, None] * Cc).T)
    np.testing.assert_allclose(Sc @ t, S @ tf)
    with pytest.raises(ValueError, match="complete groups"):
        module.aggregate(C, S, 3, areas=areas)


def test_real_analysis_adapter_uses_ring_areas_and_same_raw_operator():
    module = _analysis_script()
    edges = np.array([0., 1., 2.])
    areas = np.pi * np.diff(edges ** 2)
    B = np.array([[2., .5], [.5, 1.]])
    C = B @ np.diag(areas)
    x = np.linspace(0., 10., 101)
    S = np.column_stack([np.exp(-x**2), .5 * np.exp(-x**2 / 2)])
    result = module.summarize("SYNTHETIC_RING_PORTS", dict(C=C, S=S, x=x, axisym=True,
                                                         depth_roof=5., cell_edges=edges))
    policy = result["operator_policy"]
    assert policy["port_metric"] == "ring_area_m2"
    assert policy["K_G_share_accepted_compliance"] is True
    assert policy["symmetrize"] is False
    np.testing.assert_allclose(policy["areas"], areas)
def test_tolerance_only_z_does_not_prove_cooperative_dynamics():
    result = I.sign_structure(np.array([[1., 1e-8], [-0.2, 1.]]))
    assert result["z_matrix"]
    assert not result["z_matrix_exact"]
    assert result["cooperative_world0"] is None

