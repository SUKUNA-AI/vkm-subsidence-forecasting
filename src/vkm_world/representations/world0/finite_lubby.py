"""Objective reference E/S LUBBY(mG=0) family; no automatic whole-mesh restart.

Mandel order xx,yy,zz,sqrt2xy,sqrt2yz,sqrt2xz. Stresses Pa, time s,
energy/dissipation density J/m3 of the declared reference volume.
Existing world0 constitutive and TL kernels perform all constitutive/kinematic updates.
"""
from dataclasses import dataclass, field
import hashlib,json
import numpy as np
from vkm_world.representations.world0.constitutive import Lubby2,ConstitutiveState,Trial,H,P
from vkm_world.representations.world0.tl_core import MANDEL,evaluate_material,green
VERSION='finite-reference-lubby-mG0-prestrain/1'
ADMISSION_VERSION='smooth-classical-positive-algorithmic-LH/1'

class UnsupportedClassicalTangent(ValueError):pass
class UncertifiedFiniteState(ValueError):pass

def real(x,name):
 a=np.asarray(x)
 if a.dtype.kind not in 'iuf' or not np.isfinite(a).all():raise ValueError(name+' requires real finite numeric data')
 return np.asarray(a,dtype=np.float64)
def positive_dt(value):
 a=real(value,'time increment')
 if a.shape!=() or a<=0:raise ValueError('positive scalar dt required')
 return float(a)
def immutable(a):
 a=real(a,'immutable array');return np.frombuffer(a.tobytes(),dtype=np.float64).reshape(a.shape)
def mv(A,x):return np.einsum('...ij,...j->...i',A,x)
def dot(a,b):return np.sum(a*b,axis=-1)

def tensor(x):
 a=real(x,'Mandel tensor')
 if a.shape[-1:]!=(6,):raise ValueError('Mandel6 shape')
 return np.einsum('...a,aij->...ij',a,MANDEL)
def mandel(x):
 a=real(x,'symmetric tensor')
 if a.shape[-2:]!=(3,3) or not np.allclose(a,a.swapaxes(-1,-2),rtol=0,atol=1e-14):raise ValueError('symmetric 3x3 tensor')
 return np.einsum('aij,...ij->...a',MANDEL,a)

def deformation_from_green(e):
 """Chosen polar rotation I, not a unique reconstruction from strain."""
 C=np.eye(3)+2*tensor(e);w,V=np.linalg.eigh(C)
 if np.any(w<=0):raise ValueError('I+2E must be positive definite')
 return (V*np.sqrt(w)[...,None,:])@V.swapaxes(-1,-2)

