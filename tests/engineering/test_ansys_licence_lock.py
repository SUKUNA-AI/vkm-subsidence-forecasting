"""The machine-wide licence lock of ``vkm-ansys``: exclusive across processes and handles, released when the holder
exits (also when it dies), holder information for status reports, POOL_BUSY when a caller does not wait."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from vkm_ansys.errors import ToolFailure
from vkm_ansys.licence_lock import LicenceLock, lock_dir, status

ROOT = Path(__file__).resolve().parents[2]


def _env(tmp_path: Path) -> dict[str, str]:
    return {"VKM_LOCK_DIR": str(tmp_path / "locks")}


def test_exclusive_within_process_and_holder_info(tmp_path):
    env = _env(tmp_path)
    first = LicenceLock("ansys", label="first", job_id="MAPDL-1", env=env)
    second = LicenceLock("ansys", label="second", env=env)
    assert first.acquire(0) and first.held
    assert second.acquire(0) is False
    holder = second.holder()
    assert holder["label"] == "first" and holder["job_id"] == "MAPDL-1" and holder["pool"] == "ansys"
    with pytest.raises(ToolFailure) as exc:
        with second.hold(0):
            pass
    assert exc.value.code == "POOL_BUSY" and exc.value.details["holder"]["label"] == "first"
    first.release()
    assert second.holder() is None
    with second.hold(0):
        assert status(env)["ansys"]["held"] is True
    assert status(env)["ansys"]["held"] is False and status(env)["matlab"]["held"] is False


def test_pools_are_independent(tmp_path):
    env = _env(tmp_path)
    with LicenceLock("ansys", env=env).hold(0):
        with LicenceLock("matlab", env=env).hold(0):
            pass
    with pytest.raises(ValueError):
        LicenceLock("nope", env=env)


def test_lock_is_released_when_the_holder_process_dies(tmp_path):
    env = _env(tmp_path)
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(ROOT / 'src')!r})
        from vkm_ansys.licence_lock import LicenceLock
        lock = LicenceLock("ansys", label="child", env={{"VKM_LOCK_DIR": {str(tmp_path / 'locks')!r}}})
        assert lock.acquire(5)
        print("held", flush=True)
        sys.stdin.readline()
        os._exit(3)                                   # dies without releasing
    """)
    child = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        mine = LicenceLock("ansys", label="parent", env=env)
        assert mine.acquire(0) is False
        assert mine.holder()["label"] == "child"
        child.stdin.write("go\n")
        child.stdin.flush()
        assert child.wait(timeout=30) == 3
        assert mine.acquire(10)                        # the OS dropped the lock of the dead process
        mine.release()
    finally:
        if child.poll() is None:
            child.kill()


def test_default_lock_dir_does_not_depend_on_the_sim_root(tmp_path):
    a = lock_dir({"VKM_SIM_ROOT": str(tmp_path / "a"), "LOCALAPPDATA": str(tmp_path / "local")})
    b = lock_dir({"VKM_SIM_ROOT": str(tmp_path / "b"), "LOCALAPPDATA": str(tmp_path / "local")})
    assert a == b
    assert lock_dir({"VKM_LOCK_DIR": str(tmp_path / "x")}) == tmp_path / "x"
