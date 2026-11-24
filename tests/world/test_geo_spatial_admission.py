from copy import deepcopy

import pytest
from shapely.geometry import box

from vkm_world.geometry_build.wholemine_foundation import SpatialConfiguration, bind_factors


def configuration(*, key="scenario-1", object_id="b", membership="TARGET", point=None):
    position = {
        "object_id": object_id,
        "xy": [1., 1.],
        "membership": membership,
        "frame": "SH1",
        "scientific_use_mask": "DO_NOT_USE_AS_SPATIAL_CONTROL",
        "epistemic_status": "MODEL_CHOICE",
        "role": "PRIOR_DOMINATED",
        "source_factor_ids": [],
        "relation_ids": ["BHJ-PROFILE-ORDER"],
    }
    position.update(point or {})
    return SpatialConfiguration(key, "branch-1", "SH1", (position,), (), {})


def factor(*, factor_id="f1", object_id="b", support=None):
    return {
        "factor_id": factor_id,
        "physical_id": object_id,
        "interpretation_id": "ROOF",
        "admission": "HARD",
        "quantity": "DEPTH_TVD",
        "epistemic_status": "FACT",
        "contact_ids": ["TOP"],
        "bounds": {"lower": 10., "upper": 10., "unit": "m"},
        "source_refs": [{"source_id": "SYNTHETIC", "locator": "row 1", "record_id": "D1"}],
        "support": support or {"kind": "NONE", "frame_id": None, "xy": None},
    }


def admitted(*, point=None):
    entry = {
        "physical_id": "b",
        "frame_id": "SH1",
        "xy": [1., 1.],
        "reason": "Named conditional spatial hypothesis for this field",
        "source_refs": ["BHJ-PROFILE-ORDER"],
    }
    entry.update(point or {})
    return {"scenario-1": {"f1": entry}}


@pytest.fixture
def domain():
    return box(0, 0, 4, 4)


@pytest.fixture
def qualified():
    return {
        "scientific_use_mask": "CONDITIONAL_LOCAL_MAP_CONTROL",
        "epistemic_status": "DERIVATION",
        "role": "IDENTIFIED_IN_DECLARED_FRAME",
        "source_factor_ids": ["QUALIFIED-LOCAL-MAP-MARK"],
    }


def test_numeric_prior_position_without_scenario_admission_is_context(domain):
    spatial = configuration()
    vertical = factor()
    bound, excluded = bind_factors([vertical], spatial, "ROOF", domain)
    assert bound == []
    assert excluded == {"f1": "NO_CONDITIONAL_SPATIAL_ADMISSION"}
    assert spatial.positions[0]["xy"] == [1., 1.]
    assert vertical["epistemic_status"] == "FACT"


def test_explicit_scenario_keeps_exact_vertical_constraint_and_conditional_xy(domain):
    spatial = configuration()
    exact = factor()
    original = deepcopy(exact)
    spatial_admissions = admitted()

    (bound,), excluded = bind_factors([exact], spatial, "ROOF", domain,
                                      spatial_admissions=spatial_admissions)
    assert excluded == {}
    assert exact == original
    assert bound["admission"] == "HARD"
    assert bound["epistemic_status"] == "FACT"
    assert bound["source_refs"] == exact["source_refs"]
    assert bound["bounds"] == exact["bounds"]
    assert bound["support"]["xy"] == spatial.positions[0]["xy"]
    provenance = bound["support"]["spatial_provenance"]
    assert provenance["application_role"] == "CONDITIONAL_SCENARIO_OPERATOR"
    assert provenance["scientific_use_mask"] == "DO_NOT_USE_AS_SPATIAL_CONTROL"
    assert provenance["position_role"] == "PRIOR_DOMINATED"
    assert provenance["position_epistemic_status"] == "MODEL_CHOICE"
    assert provenance["relation_ids"] == ["BHJ-PROFILE-ORDER"]
    assert provenance["scenario_admission"] == spatial_admissions["scenario-1"]["f1"]


def test_admission_is_bound_to_scenario_and_factor(domain):
    different_scenario = configuration(key="scenario-2")
    unmatched_factor = factor(factor_id="f2")
    rows, excluded = bind_factors([factor()], different_scenario, "ROOF", domain,
                                  spatial_admissions=admitted())
    assert rows == []
    assert excluded == {"f1": "NO_CONDITIONAL_SPATIAL_ADMISSION"}
    rows, excluded = bind_factors([unmatched_factor], configuration(), "ROOF", domain,
                                  spatial_admissions=admitted())
    assert rows == []
    assert excluded == {"f2": "NO_CONDITIONAL_SPATIAL_ADMISSION"}


