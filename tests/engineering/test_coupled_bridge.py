import json
import os
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from scipy import sparse
from scipy.io import mmread

from vkm_ansys import coupled_bridge as nb


@pytest.fixture
def config(tmp_path):
    exe = tmp_path/'ANSYS.exe'
    exe.write_bytes(b'fixture, never executed')
    return nb.NativeConfig(str(exe), nb.sha(exe), 'synthetic-lease', 'synthetic-world',
                           'synthetic-representation', 'synthetic-state', 'a'*64,
                           ram_bytes=512*2**20)


def inputs():
    K = sparse.csc_matrix([[4., 1.], [1., 3.]])
    model = SimpleNamespace(B=sparse.csc_matrix([[1.], [.5]]), storage=np.array([2.]),
                            L=sparse.csc_matrix([[.25]]), reference_displacement_free=np.array([.2, -.3]))
    fluid = SimpleNamespace(model=model, dt=2., rhs_storage=np.array([.7]))
    return K, np.array([2., -1.]), fluid, np.array([.1, .4])


def fake_native(out):
    """Test oracle only, explicitly no native/GPU qualification."""
    ind, work = out/'case/in', out/'case/work'
    work.mkdir()
    Q = mmread(ind/'operator.mtx').toarray()
    b = np.asarray(mmread(ind/'rhs.mtx')).ravel()
    z = np.linalg.solve(Q, b)
    np.savetxt(work/'native_solution.txt', z)
    np.savetxt(work/'native_action.txt', Q@z)
    (work/'jointgpu.DSP').write_text('GPU acceleration activated\npercentage of GPU accelerated flops = 25.5')
    return 0


def test_exact_mixed_equation_roundtrip_and_nonmutation(tmp_path, config):
    K, rhs, fluid, previous = inputs()
    before = [x.copy() for x in (K.data, rhs, fluid.model.B.data, fluid.rhs_storage, previous)]
    bridge = nb.AnsysCoupledBridge(tmp_path/'attempt', config)
    bridge._execute_native = fake_native
    u, p = bridge(K, rhs, fluid, previous)
    # Independent unscaled dense mixed equation; do not call assemble for oracle.
    A = np.array([[4., 1., -1.], [1., 3., -.5], [-1., -.5, -2.5]])
    exact = np.linalg.solve(A, np.r_[rhs-K@fluid.model.reference_displacement_free, -.7])
    np.testing.assert_allclose(u, exact[:2]+fluid.model.reference_displacement_free, rtol=1e-14)
    np.testing.assert_allclose(p, exact[2:], rtol=1e-14)
    for old, now in zip(before, (K.data, rhs, fluid.model.B.data, fluid.rhs_storage, previous)):
        np.testing.assert_array_equal(old, now)
    receipt = json.loads((bridge.folder/'block_001/receipt.json').read_text())
    for path, digest in receipt['files_sha256'].items():
        assert nb.sha(bridge.folder/'block_001'/path) == digest
    bridge.close()
    assert json.loads((bridge.folder/'execution.json').read_text())['CPU_fallback_count'] == 0
    with pytest.raises(RuntimeError, match='terminal'):
        bridge(K, rhs, fluid, previous)


def test_tangent_and_force_matrix_market_integrity(tmp_path, config):
    args = inputs()
    s = nb.assemble(*args, config)
    bridge = nb.AnsysCoupledBridge(tmp_path/'roundtrip', config)
    bridge._execute_native = fake_native
    bridge(*args)
    ind = bridge.folder/'block_001/case/in'
    archive = json.loads((bridge.folder/'block_001/matrix_archive.json').read_text())
    for logical, entry in archive['entries'].items():
        nb.restore_matrix(bridge.folder, entry, bridge.folder/logical)
    np.testing.assert_array_equal(mmread(ind/'operator.mtx').toarray(), s['Q'].toarray())
    np.testing.assert_array_equal(np.asarray(mmread(ind/'rhs.mtx')).ravel(), s['scale']*s['r'])
    assert (ind/'input.inp').read_text() == nb.input_deck()
    spec = json.loads((ind/'mapdl_spec.json').read_text())
    assert spec['includes'] == ['operator.mtx', 'rhs.mtx']
    assert spec['np'] == 1


@pytest.mark.parametrize('fault', ['nonsymmetric', 'nan', 'shape', 'complex', 'storage', 'dt', 'zero_row'])
def test_bad_system_never_reaches_runtime(tmp_path, config, fault):
    K, rhs, fluid, prev = inputs()
    if fault == 'nonsymmetric': K = sparse.csc_matrix([[4., 2.], [1., 3.]])
    if fault == 'nan': rhs[0] = np.nan
    if fault == 'shape': rhs = rhs[:, None]
    if fault == 'complex': K = K.astype(complex)
    if fault == 'storage': fluid.model.storage[0] = 0
    if fault == 'dt': fluid.dt = -1.
    if fault == 'zero_row': K = sparse.csc_matrix([[0., 0.], [0., 3.]])
    bridge = nb.AnsysCoupledBridge(tmp_path/fault, config)
    bridge._execute_native = lambda out: pytest.fail('runtime reached on invalid input')
    with pytest.raises(ValueError): bridge(K, rhs, fluid, prev)
    assert (bridge.folder/'block_001/failure.json').is_file()
    assert bridge.records == []


