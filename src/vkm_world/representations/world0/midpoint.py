"""Implicit midpoint numerical representation of the SAME mG0 reference laws."""
import hashlib
import numpy as np
from vkm_world.representations.world0.constitutive import ConstitutiveState,Trial,DeviatoricMaxwell,P,_deviator
from vkm_world.representations.world0.finite_lubby import ReferentialLubby,real,positive_dt,mv,dot,UnsupportedClassicalTangent
SCHEME='reference-ES-midpoint/1'

def qstress(S):
 s=_deviator(S);return s,np.sqrt(1.5*dot(s,s))

class MidpointLubby(ReferentialLubby):
 def __post_init__(self):
  super().__post_init__()
  object.__setattr__(self,'signature',hashlib.sha256((SCHEME+'|'+self.signature).encode()).hexdigest())
 def _mid_response(self,old,Sm,h):
  s,q=qstress(Sm);k0=old.memory['kelvin'];v0=old.memory['maxwell'];law=self.inner
  with np.errstate(over='raise',under='raise',invalid='raise',divide='raise'):
   ek=law.etaK0*np.exp(law.mK*q);em=law.etaM0*np.exp(law.mM*q)
   x=h*law.GK0/(2*ek);a=1/(1+x);b=x/(2*law.GK0*(1+x));c=h/(4*em)
   da=law.mK*x/(1+x)**2;db=-law.mK*x/(2*law.GK0*(1+x)**2);dc=-law.mM*c
  km=a[...,None]*k0+b[...,None]*s;vm=v0+c[...,None]*s
  estimate=mv(law.S,Sm)+km+vm
  safe=np.where(q>0,q,1.);dq=np.where((q>0)[...,None],1.5*s/safe[...,None],0.)
  der=da[...,None]*k0+(db+dc)[...,None]*s
  J=law.S+(b+c)[...,None,None]*P+der[..., :,None]*dq[...,None,:]
  force=s-2*law.GK0*km;D=h*(dot(force,force)/(2*ek)+dot(s,s)/(2*em))
  return estimate,J,km,vm,D,x,h*law.G0/(2*em)
 def _path_guard(self,old,Sm,new):
  if self.inner.mK==0:return
  K=max(self.inner.K0,self.inner.G0);eps=64*np.finfo(float).eps
  for S,k in [(old.stress,old.memory['kelvin']),(Sm,old.memory['kelvin']),(new.stress,new.memory['kelvin'])]:
   _,q=qstress(S);floor=eps*np.maximum(K,np.linalg.norm(S,axis=-1))
   if np.any(np.any(k!=0,axis=-1)&(q<=floor)):raise UnsupportedClassicalTangent('midpoint endpoint/mid q cusp with Kelvin memory')
  s0,_=qstress(old.stress);s1,_=qstress(new.stress);ds=s1-s0;den=dot(ds,ds)
  alpha=np.divide(-dot(s0,ds),den,out=np.zeros_like(den),where=den>0);closest=s0+alpha[...,None]*ds
  crossing=(alpha>0)&(alpha<1)&(np.sqrt(1.5*dot(closest,closest))<=eps*K)&np.any(old.memory['kelvin']!=0,axis=-1)
  if np.any(crossing):raise UnsupportedClassicalTangent('unqualified straight stress chord q=0; not an ODE nonexistence claim')
 def trial(self,old,strain,dt_s):
  self._check(old);E=real(strain,'Green strain');h=positive_dt(dt_s)
  if E.shape!=old.strain.shape:raise ValueError('strain shape')
  target=.5*(old.strain+E)+self.offset(E.shape);scale=np.maximum(1e-7,np.linalg.norm(target,axis=-1));Sm=old.stress.copy()
  for it in range(60):
   estimate,J,km,vm,D,x,y=self._mid_response(old,Sm,h);res=estimate-target;norm=np.linalg.norm(res,axis=-1)/scale
   if np.max(norm)<2e-11:break
   direction=np.linalg.solve(J,res[...,None])[...,0];alpha=np.ones_like(norm)
   for _ in range(30):
    candidate=Sm-alpha[...,None]*direction
    try:good=np.linalg.norm(self._mid_response(old,candidate,h)[0]-target,axis=-1)/scale<norm
    except (FloatingPointError,OverflowError):good=np.zeros_like(norm,dtype=bool)
    good=good|(norm<2e-11)
    if np.all(good):break
    alpha=np.where(good,alpha,alpha*.5)
   else:raise RuntimeError('midpoint material line search failed')
   Sm=candidate
  else:raise RuntimeError('midpoint material Newton failed')
  if np.any(x>1) or np.any(y>1):raise ValueError('midpoint nonoscillatory linear-limit domain exceeded; refine dt')
  S1=2*Sm-old.stress;memory={'kelvin':2*km-old.memory['kelvin'],'maxwell':2*vm-old.memory['maxwell']}
  new=ConstitutiveState(old.time_s+h,E,S1,memory,self.signature);self._check(new);self._path_guard(old,Sm,new)
  tangent=np.linalg.inv(J);work=dot(Sm,E-old.strain);delta=self.energy(new)-self.energy(old);residual=work-delta-D
  if not all(np.isfinite(a).all() for a in [tangent,D,work,residual]) or np.any(D<0):raise ValueError('nonfinite/nonpassive midpoint result')
  return Trial(new,S1,tangent,dict(branch_viscous_dissipation=D,midpoint_stress=Sm,discrete_material_work=work,material_energy_residual=residual,local_iterations=it,local_residual=float(np.max(norm)),scheme=SCHEME,energy_scope='Dmid quadrature, not exact continuous heat'))
 def trial_stress(self,*args,**kwargs):raise NotImplementedError('midpoint strain-controlled API only; no inherited endpoint update')
 def held_stress_audit(self,*args,**kwargs):raise NotImplementedError('endpoint held-stress audit does not apply to midpoint')

