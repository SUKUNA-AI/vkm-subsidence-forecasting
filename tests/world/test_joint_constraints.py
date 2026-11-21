"""Independent closed-form checks of the joint spatial relation engine."""

import pytest

from vkm_world.geometry_build.joint_constraints import (
    compile_relations, circle_intersections, generate_mode_candidates,
    line_circle_intersections, relation_residuals, solve_linear_feasibility,
)


def relation(kind, ids, **kwargs):
    return {"id": kind, "kind": kind, "ids": ids, "frame": "local-fixture", **kwargs}


def line(a, b):
    return {"type": "LineString", "coordinates": [a, b]}


def square(x, y, radius):
    return [[x-radius, y-radius], [x+radius, y-radius], [x+radius, y+radius],
            [x-radius, y+radius], [x-radius, y-radius]]


def test_linprog_exact_station_not_in_32_point_design():
    compiled = compile_relations([
        relation("region", ["subject"], geometry=line([0, 0], [2, 0])),
        relation("offset", ["anchor", "subject"], dx=[1, 1]),
    ])
    result = solve_linear_feasibility(compiled, fixed={"anchor": (0, 0)}, max_extrema=2)
    assert result["status"] == "CONTINUOUS_WITNESS"
    assert result["positions"]["subject"] == pytest.approx((1., 0.), abs=1e-10)
    assert result["equality_rank"] == 4


def test_linprog_preserves_two_movable_variables():
    compiled = compile_relations([
        relation("region", ["A"], id="vertical", geometry=line([0, 0], [0, 3])),
        relation("region", ["B"], id="horizontal", geometry=line([0, 2], [5, 2])),
        relation("offset", ["A", "B"], dx=[2.999, 3.001], dy=[1., 1.]),
    ])
    result = solve_linear_feasibility(compiled)
    a, b = (result["positions"][key] for key in ("A", "B"))
    assert a == pytest.approx((0., 1.))
    assert 2.999 <= b[0] <= 3.001
    assert b[1] == pytest.approx(2.)


def test_linprog_does_not_swallow_micrometre_gap():
    compiled = compile_relations([
        relation("region", ["subject"], geometry=line([0, 0], [2, 0])),
        relation("offset", ["anchor", "subject"], dx=[2.000001, 2.000001]),
    ])
    result = solve_linear_feasibility(compiled, fixed={"anchor": (0, 0)})
    assert result["status"] == "LINEAR_OUTER_MODEL_INFEASIBLE"
    assert result["witnesses"] == []
    assert "no exact certificate" in result["proof_scope"]


def test_boundary_distance_and_side_follow_shared_similarity_transform():
    geometry = line([0, 0], [10, 0])
    compiled = compile_relations([
        relation("boundary_distance", ["site"], geometry=geometry, bounds=[2, 2], map_id="shared"),
        relation("side", ["site"], geometry=geometry, side="left", map_id="shared"),
    ])
    transforms = {"shared": [100, 200, 0, 2]}
    for position, expected in [([98, 210], "SATISFIED"), ([102, 210], "VIOLATED")]:
        result = relation_residuals(compiled, {"site": position}, transforms)
        assert result[0]["values"] == (2.,)
        assert result[0]["status"] == "SATISFIED"
        assert result[1]["status"] == expected
    outside_segment = relation_residuals(compiled, {"site": [98, 222]}, transforms)[0]
    assert outside_segment["values"] == pytest.approx((2.**1.5,))
    assert outside_segment["status"] == "VIOLATED"


def test_hull_is_only_an_outer_bound_and_multipolygon_modes_survive():
    geometry = {"type": "MultiPolygon", "coordinates": [
        [square(0., 0., .1)],
        [square(10., 0., .1), square(10., 0., .05)],
    ]}
    compiled = compile_relations([relation("region", ["site"], geometry=geometry)])
    checks = [relation_residuals(compiled, {"site": xy})[0]["status"]
              for xy in [(5., 0.), (10., 0.), (10.1, 0.)]]
    assert checks == ["VIOLATED", "VIOLATED", "SATISFIED"]
    middle = solve_linear_feasibility(compiled, fixed={"site": (5., 0.)})
    assert middle["status"] == "UNRESOLVED_CONTINUOUS_SEARCH"
    result = generate_mode_candidates(compiled, {"site": None}, max_realizations=4)
    locations = [row["positions"]["site"] for row in result["realizations"]]
    assert len(set(locations)) == 4
    assert min(x for x, y in locations) < 1.
    assert max(x for x, y in locations) > 9.


def test_explicit_profile_order_and_bound_corridor():
    compiled = compile_relations([
        relation("profile_order", ["weak", "A", "B"]),
        relation("profile_corridor", ["A", "B", "weak"], station=[-1.1, -1.01],
                 cross_track=[-5., 5.]),
    ])
    fixed = {"A": (1., 1.), "B": (4., 5.)}
    result = generate_mode_candidates(compiled, {"weak": None}, fixed=fixed, max_realizations=8)
    assert result["status"] == "VERIFIED_FEASIBLE"
    assert len(result["realizations"]) >= 5
    for row in result["realizations"]:
        x, y = row["positions"]["weak"]
        assert -1.1 - 1e-8 <= ((x-1)*3 + (y-1)*4) / 25 <= -1.01 + 1e-8
        assert abs(((x-1)*-4 + (y-1)*3) / 5) <= 5 + 1e-8
        assert all(check["status"] == "SATISFIED" for check in row["verification"])


