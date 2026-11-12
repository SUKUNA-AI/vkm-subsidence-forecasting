import sys
from pathlib import Path
import numpy as np
import pytest
from numpy.testing import assert_allclose,assert_array_equal
from scipy.integrate import quad
from vkm_world.representations.world0.abel_energy import SpectralEnergyWitness,direct_history_energy,hybrid_step_defect,bulk,dev

PARAMS=dict(K=10e9,G=6e9,alpha_bulk=.35,delta_bulk=.02,alpha_dev=.65,delta_dev=.03)
def history():
    times=np.array([0,1,2.7,3,5,8,9.2,12.])
    stress=1e6*np.array([[3,3,3,0,0,0],[5,2,1,2,-1,0],[1,2,3,-2,1,1],
        [0,0,0,0,0,0],[-2,-2,-2,1,0,-2],[1,-1,0,-1,2,0],[0,0,0,0,0,0]])
    return times,stress

def test_closed_form_constant_load_matches_independent_gamma_integrals():
    t=3.;sigma=np.array([2e6,0,0,.5e6,0,0])
    r=direct_history_energy([0,t],sigma[None],**PARAMS)
    psi=0.;D=0.
    # Integrals transformed to x=lambda*t; adaptive quadrature independent
    # of log-Gauss witness and finite-history pair formulas.
    import math
    for proj,a,d,M in zip((bulk,dev),(.35,.65),(.02,.03),(3e10,1.2e10)):
        p=proj(sigma);beta=1-a
        Ipsi=quad(lambda x:x**(a-2)*(-np.expm1(-x))**2,0,1,epsabs=1e-11)[0]+quad(lambda x:x**(a-2)*(-np.expm1(-x))**2,1,np.inf,epsabs=1e-11)[0]
        ID=quad(lambda x:x**(a-2)*(-np.expm1(-2*x)),0,1,epsabs=1e-10)[0]+quad(lambda x:x**(a-2)*(-np.expm1(-2*x)),1,np.inf,epsabs=1e-10)[0]
        psi+=d/(2*M)*np.dot(p,p)*t**beta/math.gamma(a)*Ipsi
        D+=d/(2*M)*np.dot(p,p)*t**beta/math.gamma(a)*ID
    assert_allclose(r['memory_energy'][0],psi,rtol=2e-9)
    assert_allclose(r['physical_dissipation'][0],D,rtol=2e-9)
    assert abs(r['balance_error'][0])<1e-9

def test_variable_tensor_history_full_spectrum_balance_unload_and_positivity():
    times,stress=history();r=direct_history_energy(times,stress,**PARAMS)
    assert np.all(r['physical_dissipation']>=0)
    assert np.all(r['memory_energy']>=0)
    assert np.max(abs(r['balance_error']))<2e-9
    # With zero stress, no external work; memory energy decays into dissipation.
    for i in [3,6]:
        assert abs(r['work'][i]+r['elastic_energy'][i-1])<1e-10
        assert r['memory_energy'][i]<r['memory_energy'][i-1]

def test_positive_quadrature_converges_to_full_oracle_with_explicit_tail_bounds():
    times,stress=history();exact=direct_history_energy(times,stress,**PARAMS)
    values=[]
    for order in [4,8,16,32]:
        w=SpectralEnergyWitness(**PARAMS,lower=1e-28,upper=1e28,panels=32,order=order)
        s=w.initial()
        for dt,sig in zip(np.diff(times),stress):
            s,d=w.step(s,sig,float(dt))
            assert np.min(d['physical_dissipation'])>=0
            assert np.max(abs(d['balance_error']))<5e-9
        elastic,psi=w.energies(s);tails=w.tail_bounds(s)
        values.append(float(s.cumulative_work))
        if order==16:
            assert abs(float(psi-exact['memory_energy'][-1]))<=float(tails['psi_low']+tails['psi_high'])+1e-7
            assert abs(float(s.cumulative_dissipation-exact['cumulative_dissipation'][-1]))<=float(tails['D_low']+tails['D_high'])+1e-7
    # Quadrature convergence is within the SAME finite spectral band.
    # The separately bounded omitted tails set a nonzero error floor relative
    # to the full-spectrum oracle and must not be called quadrature error.
    errors=[abs(v-values[-1]) for v in values[:-1]]
    assert errors[1]<errors[0]/100
    assert errors[2]<errors[1]/100

