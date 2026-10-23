"""Synthetic checks of the conditional Gaussian layered ensemble (no project data)."""
import math

import numpy as np
import pytest

from vkm_world.geometry_build import gp_ensemble as ge

GRID = ge.ParameterGrid(ranges_m=(300.0, 800.0, 2000.0, 5000.0), nugget_ratios=(0.0, 0.1, 0.3), jitter=1e-6)


def _gp_sample(rng, xy, kind="exponential", range_m=1500.0, nugget=0.1, sill=4.0, mean=10.0):
    d = ge.pairwise_distance(xy, xy)
    c = sill * ((1 - nugget) * ge.correlation(d, kind, range_m) + nugget * (d <= 1e-9))
    return mean + np.linalg.cholesky(c + 1e-9 * np.eye(len(xy))) @ rng.standard_normal(len(xy))


def test_correlation_shapes_and_practical_range():
    h = np.array([0.0, 1000.0, 2000.0])
    assert np.allclose(ge.correlation(h, "exponential", 1000.0)[:2], [1.0, math.exp(-3)])
    assert np.allclose(ge.correlation(h, "gaussian", 1000.0)[:2], [1.0, math.exp(-3)])
    assert np.allclose(ge.correlation(h, "spherical", 1000.0), [1.0, 0.0, 0.0])
    with pytest.raises(ValueError):
        ge.correlation(h, "cubic", 1.0)


def test_parameter_grid_validation():
    with pytest.raises(ValueError):
        ge.ParameterGrid(nugget_ratios=(0.0, 1.0))
    with pytest.raises(ValueError):
        ge.ParameterGrid(ranges_m=(-1.0,))


def test_posterior_draws_are_finite_and_kriging_reproduces_data():
    rng = np.random.default_rng(1)
    xy = rng.uniform(0, 4000, (15, 2))
    y = _gp_sample(rng, xy)
    post = ge.GridPosterior(xy, "exponential", GRID)
    params = post.draw(rng, np.tile(y, (200, 1)))
    assert np.all(params["sigma2"] > 0) and np.isfinite(params["mean"]).all()
    draws = ge.kriging_draws(rng, post, np.tile(y, (200, 1)), params, xy[:3])
    assert np.allclose(draws, y[:3], atol=1e-6)


def test_type_selection_rule_is_deterministic():
    rng = np.random.default_rng(2)
    xy = rng.uniform(0, 4000, (20, 2))
    y = _gp_sample(rng, xy, kind="gaussian", range_m=3000.0, nugget=0.0)
    best, evidence = ge.select_covariance_type(xy, y, grid=GRID)
    assert best in ge.COVARIANCE_TYPES and set(evidence) == set(ge.COVARIANCE_TYPES)
    assert evidence[best] == max(evidence.values())


def test_censored_reading_is_imputed_below_its_bound():
    rng = np.random.default_rng(3)
    xy = rng.uniform(0, 3000, (12, 2))
    y = _gp_sample(rng, xy, mean=20.0, sill=25.0)
    censored = np.zeros(12, bool)
    censored[4] = True
    y[4] = 0.0
    post = ge.GridPosterior(xy, "exponential", GRID)
    params, used = ge.draw_parameters(rng, post, np.tile(y, (300, 1)), censored, np.zeros(12), sweeps=10)
    assert np.all(used[:, 4] <= 0.0)
    assert np.allclose(used[:, ~censored], y[~censored])
    at_point = ge.kriging_draws(rng, post, used, params, xy[[4]])
    assert np.all(np.maximum(at_point, 0.0) == 0.0)


def _readings():
    depth = np.array([[5.0, 60.0, 170.0, 200.0],
                      [8.0, 70.0, np.nan, 190.0],
                      [3.0, 50.0, 160.0, np.nan],
                      [6.0, 65.0, 150.0, 185.0]])
    return ge.LayeredReadings(["a", "b", "c", "d"], [[0, 0], [1000, 0], [0, 1000], [1000, 1000]], depth,
                              ["T0", "T1", "T2", "T3"], absent=[(1, 2, 3)],
                              checks=[{"row": 2, "contact": 3, "deeper_than_m": 180.0}])


def test_absence_fills_the_missing_contact_and_is_censored():
    r = _readings()
    assert r.depth[1, 2] == 190.0 and r.filled[1, 2]
    rows, value, censored = r.field_readings(3)
    assert list(rows) == [0, 1, 3] and censored.tolist() == [False, True, False] and value[1] == 0.0
    rows, value, censored = r.field_readings(0)
    assert np.allclose(value, np.log([5.0, 8.0, 3.0, 6.0]))
    sub = r.subset([1, 3])
    assert sub.filled[0, 2] and sub.depth[0, 2] == 190.0 and sub.absent == [(0, 2, 3)]


