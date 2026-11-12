import json
from dataclasses import asdict
import numpy as np
import pytest
from scipy.linalg import expm
from vkm_world.representations.world0.multiphysics import (
 ContactState,contact_trial,BirthBackfillState,birth_backfill_trial,CohesiveState,
 cohesive_damage_trial,incidence,conservative_hm_step,directional_conductance,
 transport_step,callahan_pressure_solution_component,dissolution_step)


def test_contact_stick_slip_open_and_force_pair():
    a=contact_trial(-.01,[.001,.002],ContactState(),1e8,2e7,.3)
    assert a['status']=='STICK'
    assert np.allclose(a['traction']+a['opposite_traction'],0)
    b=contact_trial(-.01,[.1,.2],a['state'],1e8,2e7,.3)
    assert b['status']=='SLIP'
    assert np.linalg.norm(b['tau'])==pytest.approx(.3*b['p'])
    assert b['friction_J_m2']>0
    c=contact_trial(.02,[.1,.2],b['state'],1e8,2e7,.3)
    assert c['stored_J_m2']==0 and c['opening_heat_J_m2']>0
    d=contact_trial(-.01,[.1,.2],c['state'],1e8,2e7,.3)
    assert np.array_equal(d['tau'],[0,0])


@pytest.mark.parametrize('slip',[[.001,.002],[.1,.2]])
def test_contact_tangent_finite_difference(slip):
    x=np.r_[.01,slip]; s=ContactState()
    def evaluate(x):
        r=contact_trial(-x[0],x[1:],s,1e8,2e7,.3)
        return np.r_[r['p'],r['tau']],r['tangent']
    y,J=evaluate(x); h=1e-7
    fd=np.column_stack([(evaluate(x+np.eye(3)[j]*h)[0]-evaluate(x-np.eye(3)[j]*h)[0])/(2*h) for j in range(3)])
    assert np.linalg.norm(fd-J)/np.linalg.norm(J)<1e-5


def test_contact_trial_rollback_and_orientation_negative():
    s=ContactState(); before=asdict(s)
    contact_trial(-.01,[.1,0],s,1e8,2e7,.3)
    assert asdict(s)==before
    with pytest.raises(ValueError): contact_trial(-.01,[0,0],s,1,1,.3,opposite=(0,0,1))
    with pytest.raises(ValueError): contact_trial(-.01,[0,0],s,-1,1,.3)


def test_contact_cycle_positive_work():
    s=ContactState(); prev=np.zeros(2); work=0; oldtau=np.zeros(2)
    for z in np.r_[np.linspace(0,.1,1001),np.linspace(.1,-.1,2001),np.linspace(-.1,0,1001)]:
        slip=np.array([z,0]); r=contact_trial(-.01,slip,s,1e8,2e7,.3)
        work+=.5*(oldtau+r['tau'])@(slip-prev)
        s=r['state']; prev=slip; oldtau=r['tau']
    # Normal spring energy is constant; account only tangent elastic energy.
    tangent_energy=.5*np.dot(r['tau'],r['tau'])/2e7
    assert abs(work-tangent_energy-s.dissipation_J_m2)/work<2e-3


def test_birth_reference_age_and_unloading_memory():
    s=BirthBackfillState(10,2)
    args=(1.,.05,1e7,.5,1e8)
    assert not birth_backfill_trial(9,3,s,*args)['active']
    a=birth_backfill_trial(10,2,s,*args)
    assert a['stress_Pa']==0 and a['age_s']==0
    b=birth_backfill_trial(20,2.15,a['state'],*args)
    c=birth_backfill_trial(30,2.1,b['state'],*args)
    assert b['stress_Pa']>c['stress_Pa']
    assert c['state'].eps_max==b['state'].eps_max
    assert c['state'].birth_convergence_m==2
    restarted=BirthBackfillState(**json.loads(json.dumps(asdict(c['state']))))
    assert birth_backfill_trial(40,2.16,restarted,*args)==birth_backfill_trial(40,2.16,c['state'],*args)
    with pytest.raises(ValueError): birth_backfill_trial(40,2.6,s,*args)
    with pytest.raises(ValueError): birth_backfill_trial(20,2.15,s,1,.05,1e7,.5,1e7)


