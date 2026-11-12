from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import time
import numpy as np
import pytest
from scipy import sparse
from vkm_ansys import persistent_bridge as p


def config(tmp_path):
    return p.PersistentConfig(executable='ANSYS261.exe',executable_sha256='a'*64,
        lease_owner='TEST_ONLY',world_id='synthetic',representation_id='synthetic',
        state_id='virgin',geometry_sha256='b'*64,ram_bytes=2*2**30,matrix_structure='general')


class V:
    def __init__(self,a): self.a=np.asarray(a).copy()
    def asarray(self): return self.a.copy()


class M:
    def __init__(self,q): self.q=q.copy()
    def asarray(self): return self.q.copy()
    def dot(self,v): return V(self.q@v.a)


class Factor:
    def __init__(self,q): self.q=q.copy()
    def solve(self,b): return V(np.linalg.solve(self.q.toarray(),b.a))


class Math:
    def __init__(self,work): self.work=work;self.factor_count=0;self.bad_upload=False;self.gpu=True
    def free(self,*args): pass
    def matrix(self,q,*,name,triu):
        assert triu is False
        q=q.copy()
        if self.bad_upload:q.data[0]+=1
        return M(q)
    def factorize(self,m,*,algo,inplace):
        assert algo=='DSP' and inplace is False
        self.factor_count+=1
        with (self.work/'jointgpu.DSP').open('a') as f:
            f.write('\nGPU acceleration activated\npercentage of GPU accelerated flops = '+('100' if self.gpu else '0')+'\n')
        return Factor(m.q)
    def set_vec(self,b,*,name): return V(b)


def fake_session(tmp_path):
    s=p.PersistentSession(tmp_path/'native',config(tmp_path))
    s._math=Math(s.work);s._client=object();s.start=lambda:None
    s.close=lambda:setattr(s,'closed',True)
    return s


def test_same_full_operator_reuses_factor_new_rhs_and_changed_operator_refactors(tmp_path):
    s=fake_session(tmp_path);q=sparse.csr_matrix([[4.,2.],[1.,3.]])
    rows=[]
    for index,(a,b) in enumerate([(q,[1.,2.]),(q,[3.,-1.]),(q+sparse.eye(2)*.1,[1.,2.])]):
        out=tmp_path/f'request{index}';out.mkdir()
        y,action,row=s.solve(a,np.array(b),out);rows.append(row)
        np.testing.assert_allclose(a@y,b,atol=1e-14)
        np.testing.assert_allclose(action,b,atol=1e-14)
    assert s.factorizations==2 and s.reuses==1 and s.solves==3
    assert [x['reused_factorization'] for x in rows]==[False,True,False]


def test_hash_includes_all_operator_entries_and_is_storage_independent():
    a=sparse.csr_matrix([[4.,2.],[1.,3.]])
    assert p.canonical_operator(a)[1]==p.canonical_operator(a.tocsc())[1]
    b=a.copy();b.data[-1]=np.nextafter(b.data[-1],4.)
    assert p.canonical_operator(a)[1]!=p.canonical_operator(b)[1]
    assert p.canonical_operator(a)[1]!=p.canonical_operator(a.T)[1]


@pytest.mark.parametrize('fault',['matrix_upload','gpu','rhs_nonfinite','matrix_nonfinite'])
def test_runtime_and_array_failures_never_reuse_bad_factors(tmp_path,fault):
    s=fake_session(tmp_path);out=tmp_path/'request';out.mkdir()
    q=sparse.eye(2,format='csr');b=np.ones(2)
    if fault=='matrix_upload':s._math.bad_upload=True
    if fault=='gpu':s._math.gpu=False
    if fault=='rhs_nonfinite':b[0]=np.nan
    if fault=='matrix_nonfinite':q.data[0]=np.nan
    with pytest.raises((RuntimeError,ValueError)):s.solve(q,b,out)
    assert s.solves==0 and s._factor_key is None


