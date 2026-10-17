import numpy as np
import pytest

from vkm_world.geometry_build.column_display import column_translation, translate_contacts
from vkm_world.geometry_build.height_frames import (
    NATIVE, DISPLAY, TRANSFORMED, calculation_coordinates, geometry_coordinates,
)


def operation():
    return {"kind": "VERTICAL_OFFSET", "from_crs": "source", "to_crs": "target",
            "basis": "INDEPENDENT_GEODETIC_CONTROL", "evidence_ids": ["synthetic-control"],
            "control_points": ["synthetic-benchmark"], "parameters": {"offset_m": 7.}}


def test_display_points_rejected_even_with_native_copy():
    native = np.array([[0., 0., -50.], [1., 2., -40.]])
    points = geometry_coordinates(native, role=DISPLAY, source_frame="source", display_delta=[70., 90.])
    for role in (DISPLAY, NATIVE):
        with pytest.raises(ValueError):
            calculation_coordinates(points, native, role=role, source_frame="source", target_frame="source")
    np.testing.assert_array_equal(native[:, 2], [-50., -40.])


def test_real_points_must_apply_exactly_the_declared_vertical_operation():
    native = np.array([[0., 0., -50.], [1., 2., -40.]])
    points = geometry_coordinates(native, role=TRANSFORMED, source_frame="source",
                                  target_frame="target", operation=operation())
    np.testing.assert_array_equal(points[:, 2], [-43., -33.])
    calculation_coordinates(points, native, role=TRANSFORMED, source_frame="source",
                            target_frame="target", operation=operation())
    for bad in (native, points + [0, 0, 1.]):
        with pytest.raises(ValueError, match="Points disagree"):
            calculation_coordinates(bad, native, role=TRANSFORMED, source_frame="source",
                                    target_frame="target", operation=operation())


@pytest.mark.parametrize("change", [
    {"basis": "DSM_CLOSURE"}, {"evidence_ids": []}, {"control_points": []},
    {"parameters": {}}, {"from_crs": "target", "to_crs": "source"},
])
def test_unsupported_height_ties_fail_closed(change):
    op = operation(); op.update(change)
    with pytest.raises(ValueError):
        geometry_coordinates([[0, 0, 5]], role=TRANSFORMED, source_frame="source",
                             target_frame="target", operation=op)


def test_absolute_constraint_sensitivity_is_preserved_only_in_native_geometry():
    roof = np.array([-100., -70., -90.]); surface = np.array([120., 130., 110.])
    contacts = roof[None, :] + np.array([[-10.], [0.], [180.]])
    changed = contacts + np.array([90., -30., 55.])
    # Reproduce the review's loss of the common absolute constraint explicitly.
    np.testing.assert_allclose(translate_contacts(contacts, column_translation(contacts[-1], surface)),
                               translate_contacts(changed, column_translation(changed[-1], surface)))
    native = np.column_stack([np.arange(3), np.zeros(3), contacts[1]])
    updated = np.column_stack([np.arange(3), np.zeros(3), changed[1]])
    a = geometry_coordinates(native, role=NATIVE, source_frame="source")
    b = geometry_coordinates(updated, role=NATIVE, source_frame="source")
    np.testing.assert_array_equal(b[:, 2] - a[:, 2], [90., -30., 55.])