def test_backfill_cycle_energy_and_irreversible_compaction():
    s=BirthBackfillState(0,0); work=0.; prev=0.; oldstress=0.; previous_diss=0.
    for v in np.r_[np.linspace(0,.12,2001),np.linspace(.12,0,2001),np.linspace(0,.1,2001)]:
        r=birth_backfill_trial(1,v,s,1.,.02,1e7,.5,1e8)
        work+=.5*(oldstress+r['stress_Pa'])*(v-prev)
        assert r['dissipated_J_m3']>=previous_diss-1e-8
        s=r['state']; oldstress=r['stress_Pa']; prev=v; previous_diss=r['dissipated_J_m3']
    assert abs(work-r['stored_J_m3']-r['dissipated_J_m3'])/work<2e-3
    assert s.eps_max==pytest.approx(.1)


@pytest.mark.parametrize('h',[.1,.5,1.,2.])
def test_damage_energy_mesh_width_and_no_healing(h):
    E=1e9; ft=1e6; Gc=2000.; k=E/h; wf=2*Gc/ft
    s=CohesiveState(); w=np.linspace(0,wf,10001); tr=[]
    for wi in w:
        r=cohesive_damage_trial(wi,s,k,ft,Gc); s=r['state']; tr.append(r['traction_Pa'])
    assert np.trapezoid(tr,w)==pytest.approx(Gc,rel=2e-3)
    assert s.damage==1 and s.dissipation_J_m2==Gc
    r=cohesive_damage_trial(wf/2,s,k,ft,Gc)
    assert r['traction_Pa']==0 and r['state'].damage==1


def hm_data():
    K=np.array([[4.,-1.],[-1.,3.]])*1e8
    H=np.diag([.7,.8]); S=np.diag([2.,3.])*1e-9
    B=incidence(2,[(0,1)]); g=np.array([1e-10])
    return K,H,S,B,g


def test_hm_undrained_analytic_and_drained_limit():
    K=np.array([[1e8]]); H=np.array([[.8]]); S=np.array([[2e-9]])
    args=(K,H,S,np.zeros((1,0)),np.zeros(0),np.zeros(1),np.zeros(1),np.array([-1e5]),1.)
    r=conservative_hm_step(*args)
    exact=-.8*(-1e5/1e8)/(2e-9+.8**2/1e8)
    assert r['p'][0]==pytest.approx(exact)
    assert abs(r['m'][0])<1e-18
    dr=conservative_hm_step(*args,reservoir_g=[1e3],reservoir_p=[0.])
    assert abs(dr['p'][0])<1e-6
    assert dr['u'][0]==pytest.approx(-1e-3,rel=1e-8)


def test_hm_mass_energy_restart_neighbor_and_no_flow():
    K,H,S,B,g=hm_data(); u=np.zeros(2); p=np.zeros(2); f=np.array([-1e5,0.])
    for n in range(20):
        r=conservative_hm_step(K,H,S,B,g,u,p,f,1.)
        scale=max(abs(r['external_work_J']),r['energy_J'],1.)
        assert np.linalg.norm(r['mass_residual_m3'])<1e-14
        assert abs(r['energy_residual_J'])/scale<1e-10
        assert np.linalg.norm(r['force_residual_N'])<1e-9
        u,p=r['u'],r['p']
    assert abs(p[1])>1e-8 and abs(u[1])>1e-8
    cp=json.loads(json.dumps({'u':u.tolist(),'p':p.tolist()}))
    a=conservative_hm_step(K,H,S,B,g,u,p,f,1.)
    b=conservative_hm_step(K,H,S,B,g,cp['u'],cp['p'],f,1.)
    assert np.array_equal(a['u'],b['u']) and np.array_equal(a['p'],b['p'])


def test_hm_boundary_energy_and_pressure_reference():
    K,H,S,B,g=hm_data(); u=np.zeros(2); p=np.zeros(2)
    r=conservative_hm_step(K,H,S,B,g,u,p,[0,0],2.,reservoir_g=[1e-8,0],reservoir_p=[1e5,0])
    assert abs(r['energy_residual_J'])<1e-8
    assert np.sum(r['m'])==pytest.approx(2*np.sum(r['boundary_in_m3_s']),abs=1e-14)
    # Constant pressure with compatible elastic displacement is stationary.
    pp=np.ones(2)*12345; uu=np.linalg.solve(K,H@pp)
    rr=conservative_hm_step(K,H,S,B,g,uu,pp,[0,0],2.)
    assert np.allclose(rr['p'],pp,rtol=1e-12)


