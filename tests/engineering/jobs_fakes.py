"""Fake applications (``python -c``) and helpers for the job-layer tests (no MATLAB, no Ansys)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from vkm_jobs.roots import SimRoot
from vkm_jobs.service import JobsService

PY = sys.executable

# writes out/result.json = {"s": 136} and prints three log lines (one ERROR)
OK_APP = ("import json, pathlib; print('line one'); print('ERROR: something to grep'); print('line three'); "
          "pathlib.Path('../out/result.json').write_text(json.dumps({'s': 136, 'v': [1.0, 2.0]}))")
# starts a child that sleeps, records both pids, then sleeps itself
TREE_APP = ("import os, subprocess, sys, time, pathlib; "
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
            "pathlib.Path('pids.txt').write_text(f'{os.getpid()} {c.pid}'); time.sleep(120)")
# the main process exits at once and leaves a child behind
ORPHAN_APP = ("import subprocess, sys, pathlib; "
              "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
              "pathlib.Path('pids.txt').write_text(str(c.pid))")


def service(tmp_path: Path, **env: str) -> JobsService:
    root = SimRoot.at(tmp_path / "sim")
    assert root.path is not None, root.reason
    return JobsService(root, env={**os.environ, **env}, public_root=tmp_path / "public")


def submit(jobs: JobsService, code: str, *, pool: str = "py", timeout_s: int = 60, checks=None, wait_s: float = 0,
           **fields):
    draft = jobs.draft("PY")
    spec = draft.spec(kind="fake", pool=pool, argv=[PY, "-c", code], timeout_s=timeout_s, checks=checks or [],
                      **fields)
    return jobs.submit(draft, spec, wait_s=wait_s), draft


def wait_for(predicate, timeout_s: float = 30.0, poll_s: float = 0.1):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(poll_s)
    raise AssertionError("condition not met in time")


def read_pids(job_dir: Path) -> list[int]:
    path = job_dir / "work" / "pids.txt"
    wait_for(lambda: path.is_file() and path.read_text().strip())
    return [int(x) for x in path.read_text().split()]
