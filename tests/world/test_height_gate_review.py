"""Independent regression for invalid tolerances at the coordinate gate."""
import numpy as np
import pytest

from vkm_world.geometry_build.height_frames import NATIVE, calculation_coordinates


@pytest.mark.parametrize("tolerance", [float("inf"), -float("inf"), float("nan"), -1., 1e-3])
def test_invalid_tolerance_cannot_admit_warped_actual_points(tolerance):
    with pytest.raises(ValueError, match="tolerance"):
        calculation_coordinates([[0., 0., 1000.]], [[0., 0., 0.]], role=NATIVE,
                                source_frame="source", target_frame="source", tolerance_m=tolerance)


def test_finite_tolerance_checks_real_points_and_returns_an_independent_array():
    native = np.array([[0., 0., -40.], [1., 1., -20.]])
    within = native.copy()
    within[0, 2] += 5e-6
    accepted = calculation_coordinates(within, native, role=NATIVE, source_frame="source",
                                      target_frame="source", tolerance_m=1e-5)
    accepted[0, 2] = 999.
    assert within[0, 2] == native[0, 2]+5e-6
    with pytest.raises(ValueError, match="Points disagree"):
        calculation_coordinates(within, native, role=NATIVE, source_frame="source",
                                target_frame="source", tolerance_m=1e-7)
