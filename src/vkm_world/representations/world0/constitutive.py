"""Small-strain SI constitutive updates, Mandel [xx,yy,zz,xy,yz,xz].

Abel kernels follow Volterra stress convolution, not step-load substitution.
Nishihara is a declared J2 extension, not a damage or tertiary-creep model.
Lubby2 follows the OGS equations (see source cards), with exact frozen-stress
branch integration and an analytic end-point compliance Jacobian.
All parameters and initial memory require an external physical-world bundle.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
import hashlib
import json
import math
import numpy as np

ALGORITHM_VERSION = 'vkm.constitutive/1-soe-lowatom-loggauss-exactstress'
I6 = np.eye(6)
H = np.zeros((6, 6)); H[:3, :3] = 1 / 3
P = I6 - H
D = np.array([1., 1., 1., math.sqrt(2), math.sqrt(2), math.sqrt(2)])


def strain_to_mandel(x): return np.asarray(x, float) / D
def stress_to_mandel(x): return np.asarray(x, float) * D
def strain_to_engineering(x): return np.asarray(x, float) * D
def stress_to_engineering(x): return np.asarray(x, float) / D
def tangent_to_engineering(x): return np.asarray(x, float) / D[:, None] / D[None, :]
def _mv(a, x): return np.einsum('...ij,...j->...i', a, x)
def _deviator(x):
    """Algebraic P*x with exact zero for hydrostatic inputs and zero trace.

    Avoid multiplying equal normal stresses by rounded thirds. Differences
    remove the hydrostatic part first; the third normal component enforces
    the deviatoric invariant after floating-point operations.
    """
    x=np.asarray(x,float);out=np.array(x,copy=True)
    a=x[...,0]-x[...,2];b=x[...,1]-x[...,2]
    out[...,0]=(2*a-b)/3;out[...,1]=(2*b-a)/3
    out[...,2]=-(out[...,0]+out[...,1])
    return out
def _dot(a, b): return np.sum(a * b, axis=-1)
def _outer(a, b): return a[..., :, None] * b[..., None, :]
def _frozen(x):
    # A write-protected owning ndarray can be made writable again with
    # setflags(write=True). An immutable bytes owner cannot.
    a = np.asarray(x, dtype=float)
    return np.frombuffer(a.tobytes(order='C'), dtype=np.float64).reshape(a.shape)


I6, H, P, D = map(_frozen, (I6, H, P, D))
def _positive(**kw):
    for k, v in kw.items():
        if not np.isfinite(v) or v <= 0: raise ValueError(f'{k} must be finite and positive')
def _nonnegative(**kw):
    for k, v in kw.items():
        if not np.isfinite(v) or v < 0: raise ValueError(f'{k} must be finite and nonnegative')


@dataclass(frozen=True, slots=True)
class ConstitutiveState:
    time_s: float
    strain: np.ndarray
    stress: np.ndarray
    memory: dict
    signature: str

    def __post_init__(self):
        if not np.isfinite(self.time_s): raise ValueError('nonfinite time')
        for name in ('strain', 'stress'):
            value = _frozen(getattr(self, name))
            if value.shape[-1:] != (6,) or not np.isfinite(value).all():
                raise ValueError('finite Mandel-6 tensors required')
            object.__setattr__(self, name, value)
        if self.strain.shape != self.stress.shape: raise ValueError('state shape mismatch')
        mem = {k: _frozen(v) for k, v in self.memory.items()}
        if any(not np.isfinite(v).all() for v in mem.values()): raise ValueError('nonfinite memory')
        object.__setattr__(self, 'memory', MappingProxyType(mem))

    def to_dict(self):
        return dict(schema='vkm.constitutive-state/1', time_s=self.time_s,
                    strain=self.strain.tolist(), stress=self.stress.tolist(),
                    memory={k: {'shape': list(v.shape), 'values': v.tolist()} for k, v in self.memory.items()},
                    signature=self.signature)

    @classmethod
    def from_dict(cls, data):
        if data['schema'] != 'vkm.constitutive-state/1': raise ValueError('state schema')
        mem = {k: np.asarray(v['values']).reshape(v['shape']) for k, v in data['memory'].items()}
        return cls(data['time_s'], data['strain'], data['stress'], mem, data['signature'])


@dataclass(frozen=True, slots=True)
class Trial:
    state: ConstitutiveState
    stress: np.ndarray
    tangent: np.ndarray
    diagnostics: dict

    def __post_init__(self):
        object.__setattr__(self, 'stress', _frozen(self.stress))
        object.__setattr__(self, 'tangent', _frozen(self.tangent))
        object.__setattr__(self, 'diagnostics', MappingProxyType({k: _frozen(v) if isinstance(v, np.ndarray) else v for k,v in self.diagnostics.items()}))


class _Model:
    def __getattribute__(self, name):
        value=object.__getattribute__(self,name)
        return MappingProxyType(value) if name=='__dict__' else value

    def __setattr__(self,name,value):
        if getattr(self,'_sealed',False):
            raise AttributeError('constitutive model is immutable; construct a new parameter bundle')
        object.__setattr__(self,name,value)

    def _parameters(self, values):
        for name,value in values.items():setattr(self,name,value)

    def _seal(self):
        for name,value in list(object.__getattribute__(self,'__dict__').items()):
            if isinstance(value,np.ndarray):object.__setattr__(self,name,_frozen(value))
            elif isinstance(value,list) and all(isinstance(a,np.ndarray) for a in value):
                object.__setattr__(self,name,tuple(_frozen(a) for a in value))
        object.__setattr__(self,'_sealed',True)

    def _signature(self, parameters):
        self.signature = hashlib.sha256(json.dumps([ALGORITHM_VERSION, type(self).__name__, parameters], sort_keys=True).encode()).hexdigest()

    def initial_state(self, batch_shape=(), time_s=0.):
        z = np.zeros(tuple(batch_shape) + (6,))
        return ConstitutiveState(time_s, z, z, self._initial_memory(tuple(batch_shape)), self.signature)

    def _check(self, state, x, dt):
        self._validate_state(state)
        _positive(dt_s=dt)
        x = np.asarray(x, float)
        if x.shape != state.strain.shape or not np.isfinite(x).all(): raise ValueError('tensor shape/finite values')
        return x

    def _validate_state(self,state):
        if state.signature != self.signature: raise ValueError('state belongs to another model/parameter bundle/algorithm')
        expected=self._initial_memory(state.strain.shape[:-1])
        if state.memory.keys()!=expected.keys():raise ValueError('state memory keys')
        for key,value in expected.items():
            if state.memory[key].shape!=value.shape:raise ValueError('state memory shape')

    def commit(self, trial):
        if trial.state.signature != self.signature: raise ValueError('trial model mismatch')
        return trial.state

    def revert(self, state, trial=None):
        if state.signature != self.signature: raise ValueError('state model mismatch')
        return state

    def instantaneous(self, state, strain):
        """Zero-duration equilibrium jump at fixed memory, same physical time.

        No stress interval is appended. In Abel models the hereditary offset
        at this instant is recovered from the previous consistent state.
        This is NOT a reset of the strain reference or of material prehistory.
        """
        self._validate_state(state)
        strain=np.asarray(strain,float)
        if strain.shape!=state.strain.shape or not np.isfinite(strain).all(): raise ValueError('tensor shape/finite values')
        C=np.linalg.inv(self.S)
        offset=state.strain-_mv(self.S,state.stress)
        stress=_mv(C,strain-offset)
        new=ConstitutiveState(state.time_s,strain,stress,dict(state.memory),self.signature)
        energy=.5*(_dot(stress,_mv(self.S,stress))-_dot(state.stress,_mv(self.S,state.stress)))
        return Trial(new,stress,np.broadcast_to(C,strain.shape[:-1]+(6,6)),dict(
            dt_s=0.,elastic_energy_change=energy,
            exact_elastic_jump_work=.5*_dot(stress+state.stress,strain-state.strain),
            energy_scope='fixed memory; elastic change only; birth/reference handling belongs to event integrator'))

    def trial_stress(self, state, stress, dt_s):
        stress = self._check(state, stress, dt_s)
        strain, compliance, memory, diagnostics = self._response(state, stress, dt_s)
        new = ConstitutiveState(state.time_s + dt_s, strain, stress, memory, self.signature)
        return Trial(new, stress, np.linalg.inv(compliance), diagnostics)

    def trial(self, state, strain, dt_s):
        strain = self._check(state, strain, dt_s)
        stress = state.stress.copy()
        target_scale = np.maximum(np.linalg.norm(strain, axis=-1), 1e-7)
        for iteration in range(60):
            estimate, compliance, _, _ = self._response(state, stress, dt_s)
            residual = estimate - strain
            norm = np.linalg.norm(residual, axis=-1) / target_scale
            if np.max(norm) < 2e-11:
                result = self.trial_stress(state, stress, dt_s)
                diag = dict(result.diagnostics, local_iterations=iteration, local_residual=float(np.max(norm)))
                return Trial(result.state, result.stress, result.tangent, diag)
            direction = np.linalg.solve(compliance, residual[..., None])[..., 0]
            step = np.ones_like(norm)
            for _ in range(30):
                candidate = stress - step[..., None] * direction
                try:
                    test = self._response(state, candidate, dt_s)[0] - strain
                    good = np.linalg.norm(test, axis=-1) / target_scale < norm
                except (FloatingPointError, OverflowError):
                    good = np.zeros_like(norm, dtype=bool)
                good = good | (norm < 2e-11)
                if np.all(good): break
                step = np.where(good, step, step * .5)
            else: raise RuntimeError('constitutive Newton line search failed; state unchanged')
            stress = candidate
        raise RuntimeError('constitutive Newton did not converge; state unchanged')


class Nishihara(_Model):
    """Elastic + isotropic Kelvin + associated J2 Bingham in series.

    K0,G0,K1,G1 [Pa]; eta_bulk,eta_shear,eta_vp [Pa s]; yield_stress [Pa].
    Kelvin equation: H sigma=3 K1 H k+3 eta_bulk H kdot,
    P sigma=2 G1 P k+2 eta_shear P kdot.
    Viscoplastic strain rate=<q-y>/eta_vp * 3s/(2q), q=sqrt(3s:s/2).
    Pressure independent, no hardening/dilatancy. At yield use elastic-side
    generalized derivative; finite differences must not straddle this corner.
    """
    def __init__(self, K0, G0, K1, G1, eta_bulk, eta_shear, eta_vp, yield_stress):
        values = dict(K0=K0,G0=G0,K1=K1,G1=G1,eta_bulk=eta_bulk,eta_shear=eta_shear,eta_vp=eta_vp)
        _positive(**values); _nonnegative(yield_stress=yield_stress)
        self._parameters(values); self.yield_stress=yield_stress
        self.S = H/(3*K0)+P/(2*G0)
        self.C1 = 3*K1*H+2*G1*P
        self._signature(dict(values, yield_stress=yield_stress))
        self._seal()

    def _initial_memory(self, shape): return {'kelvin': np.zeros(shape+(6,)), 'plastic': np.zeros(shape+(6,))}

    def _response(self, state, stress, dt):
        k0, p0 = state.memory['kelvin'], state.memory['plastic']
        ah = math.exp(-dt*self.K1/self.eta_bulk); ad = math.exp(-dt*self.G1/self.eta_shear)
        B = (1-ah)*H/(3*self.K1)+(1-ad)*P/(2*self.G1)
        k = _mv(ah*H+ad*P,k0)+_mv(B,stress)
        s = _mv(P,stress); q=np.sqrt(1.5*_dot(s,s)); safe=np.where(q>0,q,1.)
        # Arithmetic roundoff in the deviator projector must not activate an
        # exactly-on-yield uniaxial state. This is a machine-epsilon guard,
        # not a tunable material yield tolerance.
        active = q-self.yield_stress > 8*np.finfo(float).eps*np.maximum(q,self.yield_stress)
        f=np.where(active,1.5/self.eta_vp*(1-self.yield_stress/safe),0.)
        flow=f[...,None]*s
        J=f[...,None,None]*P+np.where(active,2.25*self.yield_stress/self.eta_vp/safe**3,0.)[...,None,None]*_outer(s,s)
        p=p0+dt*flow
        strain=_mv(self.S,stress)+k+p
        compliance=self.S+B+dt*J
        psi=.5*_dot(stress,_mv(self.S,stress))+.5*_dot(k,_mv(self.C1,k))
        psi0=.5*_dot(state.stress,_mv(self.S,state.stress))+.5*_dot(k0,_mv(self.C1,k0))
        force=stress-_mv(self.C1,k0)
        dissK=(1-ah*ah)*_dot(_mv(H,force),_mv(H,force))/(6*self.K1)+(1-ad*ad)*_dot(_mv(P,force),_mv(P,force))/(4*self.G1)
        dissP=dt*_dot(stress,flow)
        work=_dot(stress,strain-state.strain)
        return strain, compliance, {'kelvin':k,'plastic':p}, dict(stored_energy=psi,
            physical_dissipation=dissK+dissP, endpoint_work=work,
            endpoint_work_minus_energy=work-(psi-psi0),
            quadrature_excess=work-(psi-psi0)-dissK-dissP, equivalent_stress=q,
            energy_scope='J/m3; exact constant-stress branch dissipation; endpoint work includes elastic-jump quadrature excess')


class Lubby2(_Model):
    """OGS LUBBY2, isothermal elastic bulk and deviatoric Kelvin+Maxwell.

    K0,G0,GK0 [Pa], etaK0,etaM0 [Pa s], mK,mM,mG [1/Pa].
    etaK=etaK0 exp(mK*q); etaM=etaM0 exp(mM*q); GK=GK0 exp(mG*q).
    No parameter clipping, no damage/thermal activation. Variable GK does not
    imply a thermodynamic potential: diagnostic energy is not a passivity proof.
    """
    def __init__(self,K0,G0,GK0,etaK0,etaM0,mK=0.,mM=0.,mG=0.):
        values=dict(K0=K0,G0=G0,GK0=GK0,etaK0=etaK0,etaM0=etaM0,mK=mK,mM=mM,mG=mG)
        _positive(K0=K0,G0=G0,GK0=GK0,etaK0=etaK0,etaM0=etaM0)
        if not np.isfinite([mK,mM,mG]).all(): raise ValueError('finite stress coefficients')
        self._parameters(values); self.S=H/(3*K0)+P/(2*G0); self._signature(values)
        self._seal()

    def _initial_memory(self,shape): return {'kelvin':np.zeros(shape+(6,)),'maxwell':np.zeros(shape+(6,))}

    def _response(self,state,stress,dt):
        k0,v0=state.memory['kelvin'],state.memory['maxwell']
        s=_mv(P,stress); q=np.sqrt(1.5*_dot(s,s)); safe=np.where(q>0,q,1.)
        with np.errstate(over='raise',under='ignore',invalid='raise',divide='raise'):
            GK=self.GK0*np.exp(self.mG*q); ek=self.etaK0*np.exp(self.mK*q); em=self.etaM0*np.exp(self.mM*q)
            r=GK/ek; a=np.exp(-dt*r); b=-np.expm1(-dt*r)/(2*GK); c=dt/(2*em)
            aq=-dt*r*(self.mG-self.mK)*a
            bq=-aq/(2*GK)-self.mG*b
            cq=-self.mM*c
        k=a[...,None]*k0+b[...,None]*s; v=v0+c[...,None]*s
        dq=np.where((q>0)[...,None],1.5*s/safe[...,None],0.)
        derivative=aq[...,None]*k0+(bq+cq)[...,None]*s
        compliance=self.S+(b+c)[...,None,None]*P+_outer(derivative,dq)
        strain=_mv(self.S,stress)+k+v
        force=s-2*GK[...,None]*k0
        dissK=(1-a*a)*_dot(force,force)/(4*GK); dissM=c*_dot(s,s)
        psi=.5*_dot(stress,_mv(self.S,stress))+GK*_dot(k,k)
        return strain,compliance,{'kelvin':k,'maxwell':v},dict(equivalent_stress=q,
            stored_energy_diagnostic=psi, branch_viscous_dissipation=dissK+dissM,
            energy_scope='J/m3; positive frozen-stress branch dissipation; variable GK thermodynamic passivity NOT_CLAIMED',
            nonsmooth_q_zero=bool(np.any((q==0)&(np.linalg.norm(k0,axis=-1)>0))))


class DeviatoricMaxwell(_Model):
    """Continuation of TensorStages' elastic-bulk/deviatoric Maxwell equation.

    K0,G0 [Pa], eta_shear [Pa s]. q_creep_dot=s/(2 eta_shear).
    Stress-controlled constant steps are exact. Strain-controlled end-point
    update is the existing backward-Euler ratio dt*G0/eta_shear, NOT an exact
    exponential solution for arbitrary strain histories.

    K0/G0 are one elastic group; eta_shear may be scalar or have the leading
    batch dimensions (e.g. per-cell eta for [cell,gauss,6] material state).
    A legacy engineering-strain memory needs an explicit Mandel mapping and
    provenance check; this class never silently imports a predecessor state.
    """
    def __init__(self,K0,G0,eta_shear):
        _positive(K0=K0,G0=G0)
        eta=np.asarray(eta_shear,float)
        if not np.isfinite(eta).all() or np.any(eta<=0):raise ValueError('positive finite eta_shear required')
        self.K0=K0;self.G0=G0;self.eta_shear=eta
        self.S=H/(3*K0)+P/(2*G0)
        self._signature(dict(K0=K0,G0=G0,eta_shear=eta.tolist(),eta_shape=list(eta.shape)))
        self._seal()

    def _initial_memory(self,shape):return {'creep':np.zeros(shape+(6,))}

    def _eta(self,shape):
        if self.eta_shear.ndim>len(shape):raise ValueError('eta rank exceeds batch rank')
        try:return np.broadcast_to(self.eta_shear.reshape(self.eta_shear.shape+(1,)*(len(shape)-self.eta_shear.ndim)),shape)
        except ValueError as exc:raise ValueError('eta must broadcast over leading batch dimensions') from exc

    def _validate_state(self,state):
        super()._validate_state(state)
        c=state.memory['creep']
        scale=np.maximum(np.linalg.norm(c,axis=-1),1e-20)
        if np.any(np.abs(c[...,:3].sum(axis=-1))>1e-12*scale):raise ValueError('Maxwell memory must be deviatoric')
        self._eta(state.strain.shape[:-1])

    def _response(self,state,stress,dt):
        eta=self._eta(stress.shape[:-1]);s=_deviator(stress)
        creep=_deviator(state.memory['creep']+dt*s/(2*eta[...,None]))
        compliance=self.S+(dt/(2*eta))[...,None,None]*P
        elastic=s/(2*self.G0)
        elastic[...,:3]+=np.sum(stress[...,:3],axis=-1)[...,None]/(9*self.K0)
        strain=elastic+creep
        psi=.5*_dot(stress,_mv(self.S,stress));psi0=.5*_dot(state.stress,_mv(self.S,state.stress))
        work=_dot(stress,strain-state.strain);diss=dt*_dot(s,s)/(2*eta)
        return strain,compliance,{'creep':creep},dict(stored_energy=psi,physical_dissipation=diss,
            endpoint_work=work,endpoint_work_minus_energy=work-(psi-psi0),
            quadrature_excess=work-(psi-psi0)-diss,
            energy_scope='J/m3; elastic bulk, deviatoric Maxwell; exact constant-stress dissipation, end-point strain integration is backward Euler')

    def trial(self,state,strain,dt_s):
        strain=self._check(state,strain,dt_s)
        eta=self._eta(strain.shape[:-1]);ratio=dt_s*self.G0/eta
        tangent=3*self.K0*H+(2*self.G0/(1+ratio))[...,None,None]*P
        stress=(2*self.G0/(1+ratio))[...,None]*(_deviator(strain)-state.memory['creep'])
        stress[...,:3]+=self.K0*np.sum(strain[...,:3],axis=-1)[...,None]
        response=self._response(state,stress,dt_s)
        new=ConstitutiveState(state.time_s+dt_s,response[0],stress,response[2],self.signature)
        return Trial(new,stress,tangent,response[3])


def delta_to_seconds(delta, alpha, seconds_per_unit):
    """delta has unit T^(alpha-1); convert original time-unit to seconds."""
    _positive(seconds_per_unit=seconds_per_unit); _nonnegative(delta=delta)
    if not 0<alpha<1: raise ValueError('0<alpha<1 required')
    return delta*seconds_per_unit**(alpha-1)


def _power_integral(age_left, age_right, alpha):
    # integral_{age_right}^{age_left} t^-alpha dt; stable for narrow old intervals.
    left=np.asarray(age_left); right=np.asarray(age_right); beta=1-alpha
    ratio=np.divide(right,left,out=np.zeros_like(left,dtype=float),where=left>0)
    with np.errstate(divide='ignore'):
        return left**beta*(-np.expm1(beta*np.log(ratio)))/beta


class AbelDirect(_Model):
    """Exact piecewise-constant stress convolution, O(N) memory/work per step.

    e=H(sigma+delta_bulk integral t^-alpha_bulk sigma)/(3K)
      +P(sigma+delta_dev integral t^-alpha_dev sigma)/(2G).
    Kernel uses no 1/Gamma factor: delta Gamma(1-alpha) I^(1-alpha).
    State history is stress on each accepted time interval; event jumps cost no
    history reset. Discrete relaxation is the inverse of the same Volterra map.
    """
    def __init__(self,K,G,alpha_bulk,delta_bulk,alpha_dev,delta_dev):
        _positive(K=K,G=G); _nonnegative(delta_bulk=delta_bulk,delta_dev=delta_dev)
        if not 0<alpha_bulk<1 or not 0<alpha_dev<1: raise ValueError('0<alpha<1')
        vals=dict(K=K,G=G,alpha_bulk=alpha_bulk,delta_bulk=delta_bulk,alpha_dev=alpha_dev,delta_dev=delta_dev)
        self._parameters(vals); self.S=H/(3*K)+P/(2*G); self._signature(vals)
        if type(self) is AbelDirect:self._seal()

    def _initial_memory(self,shape):
        return {'ends':np.empty(0),'starts':np.empty(0),'history':np.empty((0,)+shape+(6,))}

    def _validate_state(self,state):
        if type(self) is not AbelDirect:
            return super()._validate_state(state)
        if state.signature!=self.signature:raise ValueError('state model/parameter/algorithm mismatch')
        m=state.memory
        if set(m)!={'ends','starts','history'}:raise ValueError('history memory keys')
        n=m['ends'].size
        if m['ends'].shape!=(n,) or m['starts'].shape!=(n,) or m['history'].shape!=(n,)+state.strain.shape:
            raise ValueError('history memory shape')
        if n and (np.any(m['ends']<=m['starts']) or not np.array_equal(m['starts'][1:],m['ends'][:-1]) or m['ends'][-1]!=state.time_s):
            raise ValueError('history interval consistency')

    def _response(self,state,stress,dt):
        t=state.time_s+dt; mem=state.memory; hist=mem['history']
        past=np.zeros_like(stress)
        for alpha,delta,proj,mod in [(self.alpha_bulk,self.delta_bulk,H,3*self.K),(self.alpha_dev,self.delta_dev,P,2*self.G)]:
            if hist.shape[0]:
                weights=delta*_power_integral(t-mem['starts'],t-mem['ends'],alpha)
                past+=_mv(proj,np.tensordot(weights,hist,axes=(0,0)))/mod
        cb=self.delta_bulk*dt**(1-self.alpha_bulk)/(1-self.alpha_bulk)
        cd=self.delta_dev*dt**(1-self.alpha_dev)/(1-self.alpha_dev)
        compliance=(1+cb)*H/(3*self.K)+(1+cd)*P/(2*self.G)
        strain=_mv(compliance,stress)+past
        new={'starts':np.append(mem['starts'],state.time_s),'ends':np.append(mem['ends'],t),
             'history':np.concatenate([hist,stress[None,...]],axis=0)}
        return strain,np.broadcast_to(compliance,stress.shape[:-1]+(6,6)),new,dict(history_intervals=len(new['ends']),
            energy_scope='linear positive Abel compliance; no finite-mode free energy assigned to direct quadrature')


class AbelSOE(AbelDirect):
    """Positive exponential history, exact newest singular interval.

    Laplace quadrature t^-alpha=integral exp(-lambda*t)*lambda^(alpha-1)
    d lambda/Gamma(alpha). Positive log-Gauss quadrature plus a low-frequency
    atom matching zeroth/first moments of [0,lambda_low]; no negative fitting.
    On [t_min,horizon] kernel error is measured by kernel_error(), not presumed.
    Every dt>=t_min and age since initialization<=horizon is enforced.
    O(modes) memory independent of accepted steps. CPU NumPy batch implementation.
    """
    def __init__(self,K,G,alpha_bulk,delta_bulk,alpha_dev,delta_dev,t_min,horizon,modes=128,tail_tolerance=1e-9):
        super().__init__(K,G,alpha_bulk,delta_bulk,alpha_dev,delta_dev)
        _positive(t_min=t_min,horizon=horizon,tail_tolerance=tail_tolerance)
        if horizon<t_min or not isinstance(modes,int) or modes<8 or tail_tolerance>=.1: raise ValueError('invalid SOE window/modes/tolerance')
        self.t_min=t_min; self.horizon=horizon; self.modes=modes
        self.rates=[];self.weights=[]
        for alpha in (alpha_bulk,alpha_dev):
            # Integrate the singular low-frequency density through a positive
            # atom, avoiding an unnecessarily huge log-window for small alpha.
            # Matching mean lambda cancels the first-order Taylor error.
            lower=math.sqrt(tail_tolerance)/horizon
            upper=max(1.,-math.log(tail_tolerance)*2)/t_min
            lo,hi=math.log(lower),math.log(upper)
            nodes,quadw=np.polynomial.legendre.leggauss(modes-1)
            grid=(lo+hi)/2+(hi-lo)/2*nodes
            rates=np.exp(grid); weights=(hi-lo)/2*quadw*np.exp(alpha*grid)/math.gamma(alpha)
            rates=np.r_[lower*alpha/(alpha+1),rates]
            weights=np.r_[lower**alpha/math.gamma(alpha+1),weights]
            self.rates.append(rates);self.weights.append(weights)
        self._signature(dict(K=K,G=G,alpha_bulk=alpha_bulk,delta_bulk=delta_bulk,alpha_dev=alpha_dev,delta_dev=delta_dev,
            t_min=t_min,horizon=horizon,modes=modes,tail_tolerance=tail_tolerance))
        self._seal()

    def _initial_memory(self,shape):
        return {'bulk_modes':np.zeros((self.modes,)+shape+(6,)),
                'dev_modes':np.zeros((self.modes,)+shape+(6,)), 'elapsed':np.array(0.)}

    def kernel_error(self,points=2000):
        ages=np.geomspace(self.t_min,self.horizon,points)
        return np.array([np.max(np.abs((np.exp(-ages[:,None]*r)@w)*ages**a-1))
            for a,r,w in zip((self.alpha_bulk,self.alpha_dev),self.rates,self.weights)])

    def _response(self,state,stress,dt):
        elapsed=float(state.memory['elapsed'])+dt
        if dt<self.t_min*(1-1e-12) or elapsed>self.horizon*(1+1e-12): raise ValueError('step/history outside qualified SOE window')
        past=np.zeros_like(stress);new={'elapsed':np.array(elapsed)}
        for name,alpha,delta,proj,mod,r,w in zip(('bulk_modes','dev_modes'),(self.alpha_bulk,self.alpha_dev),
                (self.delta_bulk,self.delta_dev),(H,P),(3*self.K,2*self.G),self.rates,self.weights):
            expand=(self.modes,)+(1,)*stress.ndim
            decay=np.exp(-r*dt).reshape(expand)
            old=state.memory[name]
            past+=delta*_mv(proj,np.tensordot(w,decay*old,axes=(0,0)))/mod
            new[name]=decay*old+(-np.expm1(-r*dt)/r).reshape(expand)*stress
        cb=self.delta_bulk*dt**(1-self.alpha_bulk)/(1-self.alpha_bulk)
        cd=self.delta_dev*dt**(1-self.alpha_dev)/(1-self.alpha_dev)
        compliance=(1+cb)*H/(3*self.K)+(1+cd)*P/(2*self.G)
        return _mv(compliance,stress)+past,np.broadcast_to(compliance,stress.shape[:-1]+(6,6)),new,dict(
            modes=self.modes,memory_bytes=sum(x.nbytes for x in new.values()),
            energy_scope='positive kernel quadrature + exact near interval; error qualification required')