class MidpointMaxwell(DeviatoricMaxwell):
 def __init__(self,K0,G0,eta_shear):
  super().__init__(K0,G0,eta_shear)
  object.__setattr__(self,'signature',hashlib.sha256((SCHEME+'|'+self.signature).encode()).hexdigest())
 def _validate_state(self,state):
  super()._validate_state(state)
  estimate=mv(self.S,state.stress)+state.memory['creep'];scale=np.maximum(1e-7,np.maximum(np.linalg.norm(estimate,axis=-1),np.linalg.norm(state.strain,axis=-1)))
  if np.any(np.linalg.norm(estimate-state.strain,axis=-1)>5e-10*scale):raise ValueError('Maxwell state closure')
 def trial(self,old,strain,dt_s):
  self._validate_state(old);E=real(strain,'strain');h=positive_dt(dt_s)
  if E.shape!=old.strain.shape:raise ValueError('strain shape')
  eta=self._eta(E.shape[:-1]);ratio=h*self.G0/(2*eta)
  if np.any(ratio>1):raise ValueError('midpoint Maxwell nonoscillatory linear-limit domain exceeded')
  J=self.S+(h/(4*eta))[...,None,None]*P;Dalg=np.linalg.inv(J)
  Sm=mv(Dalg,.5*(old.strain+E)-old.memory['creep']);s=_deviator(Sm)
  creep=_deviator(old.memory['creep']+h*s/(2*eta[...,None]));S1=2*Sm-old.stress
  new=ConstitutiveState(old.time_s+h,E,S1,{'creep':creep},self.signature)
  D=h*dot(s,s)/(2*eta);delta=.5*(dot(S1,mv(self.S,S1))-dot(old.stress,mv(self.S,old.stress)));work=dot(Sm,E-old.strain)
  return Trial(new,S1,Dalg,dict(physical_dissipation=D,midpoint_stress=Sm,discrete_material_work=work,material_energy_residual=work-delta-D,scheme=SCHEME))
 def trial_stress(self,*args,**kwargs):raise NotImplementedError('midpoint strain-controlled API only')
 def instantaneous(self,old,strain):return super().instantaneous(old,real(strain,'strain'))

def transfer_point(source,target,state):
 """Explicit identical material chart/coefficients; change representation signature."""
 if isinstance(source,ReferentialLubby) and isinstance(target,MidpointLubby):
  source._check(state)
  if source.inner.signature!=target.inner.signature or not np.array_equal(source.reference_strain,target.reference_strain):raise ValueError('foreign law/chart')
 elif isinstance(source,DeviatoricMaxwell) and isinstance(target,MidpointMaxwell):
  source._validate_state(state)
  if source.K0!=target.K0 or source.G0!=target.G0 or not np.array_equal(source.eta_shear,target.eta_shear):raise ValueError('foreign Maxwell parameters')
  estimate=mv(source.S,state.stress)+state.memory['creep']
  if not np.allclose(estimate,state.strain,rtol=5e-10,atol=1e-14):raise ValueError('inconsistent source Maxwell closure')
 else:raise ValueError('unsupported representation transfer')
 result=ConstitutiveState(state.time_s,state.strain,state.stress,dict(state.memory),target.signature)
 if isinstance(target,MidpointLubby):target._check(result)
 else:target._validate_state(result)
 return result
