"""Independent Abel energy witness, SI/Mandel, no production solver imports.

Piecewise-constant stress on each positive interval; elastic jumps use
trapezoidal stress work. Finite positive spectrum is exactly passive.
Analytic omitted-spectrum bounds assume a known virgin start; quadrature
convergence is reported separately, never disguised as a certified bound.
"""
from dataclasses import dataclass,fields
from pathlib import Path
import hashlib,json,math
import numpy as np
from types import MappingProxyType

def freeze(a):
    a=np.asarray(a,dtype=float);shape=a.shape
    a=np.ascontiguousarray(a)
    return np.frombuffer(a.tobytes(),dtype=float).reshape(shape)
def bulk(s):
    out=np.zeros_like(s);out[...,:3]=np.mean(s[...,:3],axis=-1)[...,None];return out
def dev(s):return s-bulk(s)
def norm2(s):return np.sum(s*s,axis=-1)
def dot(a,b):return np.sum(a*b,axis=-1)

@dataclass(frozen=True)
class EnergyState:
    time_s:float
    stress:np.ndarray
    bulk_z:np.ndarray
    dev_z:np.ndarray
    cumulative_work:np.ndarray
    cumulative_dissipation:np.ndarray
    maximum_bulk_stress_norm:np.ndarray
    maximum_dev_stress_norm:np.ndarray
    bulk_total_variation:np.ndarray
    dev_total_variation:np.ndarray
    signature:str
    def __post_init__(self):
        for f in fields(self):
            if f.name not in ('time_s','signature'):object.__setattr__(self,f.name,freeze(getattr(self,f.name)))

