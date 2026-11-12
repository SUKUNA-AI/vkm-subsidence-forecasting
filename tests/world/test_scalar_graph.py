import copy
import pickle
import sys
from dataclasses import replace
import numpy as np
import pytest
from numpy.testing import assert_allclose,assert_array_equal
from vkm_world.representations.world0.scalar_graph import ScalarGraph,GraphState,build_world,restriction,restrict,integrate


def inputs():
    return dict(area=[1,2,1],heights=[[1,2],[2,1],[1,1]],bulk=[3,4],shear=[2,3],
        fractions=np.full((3,2,2),.2),rates=np.broadcast_to([.4,.1],(3,2,2)),
        edges=[(0,1,np.array([1,2]),1.),(1,2,np.array([2,1]),1.5)])


def graph(**delta):
    data=inputs();data.update(delta)
    return ScalarGraph.from_columns(**data,world_id='synthetic-existing-world',
        world_binding_signature='synthetic-BC-history-virgin-or-explicit-memory',unknown_fraction=np.full((3,2),.4))


def test_exact_galerkin_work_energy_and_retained_memory():
    fine=graph();coarse,P=fine.coarsen([0,0,1]);qc=np.array([.1,.2,-.1,.3]);z=np.linspace(-.1,.2,len(fine.km))
    assert_allclose(fine.energy(fine.state(P@qc,z)),coarse.energy(coarse.state(qc,z)),rtol=2e-15)
    f=np.arange(6.);assert_allclose(f@(P@qc),(P.T@f)@qc,rtol=2e-15)
    assert fine.physical_signature==coarse.physical_signature and fine.world_id==coarse.world_id
    assert fine.representation_signature!=coarse.representation_signature
    assert_array_equal(fine.km,coarse.km);assert_array_equal(fine.inverse_tau,coarse.inverse_tau)
    assert_array_equal(fine.branch_carrier,coarse.branch_carrier);assert_array_equal(fine.unknown_fraction,coarse.unknown_fraction)
    with pytest.raises(ValueError):coarse.validate_state(fine.state())


def test_nested_restriction_and_full_partition_identity():
    m=build_world(**inputs());P=restriction([0,0,1],2);Q=restriction([0,0],2)
    direct=restrict(m,P@Q);staged=restrict(restrict(m,P),Q)
    assert_allclose(direct['K0'].toarray(),staged['K0'].toarray(),atol=1e-13)
    assert_array_equal(direct['B'].toarray(),staged['B'].toarray())
    g=graph();ident,I=g.coarsen([0,1,2])
    assert ident.representation_signature==g.representation_signature


def test_analytic_parallel_phase_step_with_nonzero_memory():
    g=ScalarGraph.from_columns([2.],[[3.]],[5.],[7.],[[[.2,.3]]],[[[.4,.1]]],[],
        world_id='two-phase',world_binding_signature='fixed-base,force-2N,nonzero-memory')
    old=g.state([.1],[.04,-.03]);dt=.25;force=np.array([2.])
    m=g.operators();rr=dt*g.inverse_tau;ke=g.km/(1+rr)
    expected=(force[0]+np.dot(ke,old.memory))/(m['K0'][0,0]+ke.sum())
    out=g.advance(old,force,dt)
    assert_allclose(out.state.q,[expected],rtol=1e-14)
    assert_allclose(out.state.memory,(old.memory+rr*expected)/(1+rr),rtol=1e-14)
    assert_allclose(out.external_work_J,out.energy_change_J+out.physical_dissipation_J+out.numerical_dissipation_J,atol=1e-14)
    assert out.physical_dissipation_J>=0 and out.numerical_dissipation_J>=0
    assert m['K0'][0,0]>0 # elastic UNKNOWN remainder survives at long time


def test_same_surface_different_memory_is_not_same_state():
    g=graph();a=g.state();b=g.state(memory=np.full(len(g.km),.2));f=np.ones(6)
    assert_array_equal(a.q,b.q)
    assert np.linalg.norm(g.advance(a,f,.3).state.q-g.advance(b,f,.3).state.q)>.01


def test_coupling_port_balance_and_unknown_remainder():
    m=build_world(**inputs());assert_allclose(m['elastic_fraction'],.6)
    assert_allclose(m['lateral']@np.ones(6),0,atol=1e-14)
    assert np.linalg.eigvalsh(m['K0'].toarray()).min()>0
    q=np.arange(6.);assert_allclose(np.sum(m['lateral']@q),0,atol=1e-14)


def test_oriented_port_reuses_existing_world0_work_pair():
    from vkm_world.representations.world0.scalar_graph import lateral_port
    p=lateral_port(2.,[1.,0,0],3.,7.,.2,.05,.03,-.02)
    assert_allclose(p['force_on_i_N']+p['force_on_j_N'],0)
    assert_allclose(p['power_into_carriers_W']+p['spring_energy_rate_W'],0,atol=1e-15)
    reverse=lateral_port(2.,[-1.,0,0],3.,7.,.05,.2,-.02,.03)
    assert_allclose(reverse['force_on_i_N'],p['force_on_j_N'])
    with pytest.raises(ValueError):lateral_port(2.,[0,0,1.],3.,7.,.2,.05,.03,-.02)


