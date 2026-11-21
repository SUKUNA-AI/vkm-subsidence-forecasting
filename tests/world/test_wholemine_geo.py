import numpy as np
import pytest
from shapely.geometry import Polygon, box

from vkm_world.geometry_build.geology_area_operators import AreaOperators
from vkm_world.geometry_build.wholemine_foundation import (
    Foundation, SpatialConfiguration, bind_factors, choose_design,
)


def cfg(key, branch, xy):
    return SpatialConfiguration(key, branch, "SH1", (
        {"object_id": "a", "xy": xy, "membership": "TARGET", "frame": "SH1"},
        {"object_id": "b", "xy": None, "membership": "TARGET", "frame": None},
        {"object_id": "external", "xy": [1, 1], "membership": "SUPPORT", "frame": "SH1"},
    ), (), {})


def test_native_area_mean_preserves_affine_value_and_hole():
    nodes = [[0, 0], [4, 0], [4, 4], [0, 4]]
    p = Polygon(nodes, holes=[[[1, 1], [2, 1], [2, 3], [1, 3]]])
    indices, weights = AreaOperators(nodes, [[0, 1, 2], [0, 2, 3]]).stencil(p)[0]
    values = np.asarray(nodes) @ [3, -2] + 7
    assert p.area == 14
    assert np.dot(values[indices], weights) == pytest.approx(7 + 3*p.centroid.x - 2*p.centroid.y)


def test_area_operator_requires_whole_declared_source_support():
    operator = AreaOperators([[0, 0], [4, 0], [4, 4]], [[0, 1, 2]])
    assert operator.stencil(box(0, 0, 4, 4)) == (None, 8.)
    with pytest.raises(ValueError, match="qualified"):
        operator.stencil(Polygon([(0, 0), (2, 2), (0, 2), (2, 0)]))


def test_foundation_keeps_identity_and_hypothesis_per_configuration():
    configs = (cfg("a1", "a", [1, 1]), cfg("a2", "a", [2, 1]), cfg("b1", "b", [0, 2]))
    first = Foundation(("a", "b"), ("external",), configs, {}, None)
    second = Foundation(("a", "b"), ("external",), configs[::-1], {}, None)
    picked = choose_design(first, additional=1)
    assert len(picked) == 3
    assert {x.branch for x, _ in picked} == {"a", "b"}
    assert picked == choose_design(second, additional=1)
    assert all(x.positions[1]["xy"] is None for x, _ in picked)


def test_factors_bind_own_position_without_filling_unknown_or_using_external():
    def f(identity):
        return {"factor_id": identity, "physical_id": identity, "interpretation_id": "ROOF",
                "admission": "HARD", "support": {"kind": "NONE", "xy": None}}
    config = cfg("a1", "a", [1, 1])
    rows, excluded = bind_factors([f("a"), f("b"), f("external")], config, "ROOF", box(0, 0, 2, 2))
    assert [r["physical_id"] for r in rows] == ["a"]
    assert rows[0]["support"]["xy"] == [1, 1]
    assert rows[0]["support"]["configuration_id"] == "a1"
    assert excluded["b"] == "UNKNOWN_XY"
    assert excluded["external"] == "NO_SKRU1_VERTICAL_ADMISSION_FOR_EXTERNAL_SUPPORT"
    with pytest.raises(ValueError, match="Duplicate"):
        bind_factors([f("a"), f("a")], config, "ROOF", box(0, 0, 2, 2))
