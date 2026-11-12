"""Staggered rigid-pipe acoustics with passive nonlinear losses, SI.

Pressure is reference-volume mass potential p, not finite-volume mechanical Π.
FV pressures are cell-centred, flows on faces; midpoint physical power is exact.
Two-pipe moving-CV coupling is prescribed-volume, not a coupled solid solver.
"""
from dataclasses import dataclass,asdict
import hashlib,json
import numpy as np
from scipy.linalg import solve_banded

def freeze(a):
    b=np.ascontiguousarray(a,dtype=np.float64)
    return np.frombuffer(b.tobytes(),dtype=np.float64).reshape(b.shape)

@dataclass(frozen=True)
class Pipe:
    length_m:float
    diameter_m:float
    density_kg_m3:float=1000.
    bulk_pa:float=2.2e9
    viscosity_pa_s:float=.001
    friction_factor:float=.02
    entrance_loss:float=1.
    cells:int=32
    reference_absolute_pa:float=2e6
    minimum_absolute_pa:float=3000.
    max_mach:float=.01
    max_density_fraction:float=.01

    def __post_init__(self):
        if type(self.cells) is not int or self.cells<2:raise ValueError('cells integer >=2')
        if any(not np.isfinite(v) for v in asdict(self).values()):raise ValueError('nonfinite parameter')
        if min(self.length_m,self.diameter_m,self.density_kg_m3,self.bulk_pa,self.reference_absolute_pa,self.max_mach,self.max_density_fraction)<=0:raise ValueError('positive parameter')
        if min(self.viscosity_pa_s,self.friction_factor,self.entrance_loss,self.minimum_absolute_pa)<0:raise ValueError('negative dissipation/pressure')
        if not 0<self.max_density_fraction<1:raise ValueError('density guard')
    @property
    def signature(self):return hashlib.sha256(json.dumps(asdict(self),sort_keys=True).encode()).hexdigest()
    @property
    def area(self):return np.pi*self.diameter_m**2/4
    @property
    def sound_speed(self):return np.sqrt(self.bulk_pa/self.density_kg_m3)
    @property
    def dx(self):return self.length_m/self.cells
    @property
    def capacity(self):return self.area*self.dx/self.bulk_pa
    @property
    def face_lengths(self):
        a=np.full(self.cells+1,self.dx);a[[0,-1]]*=.5;return a
    @property
    def inertance(self):return self.density_kg_m3*self.face_lengths/self.area
    def loss(self,q):
        R=8*np.pi*self.viscosity_pa_s*self.face_lengths/self.area**2
        F=self.density_kg_m3*self.friction_factor*self.face_lengths/(2*self.diameter_m*self.area**2)
        F[0]+=self.density_kg_m3*self.entrance_loss/(2*self.area**2)*(q[0]>0)
        F[-1]+=self.density_kg_m3*self.entrance_loss/(2*self.area**2)*(q[-1]<0)
        return R*q+F*q*abs(q),R+2*F*abs(q),R*q*q,F*abs(q)**3
    def initial(self,pressure,flow=None,time_s=0.):
        p=np.broadcast_to(pressure,(self.cells,));q=np.zeros(self.cells+1) if flow is None else np.broadcast_to(flow,(self.cells+1,))
        s=State(freeze(p),freeze(q),float(time_s),self.signature);self.check(s);return s
    def check(self,s):
        if s.signature!=self.signature or s.pressure.shape!=(self.cells,) or s.flow.shape!=(self.cells+1,):raise ValueError('state identity/shape')
        if not np.isfinite(s.pressure).all() or not np.isfinite(s.flow).all() or not np.isfinite(s.time_s):raise ValueError('nonfinite state')
        density=float(abs(s.pressure).max()/self.bulk_pa)
        absolute=self.reference_absolute_pa+s.pressure+.5*s.pressure**2/self.bulk_pa
        mach=float(abs(s.flow).max()/self.area/self.sound_speed/(1-density)) if density<1 else float('inf')
        if density>self.max_density_fraction or mach>self.max_mach or absolute.min()<self.minimum_absolute_pa:
            raise DomainError(dict(density_fraction=density,Mach_upper=mach,minimum_absolute_pa=float(absolute.min())))
        return dict(density_fraction=density,Mach_upper=mach,minimum_absolute_pa=float(absolute.min()),
            max_reference_speed_m_s=float(abs(s.flow).max()/self.area),max_Re=float(self.density_kg_m3*abs(s.flow).max()*self.diameter_m/(self.area*self.viscosity_pa_s)) if self.viscosity_pa_s else None)
    def energy(self,s):return .5*self.capacity*np.dot(s.pressure,s.pressure)+.5*np.dot(self.inertance*s.flow,s.flow)
    def mass_anomaly(self,s):return self.capacity*float(s.pressure.sum())

