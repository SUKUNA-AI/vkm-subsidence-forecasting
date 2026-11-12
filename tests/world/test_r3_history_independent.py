"""Independent small-world controls; no imports from author's test fixtures."""
from dataclasses import replace
import numpy as np
import pytest
from numpy.testing import assert_allclose,assert_array_equal
from scipy.sparse import csr_matrix
from vkm_world.representations.world0.multiseam import ElasticMesh
from vkm_world.representations.world0.constitutive import Nishihara
from vkm_world.representations.world0.constitutive_history import ConstitutiveHistory,MaterialGroup,StepFailure
from vkm_world.representations.world0.hydromechanics import WaterModel,WaterState

def specimen(*,water=False,rows=(),meta=None):
    # Rectangular 2x1x1 cell, volume2; bottom fixed, actual engineering shears.
    xyz=np.array([[0,0,0],[2,0,0],[2,1,0],[0,1,0],[0,0,1],[2,0,1],[2,1,1],[0,1,1]],float)
    mesh=ElasticMesh(xyz,np.arange(8)[None],np.array([6e7]),np.array([.25]),np.arange(4))
    law=Nishihara(4e7,2.4e7,8e7,4.8e7,8e8,4.8e8,1e12,1e9)
    group=MaterialGroup('independent-salt',np.array([0]),law,'SYNTHETIC_MECHANICS_CONTROL')
    w=None
    if water:
        B=np.zeros((len(mesh.free_dofs),2)); B[2::3,0]=.08; B[0::3,1]=.03
        w=WaterModel(B,[2e-6,3e-6],[2.,3.],[.6,.7],[1e7,1e7],
            np.linspace(-2e-4,1e-4,len(mesh.free_dofs)),[[0,1,1e-7]],0,2e-7,['upper','lower'],'review-two-cell-water')
    return ConstitutiveHistory(mesh,[group],density=[2300.],gravity=9.81,support_cells=np.array([0]),
        layer_id=[7],patch_id=[3],event_rows=rows,event_metadata=meta,water=w,
        world_identity='independent-rectangle',force_atol_n=1e-7,strain_limit=.1)

def event(action,identifier,fraction,time=1.):
    return dict(time_s=time,layer_index=7,patch_index=3,action=action,increment_volume_fraction=fraction,
        layer_patch_reference_volume_m3=2.,event_id=identifier,
        body_mass_change_kg=-4600*fraction if action=='excavate' else 0.)

def test_initial_coupled_reference_pressure_matches_dense_full_block():
    m=specimen(water=True); state,_=m.initial();w=m.water
    K=m.mesh.matrix[m.free][:,m.free].toarray();B=w.B.toarray()
    f=m.body_force(np.ones(1),())[m.free]
    independent=np.linalg.solve(np.block([[K,-B],[B.T,np.diag(w.storage)]]),
        np.r_[f,B.T@w.reference_displacement_free])
    assert_allclose(state.displacement_m[m.free],independent[:len(m.free)],rtol=2e-10,atol=1e-13)
    assert_allclose(state.water.pressure_pa,independent[len(m.free):],rtol=2e-10,atol=1e-8)
    assert_allclose(w.storage*state.water.pressure_pa+state.water.dilation_m3,0,atol=1e-12)

@pytest.mark.parametrize('symmetric',[True,False])
@pytest.mark.parametrize('dt,event_volume',[(0.,np.array([.004,-.002])),(.7,np.zeros(2))])
def test_schur_nonzero_prefix_reference_and_event_against_dense(symmetric,dt,event_volume):
    K=np.array([[11.,2.,1.],[2.,8.,.5],[1.,.5,6.]])
    if not symmetric:K[1,0]=-.4
    B=np.array([[.1,-.03],[.07,.04],[-.02,.06]])
    ref=np.array([.1,-.02,.03]); old=WaterState([2.,-1.],[.005,-.009],'dense')
    w=WaterModel(B,[.2,.15],[2.,2.],[.6,.8],[100.,100.],ref,[[0,1,.03]],1,.04,['a','b'],'dense')
    f=np.array([.4,.7,-.2]);target=3.
    out=w.trial(old,dt,lambda r:np.linalg.solve(K,r),np.diag(K),f,
        well_target_pa=target,event_volume_m3=event_volume,symmetric_tangent=symmetric)
    H=np.diag(w.storage)+dt*w.L.toarray()
    rhs=w.storage*old.pressure_pa+old.dilation_m3+event_volume+B.T@ref
    rhs[1]+=dt*.04*target
    expected=np.linalg.solve(np.block([[K,-B],[B.T,H]]),np.r_[f,rhs])
    assert_allclose(out.displacement_free,expected[:3],rtol=2e-11,atol=1e-12)
    assert_allclose(out.state.pressure_pa,expected[3:],rtol=2e-11,atol=1e-12)
    assert out.diagnostics['global_balance_m3']<1e-11
    assert abs(out.diagnostics['fluid_energy_error_j'])<1e-10