def test_reading_rules_refuse_gaps_crossings_and_conflicts():
    xy = [[0, 0]]
    with pytest.raises(ValueError, match="non-adjacent"):
        ge.LayeredReadings(["a"], xy, [[5.0, np.nan, 100.0]], ["A", "B", "C"])
    with pytest.raises(ValueError, match="crossing"):
        ge.LayeredReadings(["a"], xy, [[5.0, 50.0, 40.0]], ["A", "B", "C"])
    with pytest.raises(ValueError, match="contradicts"):
        ge.LayeredReadings(["a"], xy, [[5.0, 50.0, 60.0]], ["A", "B", "C"], absent=[(0, 1, 2)])
    with pytest.raises(ValueError, match="top-contact"):
        ge.LayeredReadings(["a"], xy, [[np.nan, 50.0, 60.0]], ["A", "B", "C"])


def test_layered_predictive_is_ordered_and_nonnegative():
    rng = np.random.default_rng(4)
    xy = rng.uniform(0, 3000, (10, 2))
    top = np.exp(rng.normal(2.0, 0.3, 10))
    th = np.abs(rng.normal([60, 100, 20], [10, 15, 8], (10, 3)))
    th[3, 2] = 0.0
    depth = np.column_stack([top, top[:, None] + np.cumsum(th, axis=1)])
    r = ge.LayeredReadings([str(i) for i in range(10)], xy, depth, list("ABCD"), absent=[(3, 2, 3)])
    out = ge.layered_predictive(rng, r, np.arange(10), [[1500, 1500], xy[0]], ["exponential"] * 4, 300, GRID, sweeps=5)
    assert out["depth"].shape == (300, 2, 4)
    assert np.all(np.diff(out["depth"], axis=2) >= 0) and np.all(out["thickness"] >= 0)
    assert np.allclose(out["depth"][:, 1, :], depth[0], atol=1e-6)


def test_synthetic_leave_one_out_coverage_is_near_nominal():
    rng = np.random.default_rng(5)
    hits80 = hits90 = total = 0
    for _ in range(3):
        xy = rng.uniform(0, 5000, (22, 2))
        y = _gp_sample(rng, xy, range_m=1500.0, nugget=0.1)
        for i in range(len(xy)):
            train = np.delete(np.arange(len(xy)), i)
            post = ge.GridPosterior(xy[train], "exponential", GRID)
            yy = np.tile(y[train], (400, 1))
            draws = ge.kriging_draws(rng, post, yy, post.draw(rng, yy), xy[[i]])[:, 0]
            s = ge.score_draws(draws, y[i])
            hits80 += s["in80"]; hits90 += s["in90"]; total += 1
    assert 0.65 <= hits80 / total <= 0.93
    assert 0.78 <= hits90 / total <= 0.99


def test_crps_matches_the_gaussian_closed_form():
    rng = np.random.default_rng(6)
    x = rng.normal(0.0, 2.0, (200000, 1))
    y = np.array([1.3])
    z = 1.3 / 2.0
    phi = math.exp(-z * z / 2) / math.sqrt(2 * math.pi)
    big = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    exact = 2.0 * (z * (2 * big - 1) + 2 * phi - 1 / math.sqrt(math.pi))
    assert ge.crps_ensemble(x, y)[0] == pytest.approx(exact, rel=0.02)


def test_coverage_verdict_uses_wilson_interval():
    assert ge.coverage_verdict(80, 100, 0.8) == "CONSISTENT"
    assert ge.coverage_verdict(40, 100, 0.8) == "UNDER_COVERS"
    assert ge.coverage_verdict(100, 100, 0.8) == "OVER_COVERS"
    assert ge.coverage_verdict(0, 0, 0.8) == "NO_READINGS"


def _grid_case(rng, n_nodes=250):
    nodes = rng.uniform(0, 3000, (n_nodes, 2))
    data_idx = np.array([3, 40, 77, 120, 200, 230])
    y = _gp_sample(rng, nodes[data_idx], mean=5.0, sill=9.0)
    post = ge.GridPosterior(nodes[data_idx], "spherical", GRID)
    yy = np.tile(y, (64, 1))
    return nodes, data_idx, y, post, yy, post.draw(rng, yy)


