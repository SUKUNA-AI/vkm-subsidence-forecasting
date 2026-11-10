"""Independent controls for axial World-0 balance, events, and checkpointing."""
import copy
import json

import numpy as np
import pytest

try:
    from vkm_world.representations.world0.joint import ConvergenceError, JointSolver, main
except ModuleNotFoundError as exc:
    if exc.name != "vkm_world":
        raise
    from joint import ConvergenceError, JointSolver, main


def one_column() -> dict:
    return {
        "schema": "vkm.world0.joint/1",
        "world_id": "analytical",
        "representation_id": "axial",
        "nodes": [{
            "id": "A", "area_m2": 2, "height_m": 3, "young_pa": 1.0e9,
            "viscosity_pa_s": 4.0e13, "chamber_fraction": 0.4,
            "fill_stiffness_n_m": 2.0e8, "fill_gap_m": 0,
            "load_n": 2.0e6, "initial_memory_m": 0,
        }],
        "edges": [],
        "events": [
            {"time_s": 1000, "node": "A", "kind": "excavate"},
            {"time_s": 3000, "node": "A", "kind": "backfill"},
        ],
        "end_time_s": 4000,
        "max_dt_s": 1000,
    }


def two_columns() -> dict:
    data = one_column()
    data["nodes"].append({**data["nodes"][0], "id": "B"})
    data["edges"] = [{"a": "A", "b": "B", "stiffness_n_m": 3.0e8}]
    return data


def by_phase(snapshots, time, phase):
    return next(row for row in snapshots if row["time_s"] == time and row["phase"] == phase)


def test_constant_load_maxwell_and_area_change_closed_form():
    snapshots, _ = JointSolver(one_column()).run()
    initial = by_phase(snapshots, 0, "initial")
    pre = by_phase(snapshots, 1000, "pre_event")
    post = by_phase(snapshots, 1000, "post_event")
    # q = h*(F/A/E + integral F/A/eta dt), area reduces at fixed F.
    assert initial["convergence_m"] == pytest.approx([0.003])
    assert pre["convergence_m"] == pytest.approx([0.003075])
    assert post["convergence_m"] == pytest.approx([0.005075])
    assert pre["creep_memory_m"] == pytest.approx([0.000075])
    assert post["creep_memory_m"] == pre["creep_memory_m"]
    assert post["event_removed_energy_j"] == pytest.approx(1200)
    assert post["creep_dissipation_j"] == 0
    # An exact memory-reset counterfactual would shift the post-event convergence.
    assert post["convergence_m"][0] - 0.005 == pytest.approx(0.000075)


def test_fill_is_born_stress_free_and_later_carries_load():
    snapshots, _ = JointSolver(one_column()).run()
    pre = by_phase(snapshots, 3000, "pre_event")
    post = by_phase(snapshots, 3000, "post_event")
    later = by_phase(snapshots, 4000, "step")
    assert pre["fill_reference_m"] == [None]
    assert post["fill_reference_m"] == pytest.approx(pre["convergence_m"])
    assert post["fill_force_n"] == pytest.approx([0])
    assert later["fill_force_n"][0] > 0
    assert later["fill_reference_m"] == post["fill_reference_m"]
    assert later["pillar_force_n"][0] + later["fill_force_n"][0] == pytest.approx(2e6)


def test_edge_exchanges_equal_opposite_work_and_neighbor_moves_on_event():
    data = two_columns()
    solver = JointSolver(data)
    snapshots, _ = solver.run()
    pre = by_phase(snapshots, 1000, "pre_event")
    post = by_phase(snapshots, 1000, "post_event")
    edge = post["edge_force_n"][0]
    assert edge > 0
    assert post["coupling_force_n"] == pytest.approx([edge, -edge])
    dq = np.array([0.2, 0.04])
    assert np.dot(post["coupling_force_n"], dq) == pytest.approx(edge * (dq[0] - dq[1]))
    assert post["convergence_m"][1] > pre["convergence_m"][1]
    data["edges"] = []
    control, _ = JointSolver(data).run()
    pre_control = by_phase(control, 1000, "pre_event")
    post_control = by_phase(control, 1000, "post_event")
    assert post_control["convergence_m"][1] == pytest.approx(pre_control["convergence_m"][1])


def test_restart_is_identical_from_committed_event_checkpoint():
    solver = JointSolver(two_columns())
    full, _ = solver.run()
    prefix, state = solver.run(stop_after_event=1)
    checkpoint = json.loads(json.dumps(solver.snapshot(state), allow_nan=False))
    suffix, _ = solver.run(solver.restore(checkpoint))
    assert prefix + suffix[1:] == full
    checkpoint["next_event"] = 0
    with pytest.raises(ValueError, match="cursor"):
        solver.restore(checkpoint)


def test_cli_refuses_checkpoint_if_stiffness_input_changed(tmp_path):
    world = tmp_path / "world.json"
    prefix = tmp_path / "prefix.json"
    checkpoint = tmp_path / "checkpoint.json"
    data = two_columns()
    world.write_text(json.dumps(data), encoding="utf-8")
    assert main([str(world), str(prefix), "--stop-after-event", "1", "--checkpoint", str(checkpoint)]) == 0
    data["edges"][0]["stiffness_n_m"] *= 1.1
    world.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="world input bytes"):
        main([str(world), str(tmp_path / "wrong.json"), "--restart", str(checkpoint)])


def test_attempt_cannot_cross_event_or_mutate_previous_memory():
    solver = JointSolver(two_columns())
    accepted = solver.initial()
    saved = accepted.memory.copy()
    with pytest.raises(ValueError, match="cross"):
        solver.advance(accepted, 1001)
    with pytest.raises(ValueError, match="read-only"):
        accepted.memory[0] = 0.01
    assert np.array_equal(accepted.memory, saved)
    before = solver.advance(accepted, 1000, "pre_event")
    after, _ = solver.apply_events(before)
    assert np.array_equal(after.memory, before.memory)


def test_rejected_trial_is_retried_without_changing_prior(monkeypatch):
    solver = JointSolver(two_columns())
    initial = solver.initial()
    advance = solver.advance
    attempts = []

    def reject_once(state, target, phase):
        attempts.append((state, target))
        if len(attempts) == 1:
            raise ConvergenceError("forced rejection")
        return advance(state, target, phase)

    monkeypatch.setattr(solver, "advance", reject_once)
    snapshots, _ = solver.run(initial)
    assert attempts[0][0] is attempts[1][0] is initial
    assert attempts[0][1] == 1000 and attempts[1][1] == 500
    assert snapshots[1]["rejected_trials"] == 1
    assert snapshots[1]["time_s"] == 500


@pytest.mark.parametrize("change, expected", [
    (lambda data: data["nodes"][0].update(viscosity_pa_s=float("nan")), "finite"),
    (lambda data: data["events"][1].update(time_s=1000), "lifecycle"),
    (lambda data: data["edges"][0].update(stiffness_n_m=-1), "supported interval"),
    (lambda data: data["edges"].append(copy.copy(data["edges"][0])), "distinct undirected"),
])
def test_rejects_bad_scientific_inputs(change, expected):
    data = two_columns()
    change(data)
    with pytest.raises(ValueError, match=expected):
        JointSolver(data)
