from __future__ import annotations

import numpy as np
import pytest
from scipy import integrate, stats

from skru1.gate_c1_probabilistic import quantile_grid_crps, student_t_nll, student_t_quantiles


def test_student_t_quantiles_nll_and_crps_are_finite_and_ordered() -> None:
    truth = np.asarray([-1.0, 0.5, 2.0])
    loc = np.asarray([0.0, 0.0, 1.0])
    scale = np.asarray([1.0, 2.0, 0.5])
    df = np.asarray([3.0, 5.0, 10.0])
    quantiles = student_t_quantiles(loc, scale, df, np.arange(0.01, 1.0, 0.01))
    assert quantiles.shape == (3, 99)
    assert (np.diff(quantiles, axis=1) > 0).all()
    assert np.isfinite(student_t_nll(truth, loc, scale, df)).all()
    assert np.isfinite(quantile_grid_crps(truth, loc, scale, df)).all()


def test_student_t_crps_grid_matches_independent_cdf_integration() -> None:
    truth = np.asarray([0.35])
    loc = np.asarray([0.1])
    scale = np.asarray([1.2])
    df = np.asarray([4.5])
    approximate = float(quantile_grid_crps(truth, loc, scale, df)[0])
    distribution = stats.t(df=df[0], loc=loc[0], scale=scale[0])
    lower = distribution.ppf(1.0e-7)
    upper = distribution.ppf(1.0 - 1.0e-7)
    independent, _ = integrate.quad(
        lambda value: (distribution.cdf(value) - float(value >= truth[0])) ** 2,
        lower,
        upper,
        points=[truth[0]],
        epsabs=1.0e-8,
    )
    assert approximate == pytest.approx(independent, abs=0.03)
