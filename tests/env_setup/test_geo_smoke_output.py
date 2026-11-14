"""Exercise output ownership without importing or invoking native runtimes."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest


MODES = ("windows", "ta1", "roundtrip", "krige", "gempy", "paraview")
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/env_setup/geo_smoke.py"


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("geo_smoke_output_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def invoke(smoke, monkeypatch, mode, output):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), mode, "--output", str(output),
                                    "--gpkg-wkb", "synthetic-source.json",
                                    "--postgis", "synthetic-readback.json"])
    smoke.main()


def prevent_runtime(smoke, monkeypatch):
    def forbidden(*args):
        pytest.fail("Output guard must run before every runtime mode")
    for name in MODES:
        monkeypatch.setattr(smoke, name, forbidden)
    monkeypatch.setattr(smoke, "save", forbidden)


@pytest.mark.parametrize("mode", MODES)
def test_nonempty_attempt_is_rejected_before_mode_or_receipt(smoke, monkeypatch, tmp_path, mode):
    out = tmp_path / "previous-attempt"
    out.mkdir()
    receipt = out / (mode + "_execution.json")
    original = b'{"status":"FAIL","raw":"prior evidence"}\n'
    receipt.write_bytes(original)
    artifact = out / "nested" / "protected.bin"
    artifact.parent.mkdir()
    artifact.write_bytes(b"\x00original-artifact\xff")
    prevent_runtime(smoke, monkeypatch)
    with pytest.raises(SystemExit) as error:
        invoke(smoke, monkeypatch, mode, out)
    assert error.value.code == 2
    assert receipt.read_bytes() == original
    assert artifact.read_bytes() == b"\x00original-artifact\xff"
    assert {p.relative_to(out).as_posix() for p in out.rglob("*")} == {
        "nested", "nested/protected.bin", mode + "_execution.json"}


@pytest.mark.parametrize("mode", MODES)
def test_existing_file_is_rejected_before_mode_or_receipt(smoke, monkeypatch, tmp_path, mode):
    out = tmp_path / "protected-file"
    out.write_bytes(b"existing file must survive")
    prevent_runtime(smoke, monkeypatch)
    with pytest.raises(SystemExit) as error:
        invoke(smoke, monkeypatch, mode, out)
    assert error.value.code == 2
    assert out.read_bytes() == b"existing file must survive"
    assert list(tmp_path.iterdir()) == [out]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("precreated", [False, True], ids=["missing", "empty"])
def test_fresh_or_empty_output_runs_and_preserves_success_on_repeat(
        smoke, monkeypatch, tmp_path, mode, precreated):
    out = tmp_path / "parent" / "attempt"
    if precreated:
        out.mkdir(parents=True)
    calls = []
    def fake_mode(actual, *inputs):
        calls.append((actual, inputs))
        (actual / "result.bin").write_bytes(b"successful output")
    for name in MODES:
        monkeypatch.setattr(smoke, name, fake_mode)
    invoke(smoke, monkeypatch, mode, out)
    assert calls == [(out, (Path("synthetic-source.json"), Path("synthetic-readback.json"))
                      if mode == "roundtrip" else ())]
    receipt = out / (mode + "_execution.json")
    before = receipt.read_bytes()
    report = json.loads(before)
    assert report["status"] == "PASS" and report["exit_code"] == 0 and report["mode"] == mode
    assert report["scope"] == "SYNTHETIC_RUNTIME_TEST"
    with pytest.raises(SystemExit) as error:
        invoke(smoke, monkeypatch, mode, out)
    assert error.value.code == 2 and len(calls) == 1
    assert receipt.read_bytes() == before
    assert (out / "result.bin").read_bytes() == b"successful output"


@pytest.mark.parametrize("mode", MODES)
def test_failed_first_attempt_remains_protected_on_repeat(smoke, monkeypatch, tmp_path, mode):
    out = tmp_path / "failed-attempt"
    calls = []
    def fail(actual, *inputs):
        calls.append(actual)
        (actual / "partial.bin").write_bytes(b"partial diagnostic evidence")
        raise RuntimeError("synthetic first-run failure")
    for name in MODES:
        monkeypatch.setattr(smoke, name, fail)
    with pytest.raises(RuntimeError, match="synthetic first-run failure"):
        invoke(smoke, monkeypatch, mode, out)
    receipt = out / (mode + "_execution.json")
    before = receipt.read_bytes()
    report = json.loads(before)
    assert report["status"] == "FAIL" and report["exit_code"] == 1
    assert report["error"] == "RuntimeError: synthetic first-run failure"
    with pytest.raises(SystemExit) as error:
        invoke(smoke, monkeypatch, mode, out)
    assert error.value.code == 2 and calls == [out]
    assert receipt.read_bytes() == before
    assert (out / "partial.bin").read_bytes() == b"partial diagnostic evidence"
