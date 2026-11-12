import copy,json,math
from dataclasses import replace
import numpy as np
import pytest
from vkm_world.representations.world0.callahan import Bundle,UniaxialCarrier,checkpoint,restore
import hashlib
from scipy.integrate import solve_ivp, quad_vec

DAY=86400.
TIMES=DAY*np.array([0.,1.,11.,31.,41.,61.,71.,91.,101.,121.])
STRAINS=np.array([0.,0.,-.08,-.08,-.02,-.02,-.12,-.12,0.,0.])
HISTORY_SIGNATURE=hashlib.sha256(TIMES.tobytes()+STRAINS.tobytes()).hexdigest()


def path(t):
    i=min(len(TIMES)-2,max(0,int(np.searchsorted(TIMES,t,side='right')-1)))
    slope=(STRAINS[i+1]-STRAINS[i])/(TIMES[i+1]-TIMES[i])
    return STRAINS[i]+slope*(t-TIMES[i]),slope


def run(model,rtol=2e-8,method='DOP853',start_segment=0,y0=None):
    y=np.zeros(6) if y0 is None else np.asarray(y0).copy()
    rows=[];evaluations=0;checkpoints={}
    for i in range(start_segment,len(TIMES)-1):
        t0,t1=TIMES[i:i+2];a,b=STRAINS[i:i+2];slope=(b-a)/(t1-t0)
        def rhs(t,z): return model.rhs(a+(t-t0)*slope,slope,z)
        sol=solve_ivp(rhs,(t0,t1),y,method=method,rtol=rtol,atol=np.array([1e-11,1e-11,1e-14,1e-4,1e-4,1e-4]),dense_output=True,max_step=(t1-t0)/30)
        if not sol.success: raise RuntimeError(sol.message)
        for accepted_t,accepted_y in zip(sol.t,sol.y.T):
            model.state(a+(accepted_t-t0)*slope,accepted_y)
        evaluations+=sol.nfev
        receivers=y[3:].copy();last_t=t0
        for t in np.linspace(t0,t1,61)[1 if rows else 0:]:
            z=sol.sol(t);e=a+(t-t0)*slope;r=model.state(e,z)
            raw_receivers=z[3:].copy()
            # Independent physical quadrature, not a balance-residual repair.
            # Dense ODE interpolation of cumulative receivers can overshoot
            # their flat tails despite nonnegative instantaneous power.
            if t>last_t:
                increment,_=quad_vec(lambda s: rhs(s,sol.sol(s))[3:],last_t,t,epsabs=1e-6,epsrel=1e-9)
                receivers+=increment
            last_t=t;z[3:]=receivers
            rows.append([t,model.birth_time_s+t,e,*z,r['nominal_compression_Pa'],r['cauchy_compression_Pa'],r['D'],r['porosity'],r['volume_m3'],r['stored_J'],r['solid_mass_kg'],r['water_mass_kg'],r['gas_volume_m3'],r['boundary_gap_m'],*raw_receivers])
        y=sol.y[:,-1];y[3:]=receivers
        checkpoints[i+1]=checkpoint(model,HISTORY_SIGNATURE,i+1,t1,y)
    return np.array(rows),checkpoints,evaluations




def test_source_column_coefficients_not_mixed():
    shear=Bundle().parameters;combined=Bundle(fit='combined_star').parameters
    assert 1.041e-6*.01**shear['a']/.001**shear['p']==pytest.approx(shear['r1star'],rel=4e-4)
    raw=2.663e-5*.01**combined['a']/.001**combined['p']
    assert combined['r1star']/raw==pytest.approx(10.,rel=5e-4)
    # This intentional negative consistency control must not be renamed PASS
    # for the incompatible raw-r1 bundle. The executable uses named r1* branch.
    assert not math.isclose(raw,combined['r1star'],rel_tol=.01)


def test_source_dense_limit_retains_deviatoric_md_but_not_solution():
    b=Bundle();r=b.triaxial_compression_rates(1.,5e6,0.,0.)
    assert r['md']>0 and r['solution']==0
    assert abs(r['axial']+2*r['lateral'])<1e-25
    h=b.triaxial_compression_rates(1.,5e6,5e6,0.)
    assert h['axial']==h['lateral']==0


def test_nonassociated_flow_work_identity_and_nonnegative_dissipation():
    rng=np.random.default_rng(4127);b=Bundle()
    for _ in range(40):
        D=rng.uniform(.70,.99);lateral=rng.uniform(0,3e6);axial=lateral+rng.uniform(1e3,3e6)
        zeta=rng.uniform(0,1e-3)
        r=b.triaxial_compression_rates(D,axial,lateral,zeta)
        _,potential,_,_=b.equivalent(D,-(axial+2*lateral)/3,axial-lateral)
        power=-axial*r['axial']-2*lateral*r['lateral']
        assert power>=0
        assert power==pytest.approx(potential*(r['md']+r['solution']),rel=2e-14)


