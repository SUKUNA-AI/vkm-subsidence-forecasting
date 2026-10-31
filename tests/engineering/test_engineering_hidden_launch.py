"""Windows hidden flags on the shared process-tree launcher; POSIX untouched."""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from vkm_jobs import procs

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows creation flags only")


def test_actual_native_process_and_detached_runner_hidden(tmp_path, monkeypatch):
    original = subprocess.Popen
    observed = []
    def spy(*args, **kwargs):
        observed.append(kwargs.copy())
        return original(*args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", spy)
    with (tmp_path / "out.log").open("wb") as log:
        tree = procs.ProcessTree()
        try:
            p = tree.start([sys.executable, "-c", "print('hidden')"], cwd=tmp_path, env=os.environ, stdout=log, stderr=log)
            assert p.wait(timeout=10) == 0
            assert tree.wait_empty(5)
        finally:
            tree.close()
    native = observed[0]
    assert native["creationflags"] & procs.CREATE_NO_WINDOW
    assert native["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert native["startupinfo"].wShowWindow == subprocess.SW_HIDE
    procs.spawn_detached([sys.executable, "-c", "print('hidden runner')"], cwd=tmp_path, env=os.environ, log_path=tmp_path/'runner.log')
    detached = observed[-1]
    assert detached["creationflags"] & procs.DETACHED_PROCESS  # detached + no console, not incompatible flags
    assert detached["startupinfo"].wShowWindow == subprocess.SW_HIDE
    for proc in list(procs._KEEP):
        proc.wait(timeout=10)
    procs.reap()


def test_taskkill_fallback_hidden_without_running_taskkill(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    tree = procs.ProcessTree()
    tree.proc = SimpleNamespace(pid=123456789)
    tree.isolation = "WIN_TASKKILL_FALLBACK"
    tree.kill()
    argv, kw = calls[0]
    assert argv == ["taskkill", "/T", "/F", "/PID", "123456789"]
    assert kw["creationflags"] & procs.CREATE_NO_WINDOW
    assert kw["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert kw["startupinfo"].wShowWindow == subprocess.SW_HIDE
