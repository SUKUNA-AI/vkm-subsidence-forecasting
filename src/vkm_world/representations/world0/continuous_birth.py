"""Positive interval birth-reference distribution; no atomic mass arrivals."""
import hashlib
import json
import numpy as np


def fraction(t):
    if np.any(np.asarray(t)<0) or np.any(np.asarray(t)>1+1e-14):
        raise ValueError('deposition time outside [0,1] s')
    return t**3 * (10 + t * (-15 + 6*t))


def rate(t):
    return 30*t*t*(1-t)**2


def clipped_integrals(e, left, right):
    """Uniform positive reference distribution: E[min(e-ref,0)^q], q=1,2."""
    lo = np.minimum(left, right); hi = np.maximum(left, right)
    width = hi-lo
    full = e <= lo
    partial = (e > lo) & (e < hi)
    d = e-(lo+hi)/2
    first = np.where(full, d, 0.)
    second = np.where(full, d*d+width*width/12, 0.)
    safe = np.where(width > 0, width, 1.)
    first = np.where(partial, -(hi-e)**2/(2*safe), first)
    second = np.where(partial, (hi-e)**3/(3*safe), second)
    return first, second


class ContinuousModel:
    def __init__(self, z):
        self.z = z; self.K=z['K']; self.M=z['M']; self.dm=z['dM']
        self.Q=z['Q']; self.wk=z['Wk']; self.r0=z['r0']; self.b=z['incoming_body']
        self.n=len(self.M); self.ng=len(self.wk)
        if np.any(self.M<=0) or np.any(self.dm<0) or np.any(self.wk<0):
            raise ValueError('invalid mass/stiffness')
        void=1-z['support'][np.searchsorted(z['cell_ids'],z['fill_cells'])]
        if np.any(z['fillcapacity']<0) or np.any(z['fillcapacity']>void+1e-12):
            raise ValueError('birth exceeds source capacity')
        self.xscale=max(float(np.max(abs(np.linalg.solve(self.K,self.b)))),1e-8)
        self.vscale=100*self.xscale
        self.scale=np.r_[np.full(self.n,self.xscale),np.full(self.n,self.vscale),1.,1.]
        self.times=np.array([0.]); self.refs=np.zeros((1,self.ng))

    def fill(self, x, t):
        e=self.Q@x
        rr=np.vstack((self.refs,e))
        aa=np.r_[fraction(self.times),fraction(t)]
        weights=np.diff(aa)
        if np.min(weights)<-1e-13: raise ValueError('history time reversal')
        q1,q2=clipped_integrals(e,rr[:-1],rr[1:])
        return self.Q.T@(self.wk*(weights@q1)),float(.5*np.dot(self.wk,weights@q2))

    def rhs(self,t,y):
        yy=y*self.scale; x=yy[:self.n]; v=yy[self.n:2*self.n]
        a=fraction(t); force,_=self.fill(x,t)
        acc=(self.r0+a*self.b-self.K@x-force)/(self.M+a*self.dm)
        return np.r_[v,acc,-rate(t)*np.dot(self.b,x),.5*rate(t)*np.dot(self.dm,v*v)]/self.scale

    def commit(self,t,x):
        # Validate and allocate the complete trial before changing accepted history.
        tt=np.asarray(t); xx=np.asarray(x)
        if tt.ndim!=0 or not np.isrealobj(tt) or not np.isfinite(tt):
            raise ValueError('history commit time must be a finite real scalar')
        if t<=self.times[-1] or t>1: raise ValueError('history commit time')
        if xx.shape!=(self.n,) or not np.isrealobj(xx) or not np.isfinite(xx).all():
            raise ValueError('history commit displacement shape/domain')
        reference=self.Q@xx
        if reference.shape!=(self.ng,) or not np.isfinite(reference).all():
            raise ValueError('history commit reference shape/domain')
        new_times=np.r_[self.times,t]
        new_refs=np.vstack((self.refs,reference))
        self.times,self.refs=new_times,new_refs

    def diagnostics(self,t,y):
        x=y[:self.n]; v=y[self.n:2*self.n]; a=fraction(t)
        _,fill=self.fill(x,t)
        potential=.5*x@self.K@x-self.r0@x-a*self.b@x
        ke=.5*np.dot(self.M+a*self.dm,v*v)
        balance=potential+ke+fill-y[-2:].sum()
        return np.array([t,a,potential,ke,fill,*y[-2:],balance,a*float(self.z['incoming_mass'])])

    def save(self,path,t,y,identity):
        data=dict(time=np.array(t),state=y,times=self.times,refs=self.refs,identity=np.array(identity))
        data['checksum']=np.array(payload(data))
        np.savez_compressed(path,**data)

    def restore(self,path,identity):
        with np.load(path,allow_pickle=False) as f: data={k:f[k].copy() for k in f.files}
        if set(data)!={'time','state','times','refs','identity','checksum'}:
            raise ValueError('checkpoint schema')
        checksum=str(data.pop('checksum'))
        if str(data['identity'])!=identity or payload(data)!=checksum:
            raise ValueError('checkpoint identity/checksum')
        tt=data['time']; y=data['state']; ts=data['times']; rr=data['refs']
        # Validate raw arrays before scalar conversion or numerical coercion.
        # Complex (even zero-imaginary), bool, object and strings are not real
        # physical checkpoint numbers. Preserve valid arrays without casting.
        if any(a.dtype.kind not in 'iuf' for a in (tt,y,ts,rr)):
            raise ValueError('checkpoint numeric dtype/domain')
        if tt.shape!=() or y.shape!=(2*self.n+2,) or ts.ndim!=1 or ts.size==0 or rr.shape!=(ts.size,self.ng):
            raise ValueError('checkpoint shape/domain')
        if not all(np.isfinite(a).all() for a in (tt,y,ts,rr)):
            raise ValueError('checkpoint finite/domain')
        if ts[0]!=0 or ts[-1]!=tt or np.any(ts[1:]<=ts[:-1]) or not 0<tt<=1:
            raise ValueError('checkpoint domain')
        t=float(tt)
        if not np.array_equal(rr[-1],self.Q@y[:self.n]): raise ValueError('checkpoint reference state')
        self.times=ts; self.refs=rr
        return t,y


def payload(data):
    h=hashlib.sha256()
    for key in sorted(data):
        a=np.ascontiguousarray(data[key]); h.update(key.encode());h.update(str((a.dtype.str,a.shape)).encode());h.update(a.tobytes())
    return h.hexdigest()
