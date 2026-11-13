"""Dry grouped WEDGE6/6GP adapter of existing TL solver. SI/reference measures.
No full-void/C4 transfer, fluid, thermal, new solver backend, or field admission.
"""
from dataclasses import dataclass,field
import hashlib
import numpy as np
from vkm_world.representations.world0.multiseam import _quadrature
from vkm_world.representations.world0.tl_core import TLModel,FiniteState,MANDEL,green,frozen,evaluate_material
from vkm_world.representations.world0.finite_lubby import ReferentialLubby,real,algorithmic_admission
from vkm_world.representations.world0.constitutive import ConstitutiveState,Trial,DeviatoricMaxwell,H,P

def signature(*values):
 h=hashlib.sha256(b'dry-grouped-WEDGE6-full6GP/1')
 for v in values:
  if isinstance(v,str):h.update(v.encode())
  else:
   a=np.asarray(v);h.update(str((a.dtype.str,a.shape)).encode());h.update(a.tobytes())
 return h.hexdigest()
def indices(x,name):
 a=np.asarray(x)
 if a.dtype.kind not in 'iu':raise ValueError(name+' integer array required')
 return np.asarray(a,dtype=np.int64)
def scalar_dt(dt):
 a=real(dt,'dt')
 if a.shape!=() or a<0:raise ValueError('nonnegative scalar time required')
 return float(a)
def mv(A,x):return np.einsum('...ij,...j->...i',A,x)

@dataclass(frozen=True,slots=True)
class ReferenceElastic:
 """Fixed-memory SVK using exactly the existing isotropic compliance formula."""
 K:float
 G:float
 S:np.ndarray=field(init=False)
 C:np.ndarray=field(init=False)
 signature:str=field(init=False)
 def __post_init__(self):
  k=real(self.K,'K');g=real(self.G,'G')
  if k.shape!=() or g.shape!=():raise ValueError('scalar moduli')
  kg=np.array([float(k),float(g)])
  if np.any(kg<=0):raise ValueError('positive elastic moduli')
  S=H/(3*self.K)+P/(2*self.G)
  object.__setattr__(self,'S',frozen(S));object.__setattr__(self,'C',frozen(np.linalg.inv(S)))
  object.__setattr__(self,'signature',signature('SVK_FIXED_MEMORY',kg))
 def initial_state(self,batch_shape=(),time_s=0.):
  z=np.zeros(tuple(batch_shape)+(6,));return ConstitutiveState(time_s,z,z,{},self.signature)
 def _check(self,state):
  if state.signature!=self.signature or state.memory:raise ValueError('elastic state identity')
  if not np.allclose(state.stress,mv(self.C,state.strain),rtol=1e-12,atol=1e-7):raise ValueError('elastic state closure')
 def trial(self,state,strain,dt):
  self._check(state);dt=scalar_dt(dt);e=real(strain,'strain')
  if e.shape!=state.strain.shape:raise ValueError('elastic strain shape')
  s=mv(self.C,e);new=ConstitutiveState(state.time_s+dt,e,s,{},self.signature)
  return Trial(new,s,np.broadcast_to(self.C,e.shape[:-1]+(6,6)),{'branch_viscous_dissipation':np.zeros(e.shape[:-1])})
 def instantaneous(self,state,strain):return self.trial(state,strain,0.)
 def commit(self,trial):self._check(trial.state);return trial.state

@dataclass(frozen=True,slots=True)
class Group:
 point_ids:np.ndarray
 law:object
 def __post_init__(self):
  p=indices(self.point_ids,'group points')
  if p.ndim!=1 or len(p)==0 or np.any(p<0) or len(np.unique(p))!=len(p):raise ValueError('nonempty unique group points')
  if not isinstance(self.law,(ReferentialLubby,ReferenceElastic,DeviatoricMaxwell)):raise ValueError('unsupported grouped law')
  object.__setattr__(self,'point_ids',frozen(p,np.int64))

