"""The same hard polyhedron must retain its contact closure."""
import numpy as np
import pytest
from scipy.optimize import OptimizeResult

from vkm_world.geometry_build import geology_reconstruction as geo


POLICY = {"slope_length_m": 1., "contact_weight": 1., "thickness_weight": 0.,
          "time_limit_s": 10., "hard_atol_m": 1e-7}


def elevation(identity, contact, lower, upper):
    return {
        "factor_id": identity, "quantity": "ELEVATION", "admission": "INTERVAL",
        "epistemic_status": "FACT", "datum_mode": "COMMON_WORLD",
        "contact_ids": [contact], "bounds": {"lower": lower, "upper": upper, "unit": "m"},
        "support": {"kind": "POINT", "xy": [.25, .25], "frame_id": "L", "configuration_id": "c"},
        "independence_group": identity, "source_refs": [{"record_id": identity}],
    }


def solve(factors):
    problem = geo.compile_model([[0., 0.], [1., 0.], [0., 1.]], [[0, 1, 2]], ["TOP", "BASE"],
                                factors, frame_id="L", configuration_id="c")
    return geo.solve_model(problem, POLICY)


def test_split_intervals_preserve_closure():
    both = solve([elevation("T", "TOP", 6., 10.), elevation("B", "BASE", 0., 2.)])
    split = solve([elevation("Tlo", "TOP", 6., None), elevation("Thi", "TOP", None, 10.),
                   elevation("Blo", "BASE", 0., None), elevation("Bhi", "BASE", None, 2.)])
    assert both["status"] == split["status"] == "MODEL_CLOSED_WITH_ASSUMPTIONS"
    assert both["scenario_conditioned_rank"]["declared_contact_closure"] is True
    assert split["scenario_conditioned_rank"]["declared_contact_closure"] is True
    for result in (both, split):
        assert result["contact_uniqueness"]["status"] == "NONUNIQUE_WITH_FIXED_OBJECTIVE_OPERATORS"
        assert result["data_rank_before_prior"]["rank"] == 2
        assert np.all((6. - 1e-7 <= result["contacts"][0]) & (result["contacts"][0] <= 10. + 1e-7))
        assert np.all((-1e-7 <= result["contacts"][1]) & (result["contacts"][1] <= 2. + 1e-7))


def test_order_and_distinct_one_sided_rows_jointly_bound_contacts():
    result = solve([elevation("ceiling", "TOP", None, 10.), elevation("floor", "BASE", 0., None)])
    assert result["status"] == "MODEL_CLOSED_WITH_ASSUMPTIONS"
    assert result["scenario_conditioned_rank"]["declared_contact_closure"] is True
    assert result["scenario_conditioned_rank"]["boundedness"]["method"] == "NORMALIZED_CONTACT_RECESSION_LP"


def test_genuinely_unbounded_and_infeasible_are_distinct():
    unbounded = solve([elevation("ceiling", "TOP", None, 10.)])
    infeasible = solve([elevation("ceiling", "TOP", None, -1.), elevation("floor", "BASE", 0., None)])
    assert unbounded["status"] == "UNBOUNDED"
    assert unbounded["hard_feasibility"] == "VERIFIED_CONTINUOUS_WITNESS"
    proof = unbounded["scenario_conditioned_rank"]["boundedness"]
    assert proof["status"] == "DIRECTION_FOUND"
    roof, base = proof["direction"]
    assert max(abs(roof), abs(base)) >= 1.
    assert base <= roof <= 0.
    assert infeasible["status"] == "INFEASIBLE"
    assert infeasible["conflict"]["minimum_total_slack_m"] == pytest.approx(1.)


