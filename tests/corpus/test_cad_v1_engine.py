"""The headless runner (accoreconsole) on fake processes: success, script not read, failed steps, crash (abnormal
exit, crash-reporter child), dialog windows and timeouts kill the job's process tree — and only the job's: a process
whose parent PID merely equals the console's reused PID is left alone; the hidden instance is gated, refuses a running
user session and reports the child runner's result."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vkm_cad import hidden
from vkm_cad.engine import ConsoleRun, CoreConsole, decode_console
from vkm_cad.errors import ToolFailure
from vkm_cad.winproc import Proc, tree

PID = 4242
T0 = 1_000_000                                   # creation time of the console (FILETIME units)


class FakeProcs:
    def __init__(self, extra: list[Proc] | None = None, windows: list[tuple[int, str]] | None = None,
                 others: list[Proc] | None = None):
        self.extra = extra or []                  # children of the job (appear after the start)
        self.windows = windows or []
        self.others = others or []                # unrelated processes (a user's AutoCAD, a user's Discord)
        self.killed: list[int] = []
        self.alive = True

    def snapshot(self):
        procs = [p for p in self.others if p.pid not in self.killed]
        if self.alive:
            procs.append(Proc(PID, 1, "accoreconsole.exe", T0))
        procs += [p for p in self.extra if p.pid not in self.killed]
        return procs

    def tree(self, root, procs, *, not_before):
        return tree(root, procs, not_before=not_before)

    def creation_time(self, pid):
        return T0 if pid == PID else None

    def filetime_now(self):
        return T0 - 10

    def visible_windows(self, pids):
        return [w for w in self.windows if w[0] in pids]

    def kill(self, pids, created=None):
        pids = list(pids)
        self.killed += pids
        if PID in pids:
            self.alive = False
        return pids


class FakePopen:
    """Writes markers like the LISP frame would, then exits with ``code`` (or never, for 'hang')."""

    def __init__(self, behaviour: str, procs: FakeProcs, code: int = 0):
        self.behaviour = behaviour
        self.procs = procs
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        run_dir = Path(kw["cwd"])
        run_id = run_dir.name
        markers = []
        if self.behaviour in ("ok", "failed", "crash", "reporter"):
            markers.append(f"BEGIN {run_id}")
        if self.behaviour == "ok":
            (run_dir / "out.dwg").write_bytes(b"AC1032")
            markers += ["SAVED", f"END {run_id} OK"]
        if self.behaviour == "failed":
            (run_dir / "FAILED").write_text("HOST c3d.tin: surface exists\n", encoding="utf-8")
            markers += ["FAIL HOST c3d.tin: surface exists", f"END {run_id} FAILED"]
        if markers:
            (run_dir / "markers.txt").write_text("\n".join(markers) + "\n", encoding="utf-8")
        kw["stdout"].write("Команда: VKMHOST\r\n".encode("utf-16-le"))
        return _Proc(self)


class _Proc:
    def __init__(self, parent: FakePopen):
        self.parent = parent
        self.pid = PID

    def poll(self):
        if self.parent.behaviour == "hang" or self.parent.behaviour in ("dialog",) and self.parent.procs.alive:
            return None if self.parent.procs.alive else 1
        return self.parent.code

    def wait(self, timeout=None):
        if self.parent.behaviour in ("hang", "dialog") and self.parent.procs.alive:
            import subprocess

            raise subprocess.TimeoutExpired("x", timeout)
        return self.parent.code if self.parent.behaviour not in ("hang", "dialog") else 1


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _run(tmp_path, behaviour, *, code=0, procs=None, timeout=300.0):
    procs = procs or FakeProcs()
    popen = FakePopen(behaviour, procs, code)
    clock = Clock()
    exe = tmp_path / "Fake AutoCAD" / "accoreconsole.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    console = CoreConsole(exe, tmp_path / "isolated", locale="ru-RU", popen=popen, procs=procs, clock=clock,
                          sleep=clock.sleep, env={})
    run_dir = tmp_path / "runs" / "R001"
    run_dir.mkdir(parents=True)
    spec = ConsoleRun(product="C3D", body="VKMHOST\n", run_id="R001", run_dir=run_dir,
                      input_drawing=run_dir / "in.dwg", save_to=run_dir / "out.dwg", timeout_s=timeout)
    return console.run(spec), popen, procs, run_dir


def test_success_command_line_and_redaction(tmp_path):
    result, popen, _procs, run_dir = _run(tmp_path, "ok")
    assert result.ok and result.completed and result.script_read and not result.crashed
    cmd = popen.calls[0]
    assert cmd[cmd.index("/product") + 1] == "C3D" and cmd[cmd.index("/l") + 1] == "ru-RU"
    assert cmd[cmd.index("/isolate") + 1] == "vkm-bridge" and cmd[cmd.index("/i") + 1].endswith("in.dwg")
    assert result.command[0] == "accoreconsole.exe" and "run/job.scr" in result.command
    assert "isolate/c3d" in result.command and str(tmp_path) not in json.dumps(result.as_record())
    assert (run_dir / "console.txt").read_text(encoding="utf-8").startswith("Команда")
    script = (run_dir / "job.scr").read_bytes()
    assert script.startswith(b"\xef\xbb\xbf") and b"VKMHOST" in script
    result.raise_for_status()


def test_script_not_read_and_failed_steps(tmp_path):
    result, *_ = _run(tmp_path / "a", "no_script")
    with pytest.raises(ToolFailure) as exc:
        result.raise_for_status()
    assert exc.value.code == "CAD_SCRIPT_NOT_READ"
    result, *_ = _run(tmp_path / "b", "failed")
    assert result.completed and not result.ok and "HOST c3d.tin: surface exists" in result.failures
    with pytest.raises(ToolFailure) as exc:
        result.raise_for_status()
    assert exc.value.code == "CAD_SCRIPT_FAILED" and "surface exists" in exc.value.message


def test_abnormal_exit_is_a_crash_and_orphans_are_killed(tmp_path):
    procs = FakeProcs(extra=[Proc(777, PID, "senddmp.exe", T0 + 100)])
    result, _popen, procs, _rd = _run(tmp_path, "crash", code=-1073741819, procs=procs)
    assert result.crashed and not result.ok and 777 in procs.killed and "senddmp.exe" in result.killed
    with pytest.raises(ToolFailure) as exc:
        result.raise_for_status()
    assert exc.value.code == "CAD_ENGINE_CRASHED" and not exc.value.retryable


def test_crash_reporter_child_during_the_run_kills_the_tree(tmp_path):
    procs = FakeProcs(extra=[Proc(778, PID, "senddmp.exe", T0 + 100)])
    result, _popen, procs, _rd = _run(tmp_path, "hang", procs=procs)
    assert result.crashed and PID in procs.killed and 778 in procs.killed


def test_dialog_window_and_timeout_kill_the_tree(tmp_path):
    procs = FakeProcs(windows=[(PID, "Autodesk CER - System Error")])
    result, _popen, procs, _rd = _run(tmp_path / "d", "dialog", procs=procs)
    assert result.dialog and PID in procs.killed and "Autodesk CER" in result.windows[0]
    with pytest.raises(ToolFailure) as exc:
        result.raise_for_status()
    assert exc.value.code == "CAD_DIALOG_BLOCKED"
    procs = FakeProcs(others=[Proc(9, 1, "acad.exe")])                  # a user AutoCAD: seen, never killed
    result, _popen, procs, _rd = _run(tmp_path / "t", "hang", procs=procs, timeout=5.0)
    assert result.timed_out and PID in procs.killed and 9 not in procs.killed
    assert result.foreign_cad_processes == {"acad.exe": 1}
    with pytest.raises(ToolFailure) as exc:
        result.raise_for_status()
    assert exc.value.code == "CAD_RUN_TIMEOUT" and exc.value.retryable


def test_a_reused_parent_pid_does_not_make_foreign_processes_the_jobs(tmp_path):
    """29.09.2026: a user's Discord — children of a long-gone process whose PID the new console received — was taken
    for the job's tree, its window for a dialog of the job, and killed. Older processes never belong to the job."""
    discord = [Proc(900, PID, "discord.exe", T0 - 5_000_000), Proc(901, 900, "discord.exe", T0 - 4_000_000)]
    procs = FakeProcs(others=discord, windows=[(900, "Discord")], extra=[Proc(777, PID, "werfault.exe", T0 + 100)])
    result, _popen, procs, _rd = _run(tmp_path / "c", "crash", code=1, procs=procs)
    assert result.crashed and not result.dialog and result.windows == []
    assert 777 in procs.killed and 900 not in procs.killed and 901 not in procs.killed
    procs = FakeProcs(others=discord, windows=[(900, "Discord")])
    result, _popen, procs, _rd = _run(tmp_path / "h", "hang", procs=procs, timeout=5.0)
    assert result.timed_out and not result.dialog and PID in procs.killed
    assert 900 not in procs.killed and 901 not in procs.killed


