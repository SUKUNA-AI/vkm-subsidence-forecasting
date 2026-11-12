import copy,pickle,os,subprocess,sys
from dataclasses import replace
from pathlib import Path
import numpy as np
import pytest
from numpy.testing import assert_allclose,assert_array_equal
from scipy.linalg import expm
from vkm_world.representations.world0 import scalar_graph as sg,scalar_graph_torch as gt

def graph(pure=False,**kw):
    args=dict(area=[1.7,2.3],heights=[[1.1,2.2],[.8,1.3]],bulk=[4.,7.],shear=[3.,2.],
        fractions=np.zeros((2,2,2)) if pure else np.array([[[.11,.37],[.2,.3]],[[.05,.65],[.0,.4]]]),
        rates=np.array([[[.2,.7],[.05,1.3]],[[.9,.04],[.7,.12]]]),edges=[(0,1,[1.3,2.8],2.1)],
        world_id='independent-synthetic',world_binding_signature='SI:q_down,basal_fixed,explicit-force-path',unknown_fraction=np.full((2,2),.15))
    args.update(kw);return sg.ScalarGraph.from_columns(**args)

def dense_step(g,s,f,dt):
    m=g.operators();K=m['K0'].toarray();B=m['B'].toarray();D=np.diag(g.km);R=np.diag(dt*g.inverse_tau)
    qz=np.linalg.solve(np.block([[K+B.T@D@B,-B.T@D],[-R@B,np.eye(len(g.km))+R]]),np.r_[f,s.memory])
    return qz[:len(s.q)],qz[len(s.q):]

def test_independent_mixed_BE_nonzero_history_and_work():
    g=graph();s=g.state([.03,-.02,.04,.01],np.linspace(-.03,.07,len(g.km)),time_s=17.);before=s.checkpoint()
    for dt,f in [(0.,[1.,-2.,.5,.7]),(.13,[-.3,.2,-1.,.4]),(.71,[2.,.4,-.5,1.2]),(.05,[0.,0.,0.,0.])]:
        q,z=dense_step(g,s,np.array(f),dt);step=g.advance(s,f,dt)
        assert_allclose(step.state.q,q,rtol=2e-13,atol=1e-14);assert_allclose(step.state.memory,z,rtol=2e-13,atol=1e-14)
        assert_allclose(step.external_work_J,step.energy_change_J+step.physical_dissipation_J+step.numerical_dissipation_J,atol=1e-13)
        assert step.physical_dissipation_J>=0 and step.numerical_dissipation_J>=0;s=step.state
    assert before['time_s']==17. # no hidden accepted model state

def test_continuous_matrix_exponential_oracle_first_order():
    g=graph();m=g.operators();B=m['B'].toarray();K=m['K0'].toarray();D=np.diag(g.km);R=np.diag(g.inverse_tau);H=K+B.T@D@B
    f=np.array([.7,-.4,1.2,-.1]);z0=np.linspace(-.02,.06,len(g.km));q0=np.linalg.solve(H,f+B.T@D@z0)
    J=R@(B@np.linalg.solve(H,B.T@D)-np.eye(len(z0)));b=R@B@np.linalg.solve(H,f)
    augmented=np.zeros((len(z0)+1,len(z0)+1));augmented[:-1,:-1]=J;augmented[:-1,-1]=b
    exact=(expm(.8*augmented)@np.r_[z0,1])[:-1];errors=[]
    for n in [4,8,16]:
        s=g.state(q0,z0)
        for _ in range(n):s=g.advance(s,f,.8/n).state
        errors.append(np.linalg.norm(s.memory-exact))
    assert 1.7<errors[0]/errors[1]<2.1 and 1.7<errors[1]/errors[2]<2.1

