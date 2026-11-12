"""Exact old-prefix Abel drift and rigorous new-interval stress-norm bound.

Known virgin prefix, SI/Mandel, endpoint-constant accepted stress. This is
an independent full-kernel estimate below a production SOE's minimum dt;
it does not authorize calling that SOE outside its qualified time window.
"""
import numpy as np

def prefix_drift(times, stress, horizon_s, *, alpha, delta, modulus, branch):
    """Return exact old-memory change and a coefficient for future stress.

    For an arbitrary future projected stress bounded by Smax at every time,
    ||memory(T+h)-memory(T)|| <= ||old_change|| + new_coefficient*Smax.
    Stress shape [N,...,6], times [N], known zero memory at times[0]=0.
    dt=0 stresses have no finite-duration hereditary contribution.
    Coefficient units Pa^-1, drift is Mandel strain. No finite-SOE assertion.
    """
    t=np.asarray(times,float);s=np.asarray(stress,float);h=float(horizon_s)
    if t.ndim!=1 or len(t)<1 or t[0]!=0 or np.any(np.diff(t)<0):raise ValueError('prefix time')
    if s.shape[0]!=len(t) or s.shape[-1]!=6 or s.ndim<2:raise ValueError('stress shape')
    if not np.isfinite(t).all() or not np.isfinite(s).all():raise ValueError('nonfinite prefix')
    if not np.isfinite([h,alpha,delta,modulus]).all() or h<=0 or not 0<alpha<1 or delta<0 or modulus<=0:
        raise ValueError('invalid parameters')
    if branch not in ('bulk','dev'):raise ValueError('branch')
    bulk=np.zeros_like(s);bulk[...,:3]=s[...,:3].mean(axis=-1)[...,None]
    p=bulk if branch=='bulk' else s-bulk
    beta=1-alpha;positive=np.flatnonzero(np.diff(t)>0)+1;T=t[-1]
    def increment(age):
        # Stable (age+h)^beta-age^beta even when h << age.
        value=np.empty_like(age);zero=age==0
        value[zero]=h**beta
        value[~zero]=age[~zero]**beta*np.expm1(beta*np.log1p(h/age[~zero]))
        return value
    weights=increment(T-t[positive-1])-increment(T-t[positive])
    old=delta/(modulus*beta)*np.tensordot(weights,p[positive],axes=(0,0))
    coefficient=delta*h**beta/(modulus*beta)
    return dict(old_memory_change=old,old_memory_change_norm=np.linalg.norm(old,axis=-1),
        new_stress_norm_coefficient=coefficient,
        prefix_max_stress_norm=np.linalg.norm(p,axis=-1).max(axis=0),
        held_last_stress_memory_change=old+coefficient*p[-1],
        branch=branch,horizon_s=h,scope='Full Abel kernel, known virgin prefix, arbitrary future stress bound required')
