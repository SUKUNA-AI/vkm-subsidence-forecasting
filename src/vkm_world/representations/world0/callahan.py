"""SRC192 equations 2-3..6, 2-18, 2-20..29, 2-34..37; SI API.

Non-associated crushed-salt creep with both kinetic mechanisms. The 0D
uniaxial carrier below is a declared Hencky-elastic scenario, NOT the missing
Callahan-1995 nonlinear elastic model. Compression-negative source convention.
"""
from dataclasses import dataclass, asdict
import hashlib
import json
import math
import numpy as np
from pathlib import Path


@dataclass(frozen=True)
class Bundle:
    fit: str = 'shear'
    temperature_K: float = 293.15
    moisture_mass_fraction: float = .01
    grain_m: float = .001
    original_fractional_density: float = .64

    def __post_init__(self):
        if self.fit not in ('shear','combined_star'): raise ValueError('unknown source fit')
        if not all(math.isfinite(v) for v in asdict(self).values() if isinstance(v,(int,float))): raise ValueError('nonfinite input')
        if self.temperature_K<=0 or self.grain_m<=0 or not 0<=self.moisture_mass_fraction<1: raise ValueError('invalid thermochemical input')
        if not 0<self.original_fractional_density<.85: raise ValueError('initial porosity must exceed 15 percent for source Gamma branch')

    @property
    def parameters(self):
        # Each entire column is one fit. No mixing of the two regressions.
        if self.fit=='shear':
            return dict(eta0=.1029,eta1=3.9387,nf=3.5122,a=.3147,p=1.6332,ns=.5576,r1star=.0194,r3=15.1281,r4=.1678,Qsr=1077.46)
        return dict(eta0=.0140,eta1=3.3881,nf=3.9660,a=1e-5,p=1.0341,ns=1.5318,r1star=.3369,r3=.1286,r4=.1680,Qsr=2897.08)

    @property
    def signature(self):
        return hashlib.sha256(json.dumps({'version':'callahan-source192-v1','inputs':asdict(self),'source_column':self.parameters},sort_keys=True).encode()).hexdigest()

    @staticmethod
    def omega(D,n):
        if D==1: return 0.
        return ((1-D)*n/(1-(1-D)**(1/n))**n)**(2/(n+1))

    def equivalent(self,D,mean_Pa,difference_Pa):
        if not all(math.isfinite(x) for x in (D,mean_Pa,difference_Pa)): raise ValueError('nonfinite state')
        if not 0<D<=1 or difference_Pa<0: raise ValueError('invalid density or stress invariant')
        b=self.parameters; n=1.331
        eta=b['eta0']*self.omega(D,b['nf'])**b['eta1']
        kap=10.119*self.omega(max(D,.896),n)**1.005
        tf=((2-D)/D)**(2*b['nf']/(b['nf']+1))
        tk=((2-D)/D)**(2*n/(n+1))
        return math.sqrt(eta*mean_Pa**2+tf*difference_Pa**2),math.sqrt(kap*mean_Pa**2+tk*difference_Pa**2),kap,tk

    def kinetics(self,D,equivalent_Pa,zeta):
        if not self.original_fractional_density<D<=1 or equivalent_Pa<0 or zeta<0: raise ValueError('invalid consolidation/prestrain/memory domain')
        if not all(math.isfinite(x) for x in (D,equivalent_Pa,zeta)): raise ValueError('nonfinite state')
        if equivalent_Pa==0: return dict(md=0.,solution=0.,zeta_rate=0.,steady=0.,F=1.,Gamma=0. if D==1 else None)
        b=self.parameters; T=self.temperature_K; s=equivalent_Pa/1e6; x=s/12400.
        steady=8.386e22*x**5.5*math.exp(-12581/T)+9.672e12*x**5*math.exp(-5033/T)
        if s>20.57:
            arg=5335*(s-20.57)/12400.
            if arg>700: raise ValueError('outside finite source kinetic range')
            steady+=(6.0856e6*math.exp(-12581/T)+.03034*math.exp(-5033/T))*math.sinh(arg)
        et=6.275e5*math.exp(.009198*T)*x**3
        hard=-17.37-7.738*math.log10(x)
        if hard<0: raise ValueError('source hardening parameter became negative')
        ratio=zeta/et
        F=math.exp(hard*(1-ratio)**2) if ratio<1 else (0. if ratio>40 else math.exp(-.58*(1-ratio)**2))
        ev=math.log(self.original_fractional_density/D)
        engineering=math.expm1(ev)
        Gamma=1. if engineering>=-.15 else ((math.exp(ev)-self.original_fractional_density)/((1-self.original_fractional_density)*math.exp(ev)))**b['ns']
        if D==1: Gamma=0.
        w=self.moisture_mass_fraction
        # Direct r1* at reported reference w=1%, d=1mm; the combined raw r1
        # has a factor-ten table conflict and is never silently substituted.
        rstar=0. if w==0 else b['r1star']*(w/.01)**b['a']*(self.grain_m/.001)**(-b['p'])
        solution=rstar*math.exp((b['r3']-1)*ev-b['Qsr']/T)/(T*abs(engineering)**b['r4'])*Gamma*s
        return dict(md=F*steady,solution=solution,zeta_rate=(F-1)*steady,steady=steady,F=F,Gamma=Gamma)

    def triaxial_compression_rates(self,D,axial_compression_Pa,lateral_compression_Pa,zeta):
        if axial_compression_Pa<lateral_compression_Pa or lateral_compression_Pa<0: raise ValueError('compression path required')
        mean=-(axial_compression_Pa+2*lateral_compression_Pa)/3
        diff=axial_compression_Pa-lateral_compression_Pa
        sf,se,kap,tk=self.equivalent(D,mean,diff)
        k=self.kinetics(D,sf,zeta)
        if se==0: return dict(k,axial=0.,lateral=0.,axial_md=0.,axial_solution=0.,effective_Pa=sf)
        alpha=kap*mean/3; beta=tk*diff
        fa=(alpha-beta)/se; fl=(alpha+.5*beta)/se
        return dict(k,axial=fa*(k['md']+k['solution']),lateral=fl*(k['md']+k['solution']),axial_md=fa*k['md'],axial_solution=fa*k['solution'],effective_Pa=sf)