def test_galerkin_full_memory_external_work_and_lift():
    g=graph();c,P=g.coarsen([0,0]);q=np.array([.13,-.22]);z=np.linspace(.04,-.03,len(g.km));v=np.array([-.03,.09]);f=np.array([1.2,.1,-.4,2.])
    assert_allclose(g.energy(g.state(P@q,z)),c.energy(c.state(q,z)),atol=1e-14)
    assert_allclose(f@(P@v),(P.T@f)@v,atol=1e-15)
    assert c.physical_signature==g.physical_signature and c.representation_signature!=g.representation_signature
    assert_array_equal(c.branch_carrier,g.branch_carrier);assert_array_equal(c.inverse_tau,g.inverse_tau)
    with pytest.raises(ValueError):c.validate_state(g.state())

@pytest.mark.parametrize('protocol',[2,4,5])
def test_pickle_and_copy_keep_operator_state_immutable(protocol):
    g=graph();s=g.state(memory=np.arange(len(g.km))*.01)
    g2,s2=pickle.loads(pickle.dumps((g,s),protocol=protocol));g2.validate_state(s2)
    for obj in [g2.km,g2.inverse_tau,g2._K0.data,g2._K0.indices,g2._K0.indptr,g2.unknown_fraction,s2.q,s2.memory]:
        with pytest.raises(ValueError):obj.setflags(write=True)
    assert copy.deepcopy(g2) is g2 and copy.deepcopy(s2) is s2
    with pytest.raises(ValueError):replace(g2,km=g2.km*1.001)

def test_dt0_memory_exact_checkpoint_restart_no_change():
    g=graph();s=g.state(memory=np.linspace(-.1,.1,len(g.km)));old=s.checkpoint()
    t=g.advance(s,[1.,-.2,2.,.5],0).state
    assert_array_equal(t.memory,s.memory);assert t.time_s==s.time_s
    a=g.advance(t,[.3,.4,-.2,1.],.23);b=g.advance(sg.GraphState.restore(t.checkpoint()),[.3,.4,-.2,1.],.23)
    assert_array_equal(a.state.q,b.state.q);assert_array_equal(a.state.memory,b.state.memory);assert s.checkpoint()==old

def test_binding_actual_rate_unknown_and_detached_sparse():
    g=graph();s=g.state();m=g.operators();m['K0'].data*=0;m['B'].data*=0;m['km']*=0
    assert np.linalg.norm(g.operators()['K0'].data)>0
    other=graph(rates=np.full((2,2,2),.4))
    with pytest.raises(ValueError):other.validate_state(s)
    with pytest.raises(ValueError):replace(g,unknown_fraction=np.full((2,2),.14))
    with pytest.raises(ValueError):g.advance(s,[1,np.nan,2,3],.1)
    assert not np.any(s.q) and not np.any(s.memory)

def test_rotated_port_force_power_and_invalid_directions():
    q,_=np.linalg.qr(np.random.default_rng(73).normal(size=(3,3)));normal=q[:,0];down=q[:,1]
    p=sg.lateral_port(2.7,normal,1.3,4.1,.17,-.06,.03,-.08,down_direction=down)
    assert_allclose(p['force_on_i_N']+p['force_on_j_N'],0,atol=1e-15)
    assert_allclose(p['power_into_carriers_W']+p['spring_energy_rate_W'],0,atol=1e-15)
    with pytest.raises(ValueError):sg.lateral_port(1,normal,1,1,0,1,2,3,down_direction=down*2)

@pytest.mark.parametrize('change',[dict(edges=[(0,1,[1,1],np.nan)]),dict(edges=[(.2,1,[1,1],1.)]),dict(rates=np.full((2,2,2),-1.)),dict(area=[1.,0.])])
def test_invalid_input_refused(change):
    with pytest.raises(ValueError):graph(**change)