def test_tree_needs_creation_times_not_earlier_than_root_and_parent():
    procs = [Proc(10, 1, "accoreconsole.exe", 100), Proc(11, 10, "adpclientservice.exe", 150),
             Proc(12, 11, "conhost.exe", 160), Proc(20, 10, "discord.exe", 50), Proc(21, 20, "discord.exe", 60),
             Proc(40, 10, "unknown.exe", None)]
    assert tree(10, procs, not_before=100) == {10, 11, 12}            # 20/21 are older than the root: foreign
    assert tree(10, procs, not_before=None) == {10}                   # the root's time unknown: nothing else
    reused = [Proc(10, 1, "accoreconsole.exe", 100), Proc(11, 10, "new.exe", 200), Proc(13, 11, "old.exe", 155)]
    assert tree(10, reused, not_before=100) == {10, 11}               # 13's parent PID now names a newer process
    dead_root = [Proc(11, 10, "werfault.exe", 150), Proc(20, 10, "discord.exe", 50)]
    assert tree(10, dead_root, not_before=100) == {10, 11}            # after the console exited: orphans only


def test_console_decoding():
    assert decode_console("Команда:".encode("utf-16-le")) == "Команда:"
    assert decode_console("abc".encode("utf-8")) == "abc"
    assert decode_console("Команда".encode("cp1251")) == "Команда"