class SpectralEnergyWitness:
    def __setattr__(self,name,value):
        if getattr(self,'_sealed',False):raise AttributeError('energy spectrum is immutable; construct a new witness')
        object.__setattr__(self,name,value)
    def __init__(self,K,G,alpha_bulk,delta_bulk,alpha_dev,delta_dev,*,lower=1e-24,upper=1e24,panels=32,order=12):
        if not np.isfinite([K,G,alpha_bulk,delta_bulk,alpha_dev,delta_dev,lower,upper]).all():
            raise ValueError('finite spectrum parameters required')
        if min(K,G,lower)<=0 or upper<=lower or not all(0<a<1 for a in (alpha_bulk,alpha_dev)):
            raise ValueError('positive moduli/cutoffs and0<alpha<1 required')
        if min(delta_bulk,delta_dev)<0 or panels<1 or order<2:raise ValueError('invalid spectrum')
        self.moduli=(3*K,2*G);self.alphas=(alpha_bulk,alpha_dev);self.deltas=(delta_bulk,delta_dev)
        self.lower,self.upper=lower,upper
        lo,hi=math.log(lower),math.log(upper);edges=np.linspace(lo,hi,panels+1)
        nodes,weights=np.polynomial.legendre.leggauss(order)
        grid=((edges[:-1,None]+edges[1:,None])/2+(edges[1:,None]-edges[:-1,None])*nodes/2).ravel()
        q=((edges[1:,None]-edges[:-1,None])*weights/2).ravel()
        self.rates=freeze(np.exp(grid))
        self.weights=tuple(freeze(q*np.exp(a*grid)/math.gamma(a)) for a in self.alphas)
        assert np.all(self.rates>0) and all(np.all(w>0) for w in self.weights)
        args=dict(K=K,G=G,alpha_bulk=alpha_bulk,delta_bulk=delta_bulk,alpha_dev=alpha_dev,delta_dev=delta_dev,
                  lower=lower,upper=upper,panels=panels,order=order,algorithm='independent-positive-spectrum-energy-v1')
        args['spectrum_sha256']=hashlib.sha256(self.rates.tobytes()+b''.join(w.tobytes() for w in self.weights)).hexdigest()
        self.signature=hashlib.sha256(json.dumps(args,sort_keys=True).encode()).hexdigest()
        self.parameters=MappingProxyType(args)
        self._sealed=True
    def initial(self,batch_shape=()):
        shape=tuple(batch_shape);z=np.zeros((len(self.rates),)+shape+(6,));scalar=np.zeros(shape)
        return EnergyState(0.,np.zeros(shape+(6,)),z,z,scalar,scalar,scalar,scalar,scalar,scalar,self.signature)
    def _check(self,state,stress):
        stress=np.asarray(stress,float)
        if state.signature!=self.signature or stress.shape!=state.stress.shape or stress.shape[-1]!=6:
            raise ValueError('energy state signature/shape differs')
        if not np.isfinite(stress).all():raise ValueError('nonfinite stress')
        expected=(len(self.rates),)+stress.shape
        if state.bulk_z.shape!=expected or state.dev_z.shape!=expected or not math.isfinite(state.time_s) or state.time_s<0:
            raise ValueError('energy state memory shape/time differs')
        for f in fields(state):
            if f.name not in ('time_s','signature') and not np.isfinite(getattr(state,f.name)).all():
                raise ValueError('nonfinite energy state')
        return stress
    def energies(self,state):
        self._check(state,state.stress)
        elastic=np.zeros(state.stress.shape[:-1]);memory=np.zeros_like(elastic)
        for proj,M,delta,w,z in zip((bulk,dev),self.moduli,self.deltas,self.weights,(state.bulk_z,state.dev_z)):
            elastic+=norm2(proj(state.stress))/(2*M)
            memory+=delta/(2*M)*np.tensordot(w*self.rates,norm2(z),axes=(0,0))
        return elastic,memory
    def memory_strain(self,state):
        self._check(state,state.stress)
        return sum(delta/M*np.tensordot(w,z,axes=(0,0)) for delta,M,w,z in
                   zip(self.deltas,self.moduli,self.weights,(state.bulk_z,state.dev_z)))
    def step(self,state,stress,dt):
        stress=self._check(state,stress)
        if not math.isfinite(dt) or dt<0:raise ValueError('nonnegative interval required')
        expand=(len(self.rates),)+(1,)*stress.ndim
        r=self.rates.reshape(expand);decay=np.exp(-self.rates*dt).reshape(expand)
        factor=(-np.expm1(-self.rates*dt)/self.rates).reshape(expand)
        dz=[];diss=np.zeros(stress.shape[:-1]);work=np.zeros_like(diss);tv=[];sup=[]
        for proj,M,delta,w,z,oldtv,oldmax in zip((bulk,dev),self.moduli,self.deltas,self.weights,
                (state.bulk_z,state.dev_z),(state.bulk_total_variation,state.dev_total_variation),
                (state.maximum_bulk_stress_norm,state.maximum_dev_stress_norm)):
            s=proj(stress);s0=proj(state.stress);newz=decay*z+factor*s
            a=s-r*z
            modeD=norm2(a)*(-np.expm1(-2*self.rates*dt)/(2*self.rates)).reshape((len(self.rates),)+(1,)*(stress.ndim-1))
            diss+=delta/M*np.tensordot(w,modeD,axes=(0,0))
            work+=(norm2(s)-norm2(s0))/(2*M)+delta/M*dot(s,np.tensordot(w,newz-z,axes=(0,0)))
            dz.append(newz);tv.append(oldtv+np.sqrt(norm2(s-s0)));sup.append(np.maximum(oldmax,np.sqrt(norm2(s))))
        result=EnergyState(state.time_s+dt,stress,*dz,state.cumulative_work+work,
            state.cumulative_dissipation+diss,*sup,*tv,self.signature)
        oldpsi=sum(self.energies(state));newpsi=sum(self.energies(result))
        return result,dict(work=work,physical_dissipation=diss,stored_energy=newpsi,
                          balance_error=work-(newpsi-oldpsi)-diss,
                          energy_scope='finite positive spectrum, exact constant-stress modes and trapezoidal elastic jump')
    def tail_bounds(self,state):
        """Analytic full-spectrum tails; requires the actual virgin-origin prefix.

        These bounds do NOT bound numerical quadrature inside[lower,upper].
        T=elapsed time,S=max projected stress norm,V=total stress variation.
        """
        self._check(state,state.stress)
        out={k:np.zeros(state.stress.shape[:-1]) for k in ('psi_low','psi_high','D_low','D_high')}
        T=state.time_s;L,U=self.lower,self.upper
        for a,delta,M,S,V in zip(self.alphas,self.deltas,self.moduli,
                (state.maximum_bulk_stress_norm,state.maximum_dev_stress_norm),
                (state.bulk_total_variation,state.dev_total_variation)):
            c=delta/(M*math.gamma(a))
            out['psi_low']+=c*S*S*T*T*L**(a+1)/(2*(a+1))
            out['psi_high']+=c*S*S*U**(a-1)/(2*(1-a))
            out['D_low']+=c*V*V*T*L**a/a
            out['D_high']+=c*V*V*U**(a-1)/(2*(1-a))
        out['total_work_tail_upper']=sum(out.values())
        return out
    def save(self,path,state):
        self._check(state,state.stress)
        np.savez_compressed(path,**{f.name:getattr(state,f.name) for f in fields(state)})
        side=dict(schema='abel-energy-streaming-state/1',sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                  parameters=dict(self.parameters),origin='KNOWN_VIRGIN_PREFIX; nonzero accumulated memory retained')
        Path(str(path)+'.json').write_text(json.dumps(side,indent=2),encoding='utf8')
    def reopen(self,path):
        side=json.loads(Path(str(path)+'.json').read_text(encoding='utf8'))
        if side['sha256']!=hashlib.sha256(Path(path).read_bytes()).hexdigest():raise ValueError('checkpoint changed')
        with np.load(path,allow_pickle=False) as d:
            state=EnergyState(float(d['time_s']),*(d[f.name] for f in fields(EnergyState) if f.name not in ('time_s','signature')),str(d['signature']))
        self._check(state,state.stress);return state

