"""Independent controls, analytic identities and finite-difference Jacobians.

Run with: python -m pytest -q test_constitutive.py. No GPU or mine calibration.
"""
import json
import math
import numpy as np
import pytest
from scipy.integrate import quad, solve_ivp
from scipy.special import erfcx

from vkm_world.representations.world0.constitutive import (AbelDirect, AbelSOE, Nishihara, Lubby2, DeviatoricMaxwell,
    ConstitutiveState, delta_to_seconds, strain_to_mandel, stress_to_mandel,
    strain_to_engineering, stress_to_engineering, tangent_to_engineering)


def nish():
    # Same Poisson ratio and retardation time in both Kelvin projections.
    return Nishihara(K0=10e9,G0=6e9,K1=4e9,G1=2.4e9,
        eta_bulk=4e12,eta_shear=2.4e12,eta_vp=9e12,yield_stress=2e6)


def lubby(mG=-.02e-6):
    return Lubby2(K0=10e9,G0=6e9,GK0=2.4e9,etaK0=2.4e12,
        etaM0=9e12,mK=-.1e-6,mM=-.15e-6,mG=mG)


def abel(cls=AbelDirect, **kwargs):
    return cls(K=10e9,G=6e9,alpha_bulk=.55,delta_bulk=.002,
        alpha_dev=.7,delta_dev=.01,**kwargs)


def test_mandel_work_and_shear_stiffness():
    rng=np.random.default_rng(31)
    sig=rng.normal(size=(9,6)); eps=rng.normal(size=(9,6))
    sm=stress_to_mandel(sig); em=strain_to_mandel(eps)
    np.testing.assert_allclose(np.sum(sig*eps,axis=-1),np.sum(sm*em,axis=-1),atol=2e-15)
    np.testing.assert_allclose(stress_to_engineering(sm),sig)
    np.testing.assert_allclose(strain_to_engineering(em),eps)
    C=np.eye(6)*12e9
    np.testing.assert_allclose(tangent_to_engineering(C)[3:,3:],np.eye(3)*6e9)
    de=rng.normal(size=6)
    np.testing.assert_allclose(tangent_to_engineering(C)@de,
        stress_to_engineering(C@strain_to_mandel(de)))


@pytest.mark.parametrize('model',[nish(),lubby(),abel(),abel(AbelSOE,t_min=.1,horizon=1e5)])
def test_immutable_trial_commit_revert_and_restart(model):
    s=model.initial_state((3,))
    before=json.dumps(s.to_dict(),sort_keys=True)
    eps=np.tile([.0008,-.0001,.0001,.0003,0,0],(3,1))
    trial=model.trial(s,eps,3.)
    assert json.dumps(s.to_dict(),sort_keys=True)==before
    assert model.revert(s,trial) is s
    with pytest.raises(ValueError): trial.state.strain[0,0]=1
    with pytest.raises(TypeError): trial.state.memory['other']=np.zeros(6)
    committed=model.commit(trial)
    restored=ConstitutiveState.from_dict(json.loads(json.dumps(committed.to_dict())))
    a=model.trial(committed,eps*.8,4.)
    b=model.trial(restored,eps*.8,4.)
    np.testing.assert_array_equal(a.stress,b.stress)
    np.testing.assert_array_equal(a.tangent,b.tangent)


@pytest.mark.parametrize('model',[nish(),lubby(),abel(),abel(AbelSOE,t_min=.1,horizon=1e5)])
def test_analytic_tangent_nonzero_memory_and_engineering(model):
    s=model.initial_state()
    s=model.commit(model.trial_stress(s,np.array([4e6,-1e6,2e6,1e6,.2e6,-.1e6]),20.))
    eps=np.array([.0006,-.00015,.0002,.00013,.00004,-.00002])
    t=model.trial(s,eps,15.)
    step=1e-8
    fd=np.column_stack([(model.trial(s,eps+np.eye(6)[i]*step,15.).stress-
                        model.trial(s,eps-np.eye(6)[i]*step,15.).stress)/(2*step) for i in range(6)])
    np.testing.assert_allclose(t.tangent,fd,rtol=2e-6,atol=3.)
    ee=strain_to_engineering(eps)
    fd_eng=np.column_stack([(stress_to_engineering(model.trial(s,strain_to_mandel(ee+np.eye(6)[i]*step),15.).stress)-
                            stress_to_engineering(model.trial(s,strain_to_mandel(ee-np.eye(6)[i]*step),15.).stress))/(2*step) for i in range(6)])
    np.testing.assert_allclose(tangent_to_engineering(t.tangent),fd_eng,rtol=2e-6,atol=3.)


