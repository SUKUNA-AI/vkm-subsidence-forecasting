import numpy as np
import pytest

from vkm_world.geometry_build.contact_predictor import SupportSet, ContactPredictor, PredictorSpec
from vkm_world.geometry_build.relative_constraints import withdraw_absolute_anchors


def test_withdraw_collar_retains_gaps_and_is_invariant_to_unknown_height():
    supports = SupportSet.from_depths(["anchor", "relative"], [[0, 0], [2, 0]],
                                     [100, -900], [[10, 30], [20, 60]], ["roof", "floor"])
    derived, ledger = withdraw_absolute_anchors(supports, ["relative"])
    predictor = ContactPredictor(PredictorSpec("idw2", "LEVEL", extrapolate=True, allow_partial=True))
    z = predictor.predict(derived, [[1, 0]])["levels"][0]
    # Independent analytical LS: roof=90; equal-weight thickness=(20+40)/2=30.
    np.testing.assert_allclose(z, [90, 60], atol=1e-8)
    derived.collar[1] = 1e6
    np.testing.assert_allclose(predictor.predict(derived, [[1, 0]])["levels"][0], z, atol=1e-8)
    assert supports.depth_lower[1].tolist() == [20, 60]
    assert supports.collar.tolist() == [100, -900]
    assert ledger[0]["absolute_anchor"] == "WITHDRAWN"
    assert len(ledger[0]["derived_gap_ids"]) == 1


def test_relative_only_problem_does_not_invent_an_absolute_gauge():
    s = SupportSet.from_depths(["a"], [[0, 0]], [99], [[10, 30]], ["r", "f"])
    relative, _ = withdraw_absolute_anchors(s, ["a"])
    p = ContactPredictor(PredictorSpec("idw2", "LEVEL", extrapolate=True, allow_partial=True))
    assert np.isnan(p.predict(relative, [[1, 0]])["levels"]).all()


def test_unknown_support_rejected():
    s = SupportSet.from_depths(["a"], [[0, 0]], [99], [[10]], ["r"])
    with pytest.raises(ValueError, match="unknown support"):
        withdraw_absolute_anchors(s, ["missing"])