def test_gamma_source_switch_and_dense_decay():
    b=Bundle();D0=b.original_fractional_density;Dswitch=D0/.85
    left=b.kinetics(Dswitch-1e-8,1e6,0.)['Gamma']
    right=b.kinetics(Dswitch+1e-8,1e6,0.)['Gamma']
    assert left==1 and right<.9  # source formula is discontinuous; not smoothed.
    for D in [.8,.9,.99,.9999]:
        assert b.kinetics(D,1e6,0.)['Gamma']==pytest.approx(((1-D)/(1-D0))**b.parameters['ns'],rel=1e-11)


def test_dry_solution_exact_zero_and_low_moisture_sensitivity():
    dry=Bundle(moisture_mass_fraction=0.)
    assert dry.kinetics(.8,1e6,0.)['solution']==0
    wet=Bundle().kinetics(.8,1e6,0.)['solution']
    tiny=Bundle(moisture_mass_fraction=1e-10).kinetics(.8,1e6,0.)['solution']
    assert tiny/wet==pytest.approx((1e-8)**.3147)
    b=Bundle(fit='combined_star');nearlydry=replace(b,moisture_mass_fraction=1e-10)
    assert nearlydry.kinetics(.8,1e6,0.)['solution']/b.kinetics(.8,1e6,0.)['solution']>.999


def test_stress_free_birth_and_no_unloaded_calendar_aging():
    m=UniaxialCarrier(Bundle());y=np.zeros(6)
    assert m.state(0.,y)['stored_J']==0
    assert np.array_equal(m.rhs(0.,0.,y),np.zeros(6))
    older=replace(m,birth_time_s=m.birth_time_s+10*365.25*86400)
    assert m.state(-.01,y)==older.state(-.01,y)
    assert m.signature!=older.signature


def test_solid_and_water_balance_from_volume_density():
    m=UniaxialCarrier(Bundle());y=np.array([-.04,.01,.001,1.,2.,3.])
    for e in [-.08,-.02,0.]:
        r=m.state(e,y)
        assert r['D']*m.grain_density_kg_m3*r['volume_m3']==pytest.approx(r['solid_mass_kg'],rel=2e-15)
        assert r['gas_volume_m3']+r['solid_mass_kg']/m.grain_density_kg_m3+r['water_mass_kg']/m.water_density_kg_m3==pytest.approx(r['volume_m3'])
        assert r['nominal_compression_Pa']>=0


def test_hencky_work_conjugacy_independent_energy_derivative():
    m=UniaxialCarrier(Bundle());y=np.array([-.02,.004,.001,0,0,0.]);e=-.08;h=1e-7
    dE=(m.state(e+h,y)['stored_J']-m.state(e-h,y)['stored_J'])/(2*h)
    assert dE==pytest.approx(-m.state(e,y)['nominal_compression_Pa']*m.birth_volume_m3,rel=1e-9)


def test_restart_corruption_and_changed_material_reject():
    m=UniaxialCarrier(Bundle());data=checkpoint(m,HISTORY_SIGNATURE,3,10.,[0,0,.01,1,2,3])
    assert np.array_equal(restore(m,HISTORY_SIGNATURE,json.loads(json.dumps(data)))[2],[0,0,.01,1,2,3])
    damaged=copy.deepcopy(data);damaged['y'][3]=99
    with pytest.raises(ValueError):restore(m,HISTORY_SIGNATURE,damaged)
    with pytest.raises(ValueError):restore(replace(m,E_Pa=30e6),HISTORY_SIGNATURE,data)
    with pytest.raises(ValueError):restore(m,'different history',data)


@pytest.mark.parametrize('bad', [dict(temperature_K=0),dict(grain_m=0),dict(moisture_mass_fraction=-1),dict(original_fractional_density=.9),dict(fit='mixed')])
def test_bad_bundle(bad):
    with pytest.raises(ValueError): Bundle(**bad)


def test_invalid_states_and_wet_dense_inventory_reject():
    b=Bundle()
    for D,s,z in [(.64,1e6,0),(1.01,1e6,0),(.8,-1,0),(.8,1e6,-.01)]:
        with pytest.raises(ValueError): b.kinetics(D,s,z)
    with pytest.raises(ValueError): UniaxialCarrier(b).state(-1.,np.zeros(6))
    with pytest.raises(ValueError): UniaxialCarrier(b).state(0.,[0,0,-.01])
    # At 1 wt% water, fixed retained water physically limits accessible D.
    with pytest.raises(ValueError,match='drainage model required'):
        UniaxialCarrier(b,birth_fractional_density=.97).state(-.03,np.zeros(6))


def test_independent_ode_solver_same_physical_history():
    m=UniaxialCarrier(Bundle())
    explicit,_,_=run(m,rtol=2e-9,method='DOP853')
    implicit,_,_=run(m,rtol=2e-9,method='Radau')
    assert np.max(abs(explicit[:,9]-implicit[:,9]))<.2
    assert np.max(abs(explicit[-1,3:6]-implicit[-1,3:6]))<2e-9