def test_conditional_spatial_operator_does_not_inflate_observed_rank():
    fact = elevation("scenario-attachment", "TOP", 5., 5.)
    fact["support"]["spatial_provenance"] = {"application_role": "CONDITIONAL_SCENARIO_OPERATOR"}
    problem = geo.compile_model([[0., 0.], [1., 0.], [0., 1.]], [[0, 1, 2]], ["TOP", "BASE"],
                                [fact], frame_id="L", configuration_id="c")
    row = next(r for r in problem.rows if r.id == fact["factor_id"])
    assert row.role == "CONDITIONAL_DATA_HARD"
    assert row.scale is None and row.lower == row.upper == 5.
    assert fact["epistemic_status"] == "FACT"
    result = geo.solve_model(problem, POLICY)
    assert result["data_rank_before_prior"]["rank"] == 0
    assert result["conditional_source_rank_before_prior"]["rank"] == 1


def test_numerical_recession_interruption_is_not_unbounded(monkeypatch):
    original = geo.linprog

    def interrupted_direction(objective, *, bounds, **kwargs):
        if isinstance(bounds, list):
            return OptimizeResult(status=1, success=False)
        return original(objective, bounds=bounds, **kwargs)

    monkeypatch.setattr(geo, "linprog", interrupted_direction)
    result = solve([elevation("ceiling", "TOP", None, 10.), elevation("floor", "BASE", 0., None)])
    assert result["hard_feasibility"] == "VERIFIED_CONTINUOUS_WITNESS"
    assert result["status"] == "SEARCH_INCOMPLETE"
    closure = result["scenario_conditioned_rank"]
    assert closure["declared_contact_closure"] is None
    assert closure["boundedness"]["reason"] == "DIRECTION_NOT_VERIFIED"


def test_thickness_smoothing_retains_common_surface_modes():
    factors = [elevation("T", "TOP", 5., 5.), elevation("B", "BASE", 0., 2.)]
    factors[0]["support"]["xy"] = [0., 0.]
    for index, xy in enumerate([[1., 0.], [0., 1.]], 1):
        ceiling = elevation(f"T{index}", "TOP", 5., 5.)
        ceiling["support"]["xy"] = xy
        factors.append(ceiling)
    problem = geo.compile_model([[0., 0.], [1., 0.], [0., 1.]], [[0, 1, 2]], ["TOP", "BASE"],
                                factors, frame_id="L", configuration_id="c")
    result = geo.solve_model(problem, POLICY | {"contact_weight": 0., "thickness_weight": 1.})
    assert result["status"] == "MODEL_CLOSED_WITH_ASSUMPTIONS"
    assert result["scenario_conditioned_rank"]["basis"] == "THICKNESS_CONSTANT_DIFFERENCE_MODES"
    assert result["scenario_conditioned_rank"]["gradient_null_contact_dimensions"] == 4
    np.testing.assert_allclose(result["contacts"][0], 5., atol=1e-8)
    np.testing.assert_allclose(result["contacts"][1], result["contacts"][1, 0], atol=1e-8)


def test_censored_termination_in_declared_vertical_scenario_remains_hard():
    factor = elevation("termination", "TOP", 9., None)
    factor.update(quantity="BOTTOM_INSIDE_UNIT", physical_id="well", contact_ids=["TOP", "BASE"],
                  epistemic_status="DERIVATION", depth_basis="DECLARED_VERTICAL_SCENARIO")
    problem = geo.compile_model([[0., 0.], [1., 0.], [0., 1.]], [[0, 1, 2]], ["TOP", "BASE"],
                                [factor], frame_id="L", configuration_id="c")
    rows = [row for row in problem.rows if row.id.startswith("termination:")]
    assert {row.role for row in rows} == {"CONDITIONAL_DATA_HARD"}
    assert all(row.scale is None for row in rows)
    assert [(row.lower, row.upper) for row in rows] == [(None, 9.), (9., None)]
    result = geo.solve_model(problem, POLICY)
    assert result["hard_count"] == 5 and result["soft_count"] == 0
    assert result["data_rank_before_prior"]["rank"] == 0
    assert result["conditional_source_rank_before_prior"]["rank"] == 2
