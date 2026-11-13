import json,os
from pathlib import Path
import numpy as np
import pytest
from scipy.integrate import solve_ivp
from vkm_world.representations.world0.midpoint import *
from vkm_world.representations.world0.wedge_midpoint import transfer_grouped
from test_wedge_adapter import build,saved,affine,loadedF
from vkm_world.representations.world0.constitutive import Lubby2
from vkm_world.representations.world0.finite_lubby import finite_response,deformation_from_green,mandel
from vkm_world.representations.world0.tl_core import green

def source():return ReferentialLubby(Lubby2(1.25e9,576923076.9230769,.7*576923076.9230769,.7*576923076.9230769*63115200,576923076.9230769*473364000,mK=-2e-8,mM=-3e-8,mG=0.))
E0=np.array([.001,-.0004,.0001,.0002,0.,.0001])
def fixture():
 s=source();z=s.initial_state();old=s.commit(s.trial(z,E0,2e6));m=MidpointLubby(s.inner,s.reference_strain)
 return s,old,m,transfer_point(s,m,old)
def statebytes(x):return (x.strain.tobytes(),x.stress.tobytes(),x.time_s,{k:v.tobytes() for k,v in x.memory.items()})

def test_transfer_exact_nonzero_chart_energy_and_foreign_rejection():
 s,old,m,new=fixture();assert statebytes(old)==statebytes(new)
 assert s.energy(old).tobytes()==m.energy(new).tobytes() and old.signature!=new.signature
 with pytest.raises(ValueError):m.trial(old,E0,1e6)
 with pytest.raises(ValueError):transfer_point(s,MidpointLubby(s.inner,np.ones(6)*1e-5),old)

def test_discrete_energy_nonsymmetric_tangent_and_independent_FD():
 s,_,m,old=fixture();E=E0+np.array([.0001,-.0002,.00005,-.0001,.00008,.00009]);tr=m.trial(old,E,1e6)
 den=max(1.,float(m.energy(old)),float(m.energy(tr.state)),abs(float(tr.diagnostics['discrete_material_work'])))
 assert abs(tr.diagnostics['material_energy_residual'])/den<1e-10
 assert tr.diagnostics['branch_viscous_dissipation']>=0
 h=1e-7;fd=np.column_stack([(m.trial(old,E+np.eye(6)[j]*h,1e6).stress-m.trial(old,E-np.eye(6)[j]*h,1e6).stress)/(2*h) for j in range(6)])
 assert np.linalg.norm(fd-tr.tangent)/np.linalg.norm(fd)<2e-7
 assert np.linalg.norm(tr.tangent-tr.tangent.T)/np.linalg.norm(tr.tangent)>1e-7

def test_full_F_tangent_objectivity_and_dt0_fixed_memory():
 _,_,m,old=fixture();F=deformation_from_green(E0+np.array([.0002,-.0001,.0001,.0001,.0002,-.0001]));r=finite_response(m,old,F,1e6)
 h=1e-6;fd=np.empty((9,9))
 for j in range(9):
  d=np.zeros((3,3));d.flat[j]=h
  fd[:,j]=((finite_response(m,old,F+d,1e6).P-finite_response(m,old,F-d,1e6).P)/(2*h)).ravel()
 assert np.linalg.norm(fd-r.A.reshape(9,9))/np.linalg.norm(fd)<2e-7
 Q,_=np.linalg.qr(np.random.default_rng(918).normal(size=(3,3)));Q[:,0]*=np.linalg.det(Q)
 rq=finite_response(m,old,Q@F,1e6)
 np.testing.assert_allclose(rq.P,Q@r.P,rtol=3e-9,atol=1e-6)
 rotated=np.einsum('ia,ajbl,kb->ijkl',Q,r.A,Q)
 assert np.linalg.norm(rq.A-rotated)/np.linalg.norm(rotated)<3e-9
 z=m.instantaneous(old,old.strain)
 assert all(np.array_equal(z.state.memory[k],old.memory[k]) for k in old.memory) and z.state.time_s==old.time_s