def direct_history_energy(times,stress,*,K,G,alpha_bulk,delta_bulk,alpha_dev,delta_dev):
    """O(N³) all-endpoints full-spectrum oracle for ONE tensor point, virgin start.

    Self-derived closed forms; no finite spectral quadrature and no production
    constitutive import. times has N+1 endpoints, stress has[N,6] intervals.
    """
    t=np.asarray(times,float);s=np.asarray(stress,float)
    if not np.isfinite([K,G,alpha_bulk,delta_bulk,alpha_dev,delta_dev]).all() or min(K,G)<=0 or min(delta_bulk,delta_dev)<0 or not all(0<a<1 for a in (alpha_bulk,alpha_dev)):
        raise ValueError('invalid direct energy parameters')
    if t.ndim!=1 or s.shape!=(len(t)-1,6) or t[0]!=0 or np.any(np.diff(t)<=0) or not np.isfinite(t).all() or not np.isfinite(s).all():raise ValueError('history shape/origin')
    n=len(s);memory=np.zeros_like(s);psis=np.zeros(n);diss=np.zeros(n);work=np.zeros(n);elastic=np.zeros(n)
    for proj,a,delta,M in zip((bulk,dev),(alpha_bulk,alpha_dev),(delta_bulk,delta_dev),(3*K,2*G)):
        beta=1-a;p=proj(s);jumps=np.diff(np.vstack([np.zeros(6),p]),axis=0)
        elastic+=norm2(p)/(2*M)
        oldmem=np.zeros(6)
        for i in range(n):
            age=t[i+1]-t[:i+1]
            pair=age[:,None]**beta+age[None,:]**beta-(age[:,None]+age[None,:])**beta
            gram=jumps[:i+1]@jumps[:i+1].T
            psis[i]+=delta/(2*M*beta)*np.sum(gram*pair)
            oldage=t[i]-t[:i+1];dt=t[i+1]-t[i]
            pairD=(oldage[:,None]+oldage[None,:]+2*dt)**beta-(oldage[:,None]+oldage[None,:])**beta
            diss[i]+=delta/(2*M*beta)*np.sum(gram*pairD)
            weights=((t[i+1]-t[:i+1])**beta-(t[i+1]-t[1:i+2])**beta)/beta
            mem=delta/M*(weights@p[:i+1]);memory[i]+=mem
            work[i]+=dot(p[i],mem-oldmem);oldmem=mem
    work+=np.diff(np.r_[0.,elastic])
    total=psis+elastic
    return dict(memory_strain=memory,elastic_energy=elastic,memory_energy=psis,physical_dissipation=diss,
                work=work,balance_error=work-np.diff(np.r_[0.,total])-diss,
                cumulative_work=np.cumsum(work),cumulative_dissipation=np.cumsum(diss))

