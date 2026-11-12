"""One fixed cell/edge spring world and exact nested Galerkin restrictions.
All known-phase memory carriers are retained, including nonuniform initial z.
"""
import numpy as np
from scipy.sparse import coo_matrix,diags
from scipy.sparse.linalg import LinearOperator,cg

def build_world(area,heights,bulk,shear,fractions,rates,edges):
    area,h,bulk,G,f,r=[np.asarray(v,float) for v in (area,heights,bulk,shear,fractions,rates)]
    if h.ndim!=2 or not h.size:raise ValueError('nonempty two-dimensional heights required')
    nc,nl=h.shape;n=nc*nl
    if area.shape!=(nc,) or bulk.shape!=(nl,) or G.shape!=(nl,) or f.ndim!=3 or f.shape[:2]!=(nc,nl) or r.shape!=f.shape:
        raise ValueError('shape mismatch')
    if any(not np.isfinite(v).all() for v in (area,h,bulk,G,f,r)) or np.any(area<=0) or np.any(h<=0) or np.any(bulk<=0) or np.any(G<=0) or np.any(f<0) or np.any(f.sum(2)>1) or np.any(r<0):
        raise ValueError('invalid physical coefficients')
    top=np.arange(n);has_bottom=top%nl<nl-1
    BV=coo_matrix((np.r_[np.ones(n),-np.ones(has_bottom.sum())],(np.r_[top,top[has_bottom]],np.r_[top,top[has_bottom]+1])),shape=(n,n)).tocsr()
    kd=area[:,None]*4*G/(3*h);kb=area[:,None]*bulk/h
    active=(f>0)&(r>0);elastic=1-np.where(active,f,0).sum(2)
    cell,phase=np.nonzero(active.reshape(n,-1));B=BV[cell];km=kd.ravel()[cell]*f.reshape(n,-1)[cell,phase];rate=r.reshape(n,-1)[cell,phase]
    K0=BV.T@diags((kb+kd*elastic).ravel())@BV
    rows=[];cols=[];values=[]
    for a,b,face,distance in edges:
        numeric=np.asarray([a,b,distance],float)
        if not np.isfinite(numeric).all() or np.any(numeric[:2]!=np.floor(numeric[:2])):raise ValueError('nonfinite distance or noninteger cell index')
        a,b=int(a),int(b);face=np.asarray(face,float)
        if a==b or not(0<=a<nc and 0<=b<nc) or distance<=0 or face.shape!=(nl,) or np.any(face<=0) or not np.isfinite(face).all():raise ValueError('invalid face')
        k=G*face/distance;i=a*nl+np.arange(nl);j=b*nl+np.arange(nl)
        rows.extend([i,j,i,j]);cols.extend([i,j,j,i]);values.extend([k,k,-k,-k])
    L=coo_matrix((np.concatenate(values),(np.concatenate(rows),np.concatenate(cols))),shape=(n,n)).tocsr() if rows else K0*0
    return dict(K0=(K0+L).tocsr(),B=B.tocsr(),km=km,inv_tau=rate,branch_cell=cell,branch_phase=phase,nlayers=nl,area=area,heights=h,phase_fractions=f,elastic_fraction=elastic,lateral=L)

def restriction(labels,nlayers):
    raw=np.asarray(labels,float)
    if raw.ndim!=1 or not len(raw) or not np.isfinite(raw).all() or np.any(raw!=np.floor(raw)):raise ValueError('finite integer partition labels required')
    if not np.isfinite(nlayers) or nlayers<=0 or nlayers!=int(nlayers):raise ValueError('positive integer layer count required')
    nlayers=int(nlayers);labels=raw.astype(int)
    if np.any(labels<0) or set(labels)!=set(range(labels.max()+1)):raise ValueError('partition labels must be consecutive')
    row=np.arange(len(labels)*nlayers);col=np.repeat(labels,nlayers)*nlayers+np.tile(np.arange(nlayers),len(labels))
    return coo_matrix((np.ones(len(row)),(row,col)),shape=(len(row),(labels.max()+1)*nlayers)).tocsr()

def restrict(model,P):
    return dict(K0=(P.T@model['K0']@P).tocsr(),B=(model['B']@P).tocsr(),km=model['km'].copy(),inv_tau=model['inv_tau'].copy(),branch_cell=model['branch_cell'].copy(),branch_phase=model['branch_phase'].copy(),nlayers=model['nlayers'])