def test_abel_constant_gamma_units_and_bulk_dev():
    m=abel(); sig=np.array([8e6,2e6,-1e6,3e6,0,1e6]); t=1832.
    tr=m.trial_stress(m.initial_state(),sig,t)
    mean=sig[:3].sum()/3; hydro=np.array([mean,mean,mean,0,0,0]); dev=sig-hydro
    # Derive normalized fractional integral independently through Gamma(beta+1).
    b=1-m.alpha_bulk; d=1-m.alpha_dev
    expected=hydro/(3*m.K)*(1+m.delta_bulk*math.gamma(b)*t**b/math.gamma(b+1))
    expected+=dev/(2*m.G)*(1+m.delta_dev*math.gamma(d)*t**d/math.gamma(d+1))
    np.testing.assert_allclose(tr.state.strain,expected,rtol=2e-15)
    delta_h=.014; alpha=.6
    delta_s=delta_to_seconds(delta_h,alpha,3600)
    assert delta_s*(7200)**(1-alpha)==pytest.approx(delta_h*2**(1-alpha))
    assert np.trace(np.diag(tr.state.strain[:3]))==pytest.approx(mean/m.K*(1+m.delta_bulk*t**b/b))


def test_abel_variable_nonproportional_history_independent_quadrature():
    m=abel(); state=m.initial_state(time_s=-7.)
    times=[-7.,-3.,0.,.5,4.,19.]
    loads=np.array([[3,1,2,.4,0,0],[0,0,0,0,0,0],[-1,2,1,0,.8,0],[4,-1,1,0,0,.3],[2,2,2,1,0,0]])*1e6
    for a,b,sig in zip(times[:-1],times[1:],loads):
        state=m.commit(m.trial_stress(state,sig,b-a))
    T=times[-1]; integrals=[]
    for alpha in [m.alpha_bulk,m.alpha_dev]:
        z=np.zeros(6)
        for a,b,sig in zip(times[:-1],times[1:],loads):
            z+=sig*quad(lambda u:(T-u)**(-alpha),a,b,epsabs=1e-9)[0]
        integrals.append(z)
    bstress=loads[-1]+m.delta_bulk*integrals[0]
    dstress=loads[-1]+m.delta_dev*integrals[1]
    mean=bstress[:3].sum()/3
    dev=dstress.copy();dev[:3]-=dstress[:3].sum()/3
    expect=np.array([mean,mean,mean,0,0,0])/(3*m.K)+dev/(2*m.G)
    np.testing.assert_allclose(state.strain,expect,rtol=2e-12,atol=1e-17)
    assert np.linalg.norm(state.strain)>0


def test_abel_prehistory_changes_relaxation_not_reset():
    m=abel(); old=m.initial_state(time_s=-100.)
    old=m.commit(m.trial_stress(old,np.array([4e6,0,0,0,0,0]),100.))
    after=m.trial(old,np.zeros(6),1.)
    fresh=m.trial(m.initial_state(),np.zeros(6),1.)
    assert np.linalg.norm(after.stress)>1e3
    np.testing.assert_array_equal(fresh.stress,np.zeros(6))


def test_abel_relaxation_is_inverse_and_converges_to_erfcx():
    # alpha=1/2 exact R(t)/E=E_{1/2}(-delta*sqrt(pi*t))=erfcx(delta*sqrt(pi*t)).
    m=AbelDirect(K=3.,G=1.,alpha_bulk=.5,delta_bulk=.2,alpha_dev=.5,delta_dev=.2)
    strain=np.array([0,0,0,.1,0,0]); errors=[]
    for n in [50,100,200]:
        state=m.initial_state(); dt=10/n
        for _ in range(n): state=m.commit(m.trial(state,strain,dt))
        exact=.2*erfcx(.2*math.sqrt(math.pi*10))
        errors.append(abs(state.stress[3]-exact))
    assert errors[2]<errors[1]<errors[0]
    assert errors[2]<2e-4