def hybrid_step_defect(model,old,new,dt):
    """Independent audit of current hybrid endpoint; no edits/energy reclassification.

    model supplies positive rates/weights and parameters; states are production
    snapshots. R is signed algorithmic correction work, NOT dissipation.
    """
    if not math.isfinite(dt) or dt<0 or not np.isclose(new.time_s-old.time_s,dt,rtol=1e-12,atol=1e-12):
        raise ValueError('inconsistent hybrid time interval')
    if old.signature!=new.signature or old.signature!=model.signature:raise ValueError('hybrid model identity changed')
    energy0=np.zeros(old.stress.shape[:-1]);energy1=np.zeros_like(energy0)
    D=np.zeros_like(energy0);elastic_excess=np.zeros_like(energy0)
    c0=old.strain.copy();c1=new.strain.copy();expected_c1=np.zeros_like(new.strain);mode_error=0.
    for proj,M,alpha,delta,r,w,key in zip((bulk,dev),(3*model.K,2*model.G),(model.alpha_bulk,model.alpha_dev),
                                   (model.delta_bulk,model.delta_dev),model.rates,model.weights,('bulk_modes','dev_modes')):
        s0,s1=proj(old.stress),proj(new.stress);z0,z1=proj(old.memory[key]),proj(new.memory[key])
        rate=np.asarray(r).reshape((len(r),)+(1,)*new.stress.ndim)
        recurrence=np.exp(-rate*dt)*old.memory[key]+(-np.expm1(-rate*dt)/rate)*new.stress
        mode_error=max(mode_error,float(np.max(abs(new.memory[key]-recurrence))))
        energy0+=norm2(s0)/(2*M)+delta/(2*M)*np.tensordot(w*r,norm2(z0),axes=(0,0))
        energy1+=norm2(s1)/(2*M)+delta/(2*M)*np.tensordot(w*r,norm2(z1),axes=(0,0))
        coef=(-np.expm1(-2*r*dt)/(2*r)).reshape((len(r),)+(1,)*(new.stress.ndim-1))
        D+=delta/M*np.tensordot(w,norm2(s1-rate*z0)*coef,axes=(0,0))
        elastic_excess+=norm2(s1-s0)/(2*M)
        c0-=s0/M+delta/M*np.tensordot(w,z0,axes=(0,0))
        c1-=s1/M+delta/M*np.tensordot(w,z1,axes=(0,0))
        if dt>0:expected_c1+=delta/M*(dt**(1-alpha)/(1-alpha)-np.sum(w*(-np.expm1(-r*dt)/r)))*s1
    if dt==0:expected_c1=c0
    correction=dot(new.stress,c1-c0)
    bound=np.sqrt(norm2(new.stress))*np.sqrt(norm2(c1-c0))
    endpoint=dot(new.stress,new.strain-old.strain)
    residual=endpoint-(energy1-energy0)-D-elastic_excess
    return dict(finite_spectrum_energy_old=energy0,finite_spectrum_energy_new=energy1,
        finite_spectrum_physical_dissipation=D,endpoint_work=endpoint,elastic_quadrature_excess=elastic_excess,
        hybrid_correction_work=correction,hybrid_correction_absolute_bound=bound,
        naive_closure_residual=residual,identified_defect_error=residual-correction,
        correction_strain_old=c0,correction_strain_new=c1,
        mode_update_max_abs_error=mode_error,near_correction_expected_max_abs_error=float(np.max(abs(c1-expected_c1))),
        scope='FiniteSOE energy is not full hybrid energy; signed correction is numerical defect, not physical dissipation')