@pytest.mark.parametrize('fault', ['exit', 'action', 'solution', 'nonfinite', 'cpu', 'exception'])
def test_failure_retains_prior_success_and_input_state(tmp_path, config, fault):
    bridge = nb.AnsysCoupledBridge(tmp_path/fault, config)
    args = inputs()
    bridge._execute_native = fake_native
    bridge(*args)
    prior = nb.sha(bridge.folder/'block_001/receipt.json')
    def fail(out):
        fake_native(out)
        w = out/'case/work'
        if fault == 'exit': return 124
        if fault == 'action': np.savetxt(w/'native_action.txt', np.ones(3)*99)
        if fault == 'solution': np.savetxt(w/'native_solution.txt', np.ones(2))
        if fault == 'nonfinite': np.savetxt(w/'native_solution.txt', np.ones(3)*np.nan)
        if fault == 'cpu': (w/'jointgpu.DSP').write_text('CPU factorization only')
        if fault == 'exception': raise RuntimeError('injected failure')
        return 0
    bridge._execute_native = fail
    before = args[2].rhs_storage.copy()
    with pytest.raises((RuntimeError, ValueError)): bridge(*args)
    assert len(bridge.records) == 1
    assert nb.sha(bridge.folder/'block_001/receipt.json') == prior
    assert (bridge.folder/'block_002/case/in/operator.mtx').is_file()
    assert (bridge.folder/'block_002/failure.json').is_file()
    assert not (bridge.folder/'block_002/receipt.json').exists()
    np.testing.assert_array_equal(before, args[2].rhs_storage)
    bridge.close()
    assert json.loads((bridge.folder/'execution.json').read_text())['status'] == 'FAILED'


def test_existing_folder_never_overwritten(tmp_path, config):
    p = tmp_path/'owned'
    p.mkdir()
    marker = p/'kept'
    marker.write_text('preserve')
    with pytest.raises(FileExistsError): nb.AnsysCoupledBridge(p, config)
    assert marker.read_text() == 'preserve'


def test_scoped_hooks_and_memory_limit_restore_even_on_failure(tmp_path, config, monkeypatch):
    fake_win = SimpleNamespace(JobObjectExtendedLimitInformation=9, JOB_OBJECT_LIMIT_JOB_MEMORY=512,
        QueryInformationJobObject=lambda *a: {'BasicLimitInformation': {'LimitFlags': 8192}})
    seen = []
    fake_win.SetInformationJobObject = lambda j, t, info: seen.append(info)
    monkeypatch.setitem(sys.modules, 'win32job', fake_win)
    monkeypatch.setenv('VKM_DEEP_GPU_OWNER', config.lease_owner)
    monkeypatch.delenv('ANSGPU_OVERRIDE', raising=False)
    monkeypatch.setattr(nb.mapdl_batch, '_job_object', lambda: 'job')
    monkeypatch.setattr(nb.mapdl_batch, '_assign', lambda j, p: True)
    original = nb.mapdl_batch._job_object, nb.mapdl_batch._assign, nb.mapdl_batch.build_argv
    with pytest.raises(RuntimeError, match='injected'):
        with nb._bounded_runtime(config, tmp_path):
            assert nb.mapdl_batch._job_object() == 'job'
            assert seen[0]['JobMemoryLimit'] == config.ram_bytes
            assert seen[0]['BasicLimitInformation']['LimitFlags'] & 512
            raise RuntimeError('injected')
    assert original == (nb.mapdl_batch._job_object, nb.mapdl_batch._assign, nb.mapdl_batch.build_argv)
    assert not nb._NATIVE_LOCK.locked()


@pytest.mark.parametrize('fault', ['owner', 'override', 'pin'])
def test_private_runtime_authorization_fail_closed(tmp_path, config, monkeypatch, fault):
    monkeypatch.setenv('VKM_DEEP_GPU_OWNER', config.lease_owner)
    monkeypatch.delenv('ANSGPU_OVERRIDE', raising=False)
    if fault == 'owner': monkeypatch.setenv('VKM_DEEP_GPU_OWNER', 'other')
    if fault == 'override': monkeypatch.setenv('ANSGPU_OVERRIDE', '1')
    if fault == 'pin': config = replace(config, executable_sha256='b'*64)
    original = nb.mapdl_batch.build_argv
    with pytest.raises(RuntimeError):
        with nb._bounded_runtime(config, tmp_path): pytest.fail('invalid config accepted')
    assert nb.mapdl_batch.build_argv is original
    assert not nb._NATIVE_LOCK.locked()