@dataclass(frozen=True)
class UniaxialCarrier:
    bundle: Bundle
    E_Pa: float = 28e6
    poisson: float = .25
    birth_fractional_density: float = .70
    grain_density_kg_m3: float = 2160.
    water_density_kg_m3: float = 1000.
    birth_volume_m3: float = 1.
    birth_time_s: float = 864000.
    creep_enabled: bool = True

    def __post_init__(self):
        if not 0<=self.poisson<.5 or not self.bundle.original_fractional_density<self.birth_fractional_density<1: raise ValueError('invalid birth state')
        if min(self.E_Pa,self.grain_density_kg_m3,self.water_density_kg_m3,self.birth_volume_m3)<=0: raise ValueError('positive carrier parameters required')
        if not all(math.isfinite(v) for v in asdict(self).values() if isinstance(v,(int,float))): raise ValueError('nonfinite carrier')

    @property
    def signature(self):
        return hashlib.sha256(json.dumps({'carrier':'uniaxial-hencky-unilateral-v1','parameters':asdict(self)},sort_keys=True).encode()).hexdigest()

    def state(self,engineering_strain,y,check_memory=True):
        if engineering_strain<=-1: raise ValueError('nonpositive boundary stretch')
        eca,ecl,zeta=np.asarray(y)[:3]
        if not np.isfinite([engineering_strain,eca,ecl,zeta]).all(): raise ValueError('nonfinite state')
        if check_memory and zeta<0: raise ValueError('negative accepted transient memory')
        eb=math.log1p(engineering_strain)
        ee=min(eb-eca,0.)
        ea=eca+ee; el=ecl-self.poisson*ee
        J=math.exp(ea+2*el); D=self.birth_fractional_density/J
        if not self.bundle.original_fractional_density<D<=1: raise ValueError('density left source consolidation domain')
        tau=self.E_Pa*ee
        V=self.birth_volume_m3*J
        solid_mass=self.grain_density_kg_m3*self.birth_fractional_density*self.birth_volume_m3
        w=self.bundle.moisture_mass_fraction
        water_mass=solid_mass*w/(1-w)
        gas=V-solid_mass/self.grain_density_kg_m3-water_mass/self.water_density_kg_m3
        if gas<0: raise ValueError('fixed retained moisture no longer fits pores; drainage model required')
        return dict(ea=ea,el=el,J=J,D=D,porosity=1-D,tau_Pa=tau,cauchy_compression_Pa=-tau/J,
                    nominal_compression_Pa=-tau/(1+engineering_strain),volume_m3=V,
                    stored_J=.5*self.E_Pa*ee*ee*self.birth_volume_m3,
                    solid_mass_kg=solid_mass,water_mass_kg=water_mass,gas_volume_m3=gas,
                    boundary_gap_m=max(0.,1+engineering_strain-math.exp(ea)),elastic_axial_strain=ee)

    def rhs(self,engineering_strain,engineering_rate,y):
        r=self.state(engineering_strain,y,check_memory=False)
        # Continuous off-domain extension for ODE trial stages only. No accepted
        # memory is clipped; state() rejects every negative accepted zeta.
        k=self.bundle.triaxial_compression_rates(r['D'],r['cauchy_compression_Pa'],0.,max(0.,float(y[2])))
        if not self.creep_enabled:
            k=dict(k,axial=0.,lateral=0.,zeta_rate=0.,axial_md=0.,axial_solution=0.)
        v=self.birth_volume_m3; tau=r['tau_Pa']
        return np.array([k['axial'],k['lateral'],k['zeta_rate'],v*tau*k['axial_md'],v*tau*k['axial_solution'],v*tau*engineering_rate/(1+engineering_strain)])


def checkpoint(model,history_signature,segment,elapsed_s,y):
    state=np.asarray(y,dtype=float)
    if state.shape!=(6,) or not np.isfinite(state).all() or state[2]<0: raise ValueError('invalid checkpoint state')
    data=dict(schema='callahan-checkpoint-v1',model_signature=model.signature,
              code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              history_signature=history_signature,segment=int(segment),elapsed_s=float(elapsed_s),y=state.tolist())
    data['checksum']=hashlib.sha256(json.dumps(data,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return data


def restore(model,history_signature,data):
    payload=dict(data);digest=payload.pop('checksum',None)
    if digest!=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest(): raise ValueError('checkpoint corruption')
    if payload['schema']!='callahan-checkpoint-v1' or payload['model_signature']!=model.signature or payload['history_signature']!=history_signature:
        raise ValueError('incompatible model/history restart')
    if payload['code_sha256']!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(): raise ValueError('incompatible law code')
    y=np.asarray(payload['y'],dtype=float)
    if y.shape!=(6,) or not np.isfinite(y).all() or y[2]<0: raise ValueError('invalid restart memory')
    return int(payload['segment']),float(payload['elapsed_s']),y