def block_preconditioner(A,nlayers):
    n=A.shape[0];nc=n//nlayers
    if nc*nlayers!=n:raise ValueError('invalid column block size')
    diagonal=A.diagonal().reshape(nc,nlayers).copy();upper=np.r_[A.diagonal(1),0].reshape(nc,nlayers)[:,:-1].copy();mult=np.zeros_like(upper)
    for j in range(1,nlayers):mult[:,j-1]=upper[:,j-1]/diagonal[:,j-1];diagonal[:,j]-=mult[:,j-1]*upper[:,j-1]
    if np.any(diagonal<=0):raise ValueError('nonpositive block')
    def apply(x):
        b=np.asarray(x).reshape(nc,nlayers).copy()
        for j in range(1,nlayers):b[:,j]-=mult[:,j-1]*b[:,j-1]
        b[:,-1]/=diagonal[:,-1]
        for j in range(nlayers-2,-1,-1):b[:,j]=(b[:,j]-upper[:,j]*b[:,j+1])/diagonal[:,j]
        return b.ravel()
    return LinearOperator(A.shape,matvec=apply,dtype=float)

def integrate(model,forces,dt,initial=None,rtol=1e-12):
    K0,B,km,rate=[model[k] for k in ('K0','B','km','inv_tau')];forces=np.asarray(forces,float);n=K0.shape[0];nb=len(km)
    if not np.isfinite(dt) or dt<=0 or forces.ndim!=2 or forces.shape[1]!=n or not np.isfinite(forces).all():raise ValueError('invalid path')
    q,z=(np.zeros(n),np.zeros(nb)) if initial is None else tuple(np.array(v,float,copy=True) for v in initial)
    if q.shape!=(n,) or z.shape!=(nb,) or not np.isfinite(q).all() or not np.isfinite(z).all():raise ValueError('invalid initial')
    rr=dt*rate;ke=km/(1+rr);A=(K0+B.T@diags(ke)@B).tocsr();M=block_preconditioner(A,model['nlayers'])
    force_scale=max(1,np.linalg.norm(forces,axis=1).max());qs=[];zs=[];energy=[];residual=[];iterations=[]
    for f in forces:
        rhs=f+B.T@(ke*z);counter=[0]
        def tick(_):counter[0]+=1
        qn,info=cg(A,rhs,x0=q,M=M,rtol=rtol,atol=0,maxiter=4000,callback=tick)
        if info:raise RuntimeError(f'CG did not converge: {info}')
        new_d=B@qn;zn=(z+rr*new_d)/(1+rr);old=B@q-z;new=new_d-zn;dq=qn-q
        de=.5*(qn@(K0@qn)-q@(K0@q))+.5*np.dot(km,new**2-old**2);dis=np.dot(km*rr,new**2);num=.5*dq@(K0@dq)+.5*np.dot(km,(new-old)**2);work=f@dq
        energy.append(abs(work-de-dis-num)/max(1,abs(work),abs(de),dis));residual.append(np.linalg.norm(K0@qn+B.T@(km*new)-f)/force_scale);iterations.append(counter[0]);qs.append(qn);zs.append(zn);q,z=qn,zn
    return dict(q=np.array(qs),z=np.array(zs),energy_error=max(energy),force_residual=max(residual),iterations=iterations,A=A)


# The assembly/restriction/integration functions above are retained verbatim from
# the executed, validation-repaired scalar core. The transactional API below adds
# immutable state and explicit physical-model versus representation bindings.
from dataclasses import dataclass
import hashlib


def _freeze(value, dtype=float):
    a=np.asarray(value,dtype=dtype)
    return np.frombuffer(a.tobytes(),dtype=a.dtype).reshape(a.shape)


def _digest(*values):
    h=hashlib.sha256(b'scalar-phase-graph/1')
    for value in values:
        if isinstance(value,str):h.update(value.encode())
        else:
            a=np.asarray(value);h.update(str((a.shape,a.dtype.str)).encode());h.update(a.tobytes())
    return h.hexdigest()