def test_nonzero_prefix_restart_and_instantaneous_jump_are_exact(tmp_path):
    w=SpectralEnergyWitness(**PARAMS,panels=24,order=12);times,stress=history();s=w.initial()
    for dt,sig in zip(np.diff(times)[:3],stress[:3]):s,_=w.step(s,sig,float(dt))
    assert np.linalg.norm(s.bulk_z)>0 and s.cumulative_dissipation>0
    w.save(tmp_path/'energy.npz',s);reopened=w.reopen(tmp_path/'energy.npz')
    for dt,sig in zip(np.diff(times)[3:],stress[3:]):
        s,_=w.step(s,sig,float(dt));reopened,_=w.step(reopened,sig,float(dt))
    assert_array_equal(s.bulk_z,reopened.bulk_z);assert_array_equal(s.dev_z,reopened.dev_z)
    assert_array_equal(s.cumulative_work,reopened.cumulative_work)
    changed,d=w.step(s,np.ones(6)*1e6,0.)
    assert_array_equal(changed.bulk_z,s.bulk_z);assert_array_equal(changed.dev_z,s.dev_z)
    assert d['physical_dissipation']==0
    assert abs(d['balance_error'])<1e-10
    with pytest.raises(ValueError):changed.bulk_z.flags.writeable=True
    with pytest.raises(ValueError):w.rates.flags.writeable=True
    with pytest.raises(AttributeError):w.lower=1e-3
    with pytest.raises(TypeError):w.parameters['delta_bulk']=100.

def test_batched_witness_equals_independent_scalar_points():
    w=SpectralEnergyWitness(**PARAMS,panels=12,order=8)
    stress=np.random.default_rng(19).normal(0,1e6,(2,3,6));batch,d=w.step(w.initial((2,3)),stress,2.)
    for i in range(2):
        for j in range(3):
            s,one=w.step(w.initial(),stress[i,j],2.)
            assert_allclose(batch.cumulative_work[i,j],s.cumulative_work,rtol=2e-15)
            assert_allclose(d['physical_dissipation'][i,j],one['physical_dissipation'],rtol=2e-15)

def test_all_state_consumers_reject_foreign_spectrum():
    w=SpectralEnergyWitness(**PARAMS,panels=12,order=8)
    other=SpectralEnergyWitness(**dict(PARAMS,K=2*PARAMS['K']),panels=12,order=8)
    state,_=w.step(w.initial(),np.ones(6)*1e6,1.)
    for consume in (other.energies,other.memory_strain,other.tail_bounds):
        with pytest.raises(ValueError,match='signature'):consume(state)

def test_hybrid_small_kernel_error_does_not_close_finite_soe_energy():
    sys.dont_write_bytecode=True
    from vkm_world.representations.world0.constitutive import AbelSOE
    m=AbelSOE(10e9,6e9,.6,.01,.6,.01,1.,1e4,modes=128)
    assert np.max(m.kernel_error())<1e-10
    old=m.initial_state();residuals=[]
    for sig in (np.array([2e6,0,0,0,0,0]),np.array([1e6,0,0,0,0,0]),np.zeros(6)):
        new=m.trial_stress(old,sig,1.).state;d=hybrid_step_defect(m,old,new,1.)
        assert abs(d['identified_defect_error'])<1e-9
        assert abs(d['hybrid_correction_work'])<=d['hybrid_correction_absolute_bound']+1e-10
        assert d['finite_spectrum_physical_dissipation']>=0
        assert d['mode_update_max_abs_error']<1e-8
        assert d['near_correction_expected_max_abs_error']<1e-17
        residuals.append(float(d['naive_closure_residual']));old=new
    assert residuals[0]>.5
    assert residuals[1]<-.1
    assert abs(residuals[2])<1e-9

def test_hybrid_dt0_jump_does_not_reset_spectral_memory_or_create_dissipation():
    sys.dont_write_bytecode=True
    from vkm_world.representations.world0.constitutive import AbelSOE
    m=AbelSOE(10e9,6e9,.45,.02,.7,.03,.1,1e4,modes=128)
    old=m.trial_stress(m.initial_state(),np.array([2e6,-1e6,0,1e6,0,0]),2.).state
    new=m.instantaneous(old,old.strain*.4).state
    d=hybrid_step_defect(m,old,new,0.)
    assert d['mode_update_max_abs_error']==0
    assert d['finite_spectrum_physical_dissipation']==0
    assert abs(d['hybrid_correction_work'])<1e-10
    assert abs(d['identified_defect_error'])<1e-9

def test_hybrid_discrete_work_matches_full_abel_oracle_in_variable_history():
    sys.dont_write_bytecode=True
    from vkm_world.representations.world0.constitutive import AbelSOE,ConstitutiveState
    times,stress=history();exact=direct_history_energy(times,stress,**PARAMS)
    m=AbelSOE(**PARAMS,t_min=.1,horizon=100.,modes=128)
    old=m.initial_state();old_exact_error=np.zeros(6)
    for i,(dt,sig) in enumerate(zip(np.diff(times),stress)):
        new=m.trial_stress(old,sig,float(dt)).state
        audit=hybrid_step_defect(m,old,new,float(dt))
        full_strain=bulk(sig)/(3*PARAMS['K'])+dev(sig)/(2*PARAMS['G'])+exact['memory_strain'][i]
        new_error=new.strain-full_strain
        numerical_defect=float(audit['endpoint_work']-audit['elastic_quadrature_excess']-exact['work'][i])
        bound=np.linalg.norm(sig)*np.linalg.norm(new_error-old_exact_error)
        assert abs(numerical_defect)<=bound+2e-9
        assert abs(numerical_defect)<2e-8
        old_exact_error=new_error
        # Genuine nonzero-memory serialization/restart of the production state.
        old=ConstitutiveState.from_dict(new.to_dict()) if i==2 else new
