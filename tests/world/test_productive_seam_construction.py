"""Bounded synthetic checks of the joint stratigraphic construction contract."""
import numpy as np
import pytest

from vkm_world.geometry_build.productive_seams import (
    PartitionInterpretation, bounded_stratigraphic_partition, fixed_seam_pre_admission,
)


def interpretation(**changes):
    args = dict(interpretation_id="synthetic-v1", parent_roof="P_TOP", parent_floor="P_BOTTOM",
                ordered_layers=("above", "seam", "below"), source_locators=("fixture:a", "fixture:s", "fixture:b"),
                closure_basis="fixture: complete parent partition", target_scope="SYNTHETIC",
                unresolved_conditions=("local transfer UNKNOWN", "history UNKNOWN"))
    return PartitionInterpretation(**(args | changes))


def test_bounded_joint_partition_preserves_parent_and_p1_interior():
    top = np.array([[0., 2., -1.]])
    bottom = top - [[11., 13., 15.]]
    original = bottom.copy()
    r = bounded_stratigraphic_partition(top, bottom, [2., 4., 3.], [5., 6., 7.], interpretation())
    assert r["feasible_worlds"].tolist() == [True]
    np.testing.assert_array_equal(bottom, original)
    np.testing.assert_allclose(r["interfaces_m"][:, 0], top)
    np.testing.assert_allclose(r["interfaces_m"][:, -1], bottom)
    assert (r["thickness_m"] >= np.array([2., 4., 3.])[None, :, None]).all()
    assert (r["thickness_m"] <= np.array([5., 6., 7.])[None, :, None]).all()
    # A linear triangle interpolates every interface with the SAME weights.
    interior = r["interfaces_m"][0] @ np.array([.2, .3, .5])
    assert (np.diff(interior) <= 0).all()
    assert r["mechanical_admission"] == "NOT_ADMITTED"
    assert "no probability law" in r["construction_status"]
    assert r["remaining_prerequisites"] == interpretation().unresolved_conditions


def test_one_infeasible_location_emits_no_partial_world_and_retains_rows():
    top = np.zeros((3, 2))
    bottom = -np.array([[10., 11.], [10., 4.], [20., 11.]])
    r = bounded_stratigraphic_partition(top, bottom, [2., 4., 3.], [5., 6., 7.], interpretation())
    assert r["feasible_worlds"].tolist() == [True, False, False]
    assert r["feasible_locations"].tolist() == [[True, True], [True, False], [False, True]]
    assert np.isnan(r["interfaces_m"][1:]).all()
    assert r["violations"]["parent_thinner_than_layer_minima"][1, 1]
    assert r["violations"]["parent_thicker_than_layer_maxima"][2, 0]


def test_unknown_bounds_fail_closed_instead_of_inventing_residual():
    r = bounded_stratigraphic_partition([[0.]], [[-10.]], [2., 4., np.nan], [5., 6., np.nan], interpretation())
    assert not r["feasible_worlds"].any()
    assert r["violations"]["unknown_input"].all()
    assert np.isnan(r["interfaces_m"]).all()


def test_named_residual_and_zero_thickness_are_explicit():
    r = bounded_stratigraphic_partition([[5.]], [[-1.]], [0., 6., 0.], [0., 6., 0.], interpretation())
    np.testing.assert_allclose(r["thickness_m"].ravel(), [0., 6., 0.])
    np.testing.assert_allclose(r["interfaces_m"].ravel(), [5., 5., -1., -1.])


def test_decimal_closure_tolerance_is_disclosed_without_parent_repair():
    r = bounded_stratigraphic_partition([[-143.76]], [[-152.46]], [7.5, 1.2, 0.], [7.5, 1.2, 0.], interpretation())
    assert r["feasible_worlds"].all()
    assert np.max(np.abs(r["closure_error_m"])) <= r["closure_tolerance_m"]
    with pytest.raises(ValueError, match="closure tolerance"):
        bounded_stratigraphic_partition([[0.]], [[-10.]], [2., 4., 3.], [5., 6., 7.], interpretation(), closure_tolerance_m=-1.)


@pytest.mark.parametrize("lower,upper", [([-1., 4., 3.], [5., 6., 7.]), ([2., 4., 3.], [1., 6., 7.])])
def test_invalid_bounds_are_diagnosed(lower, upper):
    r = bounded_stratigraphic_partition([[0.]], [[-10.]], lower, upper, interpretation())
    assert not r["feasible_worlds"].any()
    assert r["violations"]["invalid_layer_bounds"].all()


def test_missing_closure_provenance_and_duplicate_layers_are_refused():
    with pytest.raises(ValueError, match="closure"):
        interpretation(closure_basis="")
    with pytest.raises(ValueError, match="unique"):
        interpretation(ordered_layers=("above", "seam", "seam"))
    with pytest.raises(ValueError, match="locator"):
        interpretation(source_locators=("fixture:a",))


def test_fixed_recipe_retains_ids_reasons_and_bad_world():
    r = fixed_seam_pre_admission([[-7.5], [-7.5]], [[-13.47], [-13.47]], [[0.], [0.]], [[-20.], [-12.]],
                                tolerance_m=.01, world_ids=["s:0", "s:1"],
                                interpretation_id="historical-offset", source_locators=["fixture:recipe"])
    assert r["world_ids"] == ("s:0", "s:1")
    assert r["valid_worlds"].tolist() == [True, False]
    assert r["violations"]["floor_below_parent"][1, 0]
    with pytest.raises(ValueError, match="unique"):
        fixed_seam_pre_admission([[0.]], [[-1.]], [[0.]], [[-2.]], tolerance_m=0.,
                                 world_ids=[], interpretation_id="x", source_locators=["fixture:x"])
