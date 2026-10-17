"""Data-free guards for plan semantics, time and registration direction."""
import numpy as np

from vkm_world.geometry_build.mining import (
    affine_inverse, apply_affine, date_bounds, fit_similarity, plan_state, seam_candidates,
    semantic_role,
    source_record_ids, mine_identity_relation,
)


def test_remaining_and_layout_do_not_become_excavation():
    assert semantic_role("REMAINING_RESERVES_2013_014") == "REMAINING_RESERVES"
    assert semantic_role("PANEL_BLOCK_LAYOUT_014") == "PANEL_OR_BLOCK_DOMAIN"
    assert semantic_role("WORKED_KR2_064_DEVELOPMENT_SUSPECT") == "DEVELOPMENT_WORKINGS_CANDIDATE"


def test_ambiguous_seams_remain_alternatives():
    assert seam_candidates("АБ|КрII (в скане не различимы)") == ["AB", "KR2"]
    assert seam_candidates("ALL (layout)") == []
    assert seam_candidates("UNKNOWN_ORIENTATION") == []
    assert seam_candidates("В (карналлитовый)") == ["VK"]


def test_ore_type_does_not_assign_an_unnamed_seam_or_drop_explicit_variants():
    for value in ["сильвинит", "сильвинит (пласты не названы)", "карналлит (пласт не назван)"]:
        assert seam_candidates(value) == []
    assert seam_candidates("АБ (сильвиниты; пласт по контексту)") == ["AB"]
    assert seam_candidates("АБ; КрII; Вс; Вк") == ["AB", "KR2", "VK", "VS"]
    assert seam_candidates("КрIII") == ["KR3"]
    assert seam_candidates("В (сильвинитовый, Вс)") == ["VS"]


def test_repeated_source_ids_retain_unique_observations_without_name_merging():
    features = [{"properties": {"id": "LABEL", "name": "panel"}, "geometry": {"type": "Point", "coordinates": xy}}
                for xy in [[0, 0], [20, 30]]]
    ids = source_record_ids("site", "points", features)
    assert len(set(ids)) == 2
    assert source_record_ids("site", "points", list(reversed(features))) == list(reversed(ids))
    assert len(set(source_record_ids("site", "points", [features[0], features[0]]))) == 2
    assert [f["properties"]["id"] for f in features] == ["LABEL", "LABEL"]


def test_mine_identity_uses_explicit_source_mine_not_folder_context():
    assert mine_identity_relation("SKRU1", "SKRU1") == "CONSISTENT_SOURCE_ATTRIBUTES"
    assert mine_identity_relation("SKRU2", "SKRU3") == "CONFLICT"
    assert mine_identity_relation("SKRU2_OR_SKRU3_UNATTRIBUTED", "SKRU3") == "AMBIGUOUS_SOURCE_ATTRIBUTION"
    assert mine_identity_relation("SKRU1", "SKRU1_OR_SKRU2_UNATTRIBUTED") == "AMBIGUOUS_SOURCE_ATTRIBUTION"
    assert mine_identity_relation(None, "SKRU1") == "UNKNOWN"


def test_publication_year_is_not_event_or_plan_date():
    assert plan_state({"layer": "REMAINING_RESERVES_2013_014"})["state_date"] is None
    assert plan_state({"state_date": "UNKNOWN (не позднее 2008)"})["state_date"] is None
    assert plan_state({"state_date_as_printed": "на 01.01.2007"})["state_date"] == "2007-01-01"
    assert plan_state({"date_printed_on_sheet": "1.10.2007г."})["time_status"].startswith("ENGINEERING_ASSUMPTION")
    assert date_bounds(None) == (None, None)
    assert date_bounds("2012") == ("2012-01-01", "2012-12-31")
    assert date_bounds("2012-02") == ("2012-02-01", "2012-02-29")


def test_frame_inverse_preserves_direction_and_units():
    matrix = [[1.02, -.01, 200], [.01, 1.02, -5600]]
    points = np.array([[0, 0], [3000, 100], [-2000, 8000]])
    np.testing.assert_allclose(apply_affine(affine_inverse(matrix), apply_affine(matrix, points)), points, atol=1e-10)


def test_similarity_rejects_unidentifiable_controls_and_reports_spatial_condition():
    import pytest
    with pytest.raises(ValueError, match="Rank deficient"):
        fit_similarity([[0, 0]] * 3, [[1, 1]] * 3)
    source = np.array([[0, 0], [100, 0], [0, 100], [100, 100]])
    target = apply_affine([[1, -.01, 20], [.01, 1, -30]], source)
    fit = fit_similarity(source, target)
    assert fit["rms_m"] < 1e-10
    assert fit["loo_rms_m"] < 1e-10
    assert fit["spatial_condition_number"] == pytest.approx(1)
