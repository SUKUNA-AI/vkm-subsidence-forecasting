"""ADM-001: inserted productive seams require a world-level envelope check."""
import numpy as np
import pytest

from vkm_world.geometry_build.gp_ensemble import seam_envelope_admissibility


def test_one_bad_location_refuses_the_entire_world_without_clipping():
    roof = np.array([[-10., -10.], [-10., -10.]])
    floor = np.array([[-16., -16.], [-16., -25.]])
    source = floor.copy()
    result = seam_envelope_admissibility(roof, floor, np.zeros((2, 2)), np.full((2, 2), -20.))
    assert result["valid_worlds"].tolist() == [True, False]
    assert result["valid_locations"].tolist() == [[True, True], [True, False]]
    assert result["violations"]["floor_below_parent"].sum() == 1
    np.testing.assert_array_equal(floor, source)


def test_zero_thickness_is_allowed_but_inversions_are_diagnosed():
    result = seam_envelope_admissibility([[0., 0., 0.]], [[0., 1., -1.]],
                                         [[0., 0., -2.]], [[0., -2., -1.]], tolerance_m=0.)
    assert result["valid_locations"][0, 0]
    assert result["violations"]["negative_seam_thickness"][0, 1]
    assert result["violations"]["inverted_parent_envelope"][0, 2]
    assert result["violations"]["roof_above_parent"][0, 2]


@pytest.mark.parametrize("bad", [np.nan, np.inf])
def test_unknown_surface_is_not_a_mechanically_admissible_world(bad):
    with pytest.raises(ValueError, match="finite"):
        seam_envelope_admissibility([[0.]], [[bad]], [[1.]], [[-2.]])


def test_wrong_shapes_and_tolerance_are_rejected():
    with pytest.raises(ValueError, match="world, location"):
        seam_envelope_admissibility([0.], [-1.], [1.], [-2.])
    with pytest.raises(ValueError, match="tolerance"):
        seam_envelope_admissibility([[0.]], [[-1.]], [[1.]], [[-2.]], tolerance_m=-1.)
