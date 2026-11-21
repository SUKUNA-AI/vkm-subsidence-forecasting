import pytest
from shapely.geometry import Point, shape

from vkm_world.geometry_build.borehole_placement import Domain, Relative, realize


def box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]}


def point(x, y):
    return {"type": "Point", "coordinates": [x, y]}


def test_joint_bounded_design_rejects_forbidden_pairs_and_repeats():
    a = Domain("a", "BH-A", "SH1", point(0, 0), fixed=True)
    b = Domain("b", "BH-B", "SH1", box(-4, -2, 4, 2))
    c = Domain("c", "BH-C", "SH1", {"type": "LineString", "coordinates": [[2, 0], [4, 0]]})
    east = Relative("east", "BH-B", "BH-A", "SH1", dx=(1, 3), distance=(2, 4))
    join = Relative("join", "BH-C", "BH-B", "SH1", distance=(0, 2))
    result = realize([a, b, c], [east, join], seed=41, count=5)
    assert result == realize([c, b, a], [join, east], seed=41, count=5)
    case = result["cases"][0]
    assert case["status"] == "PLACED"
    assert len(case["realizations"]) > 1
    for run in case["realizations"]:
        current = {d["site_id"]: d for d in run["positions"]}
        assert run["id"].startswith(case["id"]) and run["seed"] == 41
        assert current["BH-A"]["xy"] == [0.0, 0.0]
        assert current["BH-A"]["coordinate_role"] == "SOURCE_FIXED"
        place = Point(*current["BH-B"]["xy"])
        assert shape(b.geometry).covers(place)
        assert 1 <= place.x <= 3
        assert current["BH-B"]["status"] == "MODEL_CHOICE"
        assert place.distance(Point(*current["BH-C"]["xy"])) <= 2


def test_alternatives_select_once_for_multiple_wells_in_native_frame():
    domains = [
        Domain(f"{i}-{case}", f"BH-{i}", "SH1", box(offset, i, offset + 1, i + 1),
               alternative_group="plan_pose", alternative_id=case)
        for i in (1, 2) for case, offset in (("a", 0), ("b", 100))
    ]
    domains.append(Domain("elsewhere", "BH-1", "PLAN_RAW", point(7, 10), fixed=True))
    all_cases = realize(domains, seed=1)["cases"]
    assert len(all_cases) == 3
    for case in all_cases:
        positions = case["realizations"][0]["positions"]
        if case["frame_id"] == "PLAN_RAW":
            assert positions[0]["xy"] == [7.0, 10.0]
            assert case["alternatives"] == {}
        else:
            assert len(positions) == 2
            offset = {"a": 0, "b": 100}[case["alternatives"]["plan_pose"]]
            assert all(offset <= row["xy"][0] <= offset + 1 for row in positions)


def test_empty_conflict_and_unbound_anchor_never_create_a_position():
    one = Domain("one", "BH-75", "SH1", box(0, 0, 1, 1))
    two = Domain("two", "BH-75", "SH1", box(5, 5, 6, 6))
    result = realize([one, two], seed=0)["cases"][0]
    assert result["status"] == "CONFLICT_EMPTY_INTERSECTION"
    assert result["realizations"] == []
    absent = Relative("rel", "BH-75", "BH-30", "SH1", dx=(0, 5))
    assert realize([one], [absent], seed=0)["cases"][0]["status"] == "UNRESOLVED_RELATION_ANCHOR"
    with pytest.raises(ValueError, match="unbounded approximate"):
        realize([Domain("not-qualified", "BH-75", "SH1", point(1, 2))], seed=0)


def test_unsatisfied_restriction_is_a_discrete_failure_not_a_position():
    one = Domain("one", "BH-75", "SH1", point(0, 0), fixed=True)
    two = Domain("two", "BH-30", "SH1", point(0, 1), fixed=True)
    rel = Relative("east", "BH-30", "BH-75", "SH1", dx=(1, None))
    result = realize([one, two], [rel], seed=0)["cases"][0]
    assert result["status"] == "NO_FEASIBLE_SAMPLED_REALIZATION"
    assert not result["realizations"]


def test_changed_coordinate_changes_case_and_realization_identity():
    p1 = Domain("same-record", "BH-A", "SH1", point(1, 2), fixed=True)
    p2 = Domain("same-record", "BH-A", "SH1", point(3, 4), fixed=True)
    c1 = realize([p1], seed=5)["cases"][0]
    c2 = realize([p2], seed=5)["cases"][0]
    assert c1["input_content_sha256"] != c2["input_content_sha256"]
    assert c1["id"] != c2["id"]
    assert c1["realizations"][0]["id"] != c2["realizations"][0]["id"]


def test_shared_plan_option_cannot_silently_omit_a_borehole():
    domains = [
        Domain("a1", "BH-A", "SH1", box(1, 0, 2, 1), alternative_group="P", alternative_id="A"),
        Domain("a2", "BH-B", "SH1", box(3, 0, 4, 1), alternative_group="P", alternative_id="A"),
        Domain("b1", "BH-A", "SH1", box(2, 0, 3, 1), alternative_group="P", alternative_id="B"),
    ]
    by_alternative = {c["alternatives"]["P"]: c for c in realize(domains, seed=3)["cases"]}
    assert by_alternative["A"]["status"] == "PLACED"
    assert by_alternative["B"]["status"] == "INCOMPLETE_ALTERNATIVE_PARTICIPANTS"
    assert by_alternative["B"]["missing_site_ids"] == ["BH-B"]
    assert not by_alternative["B"]["realizations"]
