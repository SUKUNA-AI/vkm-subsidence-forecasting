"""Characteristic FV for the SAME rigid-pipe law; explicit numerical energy.

Cell p/q, exact linear Riemann flux, Strang exact local R+F friction.
At CFL1 the interior characteristics translate one cell without dispersion.
Entrance orifices and moving CV share reference-mass-potential power ports.
"""
from dataclasses import dataclass
import numpy as np
from .acoustic_pipe import Pipe,CV,DomainError,freeze

@dataclass(frozen=True)
class CellState:
    pressure:np.ndarray
    flow:np.ndarray
    time_s:float
    signature:str

def initial(m,p,q=0.,t=0.):
    s=CellState(freeze(np.broadcast_to(p,(m.cells,))),freeze(np.broadcast_to(q,(m.cells,))),t,m.signature+':characteristic-FV1')
    check(m,s);return s
def check(m,s):
    if s.signature!=m.signature+':characteristic-FV1' or s.pressure.shape!=(m.cells,) or s.flow.shape!=(m.cells,):raise ValueError('state identity')
    return domain(m,s.pressure,s.flow)
def domain(m,p,q):
    if not np.isfinite(p).all() or not np.isfinite(q).all():raise ValueError('nonfinite field')
    delta=float(abs(p).max()/m.bulk_pa);mach=float(abs(q).max()/m.area/m.sound_speed/(1-delta)) if delta<1 else float('inf')
    minimum=float(np.min(m.reference_absolute_pa+p+.5*p*p/m.bulk_pa))
    d=dict(density_fraction=delta,Mach_upper=mach,minimum_absolute_pa=minimum,max_reference_speed_m_s=float(abs(q).max()/m.area),
        max_Re=float(m.density_kg_m3*abs(q).max()*m.diameter_m/(m.area*m.viscosity_pa_s)) if m.viscosity_pa_s else None)
    if delta>m.max_density_fraction or mach>m.max_mach or minimum<m.minimum_absolute_pa:raise DomainError(d)
    return d
def energy(m,s):return .5*m.capacity*np.dot(s.pressure,s.pressure)+.5*m.density_kg_m3*m.dx/m.area*np.dot(s.flow,s.flow)
def mass(m,s):return m.capacity*float(s.pressure.sum())
def friction(m,q,dt):
    """Exact local ODE qdot=−a q−b q|q|; positive loss split analytically."""
    a=8*np.pi*m.viscosity_pa_s/(m.density_kg_m3*m.area);b=m.friction_factor/(2*m.diameter_m*m.area);old=abs(q)
    if a==0:new=old/(1+b*old*dt)
    else:new=old*np.exp(-a*dt)/(1+b*old*(-np.expm1(-a*dt))/a)
    I=m.density_kg_m3*m.dx/m.area;total=.5*I*(old-new)*(old+new)
    if b==0:linear=total
    elif a==0:linear=np.zeros_like(total)
    else:
        diff=old-new;z=b*diff/(a+b*new)
        # Stable z-log(1+z), including tiny friction substeps.
        rest=np.where(abs(z)<1e-4,z*z*(.5-z/3+z*z/4-z**3/5),z-np.log1p(z))
        linear=I*a/(b*b)*(b*diff*(b*new/(a+b*new))+a*rest)
    return np.copysign(new,q),float(linear.sum()),float((total-linear).sum())
def orifice(delta,Z,F,enter_positive):
    active=(delta>=0) if enter_positive else (delta<=0);f=F if active else 0.
    q=2*delta/(Z+np.sqrt(Z*Z+4*f*abs(delta)))
    return q,1/(Z+2*f*abs(q)),f*abs(q)**3