@pytest.mark.parametrize('modes',[64,128,256])
def test_abel_soe_positive_spectrum_and_direct_error(modes):
    direct=abel();fast=abel(AbelSOE,t_min=.1,horizon=1e4,modes=modes)
    assert all(np.all(w>0) for w in fast.weights)
    error=fast.kernel_error()
    assert max(error)<(2e-4 if modes==64 else 2e-7)
    a=direct.initial_state();b=fast.initial_state()
    rng=np.random.default_rng(18); worst=0.
    for k in range(100):
        dt=float(rng.uniform(.1,10)); sig=rng.normal(size=6)*3e6
        a=direct.commit(direct.trial_stress(a,sig,dt));b=fast.commit(fast.trial_stress(b,sig,dt))
        worst=max(worst,np.linalg.norm(a.strain-b.strain))
    assert worst< (2e-8 if modes==64 else 1e-10)
    assert set(b.memory)=={'bulk_modes','dev_modes','elapsed'}
    assert b.memory['dev_modes'].shape==(modes,6)


def test_abel_window_and_invalid_inputs():
    m=abel(AbelSOE,t_min=1.,horizon=10)
    with pytest.raises(ValueError): m.trial(m.initial_state(),np.ones(6),.5)
    with pytest.raises(ValueError): m.trial(m.initial_state(),np.ones(6),11)
    with pytest.raises(ValueError): abel().trial(m.initial_state(),np.ones(6),1)
    with pytest.raises(ValueError): nish().trial(nish().initial_state(),np.ones(6),-1)
    with pytest.raises(ValueError): nish().trial(nish().initial_state(),np.full(6,np.nan),1)


@pytest.mark.parametrize('load',[1e6,4e6,-4e6])
def test_nishihara_uniaxial_exact_solution(load):
    m=nish();sig=np.array([load,0,0,0,0,0]);t=2387.
    tr=m.trial_stress(m.initial_state(),sig,t)
    E0=9*m.K0*m.G0/(3*m.K0+m.G0);E1=9*m.K1*m.G1/(3*m.K1+m.G1)
    eta1=E1*m.eta_shear/m.G1
    expected=load/E0+load/E1*(1-math.exp(-E1*t/eta1))+np.sign(load)*max(abs(load)-m.yield_stress,0)*t/m.eta_vp
    assert tr.state.strain[0]==pytest.approx(expected,rel=2e-15)
    assert tr.diagnostics['physical_dissipation']>=0


def test_nishihara_unload_recover_permanent_flow_and_energy():
    m=nish();s=m.initial_state()
    loads=[np.array([5e6,0,0,0,0,0]),np.zeros(6),np.array([-4e6,0,0,0,0,0]),np.zeros(6)]
    for sig in loads:
        for _ in range(10):
            trial=m.trial_stress(s,sig,100.)
            assert trial.diagnostics['physical_dissipation']>=-1e-10
            assert trial.diagnostics['endpoint_work_minus_energy']>=-1e-8
            s=m.commit(trial)
    assert abs(s.memory['plastic'][0])>1e-5
    assert np.linalg.norm(s.memory['kelvin'])>0
    old=s
    t=m.trial_stress(old,np.zeros(6),20000.)
    np.testing.assert_array_equal(t.state.memory['plastic'],old.memory['plastic'])
    assert np.linalg.norm(t.state.memory['kelvin'])<np.linalg.norm(old.memory['kelvin'])*1e-8


def test_nishihara_yield_corner_and_hydrostatic():
    m=nish();initial=m.initial_state()
    for q in [0.,m.yield_stress*.999,m.yield_stress]:
        t=m.trial_stress(initial,np.array([q,0,0,0,0,0]),100)
        np.testing.assert_array_equal(t.state.memory['plastic'],np.zeros(6))
    hydro=m.trial_stress(initial,np.array([5e6]*3+[0]*3),100)
    np.testing.assert_allclose(hydro.state.memory['plastic'],0,atol=1e-18)
    assert hydro.state.strain[0]>0