def test_unsupported_cusp_no_regularization_and_large_h_atomic():
 s,_,m,old=fixture();k=np.array([1e-4,-1e-4,0,0,0,0]);zero=ConstitutiveState(0.,k,np.zeros(6),{'kelvin':k,'maxwell':np.zeros(6)},m.signature)
 before=statebytes(zero)
 with pytest.raises(UnsupportedClassicalTangent):m.trial(zero,k,1e6)
 assert statebytes(zero)==before
 with pytest.raises(ValueError,match='nonoscillatory'):m.trial(old,old.strain,1e12)
 assert statebytes(old)==statebytes(old)

def test_maxwell_energy_transfer_closure_and_constant_strain_limit():
 s=DeviatoricMaxwell(1.25e9,5e8,1e17);old=s.trial(s.initial_state(),E0,1e6).state;m=MidpointMaxwell(s.K0,s.G0,s.eta_shear);state=transfer_point(s,m,old)
 tr=m.trial(state,E0,1e6);ratio=1e6*s.G0/(2*float(s.eta_shear))
 np.testing.assert_allclose(mv(P,tr.stress),mv(P,old.stress)*(1-ratio)/(1+ratio),rtol=1e-12,atol=1e-7)
 assert abs(tr.diagnostics['material_energy_residual'])<1e-8 and tr.diagnostics['physical_dissipation']>=0
 wrong=ConstitutiveState(state.time_s,state.strain,state.stress+np.array([1e6,-1e6,0,0,0,0]),dict(state.memory),m.signature)
 with pytest.raises(ValueError):m.trial(wrong,E0,1e6)

@pytest.mark.parametrize('two',[False,True])
def test_grouped_small_WEDGE_same_solver_energy_restart_rollback(two):
 b=build(two,two);fix=np.arange(9);f=b.mesh.body_force([0,0,-9.80665]);init=b.solve(b.initial_state(),f,fix,np.zeros(9),0.)
 m,old=transfer_grouped(b,init.state)
 assert np.array_equal(b.law.energy(init.state.material),m.law.energy(old.material))
 s=m.solve(old,f,fix,np.zeros(9),1e6,rtol=1e-9);audit=m.work_audit(old,s,f)
 den=max(1.,abs(audit['external_work_J']),float(np.sum(m.mesh.measures*m.law.energy(s.state.material))))
 assert abs(audit['algebraic_residual_J'])/den<1e-10
 assert audit['physical_branch_dissipation_J']>=0 and s.residual_relative<=1e-9
 reopened=m.restore(s.state.u,s.state.material.to_dict(),m.signature)
 a=m.solve(s.state,f,fix,np.zeros(9),1e6);bb=m.solve(reopened,f,fix,np.zeros(9),1e6)
 assert saved(a.state)==saved(bb.state)
 before=saved(s.state)
 with pytest.raises(RuntimeError):m.solve(s.state,f+1e12,fix,np.zeros(9),1e6,max_iterations=0)
 assert saved(s.state)==before
 F=np.array([[1,.001,0],[0,1,.002],[0,0,1.]])
 u=affine(m,F);force,K,r=m.assemble(old,u,1e6);h=1e-6;fd=np.empty_like(K)
 for j in range(u.size):
  d=np.zeros_like(u);d.flat[j]=h;fd[:,j]=((m.assemble(old,u+d,1e6)[0]-m.assemble(old,u-d,1e6)[0])/(2*h)).ravel()
 assert np.linalg.norm(fd-K)/np.linalg.norm(fd)<2e-7