def test_grid_simulation_honours_data_numpy():
    rng = np.random.default_rng(7)
    nodes, data_idx, y, post, yy, params = _grid_case(rng)
    assert np.array_equal(ge.data_node_index(nodes, nodes[data_idx]), data_idx)
    backend = ge.make_backend("numpy", seed=1)
    log = []
    out = ge.simulate_on_nodes(backend, nodes, data_idx, "spherical", GRID, params, yy, batch=20, log=log)
    assert out.shape == (64, len(nodes)) and np.isfinite(out).all()
    assert np.allclose(out[:, data_idx], y, atol=1e-8)
    assert log and all(entry["jitter_used"] >= GRID.jitter for entry in log)


@pytest.mark.gpu
def test_grid_simulation_honours_data_torch_cpu():
    """GPU-backend parity (torch, here on the CPU device). torch belongs to the GPU stack, not to
    the locked CPU pins, so the CPU suite deselects it; the NumPy path is covered above."""
    pytest.importorskip("torch")
    rng = np.random.default_rng(8)
    nodes, data_idx, y, post, yy, params = _grid_case(rng)
    backend = ge.make_backend("torch", seed=2, device="cpu", dtype="float64")
    out = backend.to_numpy(ge.simulate_on_nodes(backend, nodes, data_idx, "spherical", GRID, params, yy, batch=16))
    assert np.allclose(out[:, data_idx], y, atol=1e-6)
    q = ge.sorted_quantiles(backend, backend.asarray(out), [0.1, 0.5, 0.9])
    assert np.allclose(q, np.quantile(out, [0.1, 0.5, 0.9], axis=0), atol=1e-9)


def test_sorted_quantiles_numpy_matches_numpy_quantile():
    x = np.random.default_rng(9).normal(size=(101, 37))
    backend = ge.make_backend("numpy")
    assert np.allclose(ge.sorted_quantiles(backend, x, [0.05, 0.5, 0.95], chunk=10),
                       np.quantile(x, [0.05, 0.5, 0.95], axis=0))


def test_tin_operator_reproduces_linear_fields():
    nodes = np.array([[0, 0], [10, 0], [0, 10], [10, 10]], float)
    tri = np.array([[0, 1, 2], [1, 3, 2]])
    pts = np.array([[2, 3], [7, 8], [20, 20]], float)
    idx, w = ge.tin_sampling_operator(nodes, tri, pts)
    f = 3 * nodes[:, 0] - 2 * nodes[:, 1] + 1
    val = (f[idx[:2]] * w[:2]).sum(axis=1)
    assert np.allclose(val, 3 * pts[:2, 0] - 2 * pts[:2, 1] + 1)
    assert np.isnan(w[2]).all()


def test_batched_positions_match_fixed_positions():
    rng = np.random.default_rng(10)
    xy = rng.uniform(0, 3000, (9, 2))
    y = _gp_sample(rng, xy)
    fixed = ge.GridPosterior(xy, "gaussian", GRID)
    batched = ge.GridPosterior(np.repeat(xy[None], 3, axis=0), "gaussian", GRID)
    yy = np.tile(y, (3, 1))
    for a, b in zip(fixed.evaluate(yy), batched.evaluate(yy)):
        assert np.allclose(a, b)
    with pytest.raises(ValueError):
        batched.evaluate(np.tile(y, (5, 1)))


def test_plan_jitter_moves_readings_but_keeps_order():
    rng = np.random.default_rng(11)
    xy = rng.uniform(0, 3000, (8, 2))
    top = np.exp(rng.normal(2.0, 0.3, 8))
    th = np.abs(rng.normal([60, 100], [10, 15], (8, 2)))
    depth = np.column_stack([top, top[:, None] + np.cumsum(th, axis=1)])
    r = ge.LayeredReadings([str(i) for i in range(8)], xy, depth, list("ABC"))
    draws = 120
    jitter = xy[None] + rng.normal(0, 50, (draws, 8, 2))
    target = np.repeat(xy[None, :1], draws, axis=0)
    out = ge.layered_predictive(rng, r, np.arange(8), xy[:1], ["exponential"] * 3, draws, GRID, sweeps=3,
                                train_xy=jitter, target_xy=target)
    assert np.all(np.diff(out["depth"], axis=2) >= 0)
    assert np.std(out["depth"][:, 0, 2]) > 0      # the reading no longer sits exactly at the target


def test_grid_simulation_with_per_realization_data_nodes():
    rng = np.random.default_rng(12)
    nodes, data_idx, y, post, yy, params = _grid_case(rng)
    alt = np.tile(data_idx, (64, 1))
    alt[::2, 0] = 5                      # half of the realizations read the first support elsewhere
    out = ge.simulate_on_nodes(ge.make_backend("numpy", seed=3), nodes, alt, "spherical", GRID, params, yy, batch=10)
    assert np.allclose(np.take_along_axis(out, alt, axis=1), yy, atol=1e-8)