@dataclass(frozen=True,slots=True)
class _CSR:
    data: np.ndarray
    indices: np.ndarray
    indptr: np.ndarray
    shape: tuple

    def __post_init__(self):
        for name in ('indices','indptr'):
            a=np.asarray(getattr(self,name))
            if a.dtype.kind not in 'iu':raise ValueError('integer CSR indices required')
            object.__setattr__(self,name,_freeze(a,np.int64))
        object.__setattr__(self,'data',_freeze(self.data))
        if not np.isfinite(self.data).all():raise ValueError('finite CSR coefficients required')
        shape=tuple(self.shape)
        if len(shape)!=2 or any(not isinstance(x,(int,np.integer)) or x<0 for x in shape):raise ValueError('invalid CSR shape')
        object.__setattr__(self,'shape',shape)
        if self.data.ndim!=1 or self.indices.shape!=self.data.shape or self.indptr.shape!=(shape[0]+1,) or self.indptr[0]!=0 or self.indptr[-1]!=len(self.data) or np.any(np.diff(self.indptr)<0) or np.any(self.indices<0) or np.any(self.indices>=shape[1]):raise ValueError('invalid CSR structure')

    def __deepcopy__(self,memo):return self
    def __reduce__(self):return (type(self),(self.data,self.indices,self.indptr,self.shape))

    @classmethod
    def capture(cls,matrix):
        a=matrix.tocsr(copy=True);a.sum_duplicates();a.sort_indices()
        return cls(_freeze(a.data),_freeze(a.indices,np.int64),_freeze(a.indptr,np.int64),a.shape)

    def array(self):
        from scipy.sparse import csr_matrix
        return csr_matrix((self.data.copy(),self.indices.copy(),self.indptr.copy()),shape=self.shape)

    def signature(self):return _digest(self.data,self.indices,self.indptr,np.array(self.shape))


@dataclass(frozen=True,slots=True)
class GraphState:
    q: np.ndarray
    memory: np.ndarray
    time_s: float
    world_id: str
    physical_signature: str
    representation_signature: str

    def __post_init__(self):
        for name in ('q','memory'):
            a=np.asarray(getattr(self,name),float)
            if a.ndim!=1 or not np.isfinite(a).all():raise ValueError('finite state vectors required')
            object.__setattr__(self,name,_freeze(a))
        if not np.isfinite(self.time_s):raise ValueError('finite time required')

    def __deepcopy__(self,memo):return self

    def __reduce__(self):
        return (type(self),tuple(getattr(self,name) for name in self.__dataclass_fields__))

    def checkpoint(self):
        """Plain runtime payload; persistence belongs to existing checkpoint storage."""
        return dict(q=self.q.tolist(),memory=self.memory.tolist(),time_s=self.time_s,
                    world_id=self.world_id,physical_signature=self.physical_signature,
                    representation_signature=self.representation_signature)

    @classmethod
    def restore(cls,payload):return cls(**payload)


@dataclass(frozen=True,slots=True)
class GraphStep:
    state: GraphState
    energy_change_J: float
    physical_dissipation_J: float
    numerical_dissipation_J: float
    external_work_J: float
    relative_force_residual: float
    relative_energy_residual: float
    relative_memory_residual: float


