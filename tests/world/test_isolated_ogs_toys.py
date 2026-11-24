"""No solver execution: real native Git Bash runs only frozen-like write stubs."""
from pathlib import Path
import importlib.util
import json
import os
import shutil
import subprocess

import pytest

spec = importlib.util.spec_from_file_location("toy_runner", Path(__file__).parents[2] / "scripts/run_ogs_toy_regressions.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture
def source(tmp_path):
    package = tmp_path / "source" / runner.RELATIVE_PACKAGE
    (package / "ogs/tools").mkdir(parents=True)
    (package / "ogs/tools/helper.py").write_text("# generic input\n")
    for name in runner.TOYS:
        case = package / "ogs/cases" / name
        case.mkdir(parents=True)
        (case / "run.sh").write_bytes(b'''#!/usr/bin/env bash
set -euo pipefail
CASE_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$CASE_DIR/../../../../.." && pwd)"
. "$ROOT/work/ogs/.venv-ogs/bin/activate"
bash "$CASE_DIR/stub_solver.sh" "$CASE_DIR"
echo stub-stdout
''')
        (case / "stub_solver.sh").write_bytes(b'''#!/usr/bin/env bash
set -euo pipefail
printf 'new-receipt' > "$1/receipt.json"
printf 'new-summary' > "$1/summary.json"
printf 'new-log' > "$1/ogs.log"
''')
        for filename in ("receipt.json", "summary.json", "ogs.log"):
            (case / filename).write_text("FROZEN ORIGINAL")
    return package


def git_bash():
    """Discover the test prerequisite; the production CLI still needs --bash.

    Never probe Windows' WSL launcher. An explicit override must itself pass
    validation; it must not silently fall back to a different installation.
    """
    def note(executable, outcome, **details):
        # Only executable/probe facts, never an environment dump. Bound output
        # and omit any unexpected credential-bearing lines from a wrapper.
        for name in ("stdout", "stderr"):
            if name in details:
                details[name] = "\n".join(line for line in details[name].splitlines()
                    if not any(word in line.casefold() for word in
                               ("token", "password", "authorization", "secret")))[:512]
        print(json.dumps({"bash_probe": outcome, "candidate": str(executable) if executable else None,
                          **details}, ensure_ascii=True))

    override = os.getenv("VKM_TEST_GIT_BASH")
    candidates = [Path(override)] if override else []
    if not override:
        if os.name == "nt":
            for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
                if root := os.getenv(variable):
                    candidates.append(Path(root) / "Git/bin/bash.exe")
            if root := os.getenv("LOCALAPPDATA"):
                candidates.append(Path(root) / "Programs/Git/bin/bash.exe")
            if executable := shutil.which("git"):
                root = Path(executable).parent.parent
                candidates.extend((root / "bin/bash.exe", root / "usr/bin/bash.exe"))
        if executable := shutil.which("bash"):
            candidates.append(Path(executable))
    note(None, "inventory", count=len(dict.fromkeys(candidates)), explicit_override=bool(override))
    for executable in dict.fromkeys(candidates):
        if not executable.is_file():
            note(executable, "not_a_file")
            continue
        executable = executable.resolve()
        if os.name == "nt" and any(part.casefold() in {"system32", "syswow64", "windowsapps"}
                                    for part in executable.parts):
            note(executable, "blocked_wsl_or_store_path")
            continue
        try:
            result = subprocess.run([str(executable), "--version"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            note(executable, "probe_error", error_type=type(exc).__name__,
                 winerror=getattr(exc, "winerror", None))
            continue
        note(executable, "version_result", returncode=result.returncode,
             stdout=result.stdout, stderr=result.stderr)
        if (result.returncode == 0 and "GNU bash" in result.stdout
                and (os.name != "nt" or "-msys" in result.stdout)):
            note(executable, "accepted")
            return executable
        note(executable, "rejected_version")
    pytest.fail("Native Bash prerequisite unavailable or invalid: install Git Bash on Windows or Bash on POSIX; "
                "VKM_TEST_GIT_BASH may name a validated executable. WSL launchers are not supported.")


def test_native_shell_writes_only_to_new_runtime_workspace(source, tmp_path):
    before = runner.fingerprint(source)
    activation = tmp_path / "activate"
    activation.write_bytes(b":\n")
    output = tmp_path / "attempt"
    assert runner.run(source, output, git_bash(), activation) == 0
    assert runner.fingerprint(source) == before
    for name in runner.TOYS:
        staged = output / runner.RELATIVE_PACKAGE / "ogs/cases" / name
        assert (staged / "receipt.json").read_text() == "new-receipt"
        assert (staged / "summary.json").read_text() == "new-summary"
        assert (staged / "ogs.log").read_text() == "new-log"
        assert (output / (name + ".launch.log")).read_text().strip() == "stub-stdout"
    receipt = json.loads((output / "TOY_EXECUTION.json").read_bytes())
    assert receipt["source_unchanged"] and len(receipt["results"]) == len(runner.TOYS)
    assert not any(path.endswith(("receipt.json", "summary.json", "ogs.log")) for path in receipt["input_sha256"])
    assert receipt["scientific_admission"] == "NOT_ESTABLISHED"


@pytest.mark.parametrize("where", ["equal", "inside", "existing", "ancestor"])
def test_output_guards_refuse_before_writing(source, tmp_path, where):
    before = runner.fingerprint(source)
    output = {"equal": source, "inside": source / "attempt", "existing": tmp_path,
              "ancestor": source.parent}[where]
    with pytest.raises(ValueError, match="new workspace"):
        runner.run(source, output, tmp_path / "unused-bash", tmp_path / "unused-activate")
    assert runner.fingerprint(source) == before


def test_copy_path_escape_refuses_before_creating_attempt(source, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = source / "ogs/tools/escaped"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
        assert result.returncode == 0, "directory junction creation failed"
    else:
        junction.symlink_to(outside, target_is_directory=True)
    executable = tmp_path / "stub-executable"
    executable.write_text("unused")
    output = tmp_path / "attempt"
    with pytest.raises(ValueError, match="escaping source|escapes the source"):
        runner.run(source, output, executable, executable)
    assert not output.exists()


def test_shell_failure_is_recorded_without_success_or_source_writes(source, tmp_path):
    stub = source / "ogs/cases" / runner.TOYS[0] / "stub_solver.sh"
    stub.write_bytes(stub.read_bytes() + b"exit 23\n")
    before = runner.fingerprint(source)
    activation = tmp_path / "activate"
    activation.write_bytes(b":\n")
    output = tmp_path / "failed-attempt"
    assert runner.run(source, output, git_bash(), activation) == 1
    assert runner.fingerprint(source) == before
    receipt = json.loads((output / "TOY_EXECUTION.json").read_bytes())
    assert receipt["results"] == [{"case": runner.TOYS[0], "exit_code": 23}]
    assert receipt["scientific_admission"] == "NOT_ESTABLISHED"