def test_hm_negative_bad_topology_storage_and_anisotropy():
    K,H,S,B,g=hm_data(); bad=B.copy(); bad[1,0]=1
    with pytest.raises(ValueError): conservative_hm_step(K,H,S,bad,g,[0,0],[0,0],[0,0],1)
    with pytest.raises(ValueError): conservative_hm_step(K,H,-S,B,g,[0,0],[0,0],[0,0],1)
    with pytest.raises(ValueError): incidence(2,[(0,2)])
    k=np.diag([1.,2.,10.])*1e-15
    gx=directional_conductance(k,[1,0,0],1,1,.001)
    gz=directional_conductance(k,[0,0,1],1,1,.001)
    assert gz/gx==pytest.approx(10)


def test_multirate_hidden_storage_mass_and_exact_limit():
    V=np.array([1.,2.,3.]); B=incidence(3,[(0,1),(0,2)])
    g=np.array([.2,.03]); c=np.array([1.,0.,0.]); initial=c.copy()
    L=(B*g)@B.T; A=-np.diag(1/V)@L; dt=.002
    for _ in range(500):
        r=transport_step(V,c,B,g,[0,0],dt); c=r['concentration']
        assert abs(r['mass_residual_mol'])<1e-13 and min(c)>=0
    exact=expm(A)@initial
    assert np.linalg.norm(c-exact)<1e-4
    assert V@c==pytest.approx(1,abs=1e-12)
    assert c[1]>c[2]>0


def test_transport_upwind_positivity_and_zero_exchange():
    V=np.ones(3); c=np.array([1.,0.,0.]); B=incidence(3,[(0,1),(1,2)])
    r=transport_step(V,c,B,[0,0],[1,1],100.)
    assert min(r['concentration'])>=0 and sum(r['concentration'])==pytest.approx(1)
    z=transport_step(V,c,B,[0,0],[0,0],100.)
    assert np.array_equal(z['concentration'],c)


def test_callahan_component_dense_and_singular_limits():
    # Synthetic internally compatible constants; not a fitted SKRU-1 set.
    args=(.36,1e3,2.,1.,3000.,293.,2.,5.)
    r=callahan_pressure_solution_component(-.1,*args)
    assert r>0
    dense=callahan_pressure_solution_component(np.log(.64),*args)
    assert abs(dense)<1e-20
    with pytest.raises(ValueError): callahan_pressure_solution_component(0.,*args)
    assert callahan_pressure_solution_component(-.1,.36,0,2.,1.,3000.,293.,2.,5.)==0
    with pytest.raises(ValueError): callahan_pressure_solution_component(np.log(.9),.1,1e3,2.,1.,3000.,293.,2.,5.)


@pytest.mark.parametrize('ions',[.1,100.])
def test_dissolution_precipitation_component_water_solid_balance(ions):
    init=dict(solid_mol=100.,Na_mol=ions,Cl_mol=ions,water_kg=1000*(.01-ions*16e-6),pore_m3=.01,bulk_m3=.1)
    r=dissolution_step(**init,dt=100.,rate_mol_s=.1,csat_mol_m3=1000.,solid_molar_m3=27e-6,aqueous_partial_molar_m3=16e-6)
    assert r['solid_mol']+r['Na_mol']==pytest.approx(init['solid_mol']+ions)
    assert r['solid_mol']+r['Cl_mol']==pytest.approx(init['solid_mol']+ions)
    assert r['pore_m3']+r['solid_mol']*27e-6==pytest.approx(.01+100*27e-6)
    def mass(s): return s['water_kg']+.022989769*s['Na_mol']+.03545*s['Cl_mol']+.058439769*s['solid_mol']
    assert mass(r)-mass(init)==pytest.approx(r['water_in_kg'],abs=1e-12)
    assert np.sign(r['reaction_mol'])==(1 if ions<10 else -1)
    assert r['water_kg']/1000+r['Na_mol']*16e-6==pytest.approx(r['pore_m3'],rel=1e-12)


@pytest.mark.parametrize('water,cl',[(0.,.1),(10.,.1),(9.9984,.2)])
def test_dissolution_rejects_unsaturated_or_uncharged_inputs(water,cl):
    with pytest.raises(ValueError):
        dissolution_step(100.,.1,cl,water,.01,.1,100.,.1,1000.,27e-6,16e-6)


def test_dissolution_rejects_excess_solid_volume():
    with pytest.raises(ValueError):
        dissolution_step(1e6,.1,.1,9.9984,.01,.1,100.,.1,1000.,27e-6,16e-6)
