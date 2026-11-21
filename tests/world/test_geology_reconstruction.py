"""Closed-form acceptance fixtures for the sparse P1 reconstruction."""
import numpy as np
import pytest

from vkm_world.geometry_build.geology_reconstruction import compile_model, evaluate_contacts, solve_model


NODES = [[0., 0.], [2., 0.], [0., 2.], [2., 2.]]
TRIANGLES = [[0, 1, 2], [1, 3, 2]]
POLICY = {"slope_length_m": 2., "contact_weight": 1., "thickness_weight": 1.,
          "time_limit_s": 30., "max_iterations": 100, "hard_atol_m": 1e-7}


def factor(name, quantity, contacts, bounds, *, point=(.5, .5), owner="A", **extra):
    return {"factor_id": name, "quantity": quantity, "admission": "INTERVAL",
            "epistemic_status": "DERIVATION", "contact_ids": contacts,
            "bounds": {"lower": bounds[0], "upper": bounds[1], "unit":"m"},
            "support": {"kind": "POINT", "xy": point, "frame_id": "metric", "configuration_id": "c"},
            "independence_group":"fixture", "source_refs":[{"record_id": name}],
            "physical_id": owner, **extra}


def compile(factors=(), priors=(), **kwargs):
    nodes = kwargs.pop("nodes", NODES)
    triangles = kwargs.pop("triangles", TRIANGLES)
    ids = kwargs.pop("contact_ids", ("A", "B"))
    return compile_model(nodes, triangles, ids, factors, frame_id="metric", configuration_id="c",
                         priors=priors, scenario_basis="declared analytic synthetic gauge", **kwargs)


def roof(height=50.):
    return {"id": "roof", "basis": "declared analytic synthetic gauge", "kind": "roof_plane",
            "contact_id": "A", "origin_xy": [0., 0.], "gradient": [0., 0.],
            "intercept_m": height, "scale_m": 1.}


def thickness(top="A", bottom="B", target=20.):
    return {"id": f"thickness-{top}-{bottom}", "kind": "thickness", "contact_ids": [top, bottom],
            "center_m": target, "scale_m": 1., "basis": "declared analytic closure"}


def test_latent_collar_does_not_erase_vertical_gauge():
    rows = [factor("top", "DEPTH_TVD", ["A"], [100., 100.]),
            factor("base", "DEPTH_TVD", ["B"], [120., 120.])]
    problem = compile(rows)
    result = solve_model(problem, POLICY)
    assert result["status"] == "UNBOUNDED"
    assert result["hard_feasibility"] == "VERIFIED_CONTINUOUS_WITNESS"
    assert result["data_rank_before_prior"]["rank"] == 2
    assert result["scenario_conditioned_rank"]["gradient_null_contact_dimensions"] == 2
    assert "contacts" not in result


def test_declared_roof_closes_two_depths_without_fake_measured_collar():
    rows = [factor("top", "DEPTH_TVD", ["A"], [100., 100.]),
            factor("base", "DEPTH_TVD", ["B"], [120., 120.])]
    problem = compile(rows, [roof()])
    result = solve_model(problem, POLICY)
    assert result["status"] == "MODEL_CLOSED_WITH_ASSUMPTIONS"
    assert result["data_count"] == 2
    values = evaluate_contacts(problem, result["contacts"], [[.5, .5], [10., 10.], [0., 0.]])
    np.testing.assert_allclose(values[0], [50., 30.], atol=1e-8)
    np.testing.assert_allclose(values[2], [50., 30.], atol=1e-8)
    assert np.isnan(values[1]).all()
    assert len(result["nuisance"]) == 1
    assert result["nuisance"][0]["model_value_m"] == pytest.approx(150.)
    assert result["nuisance"][0]["status"] == "CONDITIONAL_MODEL_VARIABLE"


def test_separate_unconnected_components_keep_own_gauges():
    nodes = NODES + [[10., 10.], [12., 10.], [10., 12.]]
    triangles = TRIANGLES + [[4, 5, 6]]
    rows = [factor("local_top", "ELEVATION", ["A"], [100., 100.], datum_mode="COMMON_WORLD"),
            factor("local_base", "ELEVATION", ["B"], [80., 80.], datum_mode="COMMON_WORLD"),
            factor("other_gap", "THICKNESS_VERTICAL", ["A", "B"], [20., 20.], point=(10.5, 10.5))]
    result = solve_model(compile(rows, nodes=nodes, triangles=triangles), POLICY)
    assert result["status"] == "UNBOUNDED"
    assert result["scenario_conditioned_rank"]["horizontal_components"] == 2


def test_censoring_bottom_inside_requires_both_inequalities():
    row = factor("bottom-inside", "BOTTOM_INSIDE_UNIT", ["A", "B"], [395.9, None], depth_basis="TVD")
    problem = compile([row])
    selected = [r for r in problem.rows if r.id.startswith("bottom-inside")]
    assert len(selected) == 2
    assert (selected[0].lower, selected[0].upper) == (None, 395.9)
    assert (selected[1].lower, selected[1].upper) == (395.9, None)
    assert set(selected[0].indices) != set(selected[1].indices)
    assert problem.variable_ids[selected[0].indices[-1]] == "COLLAR:A"
    assert problem.variable_ids[selected[1].indices[-1]] == "COLLAR:A"