def test_lubby_ogs_benchmark_analytic_and_nonlinearity():
    # Exact official benchmark bundle, MPa -> Pa; exponents 1/MPa -> 1/Pa.
    m=Lubby2(K0=.8e6,G0=.8e6,GK0=.8e6,etaK0=.5e6,etaM0=.5e6,
        mK=-.2e-6,mM=-.3e-6,mG=-.2e-6)
    for tau in [.01e6,.5e6]:
        sig=np.array([0,0,0,math.sqrt(2)*tau,0,0]);q=math.sqrt(3)*tau
        g=.8e6*math.exp(-.2e-6*q);ek=.5e6*math.exp(-.2e-6*q);em=.5e6*math.exp(-.3e-6*q)
        tr=m.trial_stress(m.initial_state(),sig,2.)
        # Tensor shear epsilon_xy, not engineering gamma.
        exact=tau/(2*.8e6)+tau/(2*g)*(1-math.exp(-g*2/ek))+tau*2/(2*em)
        assert tr.state.strain[3]/math.sqrt(2)==pytest.approx(exact,rel=2e-15)
    low=m.trial_stress(m.initial_state(),np.array([1e5,0,0,0,0,0]),1.)
    high=m.trial_stress(m.initial_state(),np.array([2e6,0,0,0,0,0]),1.)
    assert not np.isclose(high.state.strain[0]/20,low.state.strain[0],rtol=.01)


def test_lubby_hydrostatic_is_elastic_and_unloading_memory():
    m=lubby();s=m.initial_state();h=np.array([5e6]*3+[0]*3)
    t=m.trial_stress(s,h,100.)
    np.testing.assert_allclose(t.state.strain[:3],5e6/(3*m.K0))
    np.testing.assert_allclose(t.state.memory['kelvin'],0,atol=1e-20)
    s=m.commit(m.trial_stress(s,np.array([5e6,-1e6,0,2e6,0,0]),100.))
    old=s.memory['maxwell'].copy()
    s=m.commit(m.trial_stress(s,np.zeros(6),100.))
    np.testing.assert_array_equal(s.memory['maxwell'],old)
    assert np.linalg.norm(s.memory['kelvin'])>0


@pytest.mark.parametrize('model',[nish(),lubby()])
def test_strain_control_refinement_against_independent_ode(model):
    duration=1200.;target=np.array([.001,-.0002,0,.0002,0,0]);edot=target/duration
    # Independent ODE in [stress, Kelvin, irreversible strain].
    h=np.zeros((6,6));h[:3,:3]=1/3;p=np.eye(6)-h
    elastic=3*model.K0*h+2*model.G0*p
    def rhs(t,y):
        stress=y[:6];kelvin=y[6:12];s=p@stress;q=np.sqrt(1.5*s@s)
        if isinstance(model,Nishihara):
            kd=h@(stress-3*model.K1*kelvin)/(3*model.eta_bulk)+p@(stress-2*model.G1*kelvin)/(2*model.eta_shear)
            vd=max(q-model.yield_stress,0)/model.eta_vp*1.5*s/q if q>0 else np.zeros(6)
        else:
            g=model.GK0*np.exp(model.mG*q);ek=model.etaK0*np.exp(model.mK*q);em=model.etaM0*np.exp(model.mM*q)
            kd=(s-2*g*kelvin)/(2*ek);vd=s/(2*em)
        return np.r_[elastic@(edot-kd-vd),kd,vd]
    reference=solve_ivp(rhs,[0,duration],np.zeros(18),rtol=2e-11,atol=1e-12,method='DOP853').y[:6,-1]
    errors=[]
    for n in [20,40,80]:
        s=model.initial_state()
        for k in range(n):s=model.commit(model.trial(s,target*(k+1)/n,duration/n))
        errors.append(np.linalg.norm(s.stress-reference)/np.linalg.norm(reference))
    assert errors[2]<errors[1]*.6<errors[0]*.36
    assert errors[-1]<.015


@pytest.mark.parametrize('model',[nish(),lubby(),abel(),abel(AbelSOE,t_min=.1,horizon=1000)])
def test_batch_matches_scalar(model):
    rng=np.random.default_rng(54);states=[model.initial_state() for _ in range(8)];batch=model.initial_state((8,))
    for _ in range(4):
        strains=rng.normal(size=(8,6))*.0003
        batch=model.commit(model.trial(batch,strains,10.))
        for i in range(8):
            states[i]=model.commit(model.trial(states[i],strains[i],10.))
            np.testing.assert_allclose(batch.stress[i],states[i].stress,rtol=1e-9,atol=1e-3)


