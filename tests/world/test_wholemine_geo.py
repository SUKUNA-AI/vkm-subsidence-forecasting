import numpy as np
import pytest
from shapely.geometry import Polygon, box

from vkm_world.geometry_build.geology_area_operators import AreaOperators
from vkm_world.geometry_build.wholemine_foundation import (
    Foundation, SpatialConfiguration, bind_factors, choose_design,
)


def cfg(key, branch, xy):
    return SpatialConfiguration(key, branch, "SH1", (
        {"object_id": "a", "xy": xy, "membership": "TARGET", "frame": "SH1",
         "scientific_use_mask": "CONDITIONAL_LOCAL_MAP_CONTROL",
         "epistemic_status": "DERIVATION", "role": "IDENTIFIED_IN_DECLARED_FRAME",
         "source_factor_ids": ["A-LOCAL-MAP-MARK"]},
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



def test_branch_representative_is_nearest_median_not_claimed_medoid():
    # Nearest to the component median is d; the unique Euclidean medoid is e.
    # Preserve the historical criterion without calling it a medoid.
    xy = [[3, 6], [7, 2], [0, 4], [5, 6], [5, 5], [7, 7], [7, 6]]
    configs = tuple(cfg(chr(97+i), "branch", point) for i, point in enumerate(xy))
    foundation = Foundation(("a", "b"), ("external",), configs, {}, None)
    picked = choose_design(foundation, additional=0)
    points = np.asarray(xy, dtype=float)
    midpoint = np.median(points, axis=0)
    nearest = int(np.argmin(((points - midpoint) ** 2).sum(axis=1)))
    medoid = int(np.argmin(np.linalg.norm(points[:, None] - points[None, :], axis=2).sum(axis=1)))
    assert configs[nearest].id == "d" and configs[medoid].id == "e"
    assert nearest != medoid
    assert picked[0][0].id == configs[nearest].id
    assert picked[0][1] == "WITHIN_BRANCH_MEDIAN_NEAREST"
    assert picked == choose_design(Foundation(("a", "b"), ("external",), configs[::-1], {}, None), additional=0)



def interval_semantics_document():
    contacts = ["PDKS", "MG", "MG_BASE", "GAT", "BOTTOM"]
    rows = []
    for upper, lower, unit in zip(contacts[:-1], contacts[1:], ["PDKS_UPPER", "MARKER_CLAY", "PDKS_LOWER", None]):
        rows.append({"upper_contact_id": upper, "lower_contact_id": lower,
                     "unit_id": unit, "material_id": None,
                     "role": "GEOLOGICAL_UNIT" if unit else "NUMERICAL_BUFFER",
                     "source_refs": [{"source_id": "synthetic", "locator": "column"}]})
    return contacts, {"schema": "vkm.interval-semantics/1", "frame_id": "SH1",
                      "source_identity": "field-generation", "intervals": rows}


def test_body_semantics_uses_both_boundaries_and_preserves_thin_marker():
    from vkm_world.geometry_build.wholemine_foundation import bind_interval_semantics
    contacts, document = interval_semantics_document()
    document["intervals"].reverse()
    bound = bind_interval_semantics(contacts, document, frame_id="SH1", source_identity="field-generation")
    heights = np.array([50., 18., 16., -120., -140.])
    bodies = {row["unit_id"]: (row["upper_contact_id"], row["lower_contact_id"],
                              heights[i] - heights[i+1]) for i, row in enumerate(bound)}
    assert bodies["PDKS_UPPER"] == ("PDKS", "MG", 32.)
    assert bodies["MARKER_CLAY"] == ("MG", "MG_BASE", 2.)
    assert bound[-1]["role"] == "NUMERICAL_BUFFER" and bound[-1]["unit_id"] is None


@pytest.mark.parametrize("change", ["frame", "source", "pair", "duplicate", "missing", "buffer_material"])
def test_interval_semantics_rejects_cross_generation_or_incomplete_bindings(change):
    from vkm_world.geometry_build.wholemine_foundation import bind_interval_semantics
    contacts, document = interval_semantics_document()
    if change == "frame": document["frame_id"] = "UTM40N"
    if change == "source": document["source_identity"] = "another-field"
    if change == "pair": document["intervals"][0]["upper_contact_id"] = "MG"
    if change == "duplicate": document["intervals"].append(document["intervals"][0])
    if change == "missing": document["intervals"].pop()
    if change == "buffer_material": document["intervals"][-1]["material_id"] = "clay"
    with pytest.raises(ValueError):
        bind_interval_semantics(contacts, document, frame_id="SH1", source_identity="field-generation")
