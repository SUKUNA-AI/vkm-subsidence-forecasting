import numpy as np
import pytest
from shapely.geometry import Polygon

from vkm_world.geometry_build.constrained_geology import interval_envelope, joint_predict, grouped_contact_cv


def test_thickness_has_free_datum_and_no_fabricated_anchor():
    r = interval_envelope(2, [{"id": "thickness", "coefficients": [1, -1], "lower": 4, "upper": 7}])
    assert r["feasible"] and r["global_translation_free"]
    assert r["constraint_rank"] == 1
    assert r["bounds"] == [[None, None], [None, None]]


def test_interval_is_not_replaced_by_its_midpoint():
    r = interval_envelope(2, [{"coefficients": [1, 0], "lower": -174, "upper": -147},
                               {"coefficients": [1, -1], "lower": 6, "upper": 6}])
    assert r["bounds"] == [[-174, -147], [-180, -153]]
    assert not r["global_translation_free"]


def test_absence_and_contradiction_are_not_hidden_by_clipping():
    records = [{"coefficients": [1, -1], "lower": 0, "upper": 0}]
    assert interval_envelope(2, records)["feasible"]
    records[0].update(lower=-2, upper=-1)
    assert not interval_envelope(2, records)["feasible"]
    with pytest.raises(ValueError, match="crossing"):
        joint_predict([[0, 0]], [[-2, -1]], [[0, 0]], "linear")


@pytest.mark.parametrize("method", ["nearest", "idw2", "linear"])
def test_common_weights_preserve_order_and_reproduce_source_pinchout(method):
    xy = np.array([[0, 0], [1, 0], [0, 1]])
    levels = np.array([[5, 5, -1], [6, 3, -5], [8, 2, 0]])
    result, masks, weights = joint_predict(xy, levels, [[0, 0], [.3, .3], [2, 2]], method)
    np.testing.assert_equal(result[0], levels[0])
    assert np.all(np.diff(result[:2], axis=1) <= 0)
    assert masks.tolist() == ["DIRECT_CONSTRAINT", "INTERPOLATION", "UNKNOWN"]
    assert np.isnan(result[2]).all()
    np.testing.assert_allclose(weights[:2].sum(axis=1), 1)


def test_support_hole_remains_unknown_despite_convex_hull():
    xy = [[0,0], [4,0], [0,4], [4,4]]
    support = Polygon(xy[:2]+[xy[3], xy[2]], holes=[[(1,1),(3,1),(3,3),(1,3)]])
    result, masks, _ = joint_predict(xy, [[0,-2]]*4, [[2,2], [.5,.5], [0,0]], "linear", support)
    assert masks.tolist() == ["UNKNOWN", "UNKNOWN", "DIRECT_CONSTRAINT"]


def test_disconnected_regions_cannot_share_interpolation_weights():
    from shapely.geometry import MultiPolygon
    a = Polygon([(0,0),(1,0),(0,1)])
    b = Polygon([(2,0),(3,0),(2,1)])
    xy = [[0,0],[1,0],[0,1],[2,0],[3,0],[2,1]]
    levels = [[10,5]]*3+[[100,50]]*3
    for method in ("nearest", "idw2", "linear"):
        result, masks, weights = joint_predict(xy, levels, [[.2,.2],[2.2,.2]], method, MultiPolygon([a,b]))
        np.testing.assert_allclose(result, [[10,5],[100,50]])
        np.testing.assert_equal(weights[0,3:], 0)
        np.testing.assert_equal(weights[1,:3], 0)


def test_engineering_extension_is_explicit_and_cannot_invert_contacts():
    xy=[[0,0],[1,0],[0,1]];levels=[[5,0],[7,-2],[4,4]]
    a,m,w=joint_predict(xy,levels,[[5,5]],"idw2",extrapolate=True)
    assert m[0]=="EXTRAPOLATION" and a[0,0]>=a[0,1]
    assert np.isnan(joint_predict(xy,levels,[[5,5]],"idw2")[0]).all()
    with pytest.raises(ValueError,match="linear extrapolation"):
        joint_predict(xy,levels,[[5,5]],"linear",extrapolate=True)


def test_cv_uses_same_support_and_excludes_whole_physical_well():
    xy = [[0,0], [4,0], [0,4], [4,4], [1,1]]
    levels = [[0, -2], [0, -4], [0, -8], [0, -7], [0, -3]]
    ids = ["a", "b", "c", "d", "e"]
    r = grouped_contact_cv(xy, levels, ids, ["west","east","west","east","west"])
    assert all(row["well"] not in row["training_groups"] for row in r["predictions"])
    for method in ("nearest", "idw2", "linear"):
        assert r["summary"]["leave_one_physical_borehole_out/"+method]["common_support_wells"] == ["e"]
    with pytest.raises(ValueError, match="physical duplicate"):
        grouped_contact_cv(xy, levels, ["a","a","b","c","d"])
