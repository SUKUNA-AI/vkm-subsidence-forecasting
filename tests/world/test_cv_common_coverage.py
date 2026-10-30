"""R3-07: common coverage is an intersection of observation keys (synthetic)."""
from copy import deepcopy

import numpy as np
import pytest

from vkm_world.geometry_build.contact_predictor import summarize_cv


def _record(method, levels, thickness=None, **extra):
    return dict(fold="loo", label="w", method=method, well="w", physical_well="physical-w",
                position="INTERPOLATION", prediction_finite=True, level_error_m=levels,
                thickness_error_m=thickness or [None] * (len(levels)-1), **extra)


def test_r3_07_different_observed_contacts_have_no_common_score():
    summary = summarize_cv([_record("A", [1.0, None]), _record("B", [None, 2.0])], ["A", "B"], 2)
    for result in summary.values():
        assert result["common_coverage_wells"] == ["w"]  # descriptive well coverage is insufficient
        assert result["common_level_observation_keys"] == []
        assert result["common_level_scores"] == [None, None]


def test_common_residuals_are_paired_by_contact_and_thickness_pair():
    records = [_record("A", [1.0, 3.0, 100.0], [2.0, None]),
               _record("B", [2.0, None, 4.0], [7.0, 9.0])]
    summary = summarize_cv(records, ["A", "B"], 3)
    a = summary["loo/INTERPOLATION/A"]
    b = summary["loo/INTERPOLATION/B"]
    assert a["common_level_scores"][1] is b["common_level_scores"][1] is None
    assert a["common_level_scores"][2]["mae_m"] == 100.0
    assert b["common_level_scores"][2]["mae_m"] == 4.0
    assert a["common_thickness_scores"][0]["mae_m"] == 2.0
    assert b["common_thickness_scores"][0]["mae_m"] == 7.0
    assert a["common_thickness_scores"][1] is b["common_thickness_scores"][1] is None
    assert summary == summarize_cv(records[::-1], ["A", "B"], 3)


@pytest.mark.parametrize("field,value", [("label", "another-held-group"), ("experiment", "another-run"),
                                         ("physical_well", "another-object")])
def test_names_shared_across_different_experiments_or_folds_are_not_paired(field, value):
    a, b = _record("A", [1.0, 2.0]), _record("B", [3.0, 4.0])
    b[field] = value
    for result in summarize_cv([a, b], ["A", "B"], 2).values():
        assert result["common_level_scores"] == [None, None]


def test_duplicate_cv_rows_are_rejected_instead_of_counted_twice():
    a = _record("A", [1.0, 2.0])
    with pytest.raises(ValueError, match="duplicate CV"):
        summarize_cv([a, deepcopy(a)], ["A"], 2)


@pytest.mark.parametrize("bad", [np.nan, np.inf])
def test_nonfinite_residual_is_rejected_instead_of_scored(bad):
    with pytest.raises(ValueError, match="finite"):
        summarize_cv([_record("A", [bad, 2.0])], ["A"], 2)


def test_legacy_single_method_finite_rows_keep_their_scores():
    a = _record("A", [1.0, 2.0])
    del a["physical_well"]
    result = summarize_cv([a], ["A"], 2)["loo/INTERPOLATION/A"]
    assert result["common_level_scores"] == result["own_level_scores"]


def test_refused_prediction_never_enters_common_score():
    a, b = _record("A", [1.0, 2.0]), _record("B", [None, None])
    b["prediction_finite"] = False
    for result in summarize_cv([a, b], ["A", "B"], 2).values():
        assert result["common_level_scores"] == [None, None]