@dataclass(frozen=True,slots=True)
class GroupedLaw:
 groups:tuple
 point_shape:tuple
 signature:str=field(init=False)
 def __post_init__(self):
  shape=indices(self.point_shape,'point shape')
  if shape.ndim!=1 or len(shape)!=2 or shape[1]!=6 or np.any(shape<=0):raise ValueError('WEDGE cell,6GP shape')
  groups=tuple(self.groups);n=int(np.prod(shape));ids=np.concatenate([g.point_ids for g in groups]) if groups else np.empty(0,int)
  if len(ids)!=n or not np.array_equal(np.sort(ids),np.arange(n)):raise ValueError('groups must cover everyGP exactly once')
  object.__setattr__(self,'groups',groups);object.__setattr__(self,'point_shape',tuple(shape.tolist()))
  object.__setattr__(self,'signature',signature('GROUPED_ES',shape,*[signature(g.point_ids,g.law.signature) for g in groups]))
 def initial_state(self,batch_shape=None,time_s=0.):
  if batch_shape is not None and tuple(batch_shape)!=self.point_shape:raise ValueError('group shape')
  states=[g.law.initial_state((len(g.point_ids),),time_s) for g in self.groups]
  return self._join(states)
 def _extract(self,state,i):
  g=self.groups[i];prefix=str(i)+'/'
  return ConstitutiveState(state.time_s,state.strain.reshape(-1,6)[g.point_ids],state.stress.reshape(-1,6)[g.point_ids],{k[len(prefix):]:v for k,v in state.memory.items() if k.startswith(prefix)},g.law.signature)
 def _join(self,states):
  n=int(np.prod(self.point_shape));e=np.empty((n,6));s=e.copy();mem={};time=states[0].time_s
  for i,(g,state) in enumerate(zip(self.groups,states)):
   if state.time_s!=time:raise ValueError('group clocks differ')
   e[g.point_ids]=state.strain;s[g.point_ids]=state.stress
   mem.update({str(i)+'/'+k:v for k,v in state.memory.items()})
  return ConstitutiveState(time,e.reshape(self.point_shape+(6,)),s.reshape(self.point_shape+(6,)),mem,self.signature)
 def validate(self,state):
  if state.signature!=self.signature or state.strain.shape!=self.point_shape+(6,):raise ValueError('grouped state identity/shape')
  expected=set()
  for i,g in enumerate(self.groups):
   part=self._extract(state,i)
   if isinstance(g.law,(ReferenceElastic,ReferentialLubby)):g.law._check(part)
   else:
    g.law._validate_state(part)
    estimate=mv(g.law.S,part.stress)+part.memory['creep']
    scale=np.maximum(1e-7,np.maximum(np.linalg.norm(estimate,axis=-1),np.linalg.norm(part.strain,axis=-1)))
    if np.any(np.linalg.norm(part.strain-estimate,axis=-1)>5e-10*scale):raise ValueError('Maxwell strain-stress-memory closure')
   expected.update(str(i)+'/'+k for k in part.memory)
  if expected!=set(state.memory):raise ValueError('unknown grouped memory namespace')
 def _step(self,state,strain,dt):
  self.validate(state);e=real(strain,'grouped strain');dt=scalar_dt(dt)
  if e.shape!=state.strain.shape:raise ValueError('grouped strain shape')
  trials=[];D=np.empty(self.point_shape+(6,6));flatD=D.reshape(-1,6,6);diss=np.zeros(int(np.prod(self.point_shape)))
  for i,g in enumerate(self.groups):
   old=self._extract(state,i);target=e.reshape(-1,6)[g.point_ids]
   tr=g.law.instantaneous(old,target) if dt==0 else g.law.trial(old,target,dt)
   trials.append(tr);flatD[g.point_ids]=tr.tangent
   if dt>0:
    key='physical_dissipation' if isinstance(g.law,DeviatoricMaxwell) else 'branch_viscous_dissipation'
    diss[g.point_ids]=tr.diagnostics[key]
  new=self._join([t.state for t in trials]);return Trial(new,new.stress,D,{'branch_viscous_dissipation':diss.reshape(self.point_shape),'stored_energy_density':self.energy(new)})
 def trial(self,state,strain,dt):
  if scalar_dt(dt)<=0:raise ValueError('trial positive dt; instantaneous for0')
  return self._step(state,strain,dt)
 def instantaneous(self,state,strain):return self._step(state,strain,0.)
 def commit(self,trial):self.validate(trial.state);return trial.state
 def revert(self,state,trial=None):self.validate(state);return state
 def restore(self,data):
  if not isinstance(data,dict) or set(data)!={'schema','time_s','strain','stress','memory','signature'}:raise ValueError('checkpoint keys')
  if data['schema']!='vkm.constitutive-state/1':raise ValueError('checkpoint schema')
  scalar_dt(data['time_s'])
  for n in ['strain','stress']:real(data[n],n)
  if not isinstance(data['memory'],dict):raise ValueError('memory mapping')
  for name,entry in data['memory'].items():
   if not isinstance(entry,dict) or set(entry)!={'shape','values'}:raise ValueError('memory record')
   shape=indices(entry['shape'],'memory shape');a=real(entry['values'],name)
   if shape.ndim!=1 or np.any(shape<0) or tuple(shape.tolist())!=a.shape:raise ValueError('memory shape declaration')
  state=ConstitutiveState.from_dict(data);self.validate(state);return state
 def energy(self,state):
  self.validate(state);v=np.empty(int(np.prod(self.point_shape)))
  for i,g in enumerate(self.groups):
   part=self._extract(state,i)
   if isinstance(g.law,ReferentialLubby):val=g.law.energy(part)
   else:val=.5*np.sum(part.stress*mv(g.law.S,part.stress),axis=-1)
   v[g.point_ids]=val
  return v.reshape(self.point_shape)
 def elastic_defect(self,old,new):
  self.validate(old);self.validate(new);v=np.empty(int(np.prod(self.point_shape)))
  for i,g in enumerate(self.groups):
   ds=self._extract(new,i).stress-self._extract(old,i).stress;S=g.law.inner.S if isinstance(g.law,ReferentialLubby) else g.law.S
   v[g.point_ids]=.5*np.sum(ds*mv(S,ds),axis=-1)
  return v.reshape(self.point_shape)

