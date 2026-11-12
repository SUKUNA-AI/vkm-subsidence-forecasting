"""MAPDL wrapper must release its licence if job/cap setup fails before launch."""
from types import SimpleNamespace

import pytest

from vkm_ansys import detect, licence_lock, mapdl_batch


def test_job_creation_failure_releases_licence_without_launch(tmp_path, monkeypatch):
    ind = tmp_path / 'job' / 'in'
    ind.mkdir(parents=True)
    (ind / 'input.inp').write_text('FINISH\n/EXIT,NOSAVE\n', encoding='ascii')
    mapdl_batch.MapdlSpec(deck='input.inp', jobname='allocation_test', np=1,
                         parallel='smp').dump(ind / 'mapdl_spec.json')
    exe = tmp_path / 'fixture_executable'
    exe.write_bytes(b'Not executable: process launch is forbidden in this test')
    lock = SimpleNamespace(acquired=False, released=False)

    def acquire(timeout_s):
        lock.acquired = True
        return True

    def release():
        lock.released = True

    def fail_job_creation():
        raise RuntimeError('job memory cap allocation failed')

    lock.acquire, lock.release = acquire, release
    monkeypatch.setattr(licence_lock, 'LicenceLock', lambda *a, **kw: lock)
    monkeypatch.setattr(detect, 'find_root', lambda env: (tmp_path, '999', 'fixture'))
    monkeypatch.setattr(detect, 'exe_path', lambda *a: exe)
    monkeypatch.setattr(mapdl_batch, '_job_object', fail_job_creation)
    monkeypatch.setattr(mapdl_batch.subprocess, 'Popen',
                        lambda *a, **kw: pytest.fail('solver launched after job setup failure'))

    with pytest.raises(RuntimeError, match='job memory cap allocation failed'):
        mapdl_batch.run_job(tmp_path / 'job', {})

    assert lock.acquired
    assert lock.released