@pytest.mark.parametrize("patch", [
    {"physical_id": "wrong"}, {"frame_id": "NATIVE"}, {"xy": [2., 1.]},
    {"xy": [True, 1.]}, {"reason": " "}, {"source_refs": []},
])
def test_invalid_conditional_spatial_admission_is_rejected(patch, domain):
    with pytest.raises(ValueError, match="scenario spatial admission"):
        bind_factors([factor()], configuration(), "ROOF", domain,
                     spatial_admissions=admitted(point=patch))


def test_qualified_local_map_preserves_role_and_is_a_distinct_approval(domain, qualified):
    (row,), excluded = bind_factors([factor()], configuration(point=qualified), "ROOF", domain)
    assert excluded == {}
    provenance = row["support"]["spatial_provenance"]
    assert provenance["application_role"] == "QUALIFIED_LOCAL_MAP_CONTROL"
    assert provenance["position_epistemic_status"] == "DERIVATION"
    assert provenance["source_factor_ids"] == ["QUALIFIED-LOCAL-MAP-MARK"]


@pytest.mark.parametrize("patch", [
    {"scientific_use_mask": None}, {"role": "PRIOR_DOMINATED"},
    {"epistemic_status": "FACT"}, {"source_factor_ids": []},
])
def test_inconsistent_claimed_local_map_qualification_is_rejected(patch, domain, qualified):
    bounds, excluded = bind_factors([factor()], configuration(point=qualified | patch), "ROOF", domain)
    assert bounds == []
    assert excluded == {"f1": "UNQUALIFIED_BHJ_SPATIAL_CONTROL"}


def test_external_vertical_admission_is_independent_of_spatial_allowance(domain, qualified):
    ext = configuration(membership="SUPPORT", point=qualified)
    vertical = factor()
    rows, excluded = bind_factors([vertical], ext, "ROOF", domain)
    assert rows == []
    assert excluded == {"f1": "NO_SKRU1_VERTICAL_ADMISSION_FOR_EXTERNAL_SUPPORT"}
    (row,), excluded = bind_factors([vertical], ext, "ROOF", domain, allowed_supports=["b"])
    assert excluded == {}
    assert row["support"]["spatial_provenance"]["membership"] == "SUPPORT"

    ext_conditional = configuration(membership="SUPPORT")
    rows, excluded = bind_factors([vertical], ext_conditional, "ROOF", domain,
                                  spatial_admissions=admitted())
    assert rows == []
    assert excluded == {"f1": "NO_SKRU1_VERTICAL_ADMISSION_FOR_EXTERNAL_SUPPORT"}
    (row,), excluded = bind_factors([vertical], ext_conditional, "ROOF", domain,
                                    allowed_supports=["b"], spatial_admissions=admitted())
    assert excluded == {}
    assert row["support"]["spatial_provenance"]["application_role"] == "CONDITIONAL_SCENARIO_OPERATOR"


@pytest.mark.parametrize("patch", [
    {"frame_id": "UNREGISTERED_NATIVE"}, {"xy": [2., 1.]}, {"configuration_id": "stale"},
])
def test_bound_point_cannot_overwrite_source_support(patch, domain, qualified):
    support = {"kind": "POINT", "frame_id": "SH1", "xy": [1., 1.], "configuration_id": None}
    rows, excluded = bind_factors([factor(support=support | patch)], configuration(point=qualified),
                                  "ROOF", domain)
    assert rows == []
    assert excluded == {"f1": "POINT_SUPPORT_DOES_NOT_MATCH_BHJ_BINDING"}


def test_exact_current_frame_explicit_point_survives_binding(domain, qualified):
    support = {"kind": "POINT", "frame_id": "SH1", "xy": [1., 1.], "configuration_id": None}
    (row,), excluded = bind_factors([factor(support=support)], configuration(point=qualified),
                                    "ROOF", domain)
    assert excluded == {}
    assert row["support"]["xy"] == support["xy"]
    assert row["support"]["frame_id"] == support["frame_id"]
    assert row["support"]["configuration_id"] == "scenario-1"
    assert row["support"]["spatial_provenance"]["vertical_template_support"] == support


def test_external_vertical_admission_rejects_string_membership(domain, qualified):
    ext = configuration(membership="SUPPORT", object_id="b", point=qualified)
    with pytest.raises(ValueError, match="exact owner IDs"):
        bind_factors([factor()], ext, "ROOF", domain, allowed_supports="bb")