@dataclass(frozen=True,slots=True,eq=False)
class ReferentialLubby:
 inner: Lubby2
 reference_strain: np.ndarray = field(default_factory=lambda:np.zeros(6))
 signature: str = field(init=False)
 def __post_init__(self):
  if type(self.inner) is not Lubby2 or self.inner.mG!=0.:raise ValueError('existing Lubby2 with mG exactly zero required')
  ref=immutable(self.reference_strain)
  if ref.shape[-1:]!=(6,):raise ValueError('reference strain must be Mandel6')
  object.__setattr__(self,'reference_strain',ref)
  payload=VERSION+'|'+self.inner.signature+'|'+str(ref.shape)+'|'+hashlib.sha256(ref.tobytes()).hexdigest()
  object.__setattr__(self,'signature',hashlib.sha256(payload.encode()).hexdigest())
 def __deepcopy__(self,memo):return self
 def offset(self,shape):
  try:return np.broadcast_to(self.reference_strain,shape)
  except ValueError as exc:raise ValueError('reference strain / state shape mismatch') from exc
 def _check(self,state):
  if not isinstance(state,ConstitutiveState) or state.signature!=self.signature:raise ValueError('foreign finite family state')
  if set(state.memory)!={'kelvin','maxwell'}:raise ValueError('finite LUBBY memory keys')
  for name,a in state.memory.items():
   real(a,name)
   if a.shape!=state.strain.shape:raise ValueError('memory shape mismatch')
   if np.any(abs(np.sum(a[...,:3],axis=-1))>1e-12*np.maximum(1,np.linalg.norm(a,axis=-1))):raise ValueError('deviatoric branch memory required')
  estimate=mv(self.inner.S,state.stress)+state.memory['kelvin']+state.memory['maxwell']
  scale=np.maximum(1e-7,np.linalg.norm(estimate,axis=-1))
  if np.any(np.linalg.norm(state.strain+self.offset(state.strain.shape)-estimate,axis=-1)>5e-10*scale):raise ValueError('inconsistent constitutive state')
 def _underlying(self,state):
  self._check(state)
  return ConstitutiveState(state.time_s,state.strain+self.offset(state.strain.shape),state.stress,dict(state.memory),self.inner.signature)
 def _convert(self,result):
  s=result.state
  new=ConstitutiveState(s.time_s,s.strain-self.offset(s.strain.shape),s.stress,dict(s.memory),self.signature)
  diagnostics=dict(result.diagnostics,finite_family=VERSION,stress_measure='SECOND_PIOLA_REFERENCE',energy_scope='mG0 state potential per reference volume; not full global finite first law')
  return Trial(new,result.stress,result.tangent,diagnostics)
 def initial_state(self,batch_shape=None,time_s=0.):
  shape=self.reference_strain.shape[:-1] if batch_shape is None else tuple(batch_shape)
  old=self.inner.initial_state(shape,time_s);target=self.offset(old.strain.shape)
  return self._convert(self.inner.instantaneous(old,target)).state
 @classmethod
 def anchored_from_small(cls,inner,old):
  """Explicit new F=I reference chart. Keeps time and both irreversible memories.

  Only the material-point energy/stress/nominal response at the new reference
  anchor is retained. This is NOT a geometry/volume/port map of an old mesh.
  """
  inner._validate_state(old)
  model=cls(inner,old.strain)
  state=ConstitutiveState(old.time_s,np.zeros_like(old.strain),old.stress,dict(old.memory),model.signature)
  model._check(state);return model,state
 def _classical_trial(self,old,trial):
  # No regularization: refuse to publish a classical derivative at the cusp.
  s=mv(P,trial.stress);q=np.sqrt(1.5*dot(s,s))
  floor=64*np.finfo(np.float64).eps*np.maximum(max(self.inner.K0,self.inner.G0),np.linalg.norm(trial.stress,axis=-1))
  active=np.any(old.memory['kelvin']!=0,axis=-1)
  if self.inner.mK!=0. and np.any(active & (q<=floor)):
   raise UnsupportedClassicalTangent('positive-dt Kelvin memory at numerically unresolved q=0: no classical tangent; law not regularized')
  if not np.isfinite(trial.tangent).all():raise UnsupportedClassicalTangent('nonfinite tangent')
  return trial
 def trial(self,state,strain,dt_s):
  e=real(strain,'Green strain')
  if e.shape!=state.strain.shape:raise ValueError('strain shape')
  return self._classical_trial(state,self._convert(self.inner.trial(self._underlying(state),e+self.offset(e.shape),positive_dt(dt_s))))
 def trial_stress(self,state,stress,dt_s):
  s=real(stress,'second Piola stress')
  return self._classical_trial(state,self._convert(self.inner.trial_stress(self._underlying(state),s,positive_dt(dt_s))))
 def instantaneous(self,state,strain):
  e=real(strain,'Green strain')
  if e.shape!=state.strain.shape:raise ValueError('strain shape')
  return self._convert(self.inner.instantaneous(self._underlying(state),e+self.offset(e.shape)))
 def commit(self,trial):self._check(trial.state);return trial.state
 def revert(self,state,trial=None):self._check(state);return state
 def energy(self,state):
  self._check(state)
  return .5*dot(state.stress,mv(self.inner.S,state.stress))+self.inner.GK0*dot(state.memory['kelvin'],state.memory['kelvin'])
 def rates(self,state):
  self._check(state);s=mv(P,state.stress);q=np.sqrt(1.5*dot(s,s));k=state.memory['kelvin'];force=s-2*self.inner.GK0*k
  with np.errstate(over='raise',invalid='raise',divide='raise',under='raise'):
   etaK=self.inner.etaK0*np.exp(self.inner.mK*q);etaM=self.inner.etaM0*np.exp(self.inner.mM*q)
   kd=force/(2*etaK[...,None]);vd=s/(2*etaM[...,None]);d=dot(force,kd)+dot(s,vd)
  if not np.isfinite(d).all() or np.any(d<0):raise ValueError('nonfinite/nonpassive material rate')
  return dict(kelvin_rate=kd,maxwell_rate=vd,dissipation_rate=d,equivalent_second_piola=q)
 def held_stress_audit(self,old,trial):
  self._check(old);self._check(trial.state)
  s0=old.stress;s1=trial.stress;ds=s1-s0
  dk=trial.state.memory['kelvin']-old.memory['kelvin'];dv=trial.state.memory['maxwell']-old.memory['maxwell']
  elastic_ramp_work=.5*dot(s0+s1,mv(self.inner.S,ds))
  work=elastic_ramp_work+dot(s1,dk+dv)
  diss=np.asarray(trial.diagnostics['branch_viscous_dissipation'])
  delta=self.energy(trial.state)-self.energy(old)
  residual=work-delta-diss
  if not all(np.isfinite(a).all() for a in [work,diss,delta,residual]):raise ValueError('nonfinite energy audit')
  return dict(reference_work=work,stored_energy_change=delta,physical_branch_dissipation=diss,residual=residual,scope='Exact elastic stress ramp at fixed memory plus held-second-Piola branch interval; no global endpoint-P work claim')
 def restore(self,data):
  if not isinstance(data,dict) or set(data)!={'schema','time_s','strain','stress','memory','signature'}:raise ValueError('checkpoint keys')
  t=real(data['time_s'],'time')
  if t.shape!=():raise ValueError('scalar time')
  for k in ['strain','stress']:real(data[k],k)
  if not isinstance(data['memory'],dict) or set(data['memory'])!={'kelvin','maxwell'}:raise ValueError('checkpoint memory')
  for name,record in data['memory'].items():
   if set(record)!={'shape','values'}:raise ValueError('memory record')
   a=real(record['values'],name)
   if list(a.shape)!=record['shape']:raise ValueError('memory shape declaration')
  state=ConstitutiveState.from_dict(data);self._check(state);return state