# ------------------------------------------------------------------------------------------------ hidden instance
def _hidden(tmp_path, env, procs, popen=None):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    return hidden.run(acad_exe=tmp_path / "acad.exe", product="C3D", language="ru-RU", clsid="{X}",
                      civil_progid="AeccXUiLand.AeccApplication.13.8", run_dir=run_dir, out_dir=tmp_path,
                      code="result['n'] = 1", input_drawing=None, save_to=None, timeout_s=60, env=env, procs=procs,
                      popen=popen or (lambda *a, **k: None), sleep=lambda s: None)


def test_hidden_instance_gate_and_user_session(tmp_path):
    with pytest.raises(ToolFailure) as exc:
        _hidden(tmp_path, {}, FakeProcs())
    assert exc.value.code == "HIDDEN_INSTANCE_NOT_ALLOWED"
    with pytest.raises(ToolFailure) as exc:
        _hidden(tmp_path, {hidden.GATE: "1"}, FakeProcs(others=[Proc(9, 1, "acad.exe")]))
    assert exc.value.code == "USER_SESSION_RUNNING"


def test_hidden_instance_reports_the_runner_result(tmp_path):
    class Runner:
        pid = 5151

        def __init__(self, cmd, **kw):
            request = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
            assert cmd[1:3] == ["-m", "vkm_cad.hidden_runner"] and request["clsid"] == "{X}"
            Path(request["result_file"]).write_text(json.dumps({"ok": True, "result": {"n": 1}, "log": ["hi"],
                                                                "attach_s": 9.0}), encoding="utf-8")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    out = _hidden(tmp_path, {hidden.GATE: "1"}, FakeProcs(), popen=Runner)
    assert out["result"] == {"n": 1} and out["channel"] == "HIDDEN_INSTANCE_COM" and out["log"] == ["hi"]
    assert (tmp_path / "run" / "user_code.py").read_text(encoding="utf-8") == "result['n'] = 1"
