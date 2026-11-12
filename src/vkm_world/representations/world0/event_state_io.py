"""Exact restart identity and integrity for the finite-event mechanical state."""
import hashlib,json
from pathlib import Path
import numpy as np
def array_digest(value):
    a=np.ascontiguousarray(value);h=hashlib.sha256()
    h.update(json.dumps([a.dtype.str,a.shape]).encode());h.update(a.tobytes());return h.hexdigest()
def sparse_digest(a):
    a=a.tocsr();return dict(shape=a.shape,data=array_digest(a.data),indices=array_digest(a.indices),indptr=array_digest(a.indptr))
def signature(payload):return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
def state_digest(x,v,flux,step,identity):
    return signature(dict(x=array_digest(x),v=array_digest(v),flux=array_digest(flux),step=int(step),identity=identity))
def save(path,x,v,flux,step,identity):
    x=np.asarray(x);v=np.asarray(v);flux=np.asarray(flux)
    if x.ndim!=1 or v.shape!=x.shape or flux.shape!=(4,) or not all(np.isfinite(a).all() for a in (x,v,flux)) or type(step) is not int or step<0:
        raise ValueError('invalid checkpoint state')
    np.savez_compressed(path,x=x,v=v,flux=flux,step=step,signature=identity,payload_sha256=state_digest(x,v,flux,step,identity))
def load(path,identity,shape,end_step):
    with np.load(path,allow_pickle=False) as z:
        if set(z.files)!={'x','v','flux','step','signature','payload_sha256'}:raise ValueError('checkpoint schema')
        x=z['x'];v=z['v'];flux=z['flux'];rawstep=z['step']
        if rawstep.shape!=() or rawstep.dtype.kind not in 'iu':raise ValueError('checkpoint step')
        step=int(rawstep)
        if str(z['signature'])!=identity:raise ValueError('restart actual operators/source/dt/law identity differs')
        if x.shape!=shape or v.shape!=shape or flux.shape!=(4,) or not 0<=step<end_step:raise ValueError('restart dimensions/time')
        if not all(a.dtype.kind=='f' and np.isfinite(a).all() for a in (x,v,flux)):raise ValueError('nonfinite/nonfloat restart')
        if str(z['payload_sha256'])!=state_digest(x,v,flux,step,identity):raise ValueError('checkpoint payload changed')
    return x.copy(),v.copy(),flux.copy(),step

def save_stage(path,x,v,flux,step,identity,warm):
    if x.ndim!=1 or v.shape!=x.shape or flux.shape!=(4,) or warm.shape!=(2*len(x),) or not all(np.isfinite(a).all() for a in (x,v,flux,warm)) or type(step) is not int or step<0:raise ValueError('stage checkpoint state')
    digest=signature(dict(state=state_digest(x,v,flux,step,identity),warm=array_digest(warm)))
    np.savez_compressed(path,x=x,v=v,flux=flux,step=step,signature=identity,warm=warm,payload_sha256=digest)

def load_stage(path,identity,shape,end_step):
    with np.load(path,allow_pickle=False) as z:
        if set(z.files)!={'x','v','flux','step','signature','warm','payload_sha256'}:raise ValueError('stage checkpoint schema')
        x=z['x'];v=z['v'];flux=z['flux'];warm=z['warm'];rawstep=z['step']
        if rawstep.shape!=() or rawstep.dtype.kind not in 'iu':raise ValueError('stage checkpoint step')
        step=int(rawstep)
        if str(z['signature'])!=identity or x.shape!=shape or v.shape!=shape or flux.shape!=(4,) or warm.shape!=(2*shape[0],) or not 0<=step<end_step:raise ValueError('stage checkpoint identity/dimensions')
        if not all(a.dtype.kind=='f' and np.isfinite(a).all() for a in (x,v,flux,warm)):raise ValueError('stage checkpoint nonfinite')
        expected=signature(dict(state=state_digest(x,v,flux,step,identity),warm=array_digest(warm)))
        if str(z['payload_sha256'])!=expected:raise ValueError('stage checkpoint payload changed')
    return x.copy(),v.copy(),flux.copy(),step,warm.copy()
