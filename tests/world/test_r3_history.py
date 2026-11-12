import numpy as np
import pytest
from scipy.sparse import csr_matrix
from vkm_world.representations.world0.multiseam import ElasticMesh
from vkm_world.representations.world0.constitutive import Nishihara,Lubby2
from vkm_world.representations.world0.constitutive_history import MaterialGroup,ConstitutiveHistory,StepFailure
from vkm_world.representations.world0.hydromechanics import WaterModel


def make(*,water=False,events=False,strain_limit=.05,variant=1.):
    xy=np.array([[0,0],[1,0],[1,1],[0,1]])
    xyz=np.vstack([np.column_stack((xy,np.full(4,z))) for z in (0,1,2)])
    cells=np.array([[0,1,2,3,4,5,6,7],[4,5,6,7,8,9,10,11]])
    mesh=ElasticMesh(xyz,cells,np.full(2,3e7),np.full(2,.25),np.arange(4))
    K=2e7;G=1.2e7
    laws=[Nishihara(K,G,K*2,G*2,1e9,1e9,1e10,1e4),
          Lubby2(K,G,G*3,1e9,1e10*variant,mK=1e-7,mM=1e-7)]
    groups=[MaterialGroup(str(i),np.array([i]),m,'SYNTHETIC_NOT_FIELD') for i,m in enumerate(laws)]
    hydro=None
    if water:
        B=csr_matrix(np.full((len(mesh.free_dofs),1),1e-5))
        hydro=WaterModel(B,[1e-8],[1.],[.5],[1e6],np.zeros(len(mesh.free_dofs)),[],0,1e-10,['one'],'synthetic')
    rows=[];meta={}
    if events:
        rows=[dict(time_s=2.,layer_index=0,patch_index=0,action='excavate',increment_volume_fraction=.1,
                   layer_patch_reference_volume_m3=1.,event_id='cut',body_mass_change_kg=-210.),
              dict(time_s=3.,layer_index=0,patch_index=0,action='backfill',increment_volume_fraction=.1,
                   layer_patch_reference_volume_m3=1.,event_id='fill',body_mass_change_kg=0.)]
        meta={'fill':dict(fill_fraction=.7,fill_density_kg_m3=1800.,fill_young_pa=2.8e7)}
    return ConstitutiveHistory(mesh,groups,density=[2100,2200],gravity=9.81,support_cells=np.array([0]),
        layer_id=[0,1],patch_id=[0,0],event_rows=rows,event_metadata=meta,water=hydro,
        world_identity='synthetic-two-layers',strain_limit=strain_limit,force_atol_n=1e-8)


def test_actual_joint_tensor_equilibrium_and_memory():
    model=make(water=True)
    state,d=model.initial()
    assert d['force_error_n']<1e-5
    nxt,d=model.advance(state,1.)
    assert d['relative_force_error']<1e-9
    assert d['hydraulic']['global_balance_m3']<1e-8
    assert np.linalg.norm(nxt.materials[0].memory['kelvin'])>0
    assert np.linalg.norm(nxt.materials[1].memory['maxwell'])>0
    assert np.max(abs(state.materials[0].memory['kelvin']))==0
    assert not nxt.displacement_m.flags.writeable
    assert d['material_energy_j']['0']['physical_dissipation']>=0


def test_restart_matches_uninterrupted_joint_fields(tmp_path):
    model=make(water=True)
    state,_=model.initial(); state,_=model.advance(state,1.)
    path=tmp_path/'state.npz';model.checkpoint(path,state)
    reopened=model.reopen(path)
    one,_=model.advance(state,2.);two,_=model.advance(reopened,2.)
    np.testing.assert_array_equal(one.displacement_m,two.displacement_m)
    for a,b in zip(one.materials,two.materials):
        for k in a.memory: np.testing.assert_array_equal(a.memory[k],b.memory[k])
    np.testing.assert_array_equal(one.water.pressure_pa,two.water.pressure_pa)
    fields=model.fields(two)
    assert fields['strain_engineering'].shape==(2,8,6)
    assert fields['reference_skeleton_stress_engineering_pa'].shape==(2,8,6)


def test_event_keeps_remaining_material_memory_and_birth(tmp_path):
    model=make(water=True,events=True)
    state,_=model.initial(); pre,_=model.advance(state,2.)
    post,diag=model.apply_events(pre)
    assert post.event_cursor==1 and post.support[0]==.9
    assert diag['event_removed_rock_mass_kg']==pytest.approx(210.)
    for a,b in zip(pre.materials,post.materials):
        for k in a.memory: np.testing.assert_array_equal(a.memory[k],b.memory[k])
    state,_=model.advance(post,3.); state,diag=model.apply_events(state)
    assert len(state.cohorts)==1 and diag['event_added_fill_mass_kg']>0
    model.checkpoint(tmp_path/'birth.npz',state)
    restored=model.reopen(tmp_path/'birth.npz')
    np.testing.assert_array_equal(state.cohorts[0].mass_kg,restored.cohorts[0].mass_kg)


def test_failed_state_keeps_memory_and_candidate(tmp_path):
    model=make(strain_limit=1e-12)
    with pytest.raises(StepFailure) as error: model.initial()
    failure=error.value
    assert 'applicability' in str(failure)
    assert failure.displacement is not None
    assert np.max(abs(failure.previous.materials[0].memory['kelvin']))==0
    assert failure.candidate_state is not None
    model.checkpoint(tmp_path/'rejected.npz',failure.candidate_state,accepted=False)
    with pytest.raises(ValueError,match='rejected trial'):model.reopen(tmp_path/'rejected.npz')


