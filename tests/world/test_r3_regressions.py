"""Independent scalar LOS, immutable history and reversible batch lifecycle controls."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import numpy as np
import pytest

from vkm_world.observations.operators import gnss, insar
from vkm_world.representations import mapdl_batch as batch
from vkm_world.representations.world0.multiseam_stages import StageState

FRAME = dict(frame_id="ENU", observation_frame_id="ENU", model_to_enu=np.eye(3))
U = np.array([[[1., 2., 3.], [4., 5., 6.]], [[1.01, 2.02, 3.03], [4.04, 5.05, 6.06]]])
LOS = np.array([[.6, 0., .8], [.8, 0., .6]])


def measure(weights=np.eye(2), **kwargs):
    return insar(U, weights, 1, 0, enu_toward_satellite=kwargs.pop("los", LOS),
                 positive_toward_satellite=kwargs.pop("toward", True), **FRAME, **kwargs)


def test_varying_los_reference_is_scalar_after_projection():
    absolute = np.einsum("ij,ij->i", U[1] - U[0], LOS)
    np.testing.assert_allclose(measure(reference_weights=[0., 1.]), absolute - absolute[1], atol=1e-15)
    np.testing.assert_allclose(measure(reference_weights=[0., 1.], toward=False), absolute[1] - absolute, atol=1e-15)


def test_nonidentical_node_and_station_support_require_explicit_geometry():
    weights = np.array([[.2, .8], [.7, .3], [.5, .5]])
    los = np.array([[.6, 0., .8], [.8, 0., .6], [0., 0., 1.]])
    with pytest.raises(ValueError, match="explicit reference LOS"):
        measure(weights, los=los, reference_weights=[0., 1.])
    expected = np.einsum("ij,ij->i", weights @ (U[1] - U[0]), los) - (U[1, 1] - U[0, 1]) @ LOS[1]
    np.testing.assert_allclose(measure(weights, los=los, reference_weights=[0., 1.],
                              reference_enu_toward_satellite=LOS[1]), expected, atol=1e-15)


def test_reference_station_average_is_not_node_vector_average():
    weights = np.array([[.2, .8], [.7, .3]])
    absolute = np.einsum("ij,ij->i", weights @ (U[1] - U[0]), LOS)
    expected = absolute - np.array([.3, .7]) @ absolute
    np.testing.assert_allclose(measure(weights, reference_station_weights=[.3, .7]), expected, atol=1e-15)
    with pytest.raises(ValueError, match="Choose either"):
        measure(reference_weights=[0., 1.], reference_station_weights=[0., 1.])


@pytest.mark.parametrize("bad", [[1.], [1., 1.], [np.nan, 0.]])
def test_invalid_station_reference_rejected(bad):
    with pytest.raises(ValueError, match="station weights"):
        measure(reference_station_weights=bad)


@pytest.mark.parametrize("bad", [[1., 1., 1.], [np.nan, 0., 1.], [1., 0.]])
def test_invalid_explicit_reference_los_rejected(bad):
    with pytest.raises(ValueError, match="Reference LOS"):
        measure(reference_weights=[0., 1.], reference_enu_toward_satellite=bad)


def test_constant_los_and_gnss_retain_vector_reference_contract():
    weights = np.array([[.2, .8], [.7, .3]])
    reference = np.array([.4, .6])
    expected_vectors = weights @ (U[1] - U[0]) - reference @ (U[1] - U[0])
    np.testing.assert_allclose(gnss(U, weights, 1, 0, reference_weights=reference, **FRAME), expected_vectors)
    np.testing.assert_allclose(measure(weights, los=LOS[0], reference_weights=reference), expected_vectors @ LOS[0], atol=1e-15)


def test_duplicate_support_with_conflicting_los_needs_explicit_reference():
    with pytest.raises(ValueError, match="explicit reference LOS"):
        measure(np.array([[0., 1.], [0., 1.]]), reference_weights=[0., 1.])


ARRAY_FIELDS = ("displacement_m", "creep", "support", "fill", "fill_ref")


@pytest.mark.parametrize("field", ARRAY_FIELDS)
def test_state_copies_inputs_and_cannot_be_made_writeable(field):
    original = np.arange(6., dtype=float).reshape(2, 3)
    state = StageState(0., original, original, original, original, original, 0)
    expected = original.copy()
    original[:] = -9
    array = getattr(state, field)
    np.testing.assert_array_equal(array, expected)
    with pytest.raises(ValueError, match="read-only"):
        array.flat[0] = -1
    with pytest.raises(ValueError):
        array.flags.writeable = True
    with pytest.raises(ValueError):
        array.base.flags.writeable = True
    with pytest.raises(ValueError):
        getattr(deepcopy(state), field).flags.writeable = True
    trial = array.copy()
    trial[:] = 1
    accepted = replace(state, **{field: trial})
    trial[:] = 7
    np.testing.assert_array_equal(getattr(accepted, field), np.ones_like(expected))
    np.testing.assert_array_equal(array, expected)


def setup_run(tmp_path, monkeypatch):
    monkeypatch.setenv("VKM_MAPDL_EXE", sys.executable)
    files = {"case.inp": b"/PREP7", "case.rst": b"scientific result", "history.txt": b"history",
             "receipt.json": b"{}", "case.rdb": b"restart database", "case.ldhi": b"history index", "case.r001": b"frame"}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    return files


@pytest.mark.parametrize("failure", ["missing_exe", "wrong_exe", "missing_deck", "nproc_zero", "nproc_float",
                                     "nproc_bool", "timeout", "restart", "deck_path", "jobname"])
def test_preflight_failure_never_changes_prior_bytes(tmp_path, monkeypatch, failure):
    files = setup_run(tmp_path, monkeypatch)
    kwargs = {}
    if failure == "missing_exe":
        monkeypatch.delenv("VKM_MAPDL_EXE")
    elif failure == "wrong_exe":
        monkeypatch.setenv("VKM_MAPDL_EXE", str(tmp_path / "absent.exe"))
    elif failure == "missing_deck":
        kwargs["deck"] = "missing.inp"
    elif failure.startswith("nproc"):
        kwargs["n_proc"] = {"nproc_zero": 0, "nproc_float": 1.5, "nproc_bool": True}[failure]
    elif failure == "timeout":
        kwargs["timeout_s"] = float("nan")
    elif failure == "restart":
        (tmp_path / "case.r001").unlink()
        files.pop("case.r001")
        kwargs["restart"] = True
    elif failure == "deck_path":
        kwargs["deck"] = "../case.inp"
    elif failure == "jobname":
        kwargs["jobname"] = "../case"
    with patch("subprocess.call") as launch, pytest.raises((RuntimeError, ValueError, FileNotFoundError)):
        batch.run_deck(tmp_path, **kwargs)
    launch.assert_not_called()
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == files


def test_directory_preflight_does_not_create_missing_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("VKM_MAPDL_EXE", sys.executable)
    with pytest.raises(FileNotFoundError):
        batch.run_deck(tmp_path / "absent")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("restart", [False, True])
def test_rotation_retains_all_previous_bytes_and_new_scratch(tmp_path, monkeypatch, restart):
    files = setup_run(tmp_path, monkeypatch)
    def solver(*args, **kwargs):
        assert not (tmp_path / "history.txt").exists()
        assert (tmp_path / "case.r001").exists() == restart
        (tmp_path / "case.rst").write_bytes(b"new scientific result")
        (tmp_path / "history.txt").write_bytes(b"new history")
        return 0
    with patch("subprocess.call", side_effect=solver):
        receipt = batch.run_deck(tmp_path, restart=restart)
    old = tmp_path / receipt["previous_output_archive"]["directory"]
    for name, data in files.items():
        if name != "case.inp":
            assert (old / name).read_bytes() == data
            assert batch.sha256(old / name) == receipt["previous_output_archive"]["files_sha256"][name]
    fresh = tmp_path / receipt["scratch_archive"]["directory"]
    assert (fresh / "case.rst").read_bytes() == b"new scientific result"
    assert (tmp_path / "history.txt").read_bytes() == b"new history"
    assert json.loads((old / "_archive_metadata/manifest.json").read_text())["files_sha256"]


def test_copy_failure_retains_every_original(tmp_path, monkeypatch):
    files = setup_run(tmp_path, monkeypatch)
    with patch.object(batch.shutil, "copy2", side_effect=OSError("disk full")), pytest.raises(OSError):
        batch.run_deck(tmp_path)
    assert {name: (tmp_path / name).read_bytes() for name in files} == files


def test_partial_retirement_failure_restores_previous_outputs(tmp_path, monkeypatch):
    files = setup_run(tmp_path, monkeypatch)
    unlink = Path.unlink
    def fail_once(path, *args, **kwargs):
        if path.name == "history.txt":
            raise PermissionError("locked")
        return unlink(path, *args, **kwargs)
    with patch.object(Path, "unlink", fail_once), pytest.raises(PermissionError):
        batch.run_deck(tmp_path)
    assert {name: (tmp_path / name).read_bytes() for name in files} == files


@pytest.mark.parametrize("error", [OSError("launch failed"), subprocess.TimeoutExpired("mapdl", 1)])
def test_launch_failure_or_timeout_keeps_recoverable_old_results(tmp_path, monkeypatch, error):
    files = setup_run(tmp_path, monkeypatch)
    with patch("subprocess.call", side_effect=error):
        if isinstance(error, subprocess.TimeoutExpired):
            assert batch.run_deck(tmp_path)["exit_code"] == -9
        else:
            with pytest.raises(OSError):
                batch.run_deck(tmp_path)
    archive = next((tmp_path / ".mapdl-archive").glob("previous-*"))
    assert all((archive / name).read_bytes() == data for name, data in files.items() if name != "case.inp")