def finite_response(model,state,F,dt_s):
 F=real(F,'F');dt=real(dt_s,'time increment')
 if dt.shape!=() or dt<0:raise ValueError('finite nonnegative scalar dt')
 response=evaluate_material(model,state,F,float(dt))
 algorithmic_admission(response)
 return response

def instantaneous_ellipticity_bound(model,response):
 """Sufficient elastic rank-one lower bound in Pa. Not algorithmic/global stability."""
 smin=np.linalg.svd(response.F,compute_uv=False)[...,-1]
 return np.linalg.eigvalsh(response.S)[...,0]+.5*min(3*model.inner.K0,2*model.inner.G0)*smin*smin


def algorithmic_admission(response):
 """Sufficient pointwise LH bound of actual sym(D), with FP safety margins.
 Negative bound means unqualified, not necessarily physically unstable.
 This is not interval arithmetic, global stability, or field validation.
 """
 D=real(response.material_trial.tangent,'algorithmic tangent')
 if D.shape[-2:]!=(6,6):raise ValueError('Mandel tangent shape')
 sym=(D+D.swapaxes(-1,-2))/2;eps=np.finfo(np.float64).eps
 eig=np.linalg.eigvalsh(sym);dnorm=np.max(abs(eig),axis=-1)
 dmin=eig[...,0];dcert=dmin-64*eps*dnorm
 seig=np.linalg.eigvalsh(real(response.S,'second Piola'))
 scert=seig[...,0]-64*eps*np.max(abs(seig),axis=-1)
 sv=np.linalg.svd(real(response.F,'F'),compute_uv=False)
 fcert=np.maximum(0,sv[...,-1]-64*eps*sv[...,0])
 lower=scert+.5*dcert*fcert*fcert
 if not np.isfinite(lower).all() or np.any(dcert<=0) or np.any(lower<=0):
  raise UncertifiedFiniteState('positive sufficient algorithmic Legendre-Hadamard bound unavailable')
 return dict(symmetric_material_minimum_Pa=dmin,symmetric_material_lower_Pa=dcert,
             sufficient_rankone_lower_Pa=lower,admission_version=ADMISSION_VERSION)


from vkm_world.representations.world0.tl_core import TLModel
class AdmittedTLModel(TLModel):
 """Existing small TET4 solver with the same pointwise admission on every trial.
 No new solver or global stability claim. Fine WEDGE assembly is not this API.
 """
 def assemble(self,old,u,dt):
  result=super().assemble(old,u,dt)
  algorithmic_admission(result[2])
  return result