def test_common_source_datum_exposes_conflict_in_unchanged_intervals():
    rows = [factor("zA", "ELEVATION", ["A"], [100., 100.],
                   datum_mode="LATENT_SOURCE_OFFSET", datum_group="shared"),
            factor("zB", "ELEVATION", ["B"], [120., 120.],
                   datum_mode="LATENT_SOURCE_OFFSET", datum_group="shared")]
    problem = compile(rows)
    assert [s for s in problem.variable_ids if s.startswith("DATUM:")] == ["DATUM:shared"]
    result = solve_model(problem, POLICY)
    assert result["status"] == "INFEASIBLE"
    assert result["conflict"]["minimum_total_slack_m"] == pytest.approx(20.)
    assert result["conflict"]["rows_with_slack"]
    assert "contacts" not in result


def test_unqualified_md_datum_and_wrong_frame_retain_dispositions():
    rows = [factor("md", "DEPTH_MD", ["A"], [100., 100.]),
            factor("unknown_frame", "ELEVATION", ["A"], [90., 90.]),
            factor("out", "THICKNESS_VERTICAL", ["A", "B"], [100., 100.], point=(20., 20.))]
    result = solve_model(compile(rows, [roof(), thickness()]), POLICY)
    assert result["status"] == "MODEL_CLOSED_WITH_ASSUMPTIONS"
    assert result["data_count"] == 0
    assert {row["reason"] for row in result["dispositions"]} == {
        "MD_NEEDS_TRAJECTORY_OR_EXPLICIT_VERTICAL_SCENARIO", "HEIGHT_DATUM_OPERATION_UNQUALIFIED",
        "OUTSIDE_FIXED_CARRIER",
    }


def test_area_integral_operator_uses_declared_weights():
    datum = factor("area-mean", "CONTACT_ELEVATION", ["A"], [123., 123.], datum_mode="COMMON_WORLD")
    datum["support"] = {"kind": "AREA_MEAN", "frame_id": "metric", "configuration_id": "c",
                        "node_indices": [0, 1, 2], "weights": [1/3, 1/3, 1/3], "basis": "analytic integral"}
    result = solve_model(compile([datum], [thickness()]), POLICY)
    assert result["status"] == "MODEL_CLOSED_WITH_ASSUMPTIONS"
    np.testing.assert_allclose(result["contacts"][0], 123., atol=1e-8)
    datum["support"]["weights"] = [.2, .2, .2]
    with pytest.raises(ValueError, match="normalized"):
        compile([datum], [thickness()])


def test_opposite_depth_constraint_rejects_contact_crossing():
    rows = [factor("top", "DEPTH_TVD", ["A"], [120., 120.]),
            factor("base", "DEPTH_TVD", ["B"], [100., 100.])]
    result = solve_model(compile(rows, [roof()]), POLICY)
    assert result["status"] == "INFEASIBLE"
    assert result["conflict"]["minimum_total_slack_m"] == pytest.approx(20.)


def test_p1_physical_gradient_energy_invariant_under_linear_subdivision():
    prior = roof()
    prior["gradient"] = [1., 2.]
    base = compile([], [prior, thickness()])
    q = np.asarray(base.nodes) @ [1., 2.]
    nodes = NODES + [[1., 1.]]
    triangles = [[0, 1, 4], [0, 4, 2], [1, 3, 4], [4, 3, 2]]
    refined = compile([], [prior, thickness()], nodes=nodes, triangles=triangles)
    q1 = np.asarray(refined.nodes) @ [1., 2.]
    assert np.linalg.norm(base.gradient @ q)**2 == pytest.approx(5.)
    assert np.linalg.norm(refined.gradient @ q1)**2 == pytest.approx(5.)


@pytest.mark.parametrize("bad", [True, float("inf"), float("nan"), "1", 0+2j])
def test_non_real_numeric_interval_rejected(bad):
    with pytest.raises(ValueError, match="finite real"):
        compile([factor("bad", "THICKNESS_VERTICAL", ["A", "B"], [bad, 20.])])


def test_source_conditional_factors_and_prior_rank_are_distinct():
    inferred = factor("interpreted", "THICKNESS_VERTICAL", ["A", "B"], [18., 22.])
    inferred["epistemic_status"] = "MODEL_CHOICE"
    result = solve_model(compile([inferred], [roof(), thickness()]), POLICY)
    assert result["status"] == "MODEL_CLOSED_WITH_ASSUMPTIONS"
    assert result["data_count"] == 0
    assert result["data_rank_before_prior"]["rank"] == 0
    assert result["conditional_source_count"] == 1
    assert result["conditional_source_rank_before_prior"]["rank"] == 1
    assert result["scenario_conditioned_rank"]["declared_contact_closure"]


def test_boolean_in_carrier_cannot_promote_to_coordinate():
    nodes = [[False, 0.], *NODES[1:]]
    with pytest.raises(ValueError, match="booleans"):
        compile(nodes=nodes)


def test_aggregate_thickness_prior_spanning_nested_contact():
    problem = compile([], [roof(), thickness("A", "B", 3.), thickness("B", "C", 7.),
                           thickness("A", "C", 10.)], contact_ids=("A", "B", "C"))
    result = solve_model(problem, POLICY)
    assert result["status"] == "MODEL_CLOSED_WITH_ASSUMPTIONS"
    np.testing.assert_allclose(result["contacts"][:, 0], [50., 47., 40.], atol=1e-8)


def test_sparse_active_set_uses_interval_edge_instead_of_trial_midpoint():
    measured = factor("actual_gap", "THICKNESS_VERTICAL", ["A", "B"], [30., 40.], point=(0., 0.))
    problem = compile([measured], [roof(), thickness(target=20.)])
    result = solve_model(problem, POLICY)
    assert result["status"] == "MODEL_CLOSED_WITH_ASSUMPTIONS"
    assert "Sparse primal active-set" in result["optimization"][0]["message"]
    assert result["contacts"][0, 0] - result["contacts"][1, 0] == pytest.approx(30., abs=1e-10)
    assert result["worst_hard_residual_m"] < 1e-9
