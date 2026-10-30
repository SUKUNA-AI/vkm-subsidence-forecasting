"""ENS-002/007: independent small reference for the actual grid sampling path.

The seven-standard-error limits are chosen from Gaussian sampling theory before
the run. Hyperparameters/mean are fixed here; their posterior uncertainty is a
separate layer. All coordinates/values below are synthetic.
"""
import numpy as np
import pytest

from vkm_world.geometry_build import gp_ensemble as ge


def _case(draws=18000):
    nodes = np.array([[0., 0.], [350., 90.], [375., 100.], [1200., 0.],
                      [800., 700.], [300., 1100.]])
    idx = np.array([0, 3])
    grid = ge.ParameterGrid(ranges_m=(1800.,), nugget_ratios=(0.1,), jitter=1e-6)
    params = dict(a_idx=np.zeros(draws, int), v_idx=np.zeros(draws, int),
                  sigma2=np.full(draws, 4.), mean=np.full(draws, 2.))
    y = np.tile([1., 4.], (draws, 1))
    # Independent direct covariance/Schur complement, not the sampler helper.
    distance = np.linalg.norm(nodes[:, None] - nodes[None, :], axis=-1)
    covariance = 4. * (0.9*np.exp(-3.*distance/1800.) + (0.1+0.9e-6)*np.eye(len(nodes)))
    cross = covariance[:, idx]
    weights = np.linalg.solve(covariance[np.ix_(idx, idx)], cross.T).T
    mean = 2. + weights @ (y[0]-2.)
    conditional = covariance - weights @ cross.T
    return nodes, idx, grid, params, y, mean, conditional


def _check_reference(samples, mean, covariance):
    residual = samples - mean
    n = len(samples)
    empirical = np.cov(residual, rowvar=False)
    diagonal = np.maximum(np.diag(covariance), 0.)
    mean_limit = 7.*np.sqrt(diagonal/n) + 1e-7
    covariance_limit = 7.*np.sqrt((diagonal[:, None]*diagonal[None, :] + covariance**2)/(n-1)) + 1e-7
    assert np.all(np.abs(residual.mean(axis=0)) < mean_limit)
    assert np.all(np.abs(empirical-covariance) < covariance_limit)
    # A near-neighbour contrast and a nonlocal linear functional test the joint law.
    for weights in ([0., 1., -1., 0., 0., 0.], [0., 0.5, 0.25, 0., -1., 0.75]):
        weights = np.asarray(weights)
        expected = float(weights @ covariance @ weights)
        observed = float(np.var(residual @ weights, ddof=1))
        assert abs(observed-expected) < 7.*expected*np.sqrt(2./(n-1)) + 1e-7


@pytest.mark.parametrize("batch", [257, 18000])
def test_grid_joint_covariance_variogram_and_linear_functional_numpy(batch):
    nodes, idx, grid, params, y, mean, covariance = _case()
    samples = ge.simulate_on_nodes(ge.make_backend("numpy", seed=821), nodes, idx,
                                   "exponential", grid, params, y, batch=batch)
    np.testing.assert_allclose(samples[:, idx], y, atol=1e-12)
    _check_reference(samples, mean, covariance)


def test_grid_node_permutation_keeps_the_same_conditional_law():
    nodes, idx, grid, params, y, mean, covariance = _case()
    order = np.array([4, 1, 3, 5, 2, 0])
    reverse = np.argsort(order)
    samples = ge.simulate_on_nodes(ge.make_backend("numpy", seed=823), nodes[order], reverse[idx],
                                   "exponential", grid, params, y, batch=421)[:, reverse]
    _check_reference(samples, mean, covariance)


def test_target_chunking_does_not_create_a_seam_in_one_grid_draw():
    nodes, idx, grid, params, y, _, _ = _case(draws=20)
    backend = ge.make_backend("numpy", seed=9)
    x = np.broadcast_to(nodes[idx], (20, len(idx), 2))
    indices = np.broadcast_to(idx, (20, len(idx)))
    coef = np.arange(40).reshape(20, 2)/40.
    args = (nodes, x, indices, "exponential", 1800., np.full(20, 0.1), np.full(20, 0.1000009), coef)
    np.testing.assert_allclose(backend.cross_kriging(*args, chunk=1),
                               backend.cross_kriging(*args, chunk=len(nodes)), rtol=0, atol=0)


def test_point_cv_route_has_marginals_and_is_not_used_as_joint_field():
    nodes, idx, grid, params, y, mean, covariance = _case()
    post = ge.GridPosterior(nodes[idx], "exponential", grid)
    samples = ge.kriging_draws(np.random.default_rng(826), post, y, params, nodes[[1, 2]])
    empirical = np.cov(samples, rowvar=False)
    assert covariance[1, 2] > 1.0  # joint field has a strong positive near-neighbour covariance
    assert abs(empirical[0, 1]) < 0.1  # this explicit marginal API does not promise the joint law
    np.testing.assert_allclose(np.diag(empirical), np.diag(covariance)[[1, 2]], rtol=0.05)


@pytest.mark.gpu
def test_torch_cpu_joint_covariance_small_reference():
    pytest.importorskip("torch")
    nodes, idx, grid, params, y, mean, covariance = _case()
    backend = ge.make_backend("torch", seed=825, device="cpu", dtype="float64", row_chunk=2)
    samples = backend.to_numpy(ge.simulate_on_nodes(backend, nodes, idx, "exponential", grid, params, y, batch=421))
    _check_reference(samples, mean, covariance)