@dataclass(frozen=True,slots=True)
class WedgeMesh:
 nodes:np.ndarray
 cells:np.ndarray
 density:np.ndarray
 fraction:np.ndarray=1.
 grad:np.ndarray=field(init=False)
 shape:np.ndarray=field(init=False)
 weights:np.ndarray=field(init=False)
 dofs:np.ndarray=field(init=False)
 signature:str=field(init=False)
 def __post_init__(self):
  x=real(self.nodes,'nodes');c=indices(self.cells,'cells')
  if x.ndim!=2 or x.shape[1]!=3 or c.ndim!=2 or c.shape[1]!=6 or len(c)==0:raise ValueError('WEDGE nodes/cells shape')
  if np.any(c<0) or np.any(c>=len(x)) or any(len(np.unique(row))!=6 for row in c):raise ValueError('WEDGE connectivity')
  rho=np.broadcast_to(real(self.density,'density'),(len(c),));a=np.broadcast_to(real(self.fraction,'fraction'),(len(c),))
  if np.any(rho<=0) or np.any(a<=0) or np.any(a>1):raise ValueError('positive density and0<fraction<=1; fullvoid unsupported')
  grad=[];weights=[];shape=[];coords=x[c]
  for dn,N,w in _quadrature(6):
   J=coords.swapaxes(-2,-1)@dn;det=np.linalg.det(J)
   if not np.isfinite(det).all() or np.any(det<=0):raise ValueError('reference WEDGE Jacobian')
   grad.append(dn@np.linalg.inv(J));weights.append(det*w);shape.append(N)
  values={'nodes':x,'cells':c,'density':rho,'fraction':a,'grad':np.stack(grad,axis=1),'weights':np.stack(weights,axis=1),'shape':np.stack(shape),'dofs':(3*c[:,:,None]+np.arange(3)).reshape(-1,18)}
  for name,val in values.items():object.__setattr__(self,name,frozen(val,np.int64 if name in ['cells','dofs'] else float))
  object.__setattr__(self,'signature',signature('WEDGE6_FULL6GP_REFERENCE',x,c,rho,a))
 @property
 def measures(self):return self.weights*self.fraction[:,None]
 def deformation_gradient(self,u):
  u=real(u,'u')
  if u.shape!=self.nodes.shape:raise ValueError('u shape')
  return np.eye(3)+np.einsum('eai,egaj->egij',u[self.cells],self.grad)
 def body_force(self,acceleration):
  b=real(acceleration,'acceleration')
  if b.shape!=(3,):raise ValueError('acceleration3')
  fe=np.einsum('eg,e,ga,i->eai',self.measures,self.density,self.shape,b);f=np.zeros_like(self.nodes);np.add.at(f,self.cells,fe);return f