def test_lossless_archive_deduplicates_and_restores(tmp_path):
    root = tmp_path/'attempt'
    out = root/'block_001'
    for part in ('in', 'work'):
        (out/'case'/part).mkdir(parents=True)
        (out/'case'/part/'operator.mtx').write_text('%%MatrixMarket matrix coordinate real general\n2 2 2\n1 1 2\n2 2 3\n')
    original = nb.sha(out/'case/in/operator.mtx')
    entries = nb.archive_matrices(root, out)
    assert len(entries) == 2
    assert len(list((root/'matrix_cas').glob('*.gz'))) == 1
    assert not list(out.glob('case/*/*.mtx'))
    restored = nb.restore_matrix(root, next(iter(entries.values())), root/'readback.mtx')
    assert nb.sha(restored) == original
    np.testing.assert_array_equal(mmread(restored).toarray(), np.diag([2., 3.]))
    with pytest.raises(FileExistsError): nb.restore_matrix(root, next(iter(entries.values())), restored)
    with pytest.raises(ValueError, match='outside'): nb.restore_matrix(root, next(iter(entries.values())), tmp_path/'escape')


def test_corrupt_existing_cas_preserves_matrix(tmp_path):
    root = tmp_path/'attempt'
    source = root/'block_001/case/in/operator.mtx'
    source.parent.mkdir(parents=True)
    source.write_text('%%MatrixMarket matrix array real general\n1 1\n3\n')
    (root/'matrix_cas').mkdir()
    (root/'matrix_cas'/f'{nb.sha(source)}.mtx.gz').write_bytes(b'broken')
    with pytest.raises(OSError): nb.archive_matrices(root, root/'block_001')
    assert source.exists()


def test_disk_budget_refuses_before_native_and_matrix_write(tmp_path, config, monkeypatch):
    bridge = nb.AnsysCoupledBridge(tmp_path/'disk', config)
    monkeypatch.setattr(nb.shutil, 'disk_usage', lambda p: SimpleNamespace(free=1))
    bridge._execute_native = lambda out: pytest.fail('native reached with no headroom')
    with pytest.raises(RuntimeError, match='DISK_BUDGET'): bridge(*inputs())
    assert not list(bridge.folder.rglob('*.mtx'))


def test_explicit_general_preserves_asymmetry_and_equations(tmp_path, config):
    config=replace(config,matrix_structure='general')
    K,rhs,fluid,prev=inputs()
    K=sparse.csc_matrix([[4.,2.],[1.,3.]])
    s=nb.assemble(K,rhs,fluid,prev,config)
    assert s['relative_asymmetry']>.1
    bridge=nb.AnsysCoupledBridge(tmp_path/'general',config)
    bridge._execute_native=fake_native
    u,p=bridge(K,rhs,fluid,prev)
    np.testing.assert_allclose(K@u-fluid.model.B@p,rhs,atol=1e-13)
    np.testing.assert_allclose(fluid.model.B.T@(u-fluid.model.reference_displacement_free)+2.5*p,fluid.rhs_storage,atol=1e-13)
    assert not np.allclose(s['Q'].toarray(),s['Q'].toarray().T)


@pytest.mark.parametrize('result', ['valid', 'missing', 'failure', 'inaccessible', 'invalid'])
def test_cim_lifecycle_fails_closed(tmp_path, monkeypatch, result):
    own=tmp_path/'attempt'
    row={'ProcessId':123,'CommandLine':str(own/'case/work')}
    foreign={'ProcessId':456,'CommandLine':str(tmp_path/'attempt_other/case/work')}
    response=SimpleNamespace(returncode=0,stdout=json.dumps([row,foreign]),stderr='')
    if result=='missing': response.stdout=''
    if result=='failure': response.returncode=1;response.stderr='CIM unavailable'
    if result=='inaccessible': response.stdout=json.dumps([{'ProcessId':123,'CommandLine':None}])
    if result=='invalid': response.stdout='{}'
    # Simulate Windows API availability for portable CPU-only test discovery.
    monkeypatch.setattr(nb,'os',SimpleNamespace(name='nt',environ=dict(os.environ)))
    monkeypatch.setattr(nb.subprocess,'CREATE_NO_WINDOW',0,raising=False)
    monkeypatch.setattr(nb.subprocess,'run',lambda *a,**kw:response)
    if result=='valid': assert nb._own_processes(own,'ANSYS261.exe')==[123]
    else:
        with pytest.raises(RuntimeError): nb._own_processes(own,'ANSYS261.exe')
