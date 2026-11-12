"""Analytic method controls, independent of field validation or model fitting."""
from datetime import date

import numpy as np
import pytest

from vkm_world.core.provenance import TemporalSupport
from vkm_world.observations.operators import (
    exact_epoch_index,
    gnss,
    insar,
    leveling,
    triangle_weights,
    unique_evidence_groups,
)
from vkm_world.validation.leakage import (
    LeakageViolation,
    assert_available_at,
    assert_disjoint_sample_sets,
)


@pytest.fixture
def affine_state():
    nodes = np.array([[0., 0.], [2., 0.], [0., 2.]])
    triangles = np.array([[0, 1, 2]])
    points = np.array([[.5, .5], [1., .5]])
    weights, supports = triangle_weights(nodes, triangles, points)
    baseline = np.tile([3., -4., 5.], (3, 1))
    increment = np.column_stack((nodes[:, 0] + 1, 2 * nodes[:, 1] - 1,
                                 -3 * nodes[:, 0] + nodes[:, 1]))
    field = np.stack((baseline, baseline + increment))
    expected = np.column_stack((points[:, 0] + 1, 2 * points[:, 1] - 1,
                               -3 * points[:, 0] + points[:, 1]))
    return field, weights, expected


FRAME = {"frame_id": "analytic_ENU", "observation_frame_id": "analytic_ENU"}


def test_affine_displacement_and_declared_subsidence_sign(affine_state):
    field, weights, expected = affine_state
    actual = gnss(field, weights, 1, 0, model_to_enu=np.eye(3), **FRAME)
    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-14)
    np.testing.assert_allclose(leveling(field, weights, 1, 0, **FRAME), expected[:, 2])
    np.testing.assert_allclose(leveling(field, weights, 1, 0, positive_down=True, **FRAME),
                               -expected[:, 2])


def test_common_zero_epoch_and_reference_station_cancel(affine_state):
    field, weights, expected = affine_state
    common_unknown_offset = np.array([500., -200., 300.])
    translated = field + common_unknown_offset
    np.testing.assert_allclose(leveling(translated, weights, 1, 0, **FRAME), expected[:, 2])
    common_motion = np.array([7., 9., -4.])
    moving_reference = field.copy()
    moving_reference[1] += common_motion
    original = gnss(field, weights, 1, 0, model_to_enu=np.eye(3),
                    reference_weights=weights[0], **FRAME)
    shifted = gnss(moving_reference, weights, 1, 0, model_to_enu=np.eye(3),
                   reference_weights=weights[0], **FRAME)
    np.testing.assert_allclose(shifted, original, rtol=0, atol=1e-14)
    np.testing.assert_allclose(original, expected - expected[0], rtol=0, atol=1e-14)


def test_gnss_rotation_and_full_vector_los(affine_state):
    field, weights, expected = affine_state
    rotation = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    rotated = gnss(field, weights, 1, 0, model_to_enu=rotation, **FRAME)
    np.testing.assert_allclose(rotated, expected @ rotation.T)
    directions = np.array([[.6, 0., .8], [0., .6, .8]])
    los = insar(field, weights, 1, 0, model_to_enu=rotation,
                enu_toward_satellite=directions, positive_toward_satellite=True, **FRAME)
    np.testing.assert_allclose(los, np.einsum("ij,ij->i", rotated, directions))
    assert np.max(np.abs(los - .8 * rotated[:, 2])) > .5
    away = insar(field, weights, 1, 0, model_to_enu=rotation,
                 enu_toward_satellite=directions, positive_toward_satellite=False, **FRAME)
    np.testing.assert_allclose(away, -los)


@pytest.mark.parametrize("frame,other", [("", ""), ("   ", "   "), ("UNKNOWN", "UNKNOWN"),
                                          (" unknown ", " unknown "), ("A", "B"), (None, None)])
def test_unknown_blank_or_conflicting_frames_rejected(affine_state, frame, other):
    field, weights, _ = affine_state
    with pytest.raises(ValueError, match="common displacement frame"):
        leveling(field, weights, 1, 0, frame_id=frame, observation_frame_id=other)


def test_wrong_displacement_unit_and_invalid_reference_rejected(affine_state):
    field, weights, _ = affine_state
    with pytest.raises(ValueError, match="converted to metres"):
        leveling(field, weights, 1, 0, unit="mm", **FRAME)
    with pytest.raises(ValueError, match="Reference support must preserve"):
        leveling(field, weights, 1, 0, reference_weights=np.zeros(3), **FRAME)