class WedgeModel(TLModel):
 """Existing TLModel.solve inherited verbatim; only WEDGE geometry/assembly differ."""
 def __post_init__(self):
  if not isinstance(self.mesh,WedgeMesh) or not isinstance(self.law,GroupedLaw) or self.law.point_shape!=(len(self.mesh.cells),6):raise ValueError('WEDGE/group shape')
  super().__post_init__()
 def initial_state(self):return FiniteState(np.zeros_like(self.mesh.nodes),self.law.initial_state(),self.signature)
 def validate_state(self,old):
  if old.model_signature!=self.signature:raise ValueError('finite WEDGE state signature')
  self.law.validate(old.material);E,_=green(self.mesh.deformation_gradient(old.u));em=np.einsum('aij,egij->ega',MANDEL,E)
  if not np.allclose(old.material.strain,em,rtol=1e-11,atol=1e-13):raise ValueError('accepted strain/geometry differ')
 def restore(self,u,material_data,model_signature):
  state=FiniteState(real(u,'checkpoint u'),self.law.restore(material_data),model_signature)
  self.validate_state(state);return state
 def assemble(self,old,u,dt):
  self.validate_state(old);dt=scalar_dt(dt)
  ndof=self.mesh.nodes.size
  if ndof*ndof*8>256*1024**2:raise MemoryError('dense WEDGE matrix preflight cap256MiB')
  response=evaluate_material(self.law,old.material,self.mesh.deformation_gradient(u),dt);algorithmic_admission(response)
  w=self.mesh.measures;g=self.mesh.grad
  fe=np.einsum('eg,egij,egaj->eai',w,response.P,g)
  ke=np.einsum('eg,egaj,egijkl,egbl->eaibk',w,g,response.A,g).reshape(-1,18,18)
  f=np.zeros_like(self.mesh.nodes);np.add.at(f,self.mesh.cells,fe);K=np.zeros((ndof,ndof));d=self.mesh.dofs
  np.add.at(K,(d[:,:,None],d[:,None,:]),ke)
  return f,K,response
 def work_audit(self,old,step,external_force):
  self.validate_state(old);self.validate_state(step.state);ext=real(external_force,'external force')
  if ext.shape!=old.u.shape:raise ValueError('external force shape')
  du=step.state.u-old.u;F0=self.mesh.deformation_gradient(old.u);F1=step.response.F;dF=F1-F0;w=self.mesh.measures
  energy=float(np.sum(w*(self.law.energy(step.state.material)-self.law.energy(old.material))))
  D=float(np.sum(w*step.response.material_trial.diagnostics['branch_viscous_dissipation']))
  elastic=float(np.sum(w*self.law.elastic_defect(old.material,step.state.material)))
  geometric=float(.5*np.sum(w*np.einsum('egij,egki,egkj->eg',step.response.S,dF,dF)))
  external=float(np.sum(ext*du));internal=float(np.sum(w*np.einsum('egij,egij->eg',step.response.P,dF)))
  algebra=internal-energy-D-elastic-geometric
  return dict(external_work_J=external,internal_endpoint_work_J=internal,stored_energy_change_J=energy,physical_branch_dissipation_J=D,elastic_endpoint_defect_J=elastic,geometric_endpoint_defect_J=geometric,algebraic_residual_J=algebra,equilibrium_work_residual_J=external-internal,physical_firstlaw_defect_J=external-energy-D,scope='Endpoint reconstruction diagnostic, not exact global physical work per finite interval; fixed activity only')
