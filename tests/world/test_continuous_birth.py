import numpy as np
import pytest
from numpy.polynomial import Polynomial as P
from scipy.integrate import quad
from vkm_world.representations.world0 import continuous_birth as candidate
from vkm_world.representations.world0.continuous_birth import ContinuousModel, clipped_integrals, fraction, rate

def model():
 return candidate.ContinuousModel(dict(K=np.eye(2),M=np.ones(2),dM=np.ones(2),Q=np.eye(2),Wk=np.ones(2),r0=np.zeros(2),incoming_body=-np.ones(2),support=np.zeros(1),cell_ids=np.array([0]),fill_cells=np.array([0]),fillcapacity=np.array([.3]),incoming_mass=np.array(1.)))

@pytest.mark.parametrize('t,x',[
 (.25,np.ones(3)),(.25,np.zeros((2,1))),(.25,np.array([np.nan,0.])),(.25,np.array([np.inf,0.])),
 (np.nan,np.zeros(2)),(np.inf,np.zeros(2)),(np.array([.2,.3]),np.zeros(2)),(-.1,np.zeros(2)),(0.,np.zeros(2)),(1.1,np.zeros(2))])
def test_invalid_commit_preserves_accepted_history(t,x):
 m=model();old_times=m.times.copy();old_refs=m.refs.copy()
 with pytest.raises((ValueError,TypeError)):
  m.commit(t,x)
 np.testing.assert_array_equal(m.times,old_times);np.testing.assert_array_equal(m.refs,old_refs)

def test_reference_computation_failure_preserves_history():
 m=model()
 class FailedAction:
  def __matmul__(self,value):raise RuntimeError('independent injected action failure')
 m.Q=FailedAction();old_times=m.times.copy();old_refs=m.refs.copy()
 with pytest.raises(RuntimeError,match='injected action failure'):m.commit(.25,np.zeros(2))
 np.testing.assert_array_equal(m.times,old_times);np.testing.assert_array_equal(m.refs,old_refs)

def test_success_copies_input_and_preserves_prior_references():
 m=model();x=np.array([.001,-.002]);m.commit(.25,x);first=m.refs.copy();x[:]=99
 np.testing.assert_array_equal(m.refs,first);m.commit(.75,np.array([-.003,.004]));np.testing.assert_array_equal(m.refs[:2],first)
 np.testing.assert_array_equal(m.times,[0,.25,.75])


