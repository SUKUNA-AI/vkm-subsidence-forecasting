"""ENS-004/ENG-004: failed zero-jitter Cholesky must terminate, without a new prior."""
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from vkm_world.geometry_build import gp_ensemble as ge


def _inputs(jitter=0.0):
    nodes = np.array([[0., 0.], [0., 0.], [1., 0.]])
    grid = ge.ParameterGrid(ranges_m=(1.,), nugget_ratios=(0.,), jitter=jitter)
    params = dict(a_idx=np.zeros(1, int), v_idx=np.zeros(1, int),
                  sigma2=np.ones(1), mean=np.zeros(1))
    return nodes, grid, params


def test_zero_jitter_duplicate_nodes_terminates_in_subprocess():
    # A subprocess timeout catches the original infinite loop without hanging pytest.
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    code = """
import numpy as np
from vkm_world.geometry_build import gp_ensemble as ge
nodes=np.array([[0.,0.],[0.,0.],[1.,0.]])
grid=ge.ParameterGrid(ranges_m=(1.,),nugget_ratios=(0.,),jitter=0.)
params=dict(a_idx=np.zeros(1,int),v_idx=np.zeros(1,int),sigma2=np.ones(1),mean=np.zeros(1))
try:
    ge.simulate_on_nodes(ge.make_backend('numpy'),nodes,np.array([0,2]),'exponential',grid,params,np.zeros((1,2)))
except RuntimeError as error:
    if 'ZERO_JITTER' not in str(error): raise
    print('EXPECTED_FAIL_CLOSED')
else:
    raise SystemExit('singular zero-jitter covariance unexpectedly accepted')
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "EXPECTED_FAIL_CLOSED"


def test_zero_jitter_failure_records_one_attempt_and_no_implicit_regularisation():
    nodes, grid, params = _inputs()
    log = []
    with pytest.raises(RuntimeError, match="ZERO_JITTER"):
        ge.simulate_on_nodes(ge.make_backend("numpy"), nodes, np.array([0, 2]),
                             "exponential", grid, params, np.zeros((1, 2)), log=log)
    assert len(log) == 1
    assert log[0]["jitter_requested"] == log[0]["jitter_used"] == 0.
    assert log[0]["cholesky_attempts"] == 1
    assert log[0]["status"] == "CHOLESKY_FAILED_ZERO_JITTER"


def test_positive_jitter_retries_are_bounded_and_never_exceed_declared_ceiling():
    nodes, grid, params = _inputs(jitter=0.003)
    backend = ge.make_backend("numpy")
    attempted = []
    original = backend.correlation_matrix

    def fail_matrix(nodes, kind, range_m, jitter):
        attempted.append(jitter)
        return original(nodes, kind, range_m, jitter)

    backend.correlation_matrix = fail_matrix
    backend.cholesky = lambda covariance: (None, 1)  # fault injection: persistent numerical failure
    log = []
    with pytest.raises(RuntimeError, match="RETRY_LIMIT"):
        ge.simulate_on_nodes(backend, nodes, np.array([0, 2]), "exponential", grid,
                             params, np.zeros((1, 2)), log=log)
    assert attempted == [0.003, 0.01]
    assert log[0]["cholesky_attempts"] == 2
    assert log[0]["jitter_used"] == 0.01


def test_positive_subnormal_jitter_has_a_finite_attempt_cap():
    nodes, grid, params = _inputs(jitter=np.nextafter(0., 1.))
    backend = ge.make_backend("numpy")
    backend.cholesky = lambda covariance: (None, 1)
    log = []
    with pytest.raises(RuntimeError, match="RETRY_LIMIT"):
        ge.simulate_on_nodes(backend, nodes, np.array([0, 2]), "exponential", grid,
                             params, np.zeros((1, 2)), log=log)
    assert log[0]["cholesky_attempts"] == 12
    assert np.isfinite(log[0]["jitter_used"])


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
@pytest.mark.parametrize("field", ["jitter", "ranges_m", "nugget_ratios"])
def test_nonfinite_covariance_controls_are_rejected(bad, field):
    value = bad if field == "jitter" else (bad,)
    with pytest.raises(ValueError):
        ge.ParameterGrid(**{field: value})


def test_nonfinite_nodes_are_refused_before_cholesky():
    nodes, grid, params = _inputs()
    nodes[0, 0] = np.nan
    with pytest.raises(ValueError, match="finite"):
        ge.simulate_on_nodes(ge.make_backend("numpy"), nodes, np.array([0, 2]),
                             "exponential", grid, params, np.zeros((1, 2)))