def test_profile_order_does_not_assign_unknown_transverse_position():
    compiled = compile_relations([relation("profile_order", ["A", "weak", "B"])])
    result = generate_mode_candidates(compiled, {"weak": None}, fixed={"A": (0., 0.), "B": (10., 0.)})
    assert result["realizations"] == []
    assert result["partial_incumbents"][0]["positions"]["weak"] is None
    assert result["branches"][0]["components"][0]["status"] == "UNDERIDENTIFIED_NO_FINITE_DESIGN"


def test_circle_and_line_modes_are_not_averaged():
    assert circle_intersections((0., 0.), 13., (10., 0.), 13.) == ((5., -12.), (5., 12.))
    assert line_circle_intersections((-3, 0), (3, 0), (0, 0), 1) == ((-1., 0.), (1., 0.))
    assert line_circle_intersections((-3, 1), (3, 1), (0, 0), 1) == ((0., 1.),)
    two_ranges = compile_relations([
        relation("distance", ["A", "weak"], id="first", bounds=[13., 13.]),
        relation("distance", ["B", "weak"], id="second", bounds=[13., 13.]),
    ])
    result = generate_mode_candidates(two_ranges, {"weak": None}, fixed={"A": (0., 0.), "B": (10., 0.)})
    result_modes = {row["positions"]["weak"] for row in result["realizations"]}
    assert {(5., -12.), (5., 12.)} <= result_modes
    assert (5., 0.) not in result_modes


@pytest.mark.parametrize("illegal", [True, False, "1", float("nan"), float("inf"), 1+2j])
def test_numeric_boundary_strict(illegal):
    with pytest.raises(ValueError, match="finite JSON number"):
        compile_relations([relation("offset", ["A", "B"], dx=[illegal, 10])])


def test_compiler_rejects_wrong_flags_frames_and_collections():
    with pytest.raises(ValueError, match="must be bool"):
        compile_relations([relation("distance", ["A", "B"], bounds=[0, 1], hard="false")])
    with pytest.raises(ValueError, match="incompatible or unknown frames"):
        compile_relations([relation("distance", ["A", "B"], bounds=[0, 1])],
                          {"A": "local-fixture", "B": "other-frame"})
    with pytest.raises(ValueError, match="incompatible GeoJSON"):
        compile_relations([relation("region", ["A"], geometry={
            "type": "GeometryCollection", "geometries": [{"type": "Point", "coordinates": [0., 0.]}],
        })])


def test_alternative_budget_preserves_witness_and_reports_frontier():
    rows = [relation("region", ["site"], id=mode, alternative_group="mode", alternative_id=mode,
                     geometry={"type": "Point", "coordinates": [x, 0.]})
            for mode, x in (("first", 1.), ("second", 2.))]
    first = generate_mode_candidates(compile_relations(rows), {"site": None}, max_hypotheses=1)
    assert first["status"] == "INCOMPLETE_RESOURCE_LIMIT"
    assert first["unvisited_hypothesis_count"] == 1
    assert first["realizations"][0]["positions"]["site"] == (1., 0.)
    output = generate_mode_candidates(compile_relations(rows), {"site": None})
    reverse = generate_mode_candidates(compile_relations(rows[::-1]), {"site": None})
    assert output == reverse
    assert {row["positions"]["site"] for row in output["realizations"]} == {(1., 0.), (2., 0.)}



@pytest.mark.parametrize("point", [(True, 0), (float("inf"), 0), (float("nan"), 0), (0+1j, 0), (0,)])
def test_evaluator_never_accepts_nonfinite_or_nonreal_result(point):
    compiled = compile_relations([relation("distance", ["A", "B"], bounds=[0., 1.])])
    with pytest.raises(ValueError, match="nonfinite or nonnumeric values|wrong vector shape"):
        relation_residuals(compiled, {"A": (0., 0.), "B": point})


def test_frozen_map_marks_follow_pose_without_drifting_to_fit_profile():
    rows = [
        relation("side", ["mapped"], geometry=line([0., 0.], [10., 0.]), side="left", map_id="map"),
        relation("boundary_distance", ["mapped"], geometry=line([0., 0.], [10., 0.]),
                 bounds=[475., 525.], map_id="map"),
        relation("profile_order", ["weak", "start", "end"]),
        relation("profile_corridor", ["start", "end", "weak"], station=[-1.1, -1.01],
                 cross_track=[-50., 50.]),
    ]
    compiled = compile_relations(rows)
    baseline = {"mapped": (5., 500.), "start": (10., 10.), "end": (110., 10.)}
    for tx, ty, a, b in [(0., 0., 1., 0.), (400., -300., 0., 1.02)]:
        def transform(point):
            x, y = point
            return tx + a*x - b*y, ty + b*x + a*y

        fixed = {key: transform(value) for key, value in baseline.items()}
        result = generate_mode_candidates(compiled, {"weak": None}, {"map": (tx, ty, a, b)},
                                          fixed=fixed, max_realizations=6)
        assert len(result["realizations"]) == 6
        for solution in result["realizations"]:
            assert all(solution["positions"][key] == value for key, value in fixed.items())
            residual = {row["kind"]: row for row in solution["verification"]}
            assert residual["boundary_distance"]["values"] == pytest.approx((500. * (a + b),))
            assert all(row["status"] == "SATISFIED" for row in residual.values())