def test_independent_continuous_ODE_second_order_and_report_errors(tmp_path):
 s=source();m=MidpointLubby(s.inner);z=s.initial_state();initial=s.instantaneous(z,E0).state;old=transfer_point(s,m,initial);law=s.inner;C=np.linalg.inv(law.S);T=3e7
 amp=np.array([.0001,-.00003,.00005,.0002,-.0001,.00015]);freq=np.array([1.,2.,3.,1.,2.,3.])*np.pi/T
 def E(t):return E0+amp*np.sin(freq*t)
 def Ed(t):return amp*freq*np.cos(freq*t)
 def rhs(t,y):
  k=y[:6];v=y[6:12];S=C@(E(t)-k-v);dev=P@S;q=np.sqrt(1.5*np.dot(dev,dev));ek=law.etaK0*np.exp(law.mK*q);em=law.etaM0*np.exp(law.mM*q);force=dev-2*law.GK0*k;kd=force/(2*ek);vd=dev/(2*em)
  return np.r_[kd,vd,np.dot(force,kd)+np.dot(dev,vd),np.dot(S,Ed(t))]
 ref=solve_ivp(rhs,[0,T],np.zeros(14),method='DOP853',rtol=2e-12,atol=1e-14);assert ref.success
 exact=ref.y[:,-1];Sexact=C@(E(T)-exact[:6]-exact[6:12]);results=[]
 for N in [16,32,64]:
  st=old;D=W=0.
  for i in range(N):
   tr=m.trial(st,E((i+1)*T/N),T/N);st=tr.state;D+=float(tr.diagnostics['branch_viscous_dissipation']);W+=float(tr.diagnostics['discrete_material_work'])
  results.append(dict(N=N,stress_relative=float(np.linalg.norm(st.stress-Sexact)/np.linalg.norm(Sexact)),memory_absolute=float(np.linalg.norm(np.r_[st.memory['kelvin'],st.memory['maxwell']]-exact[:12])),D_relative=abs(D-exact[12])/max(1.,abs(exact[12])),W_relative=abs(W-exact[13])/max(1.,abs(exact[13]))))
 ratios=[results[i]['stress_relative']/results[i+1]['stress_relative'] for i in range(2)]
 assert min(ratios)>3.
 out=tmp_path/'ode_accuracy.json';out.write_text(json.dumps(dict(rows=results,stress_error_ratios=ratios,accuracy_1e4_qualified=all(results[-1][k]<=1e-4 for k in ['stress_relative','D_relative','W_relative']),scope='discrete identity separate from continuous work/dissipation accuracy'),indent=2),encoding='utf-8')

def test_grouped_nonzero_transfer_external_response_exact():
 b=build();fix=np.arange(9);f=b.mesh.body_force([0,0,-9.80665]);initial=b.solve(b.initial_state(),f,fix,np.zeros(9),0.)
 old=b.solve(initial.state,f,fix,np.zeros(9),1e6).state
 assert any(np.count_nonzero(v) for v in old.material.memory.values())
 m,st=transfer_grouped(b,old)
 assert old.u.tobytes()==st.u.tobytes() and statebytes(old.material)==statebytes(st.material)
 f0,K0,r0=b.assemble(old,old.u,0.);f1,K1,r1=m.assemble(st,st.u,0.)
 assert np.array_equal(f0,f1) and np.array_equal(K0,K1) and np.array_equal(r0.P,r1.P)

def test_small_strain_limit_and_pure_bulk_no_fictitious_creep():
 s=source();m=MidpointLubby(s.inner);z=m.initial_state();H=np.array([[.3,.2,.1],[.1,-.2,0],[0,.1,.1]]);errors=[]
 for eps in [1e-3,1e-4]:
  F=np.eye(3)+eps*H;r=finite_response(m,z,F,1e6);e=mandel(eps*.5*(H+H.T));linear=m.trial(z,e,1e6)
  from vkm_world.representations.world0.tl_core import MANDEL
  S=np.einsum('a,aij->ij',linear.stress,MANDEL);errors.append(np.linalg.norm(r.P-S)/np.linalg.norm(S))
 assert errors[0]/errors[1]>5
 mm=MidpointMaxwell(1.25e9,5e8,1e17);old=mm.initial_state();E=np.array([.01,.01,.01,0,0,0])
 t=mm.trial(old,E,1e6);mm._validate_state(t.state)
 assert np.linalg.norm(t.state.memory['creep'])<1e-18
