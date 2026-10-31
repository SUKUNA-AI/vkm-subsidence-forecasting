"""Independent analytical values and checkpoint lifecycle regressions."""
from pathlib import Path
from unittest.mock import patch

import pytest

from vkm_world.representations.mapdl_batch import clear_outputs, run_deck
from vkm_world.representations.staged_mechanics import CreepStage, TwoStageCreep, activation_deck, activation_reference


def test_two_stage_reference_retains_memory_and_energy():
    case = TwoStageCreep()
    ref = case.reference()
    assert ref["uy_m"] == pytest.approx(-0.0013)
    assert ref["creep_strain"] == pytest.approx(0.0003)
    assert ref["reaction_n"] == 1e6
    assert ref["elastic_energy_j"] == 500
    assert ref["creep_dissipation_j"] == 500
    assert ref["external_work_j"] == 1000
    lost = case.reference(reset_memory=True)
    assert lost["uy_m"] == pytest.approx(-0.0011)
    assert abs(lost["uy_m"] - ref["uy_m"]) / abs(ref["uy_m"]) > 0.15
    # Same current stress/time, different earlier history gives a different state.
    other = TwoStageCreep(first=CreepStage(1, 1e6)).reference()
    assert other["uy_m"] == pytest.approx(-0.0012)


@pytest.mark.parametrize("duration,stress", [(0, 1), (-1, 1), (1, float("nan")), (float("inf"), 1), (1, 0)])
def test_invalid_stage_cannot_generate_solver_input(duration, stress):
    with pytest.raises(ValueError):
        CreepStage(duration, stress)


def test_restart_is_native_not_fresh_construction():
    deck = TwoStageCreep().deck("restart")
    assert "RESUME,case,rdb" in deck
    assert "ANTYPE,STATIC,RESTART,1,,CONTINUE" in deck
    assert "TB,CREEP" not in deck and "INISTATE" not in deck and "/PREP7" not in deck
    assert "RESCONTROL,DEFINE,ALL,LAST" in TwoStageCreep().deck("prefix")


def test_birth_death_case_carries_gravity_and_separate_steps():
    deck = activation_deck()
    assert "ACEL,0,10,0" in deck
    assert deck.index("EKILL,2") < deck.index("EALIVE,2")
    assert "TB,CREEP" not in deck
    birth = deck.index("EALIVE,2")
    assert birth < deck.index("TIME,3", birth) < deck.index("D,ALL,UY,0", birth)
    ref = activation_reference()
    assert ref["elastic_energy_j"] == [1000, 500, 500, 500]
    assert ref["second_stress_y_pa"][2] == 0  # stress-free birth at held displacement
    assert ref["first_stress_y_pa"][2] == -1e6  # original material memory retained
    assert ref["second_stress_y_pa"][3] == 1e6  # later unload is a separate event


def _checkpoint(tmp_path):
    for name in ("case.rdb", "case.ldhi", "case.r001", "case.rst", "case.esav"):
        (tmp_path / name).write_bytes(b"checkpoint")
    for name in ("history.txt", "case.out", "run.log"):
        (tmp_path / name).write_text("stale")
    (tmp_path / "input.inp").write_text(TwoStageCreep().deck("restart"))


def test_restart_cleanup_preserves_binary_state_but_removes_stale_tables(tmp_path):
    _checkpoint(tmp_path)
    removed = clear_outputs(tmp_path, "input.inp", restart=True)
    assert "history.txt" in removed and "case.out" in removed
    assert (tmp_path / "case.r001").read_bytes() == b"checkpoint"
    assert (tmp_path / "case.rst").is_file()
    assert (tmp_path / "input.inp").is_file()


def test_fresh_cleanup_still_clears_checkpoint(tmp_path):
    _checkpoint(tmp_path)
    assert "case.r001" in clear_outputs(tmp_path, "input.inp")


def test_missing_checkpoint_fails_before_solver_or_cleanup(tmp_path):
    (tmp_path / "history.txt").write_text("keep until validation")
    with patch("subprocess.call") as launch:
        with pytest.raises(FileNotFoundError, match="multiframe restart"):
            run_deck(tmp_path, restart=True)
        launch.assert_not_called()
    assert (tmp_path / "history.txt").exists()


def test_restart_receipt_binds_checkpoint_hashes(tmp_path, monkeypatch):
    _checkpoint(tmp_path)
    monkeypatch.setenv("VKM_MAPDL_EXE", "synthetic-test-executable")

    def fake_solver(*args, **kwargs):
        assert (tmp_path / "case.r001").is_file()
        (tmp_path / "case.out").write_text("NUMBER OF ERROR   MESSAGES ENCOUNTERED = 0")
        return 0

    with patch("subprocess.call", side_effect=fake_solver):
        receipt = run_deck(tmp_path, "input.inp", n_proc=1, restart=True, keep_scratch=True)
    assert receipt["restart"] is True
    assert set(receipt["checkpoint_input_sha256"]) == {"case.rdb", "case.ldhi", "case.r001"}
    assert receipt["n_errors"] == 0
