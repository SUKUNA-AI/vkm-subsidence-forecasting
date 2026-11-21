import copy

import numpy as np
import pytest

from vkm_world.geometry_build.joint_borehole import (
    FrameFit,
    block_huber_residual,
    compile_problem,
    native_to_plane,
    registration_design,
    solve_maps,
)


def sample():
    sites = [[0, 0], [12, 4], [5, -8], [-9, 6], [-3, -4], [5, 10]]
    truth = [3., -7., 1.2, .25]
    targets = [f"well-{i}" for i in range(len(sites))]
    maps = []
    for map_id, points in (("reference", sites), ("scan", [list(native_to_plane(p, truth)) for p in sites])):
        maps.append({
            "id": map_id, "frame": map_id + "-native", "output_frame": "local",
            "initial": [0, 0, 1, 0],
            "marks": [
                {"object_id": name, "xy": p, "scale": 1, "primary": True,
                 "group": map_id, "record_id": f"{map_id}:{name}"}
                for name, p in zip(targets, points, strict=True)
            ],
        })
    maps[1]["limits"] = [[-15, 15], [-15, 15], [.5, 2], [-.5, .5]]
    return {"target_ids": targets, "support_ids": [], "maps": maps}


def test_registration_recovers_shared_transform_at_float64_precision():
    problem = compile_problem(sample())
    baseline = solve_maps(problem, frozen=True)["local"]
    fitted = solve_maps(problem)["local"]
    assert baseline["solver"]["objective"] > 1
    assert fitted["solver"]["objective"] < 1e-16
    assert max(r["norm_scaled"] for r in fitted["residuals"]) < 1e-8
    assert fitted["diagnostics"]["rank"] == 16


def test_map_analytic_jacobian_agrees_with_independent_central_difference():
    problem = compile_problem(sample())
    fit = FrameFit(problem.maps, [])
    analytical = fit.jacobian(fit.initial).toarray()
    columns = []
    for direction in np.eye(len(fit.initial)):
        step = direction * 1e-5
        columns.append((fit.residuals(fit.initial + step) - fit.residuals(fit.initial - step)) / 2e-5)
    np.testing.assert_allclose(analytical, np.asarray(columns).T, rtol=1e-5, atol=1e-8)


def test_block_loss_is_rotation_invariant_and_reports_outlier():
    delta = 2.5
    residual = np.array([3., 4.])
    rotated = np.array([0., 5.])
    assert np.dot(block_huber_residual(residual, delta), block_huber_residual(residual, delta)) == \
        pytest.approx(np.dot(block_huber_residual(rotated, delta), block_huber_residual(rotated, delta)))
    payload = sample()
    payload["maps"][1]["marks"][0]["xy"][0] += 45.
    result = solve_maps(compile_problem(payload))["local"]
    high = max(result["residuals"], key=lambda item: item["norm_scaled"])
    assert high["object_id"] == "well-0"
    assert high["weight"] < 0.3


def test_one_tie_leaves_two_registration_directions_unidentified():
    payload = sample()
    for map_ in payload["maps"]:
        map_["marks"] = map_["marks"][:1]
    result = solve_maps(compile_problem(payload))["local"]
    assert result["diagnostics"]["nullity"] == 2


def test_unregistered_maps_are_not_merged_by_identity():
    payload = sample()
    payload["maps"][1]["output_frame"] = "unregistered"
    result = solve_maps(compile_problem(payload))
    assert set(result) == {"local", "unregistered"}
    assert result["unregistered"]["diagnostics"]["nullity"] == 4
    assert not result["unregistered"]["identified"]["well-0"]


def test_order_permutation_and_rename_do_not_change_geometry():
    payload = sample()
    expected = compile_problem(payload)
    payload["target_ids"].reverse()
    payload["maps"].reverse()
    for map_ in payload["maps"]:
        map_["marks"].reverse()
    assert compile_problem(payload) == expected
    fit = solve_maps(expected)["local"]
    for map_ in payload["maps"]:
        for mark in map_["marks"]:
            mark["object_id"] = "alt-" + mark["object_id"]
    payload["target_ids"] = ["alt-" + key for key in payload["target_ids"]]
    changed = solve_maps(compile_problem(payload))["local"]
    for key, point in fit["positions"].items():
        assert changed["positions"]["alt-" + key] == pytest.approx(point, abs=1e-8)


def test_dependent_copy_does_not_add_a_measurement():
    payload = sample()
    base = solve_maps(compile_problem(payload))["local"]
    copy_mark = copy.deepcopy(payload["maps"][0]["marks"][0])
    copy_mark["record_id"] += "-copy"
    copy_mark["primary"] = False
    payload["maps"][0]["marks"].append(copy_mark)
    assert solve_maps(compile_problem(payload))["local"] == base
    copy_mark["primary"] = True
    with pytest.raises(ValueError, match="Dependent"):
        compile_problem(payload)


@pytest.mark.parametrize("bad", [True, False, "1", float("nan"), float("inf"), 1j, np.float64(2)])
def test_invalid_native_coordinates_rejected_before_fit(bad):
    payload = sample()
    payload["maps"][0]["marks"][0]["xy"][0] = bad
    with pytest.raises(ValueError, match="finite JSON number"):
        compile_problem(payload)


def test_collapsing_transform_domain_rejected():
    payload = sample()
    payload["maps"][1]["limits"][2] = [-2, 2]
    with pytest.raises(ValueError, match="zero scale"):
        compile_problem(payload)


def test_pose_design_includes_axis_limits_and_preserves_shared_pose():
    problem = compile_problem(sample())
    fit = solve_maps(problem)
    design = list(registration_design(problem, fit, 14, 2026))
    assert len(design) == 14
    assert all(set(poses) == {"scan"} for _, poses in design[1:])
    assert set(design[i][1]["scan"][0] for i in (1, 2)) == {-15, 15}


def test_profile_axis_uses_declared_anchors_not_the_first_scenario_well():
    from vkm_world.geometry_build.joint_borehole_job import _profile_states

    ids = ["scenario", "anchor-a", "anchor-b"]
    profile = {"id": "profile", "frame": "SH1", "ids": ids, "anchor_ids": ids[1:]}
    points = {"scenario": [-3., 2.], "anchor-a": [0., 0.], "anchor-b": [3., 0.]}
    result = _profile_states([profile], {"SH1": points})[0]
    assert result["registration"]["anchor_ids"] == ids[1:]
    assert result["registration"]["origin"] == [0., 0.]
    assert [row["station"] for row in result["stations"]] == [-3., 0., 3.]
    assert [row["cross_track"] for row in result["stations"]] == [2., 0., 0.]
