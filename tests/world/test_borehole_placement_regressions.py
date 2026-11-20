"""Desired contracts for the reviewed borehole foundation.

Expected RED on PUBLIC 04a1228748be1b965ac422fc96b6404f737abb39.
These are proposed regressions, NOT existing project tests or real-data failures.
Run inside the repository or with the provided isolated runtime.
"""
import pytest
from shapely.geometry import Point, Polygon, shape
from vkm_world.geometry_build.borehole_placement import Domain, Relative, realize


def point(x, y):
    return {"type": "Point", "coordinates": [x, y]}


@pytest.mark.parametrize("interval", [(1., 1.), (.999, 1.001)])
def test_known_feasible_relative_placement_is_not_lost_to_a_fixed_candidate_grid(interval):
    anchor = Domain("anchor", "BH-A", "LOCAL_METRES", point(0., 0.), fixed=True)
    line = {"type": "LineString", "coordinates": [[0., 0.], [2., 0.]]}
    subject = Domain("subject", "BH-B", "LOCAL_METRES", line)
    relation = Relative("offset", "BH-B", "BH-A", "LOCAL_METRES", dx=interval, dy=(0., 0.))
    # Analytic witness: the failure cannot be attributed to inconsistent source constraints.
    assert shape(line).covers(Point(1., 0.))
    case = realize([anchor, subject], [relation], seed=41, count=1)["cases"][0]
    assert case["status"] == "PLACED", case["status"]


@pytest.mark.parametrize("bad_value", ["false", "true", 1])
def test_fixed_requires_an_actual_boolean(bad_value):
    domain = Domain("a", "BH-A", "LOCAL_METRES", point(2., 3.), fixed=bad_value)
    with pytest.raises((ValueError, TypeError)):
        realize([domain], seed=1)


def test_relation_bounds_reject_booleans_as_distances():
    anchor = Domain("a", "BH-A", "LOCAL_METRES", point(0., 0.), fixed=True)
    subject = Domain("b", "BH-B", "LOCAL_METRES", point(1., 0.), fixed=True)
    relation = Relative("ab", "BH-B", "BH-A", "LOCAL_METRES", dx=(True, True), dy=(False, False))
    with pytest.raises((ValueError, TypeError)):
        realize([anchor, subject], [relation], seed=2, count=1)


def test_a_long_narrow_domain_has_nontrivial_variation():
    polygon = Polygon([(0., 0.), (1000., 1000.), (1000., 1001.), (0., 1.)])
    assert polygon.is_valid and polygon.area > 0
    domain = Domain("corridor", "BH-SLIVER", "LOCAL_METRES", polygon.__geo_interface__)
    case = realize([domain], seed=41, count=8)["cases"][0]
    assert len(case["realizations"]) > 1, "One representative point is not a variation study of the long domain"