@pytest.mark.parametrize('model',[nish(),lubby(),abel(),abel(AbelSOE,t_min=.1,horizon=1000)])
def test_instantaneous_jump_preserves_memory_and_time(model):
    state=model.commit(model.trial_stress(model.initial_state(),np.array([5e6,0,0,1e6,0,0]),10.))
    e=state.strain+np.array([.0001,-.0001,0,.0002,0,0])
    trial=model.instantaneous(state,e)
    assert trial.state.time_s==state.time_s
    for key in state.memory:np.testing.assert_array_equal(trial.state.memory[key],state.memory[key])
    np.testing.assert_allclose(trial.stress-state.stress,trial.tangent@(e-state.strain),rtol=2e-15,atol=2e-9)
    assert trial.diagnostics['exact_elastic_jump_work']==pytest.approx(trial.diagnostics['elastic_energy_change'],rel=1e-13)
    back=model.instantaneous(model.commit(trial),state.strain)
    np.testing.assert_allclose(back.stress,state.stress,rtol=1e-14,atol=1e-8)


def test_abel_elastic_limit_and_nishihara_just_above_yield():
    m=AbelDirect(10e9,6e9,.7,0.,.6,0.)
    s=m.initial_state();strain=np.array([.001,0,0,0,0,0])
    a=m.trial(s,strain,100.);b=m.instantaneous(s,strain)
    np.testing.assert_allclose(a.stress,b.stress,rtol=2e-15)
    n=nish();tr=n.trial_stress(n.initial_state(),np.array([n.yield_stress+1.,0,0,0,0,0]),1.)
    assert tr.state.memory['plastic'][0]>0


@pytest.mark.parametrize('model',[nish(),lubby(),abel(),abel(AbelSOE,t_min=.1,horizon=1000)])
def test_deep_snapshot_and_model_parameter_immutability(model):
    state=model.initial_state();tr=model.trial_stress(state,np.array([3e6,0,0,0,0,0]),1.)
    for array in [state.strain,tr.stress,tr.tangent,model.S,*tr.state.memory.values()]:
        with pytest.raises(ValueError):array.setflags(write=True)
    with pytest.raises(AttributeError):model.S=np.eye(6)
    with pytest.raises(TypeError):model.__dict__['signature']='changed'
    with pytest.raises(AttributeError):state.__dict__['time_s']=10
    if isinstance(model,AbelSOE):
        with pytest.raises(ValueError):model.weights[0].setflags(write=True)
        with pytest.raises(TypeError):model.weights[0]=np.zeros(model.modes)
    parameter='K' if isinstance(model,AbelDirect) else 'K0'
    with pytest.raises(AttributeError):setattr(model,parameter,1.)


def test_time_unit_similarity_new_year_scale_bundle():
    year=31557600.
    a=nish();b=Nishihara(a.K0,a.G0,a.K1,a.G1,a.eta_bulk*year,a.eta_shear*year,a.eta_vp*year,a.yield_stress)
    sa=a.initial_state();sb=b.initial_state()
    for sig in [np.array([4e6,0,0,1e6,0,0]),np.zeros(6),np.array([-5e6,0,0,0,0,0])]:
        sa=a.commit(a.trial_stress(sa,sig,100.));sb=b.commit(b.trial_stress(sb,sig,100.*year))
        np.testing.assert_allclose(sa.strain,sb.strain,rtol=2e-15)


@pytest.mark.parametrize('model',[nish(),lubby(),abel(),abel(AbelSOE,t_min=.1,horizon=1000)])
def test_malformed_restart_is_rejected(model):
    state=model.initial_state()
    bad=ConstitutiveState(0,state.strain,state.stress,{'incorrect':np.zeros(6)},model.signature)
    with pytest.raises(ValueError):model.trial(bad,np.zeros(6),1.)
    with pytest.raises(ValueError):model.instantaneous(bad,np.zeros(6))
    wrong=ConstitutiveState(0,state.strain,state.stress,dict(state.memory),'old-algorithm-signature')
    with pytest.raises(ValueError):model.trial(wrong,np.zeros(6),1.)