def test_reflection_and_unknown_los_sign_rejected(affine_state):
    field, weights, _ = affine_state
    with pytest.raises(ValueError, match="right-handed"):
        gnss(field, weights, 1, 0, model_to_enu=np.diag([1., 1., -1.]), **FRAME)
    with pytest.raises(ValueError, match="sign convention"):
        insar(field, weights, 1, 0, model_to_enu=np.eye(3),
              enu_toward_satellite=[.6, 0., .8], positive_toward_satellite=None, **FRAME)
    with pytest.raises(ValueError, match="unit vectors"):
        insar(field, weights, 1, 0, model_to_enu=np.eye(3),
              enu_toward_satellite=[1., 1., 1.], positive_toward_satellite=True, **FRAME)


def test_fractional_triangle_index_does_not_silently_truncate():
    nodes = np.array([[0., 0.], [2., 0.], [0., 2.]])
    with pytest.raises(ValueError, match="triangle connectivity"):
        triangle_weights(nodes, [[0., 1.9, 2.]], [[.5, .5]])
    weights, _ = triangle_weights(nodes, [[0., 1., 2.]], [[.5, .5]])
    np.testing.assert_allclose(weights, [[.5, .25, .25]])


@pytest.mark.parametrize("triangles", [[], np.empty((0, 3)), [[0, 1]], [[0, 1, 4]],
                                         [[0, -1, 2]], [[0, np.nan, 2]], [[0, np.inf, 2]],
                                         [[False, True, True]], [["0", "1", "2"]]])
def test_invalid_or_empty_triangle_connectivity_rejected(triangles):
    with pytest.raises(ValueError, match="[Tt]riangle connectivity"):
        triangle_weights([[0., 0.], [2., 0.], [0., 2.]], triangles, [[.5, .5]])


@pytest.mark.parametrize("nodes,stations", [([[0., 0.], [np.inf, 0.], [0., 2.]], [[.5, .5]]),
                                           ([[0., 0.], [2., 0.], [0., 2.]], [[np.nan, .5]])])
def test_nonfinite_coordinates_rejected(nodes, stations):
    with pytest.raises(ValueError, match="coordinates must be finite"):
        triangle_weights(nodes, [[0, 1, 2]], stations)


def test_triangle_orientation_and_outside_point_rejected():
    nodes = [[0., 0.], [2., 0.], [0., 2.]]
    with pytest.raises(ValueError, match="positive signed area"):
        triangle_weights(nodes, [[0, 2, 1]], [[.5, .5]])
    with pytest.raises(ValueError, match="outside the supported mesh"):
        triangle_weights(nodes, [[0, 1, 2]], [[3., 3.]])


def test_epoch_is_exact_and_integer_index_is_required(affine_state):
    assert exact_epoch_index([0., 1., 2.], 1.) == 1
    with pytest.raises(ValueError, match="interpolation is not implicit"):
        exact_epoch_index([0., 1., 2.], .5)
    with pytest.raises(ValueError, match="strictly increasing"):
        exact_epoch_index([0., 1., 1.], 1.)
    field, weights, _ = affine_state
    with pytest.raises(ValueError, match="Epoch indices must be integers"):
        leveling(field, weights, 1.9, 0, **FRAME)


def test_source_reextraction_and_protected_targets_do_not_create_new_data():
    rows = [{"source_id": "source-A", "locator": "figure-1", "physical_series_id": "series-1",
             "record_id": "digitization-1"},
            {"source_id": "source-A", "locator": "figure-1", "physical_series_id": "series-1",
             "record_id": "digitization-2"}]
    groups = unique_evidence_groups(rows)
    assert len(groups) == 1
    physical_ids = list(groups)
    with pytest.raises(LeakageViolation, match="overlap"):
        assert_disjoint_sample_sets({"parameter_selection": physical_ids, "protected_evaluator": physical_ids})
    with pytest.raises(LeakageViolation, match="available only from"):
        assert_available_at({"late-publication": TemporalSupport(available_from=date(2013, 12, 31))},
                            date(2004, 1, 1))


def test_republished_series_is_one_group_across_locators_and_sources():
    records = [
        {"source_id": "primary", "locator": "table-1", "physical_series_id": "survey-A",
         "record_id": "table-reading"},
        {"source_id": "primary", "locator": "figure-2", "physical_series_id": "survey-A",
         "record_id": "figure-reading"},
        {"source_id": "secondary", "locator": "figure-7", "physical_series_id": "survey-A",
         "record_id": "republication"},
    ]
    assert unique_evidence_groups(records) == {
        "survey-A": ["table-reading", "figure-reading", "republication"]
    }


def test_independent_series_at_one_locator_remain_distinct():
    records = [
        {"source_id": "primary", "locator": "figure-1", "physical_series_id": "survey-A",
         "record_id": "first-survey"},
        {"source_id": "primary", "locator": "figure-1", "physical_series_id": "survey-B",
         "record_id": "second-survey"},
    ]
    assert len(unique_evidence_groups(records)) == 2