@dataclass(frozen=True,slots=True)
class ScalarGraph:
    """Fixed column/shear spring hypothesis, with all phase memory retained.

    Construct via from_columns; geometry/knowledge admission remains external.
    IDs come from existing world binding. A coefficient signature prevents state
    reuse even if a caller mistakenly retains an old descriptive world_id.
    """
    _K0: _CSR
    _B: _CSR
    km: np.ndarray
    inverse_tau: np.ndarray
    branch_carrier: np.ndarray
    branch_phase: np.ndarray
    nlayers: int
    world_id: str
    physical_signature: str
    representation_signature: str
    unknown_fraction: object = None

    def __post_init__(self):
        for name in ('km','inverse_tau'):
            a=np.asarray(getattr(self,name),float)
            if a.ndim!=1 or not np.isfinite(a).all() or np.any(a<0):raise ValueError('invalid branch coefficients')
            object.__setattr__(self,name,_freeze(a))
        for name in ('branch_carrier','branch_phase'):
            raw=np.asarray(getattr(self,name))
            if raw.dtype.kind not in 'iu' or np.any(raw<0):raise ValueError('integer nonnegative branch IDs required')
            object.__setattr__(self,name,_freeze(raw,np.int64))
        n=self._K0.shape[0];nb=len(self.km)
        if self._K0.shape!=(n,n) or self._B.shape!=(nb,n) or any(len(a)!=nb for a in (self.inverse_tau,self.branch_carrier,self.branch_phase)):
            raise ValueError('operator/state dimensions disagree')
        if self.unknown_fraction is not None:
            a=np.asarray(self.unknown_fraction,float)
            if a.ndim!=2 or not np.isfinite(a).all() or np.any((a<0)|(a>1)):raise ValueError('invalid knowledge mask fraction')
            object.__setattr__(self,'unknown_fraction',_freeze(a))
        expected=_digest(self.physical_signature,self._K0.signature(),self._B.signature(),
            self.km,self.inverse_tau,self.branch_carrier,self.branch_phase,np.array([self.nlayers]),
            'unknown_fraction_not_supplied' if self.unknown_fraction is None else self.unknown_fraction)
        if expected!=self.representation_signature:raise ValueError('representation signature does not bind actual coefficients')

    def __deepcopy__(self,memo):return self

    def __reduce__(self):
        return (type(self),tuple(getattr(self,name) for name in self.__dataclass_fields__))

    @classmethod
    def from_columns(cls,area,heights,bulk,shear,fractions,rates,edges,*,world_id,world_binding_signature,unknown_fraction=None):
        if not isinstance(world_id,str) or not world_id or not isinstance(world_binding_signature,str) or not world_binding_signature:
            raise ValueError('existing world ID and full binding signature required')
        m=build_world(area,heights,bulk,shear,fractions,rates,edges)
        if unknown_fraction is not None and np.asarray(unknown_fraction).shape!=np.asarray(heights).shape:
            raise ValueError('unknown_fraction is retained on original carriers')
        k,b=_CSR.capture(m['K0']),_CSR.capture(m['B'])
        physical=_digest(world_binding_signature,k.signature(),b.signature(),m['km'],m['inv_tau'],
                         m['branch_cell'],m['branch_phase'],np.asarray(fractions),
                         'unknown_fraction_not_supplied' if unknown_fraction is None else unknown_fraction)
        rep=_digest(physical,k.signature(),b.signature(),m['km'],m['inv_tau'],m['branch_cell'],m['branch_phase'],np.array([m['nlayers']]),
            'unknown_fraction_not_supplied' if unknown_fraction is None else np.asarray(unknown_fraction,float))
        return cls(k,b,m['km'],m['inv_tau'],m['branch_cell'],m['branch_phase'],m['nlayers'],
                   world_id,physical,rep,unknown_fraction)

    def operators(self):
        """Fresh sparse copies; mutating them cannot modify this model."""
        return dict(K0=self._K0.array(),B=self._B.array(),km=self.km.copy(),inv_tau=self.inverse_tau.copy(),
                    branch_cell=self.branch_carrier.copy(),branch_phase=self.branch_phase.copy(),nlayers=self.nlayers)

    def coarsen(self,labels):
        p=restriction(labels,self.nlayers)
        if p.shape[0]!=self._K0.shape[0]:raise ValueError('partition carrier count mismatch')
        m=restrict(self.operators(),p);k,b=_CSR.capture(m['K0']),_CSR.capture(m['B'])
        rep=_digest(self.physical_signature,k.signature(),b.signature(),self.km,self.inverse_tau,self.branch_carrier,self.branch_phase,np.array([self.nlayers]),
            'unknown_fraction_not_supplied' if self.unknown_fraction is None else self.unknown_fraction)
        coarse=ScalarGraph(k,b,self.km,self.inverse_tau,self.branch_carrier,self.branch_phase,
            self.nlayers,self.world_id,self.physical_signature,rep,self.unknown_fraction)
        return coarse,p

    def state(self,q=None,memory=None,time_s=0.):
        s=GraphState(np.zeros(self._K0.shape[0]) if q is None else q,
            np.zeros(len(self.km)) if memory is None else memory,time_s,self.world_id,
            self.physical_signature,self.representation_signature)
        self.validate_state(s);return s

    def validate_state(self,state):
        if (state.world_id,state.physical_signature,state.representation_signature)!=(self.world_id,self.physical_signature,self.representation_signature):
            raise ValueError('foreign world/operator/representation state')
        if state.q.shape!=(self._K0.shape[0],) or state.memory.shape!=self.km.shape:
            raise ValueError('state dimension mismatch')

    def energy(self,state):
        self.validate_state(state);m=self.operators();e=m['B']@state.q-state.memory
        with np.errstate(over='ignore',invalid='ignore'):
            value=float(.5*state.q@(m['K0']@state.q)+.5*np.dot(self.km,e*e))
        if not np.isfinite(value):raise ValueError('nonfinite candidate energy')
        return value

    def audit(self,old,q,memory,force,dt):
        self.validate_state(old)
        if not np.isfinite(dt) or dt<0:raise ValueError('finite nonnegative dt required')
        state=self.state(q,memory,old.time_s+dt);f=np.asarray(force,float)
        if f.shape!=state.q.shape or not np.isfinite(f).all():raise ValueError('finite nodal force vector required')
        m=self.operators();K0,B=m['K0'],m['B'];e0=B@old.q-old.memory;e1=B@state.q-state.memory;dq=state.q-old.q
        de=self.energy(state)-self.energy(old);dis=float(np.dot(self.km*dt*self.inverse_tau,e1**2))
        numerical=float(.5*dq@(K0@dq)+.5*np.dot(self.km,(e1-e0)**2));work=float(f@dq)
        elastic_force=K0@state.q;branch_force=B.T@(self.km*e1)
        residual=float(np.linalg.norm(elastic_force+branch_force-f)/max(1.,np.linalg.norm(f),np.linalg.norm(elastic_force),np.linalg.norm(branch_force)))
        balance=float(abs(work-de-dis-numerical)/max(1.,abs(work),abs(de),dis))
        rr=dt*self.inverse_tau;target=(old.memory+rr*(B@state.q))/(1+rr)
        memory_residual=float(np.linalg.norm(state.memory-target)/max(1.,np.linalg.norm(target)))
        if not np.isfinite([de,dis,numerical,work,residual,balance,memory_residual]).all():
            raise ValueError('nonfinite audit metric; accepted state unchanged')
        return GraphStep(state,de,dis,numerical,work,residual,balance,memory_residual)

    def advance(self,old,force,dt,*,rtol=1e-12,acceptance_tolerance=1e-8):
        """Pure transactional step; exceptions leave the immutable old state intact."""
        from scipy.sparse.linalg import spsolve
        self.validate_state(old);self.energy(old);f=np.asarray(force,float)
        if f.shape!=old.q.shape or not np.isfinite(f).all():raise ValueError('finite nodal force vector required')
        if not np.isfinite(dt) or dt<0 or not np.isfinite(rtol) or rtol<=0 or not np.isfinite(acceptance_tolerance) or acceptance_tolerance<=0:
            raise ValueError('invalid time/tolerance')
        m=self.operators()
        if dt==0:
            A=m['K0']+m['B'].T@diags(self.km)@m['B']
            q=spsolve(A.tocsc(),f+m['B'].T@(self.km*old.memory));z=old.memory
        else:
            result=integrate(m,f[None,:],dt,(old.q,old.memory),rtol=rtol)
            q,z=result['q'][0],result['z'][0]
        step=self.audit(old,q,z,f,dt)
        if max(step.relative_force_residual,step.relative_energy_residual,step.relative_memory_residual)>acceptance_tolerance:
            raise RuntimeError('step checks failed; accepted state unchanged')
        return step