def advance(models,states,dt,left_pressure,right_pressure=None,cv=None,new_volume=None):
    if len(models)!=len(states) or not models or dt<=0 or not np.isfinite(dt):raise ValueError('step')
    for m,s in zip(models,states):
        check(m,s)
        if dt>m.dx/m.sound_speed*(1+1e-12):raise ValueError('CFL>1')
    if len({s.time_s for s in states})!=1:raise ValueError('time identity')
    left=np.broadcast_to(left_pressure,(len(models),));right=None if cv else np.broadcast_to(right_pressure,(len(models),))
    V=cv.volume_m3 if cv and new_volume is None else new_volume
    intermediate=[];Dlin=Dquad=0.
    for m,s in zip(models,states):
        q,dl,dq=friction(m,s.flow,dt/2);Dlin+=dl;Dquad+=dq;intermediate.append(q)
    Z=np.array([m.density_kg_m3*m.sound_speed/m.area for m in models]);F=np.array([m.density_kg_m3*m.entrance_loss/(2*m.area**2) for m in models])
    incoming=np.array([s.pressure[-1]+z*q[-1] for s,q,z in zip(states,intermediate,Z)])
    newcv=None;pcv=None;cv_iters=0
    if cv:
        if V is None or not np.isfinite(V) or V<=0:raise ValueError('volume')
        pn=cv.pressure_pa
        for it in range(32):
            pcv=.5*(cv.pressure_pa+pn);ports=[orifice(w-pcv,z,f,False) for w,z,f in zip(incoming,Z,F)]
            residual=V*(1+pn/cv.bulk_pa)-cv.reference_volume_m3-cv.mass-dt*sum(x[0] for x in ports)
            derivative=V/cv.bulk_pa+dt*.5*sum(x[1] for x in ports)
            if abs(residual)<=2e-15*max(1.,V):break
            pn-=residual/derivative
        else:raise RuntimeError('CV Newton')
        cv_iters=it+1;newcv=CV(V,cv.reference_volume_m3,pn,cv.bulk_pa,cv.reference_absolute_pa,cv.minimum_absolute_pa);pcv=.5*(cv.pressure_pa+pn)
    new=[];linear_wave_algorithmic=0.;boundaryW=0.;Dminor=0.;qin=[];qout=[];portdomains=[]
    for i,(m,s,q,z,f) in enumerate(zip(models,states,intermediate,Z,F)):
        p=s.pressure;P=np.empty(m.cells+1);Q=np.empty(m.cells+1)
        P[1:-1]=.5*(p[:-1]+p[1:])+z*.5*(q[:-1]-q[1:]);Q[1:-1]=.5*(q[:-1]+q[1:])+.5/z*(p[:-1]-p[1:])
        Q[0],_,lossL=orifice(float(left[i]-(p[0]-z*q[0])),z,f,True);P[0]=p[0]-z*q[0]+z*Q[0]
        pR=pcv if cv else float(right[i]);Q[-1],_,lossR=orifice(float(incoming[i]-pR),z,f,False);P[-1]=incoming[i]-z*Q[-1]
        pn=p+dt/m.capacity*(Q[:-1]-Q[1:]);I=m.density_kg_m3*m.dx/m.area;qn=q+dt/I*(P[:-1]-P[1:])
        jumps=dt*(.5*np.sum(np.diff(p)**2/z+z*np.diff(q)**2)+(P[0]-p[0])**2/z+(P[-1]-p[-1])**2/z)
        increment=.5*m.capacity*np.dot(pn-p,pn-p)+.5*I*np.dot(qn-q,qn-q)
        linear_wave_algorithmic+=float(jumps-increment)
        qfinal,dl,dq=friction(m,qn,dt/2);Dlin+=dl;Dquad+=dq;Dminor+=dt*(lossL+lossR)
        n=CellState(freeze(pn),freeze(qfinal),s.time_s+dt,m.signature+':characteristic-FV1');new.append(n)
        try:check(m,n);portdomains.append(domain(m,P,Q))
        except DomainError as e:e.candidate=new;raise
        boundaryW+=dt*(float(left[i])*Q[0]-(0 if cv else pR*Q[-1]));qin.append(float(Q[0]));qout.append(float(Q[-1]))
    oldE=sum(energy(m,s) for m,s in zip(models,states))+(cv.energy if cv else 0.);newE=sum(energy(m,s) for m,s in zip(models,new))+(newcv.energy if cv else 0.)
    Pistar=.5*(cv.pressure_pa+newcv.pressure_pa)+cv.pressure_pa*newcv.pressure_pa/(2*cv.bulk_pa) if cv else 0.
    Wvolume=-Pistar*(V-cv.volume_m3) if cv else 0.
    oldM=sum(mass(m,s) for m,s in zip(models,states))+(cv.mass if cv else 0.);newM=sum(mass(m,s) for m,s in zip(models,new))+(newcv.mass if cv else 0.)
    mflux=dt*(sum(qin)-(0 if cv else sum(qout)))
    return tuple(new),newcv,dict(time_s=new[0].time_s,energy_j=newE,mass_anomaly_ref_m3=newM,linear_D_j=Dlin,quadratic_D_j=Dquad,
        entrance_D_j=Dminor,algorithmic_energy_j=linear_wave_algorithmic,boundary_work_j=float(boundaryW),moving_volume_work_j=float(Wvolume),
        balance_error_j=float(newE-oldE+Dlin+Dquad+Dminor+linear_wave_algorithmic-boundaryW-Wvolume),
        mass_flux_ref_m3=float(mflux),mass_balance_error_ref_m3=float(newM-oldM-mflux),left_flow_m3_s=qin,right_flow_m3_s=qout,
        CV_iterations=cv_iters,Pi_discrete_pa=float(Pistar),domain=portdomains,physical_D_scope='exact local friction + entrance loss; no numerical term reclassified')