def test_maxwell_matches_existing_tensorstages_ratio_and_heterogeneous_eta():
    K,G=10e9,6e9;eta=np.array([1e13,3e13,8e13]);m=DeviatoricMaxwell(K,G,eta)
    state=m.initial_state((3,4));rng=np.random.default_rng(80)
    h=np.zeros((6,6));h[:3,:3]=1/3;p=np.eye(6)-h
    for step in range(5):
        strain=rng.normal(size=(3,4,6))*.001;dt=170.+step
        ratio=dt*G/eta[:,None]
        old=state.memory['creep']
        dev=np.einsum('ij,...j->...i',p,strain)
        expected_creep=(old+ratio[...,None]*dev)/(1+ratio[...,None])
        expected_stress=np.einsum('ij,...j->...i',3*K*h+2*G*p,strain-expected_creep)
        trial=m.trial(state,strain,dt)
        np.testing.assert_allclose(trial.stress,expected_stress,rtol=3e-13,atol=1e-8)
        np.testing.assert_allclose(trial.state.memory['creep'],expected_creep,rtol=1e-13,atol=1e-17)
        assert np.min(trial.diagnostics['physical_dissipation'])>=0
        state=m.commit(trial)


def test_maxwell_uniaxial_creep_hydrostatic_tangent_restart():
    m=DeviatoricMaxwell(10e9,6e9,5e12);sig=np.array([4e6,0,0,0,0,0]);st=m.initial_state()
    tr=m.trial_stress(st,sig,200.)
    E=9*m.K0*m.G0/(3*m.K0+m.G0)
    assert tr.state.strain[0]==pytest.approx(4e6/E+4e6*200/(3*5e12))
    hydro=m.trial_stress(st,np.array([4e6]*3+[0]*3),200.)
    np.testing.assert_allclose(hydro.state.memory['creep'],0,atol=1e-18)
    state=m.commit(tr);restored=ConstitutiveState.from_dict(json.loads(json.dumps(state.to_dict())))
    eps=np.array([.001,-.0002,0,.0001,0,0]);a=m.trial(state,eps,400.);b=m.trial(restored,eps,400.)
    np.testing.assert_array_equal(a.stress,b.stress)
    step=1e-8
    fd=np.column_stack([(m.trial(state,eps+np.eye(6)[i]*step,400.).stress-m.trial(state,eps-np.eye(6)[i]*step,400.).stress)/(2*step) for i in range(6)])
    np.testing.assert_allclose(a.tangent,fd,rtol=1e-9,atol=.1)
    jump=m.instantaneous(state,eps)
    np.testing.assert_array_equal(jump.state.memory['creep'],state.memory['creep'])
    with pytest.raises(ValueError):m.eta_shear.setflags(write=True)


def test_maxwell_rejects_volumetric_memory_and_eta_shape():
    m=DeviatoricMaxwell(10e9,6e9,5e12);st=m.initial_state()
    bad=ConstitutiveState(0,np.zeros(6),np.zeros(6),{'creep':np.array([.1,.1,.1,0,0,0])},m.signature)
    with pytest.raises(ValueError):m.trial(bad,np.zeros(6),1.)
    other=DeviatoricMaxwell(10e9,6e9,np.ones(3)*5e12)
    with pytest.raises(ValueError):other.trial(other.initial_state((2,4)),np.zeros((2,4,6)),1.)


@pytest.mark.parametrize('control',['strain','stress'])
def test_maxwell_repeated_hydrostatic_exact_zero_memory(control):
    m=DeviatoricMaxwell(10e9,6e9,5e12);state=m.initial_state()
    for value in [1.,-2.,3.,-4.,0.]:
        x=np.array([value,value,value,0,0,0])*(1e6 if control=='stress' else 1e-4)
        tr=m.trial_stress(state,x,100.) if control=='stress' else m.trial(state,x,100.)
        state=m.commit(tr)
        np.testing.assert_array_equal(state.memory['creep'],np.zeros(6))
        assert tr.diagnostics['physical_dissipation']==0.