def lateral_port(area_m2,normal_i_to_j,distance_m,shear_pa,q_i,q_j,velocity_i,velocity_j,*,down_direction=(0.,0.,-1.)):
    """One oriented scalar shear spring using the existing mechanical port API.

    Positive q is along down_direction. Force-on-carrier signs are opposite to
    the internal residual gradient; their combined power is minus spring dE/dt.
    Geometry stays explicit; merging subfaces must not average their normals.
    """
    from .ports import mechanical_port_basis,port_power_density,PortKind
    values=np.asarray([area_m2,distance_m,shear_pa,q_i,q_j,velocity_i,velocity_j],float)
    if not np.isfinite(values).all() or np.any(values[:3]<=0):raise ValueError('finite port values and positive area/distance/shear required')
    normal=np.asarray(normal_i_to_j,float);direction=np.asarray(down_direction,float)
    basis=mechanical_port_basis(normal,direction,-normal)
    k=shear_pa*area_m2/distance_m;delta=q_i-q_j;internal=k*delta
    fi=-internal*direction;fj=-fi
    power=area_m2*(port_power_density(PortKind.MECHANICAL,fi/area_m2,velocity_i*direction)+
                   port_power_density(PortKind.MECHANICAL,fj/area_m2,velocity_j*direction))
    return dict(stiffness_N_m=float(k),energy_J=float(.5*k*delta*delta),
        force_on_i_N=_freeze(fi),force_on_j_N=_freeze(fj),
        normal_i_to_j=_freeze(normal),basis=_freeze(basis),
        area_m2=float(area_m2),power_into_carriers_W=float(power),
        spring_energy_rate_W=float(internal*(velocity_i-velocity_j)))