def test_independent_analytic_controls():
    z=dict(K=np.eye(1),M=np.array([2.]),dM=np.array([3.]),Q=np.eye(1),Wk=np.ones(1),r0=np.zeros(1),incoming_body=np.array([-29.43]),support=np.zeros(1),cell_ids=np.array([0]),fill_cells=np.array([0]),fillcapacity=np.array([.3]),incoming_mass=np.array(3.))
    check=[]
    # Quad oracle splits at the exact unilateral crossing; both orientations and atoms.
    rng=np.random.default_rng(10283);errors=[];fd=[]
    for i in range(157):
     l,r,e=rng.uniform(-.1,.1,3)
     if i%11==0:r=l
     if i%13==0:e=l
     if i%17==0:e=r
     got=clipped_integrals(e,np.array([l]),np.array([r]));points=[] if l==r else [float(np.clip((e-l)/(r-l),0,1))]
     for j in (1,2):
      oracle=quad(lambda u:min(e-l-(r-l)*u,0.)**j,0,1,points=points,epsabs=1e-14,epsrel=1e-12)[0]
      errors.append(abs(float(got[j-1][0])-oracle))
     # At an atom's active-set corner, central force FD has exactly h/4 error.
     # Reduce h rather than relaxing the unchanged 1e-8 derivative criterion.
     h=1e-8;plus=clipped_integrals(e+h,np.array([l]),np.array([r]))[1][0];minus=clipped_integrals(e-h,np.array([l]),np.array([r]))[1][0]
     fd.append(abs((plus-minus)/(4*h)-got[0][0]))
    assert max(errors)<1e-13 and max(fd)<1e-8,(max(errors),max(fd));check.append('157 high-accuracy split-integral cases; reversed intervals, atoms and active-set edges')
    # Exact cumulative-mass-linear monotone history, arbitrary nonuniform commits.
    mono=[]
    for t in (.23,.61,1.):
     m=ContinuousModel(z);beta=.037
     for q in np.linspace(0,t,19)[1:]:m.commit(float(q),np.array([-beta*fraction(float(q))]))
     a=fraction(t);f,U=m.fill(np.array([-beta*a]),t);expected=np.array([-.5*beta*a*a,beta**2*a**3/6]);err=np.max(abs(np.array([f[0],U])-expected));mono.append(float(err));assert err<1e-15
    check.append('monotone birth history exact force/energy at three partial masses')
    # Nonmonotone cubic in source TIME. Analytic polynomial integration against quintic
    # mass-rate gives a separate oracle, with all roots of the clipping equation split.
    b=.15*P([-.12,1])*P([-.58,1])*P([-.91,1]);adot=P([0,0,30,-60,30])
    ext=np.r_[0.,1.,[r.real for r in b.deriv().roots() if abs(r.imag)<1e-12 and 0<r.real<1]]
    queries=np.linspace(float(b(ext).min())-.002,float(b(ext).max())+.002,13)
    exact=[]
    for e in queries:
     diff=P([e])-b;roots=sorted(r.real for r in diff.roots() if abs(r.imag)<1e-12 and 0<r.real<1);breaks=[0.,*roots,1.];vals=[]
     for power in (1,2):
      integ=(adot*diff**power).integ();val=sum(integ(r)-integ(l) for l,r in zip(breaks,breaks[1:]) if diff((l+r)/2)<0);vals.append(val/(2 if power==2 else 1))
     exact.append(vals)
    exact=np.array(exact);curves={}
    for N in (64,128,256,512,1024):
     m=ContinuousModel(z)
     for t in np.linspace(0,1,N+1)[1:]:m.commit(float(t),np.array([b(t)]))
     values=np.array([[m.fill(np.array([e]),1.)[0][0],m.fill(np.array([e]),1.)[1]] for e in queries])
     curve=np.max(abs(values-exact),axis=0)/np.max(abs(exact),axis=0);curves[N]=curve.tolist()
     assert np.all(np.diff(m.times)>0) and np.all(np.diff(fraction(m.times))>0)
    assert max(curves[512])<1e-4 and all(np.min(np.array(curves[n])/curves[n*2])>3.7 for n in (128,256,512))
    assert m.fill(np.array([queries[-1]]),1.)[1]==0;check.append('nonmonotone time-polynomial exact clipped oracle; positive intervals, reopening and second-order refinement')
    # Once deposition is complete, external work along a large reopening cycle is
    # exactly recoverable; no averaging-away of the reference distribution is allowed.
    F=lambda e:float(m.fill(np.array([e]),1.)[0][0]);lo=float(queries[0]);hi=float(queries[-1]);U0=m.fill(np.array([lo]),1.)[1]
    work=quad(F,lo,hi,points=sorted(set(float(r) for r in m.refs[:,0] if lo<r<hi)),epsabs=1e-13,limit=1500)[0]
    assert abs(work+U0)<1e-12;check.append('post-deposition external reopening work releases stored energy')
    # Exact continuous source momentum action and incoming power, nonzero velocity.
    m=ContinuousModel(z);t=.37;x=np.array([-.002]);v=np.array([.13]);y=np.r_[x,v,0.,0.];m.commit(.2,np.array([-.001]))
    deriv=m.rhs(t,y/m.scale)*m.scale;f,_=m.fill(x,t);aa=10*t**3-15*t**4+6*t**5;ap=30*t**2*(1-t)**2
    expected=(z['r0']+aa*z['incoming_body']-z['K']@x-f)/(z['M']+aa*z['dM']);np.testing.assert_allclose(deriv[1:2],expected,rtol=1e-14)
    np.testing.assert_allclose(deriv[-2:],[-ap*z['incoming_body']@x,.5*ap*z['dM']@(v*v)],rtol=1e-14)
    check.append('continuous mass action and co-moving incoming kinetic/gravity powers; no Mdot drag')
    # Current-interval interpolation is an approximation, not an exact work-preserving
    # material integrator. Preserve its stated limitation explicitly with a counterexample.
    m=ContinuousModel(z);tt=.5;xx=np.array([-.02]);h=1e-7;f,U=m.fill(xx,tt)
    partial=(m.fill(xx+h,tt)[1]-m.fill(xx-h,tt)[1])/(2*h)
    assert abs(partial-f[0])>1e-4
    lim=dict(force=float(f[0]),total_derivative_of_trial_reconstructed_energy=float(partial),difference=float(partial-f[0]),scope='documented changing current-panel reference approximation; not a defect of the fixed completed distribution')
    r=dict(status='ready_for_review',checks=check,interval_quad_max_error=max(errors),fixed_interval_energy_derivative_max_error=max(fd),monotone_max_error=max(mono),nonmonotone_time_source_curves=curves,large_reopening_work_J=work,large_initial_stored_J=U0,documented_current_panel_limit_counterexample=lim,no_ODE_solve=True)