def test_new_gpu_proof_cannot_reuse_old_factorization_log(tmp_path):
    s=fake_session(tmp_path);q=sparse.eye(2,format='csr')
    first=tmp_path/'first';first.mkdir();s.solve(q,np.ones(2),first)
    s._math.gpu=False
    second=tmp_path/'second';second.mkdir()
    with pytest.raises(RuntimeError,match='GPU'):s.solve(q*2,np.ones(2),second)
    assert s.solves==1


def test_gpu_statistics_may_arrive_only_after_first_backsolve(tmp_path):
    s=fake_session(tmp_path)
    def factorize(m,*,algo,inplace):
        factor=Factor(m.q)
        solve=factor.solve
        def delayed_stats(b):
            value=solve(b)
            (s.work/'native.out').write_text('GPU acceleration activated\npercentage of GPU accelerated flops = 100\n')
            return value
        factor.solve=delayed_stats
        return factor
    s._math.factorize=factorize
    out=tmp_path/'request';out.mkdir()
    _,_,row=s.solve(sparse.eye(2,format='csr'),np.ones(2),out)
    assert row['factor_gpu']['source']=='native.out DSPOPTION PERFORMANCE'
    assert row['factorizations']==1


def test_guard_timeout_is_terminal_for_native_call(tmp_path):
    s=fake_session(tmp_path)
    with pytest.raises(TimeoutError):
        with s.guard(.01):time.sleep(.03)
    assert s.timed_out


def test_argv_has_owned_local_transport_and_limits(tmp_path):
    c=config(tmp_path);args=p.native_argv(c,tmp_path)
    for key,value in [('-np','1'),('-transport','wnua'),('-acc','nvidia'),('-m','512'),('-db','64')]:
        assert args[args.index(key)+1]==value
    with pytest.raises(ValueError):replace(c,port=0).validate()


def test_unknown_lifecycle_retains_licence(tmp_path,monkeypatch):
    s=p.PersistentSession(tmp_path/'session',config(tmp_path));released=[]
    s._lock=SimpleNamespace(release=lambda:released.append(True))
    def bad(*a):raise RuntimeError('cannot inspect lifecycle')
    monkeypatch.setattr(p.base,'_own_processes',bad)
    with pytest.raises(RuntimeError):s.close()
    assert not released and not s.closed
    monkeypatch.setattr(p.base,'_own_processes',lambda *a:[])
    s.close();assert released and s.closed


def test_callback_mass_force_state_and_b_change_invalidate_factor(tmp_path,monkeypatch):
    bridge=p.PersistentCoupledBridge(tmp_path/'bridge',config(tmp_path))
    session=bridge.session;session.start=lambda:None
    session._client=object();session._math=Math(session.work);session.close=lambda:None
    model=SimpleNamespace(B=sparse.csr_matrix([[1.],[.5]]),storage=np.array([2.]),
        L=sparse.csr_matrix([[.25]]),reference_displacement_free=np.array([.2,-.3]))
    fluid=SimpleNamespace(model=model,dt=2.,rhs_storage=np.array([.7]))
    K=sparse.csr_matrix([[4.,2.],[1.,3.]]);old=np.array([.1,.4]);old_copy=old.copy()
    for i in range(3):
        if i==2:model.B=model.B*1.1
        rhs=np.array([2.+i,-1.]);u,pressure=bridge(K,rhs,fluid,old)
        np.testing.assert_allclose(K@u-model.B@pressure,rhs,atol=1e-14)
        np.testing.assert_allclose(model.B.T@(u-model.reference_displacement_free)+2.5*pressure,.7,atol=1e-14)
    np.testing.assert_array_equal(old,old_copy)
    assert session.factorizations==2 and session.reuses==1
    assert len(list(bridge.store.glob('*.npz')))==2
    bridge.close()
    assert json.loads((bridge.folder/'execution.json').read_text())['CPU_fallback_count']==0