@dataclass(frozen=True)
class State:
    pressure:np.ndarray
    flow:np.ndarray
    time_s:float
    signature:str

@dataclass(frozen=True)
class CV:
    volume_m3:float
    reference_volume_m3:float
    pressure_pa:float
    bulk_pa:float=2.2e9
    reference_absolute_pa:float=2e6
    minimum_absolute_pa:float=3000.
    def __post_init__(self):
        if not all(np.isfinite(v) for v in asdict(self).values()) or min(self.volume_m3,self.reference_volume_m3,self.bulk_pa)<=0:raise ValueError('CV parameters')
        if abs(self.pressure_pa)/self.bulk_pa>.01 or self.absolute<self.minimum_absolute_pa:raise DomainError({'CV_pressure_pa':self.pressure_pa,'absolute_pa':self.absolute})
    @property
    def mass(self):return self.volume_m3*(1+self.pressure_pa/self.bulk_pa)-self.reference_volume_m3
    @property
    def energy(self):return .5*self.volume_m3/self.bulk_pa*self.pressure_pa**2
    @property
    def Pi(self):return self.pressure_pa+.5*self.pressure_pa**2/self.bulk_pa
    @property
    def absolute(self):return self.reference_absolute_pa+self.Pi

class DomainError(ValueError):
    def __init__(self,diagnostics,candidate=None):
        super().__init__(str(diagnostics));self.diagnostics=diagnostics;self.candidate=candidate

def bt(p):
    a=np.empty(len(p)+1);a[0]=p[0];a[-1]=-p[-1];a[1:-1]=p[1:]-p[:-1];return a