def test_optional_modules_import_with_torch_blocked():
    path=str(Path(sg.__file__).parent);public=str(Path(sg.__file__).parents[3])
    code="import sys,builtins;sys.path.insert(0,"+repr(public)+");import vkm_world.representations.world0 as p;p.__path__.insert(0,"+repr(path)+");original=builtins.__import__\ndef guard(name,*a,**kw):\n if name=='torch' or name.startswith('torch.'):raise RuntimeError('torch import forbidden')\n return original(name,*a,**kw)\nbuiltins.__import__=guard\nfrom vkm_world.representations.world0 import scalar_graph,scalar_graph_torch\nassert 'torch' not in sys.modules\nprint('LAZY_OK')"
    result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
    assert result.returncode==0,result.stderr;assert 'LAZY_OK' in result.stdout

def test_torch_cpu_matches_independent_dense_and_restarts():
    import torch
    torch.set_num_threads(1)
    g=graph();old=g.state(memory=np.linspace(.02,.06,len(g.km)));forces=np.array([[1,-1,2,.4],[.1,0,-.2,1.3],[-.4,.2,.1,0]])
    steps,receipt=gt.run_torch(g,old,forces,.11,device='cpu');s=old
    for step,f in zip(steps,forces):
        q,z=dense_step(g,s,f,.11);assert_allclose(step.state.q,q,atol=2e-14);assert_allclose(step.state.memory,z,atol=2e-14);s=step.state
    replay,_=gt.run_torch(g,steps[0].state,forces[1:],.11,device='cpu')
    for a,b in zip(replay,steps[1:]):assert_array_equal(a.state.q,b.state.q);assert_array_equal(a.state.memory,b.state.memory)
    assert receipt['device']=='cpu' and receipt['cpu_oracle']['status']=='CHECKED'

def test_repaired_budget_rejects_before_any_dense_array(monkeypatch):
    import torch
    g=graph();s=g.state();allocations=[];original=np.zeros
    def spy(shape,*args,**kwargs):
        if isinstance(shape,tuple) and len(shape)==2:allocations.append(shape)
        return original(shape,*args,**kwargs)
    monkeypatch.setattr(np,'zeros',spy)
    with pytest.raises(MemoryError):gt.run_torch(g,s,np.ones((1,4)),.1,max_dense_bytes=1)
    assert allocations==[]

def test_phase_excess_never_creates_negative_equilibrium_energy():
    try:
        g=sg.ScalarGraph.from_columns([1.],[[1.]],[1.],[1e14],[[[1+5e-13]]],[[[1.]]],[],world_id='invalidfraction',world_binding_signature='test')
    except ValueError:return
    # An accepted coefficient set may not contain negative elastic remainder.
    s=g.state([1.],[1.]);assert g.energy(s)>=0

def test_nonfinite_energy_residual_is_rejected_not_accepted():
    g=sg.ScalarGraph.from_columns([1.],[[1.]],[1.],[1.],[[[0.]]],[[[0.]]],[],world_id='overflow',world_binding_signature='test')
    s=g.state([1e200]);force=g.operators()['K0']@s.q
    with np.errstate(all='ignore'):
        with pytest.raises((ValueError,RuntimeError,OverflowError,FloatingPointError)):
            g.advance(s,force,.1)


def test_nonfinite_energy_direct_audit_rejects():
    g=sg.ScalarGraph.from_columns([1.],[[1.]],[1.],[1.],[[[0.]]],[[[0.]]],[],world_id='overflow',world_binding_signature='test')
    s=g.state([1e200]);force=g.operators()['K0']@s.q
    with np.errstate(all='ignore'):
        with pytest.raises((ValueError,RuntimeError,OverflowError,FloatingPointError)):
            g.audit(s,s.q,s.memory,force,.1)


def test_torch_nonfinite_energy_rejects_without_cpu_oracle():
    g=sg.ScalarGraph.from_columns([1.],[[1.]],[1.],[1.],[[[0.]]],[[[0.]]],[],world_id='overflow',world_binding_signature='test')
    s=g.state([1e200]);force=g.operators()['K0']@s.q
    with np.errstate(all='ignore'):
        with pytest.raises((ValueError,RuntimeError,OverflowError,FloatingPointError)):
            gt.run_torch(g,s,force[None,:],.1,device='cpu',verify_cpu=False)