def test_material_change_invalidates_restart(tmp_path):
    model=make();state,_=model.initial();model.checkpoint(tmp_path/'state.npz',state)
    changed=make(variant=2.)
    with pytest.raises(ValueError,match='dependency'):changed.reopen(tmp_path/'state.npz')


def test_event_stop_exact_and_reject_duplicate():
    model=make(events=True);state,_=model.initial()
    with pytest.raises(ValueError,match='next unapplied'):model.advance(state,2.1)
    with pytest.raises(ValueError,match='unapplied exact'):model.apply_events(state)


def test_reaction_of_other_layer_to_event_is_coupled():
    model=make(events=True);state,_=model.initial();state,_=model.advance(state,2.)
    post,_=model.apply_events(state)
    # Upper layer was not excavated, but its displacement is part of same solve.
    assert np.linalg.norm(post.displacement_m[24:]-state.displacement_m[24:])>1e-8


def test_group_overlap_rejected():
    model=make()
    with pytest.raises(ValueError,match='overlapping'):
        ConstitutiveHistory(model.mesh,[model.groups[0],model.groups[0]],density=[2100,2200],gravity=9.81,
            support_cells=np.array([0]),layer_id=[0,1],patch_id=[0,0],world_identity='bad')


def test_accepted_joint_arrays_cannot_be_unfrozen():
    model=make(water=True);state,_=model.initial()
    for a in (state.displacement_m,state.support,model.groups[0].cells,state.water.pressure_pa,state.water.dilation_m3):
        with pytest.raises(ValueError):a.flags.writeable=True


def test_maxwell_continuation_matches_prior_backward_euler_tensor():
    from vkm_world.representations.world0.constitutive import DeviatoricMaxwell
    from vkm_world.representations.world0.constitutive import H,P
    K,G=2e9,7e8;eta=np.array([1e14,2e14]);dt=7e5
    law=DeviatoricMaxwell(K,G,eta)
    old=law.initial_state((2,3));strain=np.random.default_rng(191).normal(0,.001,(2,3,6))
    prior=law.trial(old,strain,dt).state
    newstrain=strain*.8
    ratio=(dt*G/eta)[:,None,None]
    expected=(prior.memory['creep']+ratio*np.einsum('ij,egj->egi',P,newstrain))/(1+ratio)
    trial=law.trial(prior,newstrain,dt)
    # Equivalent closed-form formulas use different arithmetic/projector
    # orders. At strain~1e-3 their measured roundoff is below1e-17 absolute;
    # this comparison does not assert bitwise identity of those expressions.
    np.testing.assert_allclose(trial.state.memory['creep'],expected,rtol=1e-12,atol=1e-16)
    np.testing.assert_allclose(trial.stress,np.einsum('ij,egj->egi',3*K*H+2*G*P,newstrain-expected),rtol=1e-12,atol=1e-7)
    instant=law.instantaneous(prior,newstrain)
    np.testing.assert_array_equal(instant.state.memory['creep'],prior.memory['creep'])


def test_nishihara_joint_time_refinement_has_convergent_displacement():
    # Fixed same world/load; independent step partitions, including all memories.
    results=[]
    for steps in (2,4,8):
        model=make();state,_=model.initial()
        for t in np.linspace(0,100.,steps+1)[1:]:state,_=model.advance(state,float(t))
        results.append(state.displacement_m)
    coarse=np.linalg.norm(results[1]-results[0]);fine=np.linalg.norm(results[2]-results[1])
    assert coarse>0 and fine/coarse<.8


def test_general_tangent_pressure_schur_matches_independent_dense_system():
    K=np.array([[9.,2.,1.],[-1.,8.,.5],[.1,2.,7.]])
    B=np.array([[.03,.01],[.02,-.01],[.01,.04]])
    water=WaterModel(csr_matrix(B),[.1,.2],[2.,3.],[.4,.5],[1e6,1e6],np.zeros(3),
        [[0,1,.02]],0,.01,['a','b'],'general-synthetic')
    rhs=np.array([.2,.1,.3]);dt=.5
    actual=water.trial(water.initial(),dt,lambda r:np.linalg.solve(K,r),np.diag(K),rhs,symmetric_tangent=False)
    H=np.diag(water.storage)+dt*water.L.toarray()
    full=np.block([[K,-B],[B.T,H]])
    expected=np.linalg.solve(full,np.r_[rhs,np.zeros(2)])
    np.testing.assert_allclose(actual.displacement_free,expected[:3],rtol=1e-11,atol=1e-13)
    np.testing.assert_allclose(actual.state.pressure_pa,expected[3:],rtol=1e-11,atol=1e-13)
    wrong=np.linalg.solve(np.block([[(K+K.T)/2,-B],[B.T,H]]),np.r_[rhs,np.zeros(2)])
    assert np.max(abs(wrong-expected))>1e-3


def test_inplace_operator_mutation_is_rejected_before_state_use(tmp_path):
    model=make(water=True);state,_=model.initial()
    model.water.B.data[0]*=1.1
    with pytest.raises(ValueError,match='operator/input changed'):model.advance(state,1.)
    with pytest.raises(ValueError,match='operator/input changed'):model.checkpoint(tmp_path/'bad.npz',state)


def test_identity_metadata_mutation_does_not_rewrite_binding():
    model=make();before=model.identity_payload
    external=model.identity_payload;external['world']='rewritten'
    assert model.identity_payload==before