def test_checkpoint_restart_dt0_and_original_numerics():
    g=graph();old=g.state(memory=np.linspace(.01,.03,len(g.km)));forces=np.array([[1,0,2,0,1,0],[0,0,0,0,0,0],[2,0,1,0,3,0.]])
    reference=integrate(build_world(**inputs()),forces,.3,(old.q,old.memory));states=[]
    for f in forces:
        step=g.advance(old,f,.3);old=step.state;states.append(old)
        assert max(step.relative_force_residual,step.relative_energy_residual,step.relative_memory_residual)<1e-12
    assert_allclose(np.array([s.q for s in states]),reference['q'],atol=1e-13)
    restore=GraphState.restore(states[0].checkpoint());replay=g.advance(restore,forces[1],.3)
    assert_array_equal(replay.state.q,states[1].q);assert_array_equal(replay.state.memory,states[1].memory)
    event=g.advance(old,np.ones(6),0).state
    assert event.time_s==old.time_s;assert_array_equal(event.memory,old.memory)


def test_immutable_operator_binding_and_failure_rollback():
    g=graph();old=g.state();payload=old.checkpoint()
    for a in (old.q,old.memory,g.km,g._K0.data,copy.deepcopy(old).memory):
        with pytest.raises(ValueError):a.setflags(write=True)
    with pytest.raises(AttributeError):g.km=np.ones_like(g.km)
    with pytest.raises(ValueError):replace(g,km=g.km*2)
    with pytest.raises(ValueError):replace(g,unknown_fraction=np.full((3,2),.5))
    bad=graph(rates=np.full((3,2,2),.2))
    with pytest.raises(ValueError):bad.validate_state(old)
    exposed=g.operators();exposed['K0'].data[:]=0
    assert np.linalg.norm(g.operators()['K0'].data)>0
    with pytest.raises(ValueError):g.advance(old,np.full(6,np.nan),.3)
    with pytest.raises(RuntimeError):g.advance(old,np.arange(6.),.3,acceptance_tolerance=1e-30)
    assert old.checkpoint()==payload


def test_pickle_roundtrip_retains_immutable_buffers_and_bindings():
    g=graph();old=g.state(memory=np.linspace(.01,.03,len(g.km)))
    model=pickle.loads(pickle.dumps(g));state=pickle.loads(pickle.dumps(old))
    for a in (state.q,state.memory,model.km,model.inverse_tau,model._K0.data,model._K0.indices,model._K0.indptr,model.unknown_fraction):
        with pytest.raises(ValueError):a.setflags(write=True)
    model.validate_state(state)
    a=g.advance(old,np.ones(6),.3);b=model.advance(state,np.ones(6),.3)
    assert_array_equal(a.state.q,b.state.q);assert_array_equal(a.state.memory,b.state.memory)
    with pytest.raises(ValueError):replace(model,inverse_tau=model.inverse_tau*2)


def test_dense_resource_guard_precedes_host_allocations(monkeypatch):
    from scipy.sparse import csr_matrix
    from vkm_world.representations.world0 import scalar_graph_torch as module
    B=csr_matrix((np.ones(3),(np.arange(3),np.arange(3))),shape=(3,100000))
    original_zeros=module.np.zeros
    def forbidden(shape,*args,**kwargs):
        # Sparse validation may allocate O(nb) boolean masks; prohibit the
        # large dense carrier/operator buffers targeted by this resource cap.
        if np.prod(shape)>1000:raise AssertionError('large dense zeros allocated before preflight')
        return original_zeros(shape,*args,**kwargs)
    monkeypatch.setattr(module.np,'zeros',forbidden)
    with pytest.raises(MemoryError):module._compressed_incidence(B,max_dense_bytes=1)


@pytest.mark.parametrize('distance',[np.nan,np.inf,-1.])
def test_invalid_edges(distance):
    d=inputs();d['edges']=[(0,1,np.ones(2),distance)]
    with pytest.raises(ValueError):build_world(**d)


def test_invalid_fractional_ids_and_partition():
    d=inputs();d['edges']=[(.5,1,np.ones(2),1.)]
    with pytest.raises(ValueError):build_world(**d)
    for labels in ([0.,1.2],[0,2],[-1,0]):
        with pytest.raises(ValueError):restriction(labels,2)


def test_lazy_torch_import_and_cpu_torch_path():
    # Module can be imported without torch; allocation only occurs on execution.
    from vkm_world.representations.world0.scalar_graph_torch import run_torch
    pytest.importorskip('torch')
    g=graph();old=g.state(memory=np.linspace(.01,.03,len(g.km)))
    f=np.array([[1,0,2,0,1,0],[0,0,0,0,0,0],[2,0,1,0,3,0.]])
    out,record=run_torch(g,old,f,.3,device='cpu')
    assert record['cpu_oracle']['status']=='CHECKED' and record['precision']=='float64'
    restart,_=run_torch(g,GraphState.restore(out[0].state.checkpoint()),f[1:],.3,device='cpu')
    for a,b in zip(out[1:],restart):assert_array_equal(a.state.q,b.state.q);assert_array_equal(a.state.memory,b.state.memory)
    with pytest.raises(MemoryError):run_torch(g,old,f,.3,max_dense_bytes=1)
    with pytest.raises(ValueError):run_torch(g,old,f,.3,precision='float32')
    with pytest.raises(RuntimeError):run_torch(g,old,f,.3,device='cuda',cuda_lease_verifier=None)
    assert old.time_s==0