def advance(models,states,dt,left_pressure,right_pressure=None,cv=None,new_volume=None,check_domain=True):
    """One or several parallel pipes. All right ends connect to CV if given.
    Boundary p values are midpoint potentials. End work is p*q, not Π*q.
    """
    if len(models)!=len(states) or not len(models) or not np.isfinite(dt) or dt<=0:raise ValueError('step input')
    for m,s in zip(models,states):m.check(s)
    if len({s.time_s for s in states})!=1:raise ValueError('unsynchronized states')
    left=np.broadcast_to(left_pressure,(len(models),)).astype(float)
    right=None if cv is not None else np.broadcast_to(right_pressure,(len(models),)).astype(float)
    if not np.isfinite(left).all() or (right is not None and not np.isfinite(right).all()):raise ValueError('boundary')
    V=cv.volume_m3 if cv is not None and new_volume is None else new_volume
    if cv is not None and (V is None or not np.isfinite(V) or V<=0):raise ValueError('CV volume')
    q=[s.flow.copy() for s in states];lam=dt*cv.bulk_pa/(2*V) if cv else 0.
    def endpressure(qs):
        pn=cv.bulk_pa*((cv.mass+cv.reference_volume_m3+dt*sum(x[-1] for x in qs))/V-1)
        return .5*(cv.pressure_pa+pn),pn
    def evaluate(qs,need_jac=True):
        pr,pnew=endpressure(qs) if cv else (None,None);res=[];bands=[];losses=[]
        for i,(m,s,z) in enumerate(zip(models,states,qs)):
            pbar=s.pressure+dt/(2*m.capacity)*(z[:-1]-z[1:]);loss,der,dl,dq=m.loss(z)
            r=2*m.inertance*(z-s.flow)/dt+loss+bt(pbar);r[0]-=left[i];r[-1]+=pr if cv else right[i]
            res.append(r);losses.append((dl,dq))
            if need_jac:
                a=dt/(2*m.capacity);diag=2*m.inertance/dt+der;diag+=a*np.r_[1.,np.full(m.cells-1,2.),1.]
                band=np.zeros((3,m.cells+1));band[1]=diag;band[0,1:]=-a;band[2,:-1]=-a;bands.append(band)
        return res,bands,losses,pnew
    scale=max(1.,float(abs(left).max()),*(float(abs(s.pressure).max()) for s in states))
    for it in range(24):
        res,bands,losses,pnew=evaluate(q);norm=max(float(abs(r).max()) for r in res)
        if norm<=1e-10+2e-12*scale:break
        y=[solve_banded((1,1),b,-r,check_finite=False) for b,r in zip(bands,res)]
        if cv:
            z=[]
            for b in bands:
                rhs=np.zeros(b.shape[1]);rhs[-1]=1;z.append(solve_banded((1,1),b,rhs,check_finite=False))
            factor=lam*sum(a[-1] for a in y)/(1+lam*sum(a[-1] for a in z));y=[a-factor*b for a,b in zip(y,z)]
        fac=1.
        for _ in range(15):
            candidate=[a+fac*b for a,b in zip(q,y)];rr=evaluate(candidate,False)[0]
            if max(float(abs(r).max()) for r in rr)<norm:break
            fac*=.5
        else:raise RuntimeError('Newton line search')
        q=candidate
    else:raise RuntimeError('Newton iteration')
    new=[];domain=[]
    for m,s,z in zip(models,states,q):
        p=s.pressure+dt/m.capacity*(z[:-1]-z[1:]);v=State(freeze(p),freeze(2*z-s.flow),s.time_s+dt,m.signature);new.append(v)
        if check_domain:
            try:domain.append(m.check(v))
            except DomainError as e:e.candidate=new;raise
    newcv=None
    if cv:
        try:newcv=CV(V,cv.reference_volume_m3,pnew,cv.bulk_pa,cv.reference_absolute_pa,cv.minimum_absolute_pa)
        except DomainError as e:e.candidate=new;raise
    oldE=sum(m.energy(s) for m,s in zip(models,states))+(cv.energy if cv else 0.)
    newE=sum(m.energy(s) for m,s in zip(models,new))+(newcv.energy if cv else 0.)
    linear=dt*sum(float(a.sum()) for a,b in losses);quadratic=dt*sum(float(b.sum()) for a,b in losses)
    Wleft=dt*sum(p*z[0] for p,z in zip(left,q));Wright=0. if cv else -dt*sum(p*z[-1] for p,z in zip(right,q))
    Pi_star=.5*(cv.pressure_pa+newcv.pressure_pa)+cv.pressure_pa*newcv.pressure_pa/(2*cv.bulk_pa) if cv else 0.
    Wvolume=-Pi_star*(newcv.volume_m3-cv.volume_m3) if cv else 0.
    oldmass=sum(m.mass_anomaly(s) for m,s in zip(models,states))+(cv.mass if cv else 0.)
    newmass=sum(m.mass_anomaly(s) for m,s in zip(models,new))+(newcv.mass if cv else 0.)
    inflow=dt*sum(z[0] for z in q)-(0. if cv else dt*sum(z[-1] for z in q))
    return tuple(new),newcv,dict(time_s=new[0].time_s,energy_j=newE,linear_D_j=linear,quadratic_D_j=quadratic,
        boundary_left_work_j=float(Wleft),boundary_right_work_j=float(Wright),moving_volume_work_j=float(Wvolume),
        numerical_energy_defect_j=float(newE-oldE+linear+quadratic-Wleft-Wright-Wvolume),
        mass_anomaly_ref_m3=newmass,mass_flux_ref_m3=float(inflow),mass_balance_error_ref_m3=float(newmass-oldmass-inflow),
        left_flow_mid_m3_s=[float(z[0]) for z in q],right_flow_mid_m3_s=[float(z[-1]) for z in q],
        endpoint_potential_work_convention='p*q; Π only moving volume',Pi_discrete_pa=float(Pi_star),Newton_iterations=it+1,
        Newton_residual_pa=norm,domain=domain)