def test_engineering_affine_shear_and_material_power():
    m=specimen();s,_=m.initial()
    gradient=np.array([[.001,.004,-.002],[.003,-.001,.006],[.005,-.004,.002]])
    u=(m.mesh.xyz@gradient.T).ravel()
    eps=np.array([.001,-.001,.002,.007,.002,.003])
    r=m.response(s,u,0.,np.ones(1),())
    assert_allclose(r['strain'],np.broadcast_to(eps,(1,8,6)),atol=1e-17)
    sig=m.mesh.D[0]@eps
    assert_allclose(r['stress'],np.broadcast_to(sig,(1,8,6)),rtol=1e-14,atol=1e-8)
    assert_allclose(u@r['force'],2*eps@sig,rtol=1e-13)
    assert_allclose(r['matrix'].toarray(),m.mesh.matrix.toarray(),rtol=1e-13,atol=1e-8)

def test_simultaneous_valid_cut_fill_dt0_memory_mass_and_restart(tmp_path):
    rows=[event('excavate','cut',.2),event('backfill','fill',.2)]
    meta={'fill':dict(fill_fraction=.8,fill_density_kg_m3=1800.,fill_young_pa=1e7)}
    m=specimen(water=True,rows=rows,meta=meta);s,_=m.initial();pre,_=m.advance(s,1.)
    post,d=m.apply_events(pre)
    assert post.event_cursor==1
    for key in pre.materials[0].memory:assert_array_equal(pre.materials[0].memory[key],post.materials[0].memory[key])
    ref=m.strain(pre.displacement_m)[0,:,2]
    mass=1800*.16*np.sum(m.weights[0]*(1+ref))
    assert_allclose(post.cohorts[0].mass_kg,[mass],rtol=1e-14)
    assert_allclose(d['event_removed_rock_mass_kg'],920.)
    assert_allclose(-m.body_force(post.support,post.cohorts).reshape(-1,3)[:,2].sum()/9.81,3680+mass)
    assert_allclose(post.water.dilation_m3-pre.water.dilation_m3+
                    m.water.storage*(post.water.pressure_pa-pre.water.pressure_pa),0,atol=1e-11)
    m.checkpoint(tmp_path/'post.npz',post);restored=m.reopen(tmp_path/'post.npz')
    a,_=m.advance(post,2.);b,_=m.advance(restored,2.)
    assert_array_equal(a.displacement_m,b.displacement_m)
    for key in a.materials[0].memory:assert_array_equal(a.materials[0].memory[key],b.materials[0].memory[key])
    with pytest.raises(ValueError):m.apply_events(post)

def test_failed_event_retains_previous_and_actual_candidate():
    row=event('excavate','wrong-mass',.2);row['body_mass_change_kg']=-1.
    m=specimen(rows=[row]);s,_=m.initial();pre,_=m.advance(s,1.)
    with pytest.raises(StepFailure) as out:m.apply_events(pre)
    assert out.value.previous is pre
    assert out.value.candidate_state is not None
    assert_array_equal(pre.support,[1.]);assert pre.event_cursor==0

def test_backfill_without_excavated_capacity_must_be_rejected():
    m=specimen(rows=[event('backfill','impossible-fill',.5)],
        meta={'impossible-fill':dict(fill_fraction=1.,fill_density_kg_m3=1800.,fill_young_pa=1e7)})
    s,_=m.initial();pre,_=m.advance(s,1.)
    with pytest.raises((ValueError,StepFailure)):m.apply_events(pre)

def test_invalid_event_cursor_cannot_skip_source_event():
    m=specimen(rows=[event('excavate','cut',.2)]);s,_=m.initial()
    # Public immutable state constructor can carry a stale/scrambled cursor.
    bad=replace(s,event_cursor=1)
    with pytest.raises((ValueError,StepFailure)):m.advance(bad,2.)

def test_derived_operator_mutation_cannot_reuse_identity():
    m=specimen();s,_=m.initial();identity=m.identity
    m.b*=1.1
    with pytest.raises((ValueError,StepFailure)):m.advance(s,1.)
    assert m.identity==identity
