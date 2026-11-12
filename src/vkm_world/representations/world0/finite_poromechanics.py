"""Finite-J reference-storage poromechanics; density API for TL assembly.

zeta [m3] is reference-volume fluid mass anomaly, S [m3/Pa] is fixed-strain
reference storage. W [J/m3], P [Pa], A [Pa] are per reference volume; an FE
assembler multiplies them by V0 ONCE. No current-geometry Darcy law is implied.
"""
from dataclasses import dataclass,asdict
import hashlib,json
import numpy as np


def cofactor(F):
    F=np.asarray(F,dtype=float)
    if F.shape!=(3,3) or not np.isfinite(F).all(): raise ValueError('finite 3x3 F required')
    J=float(np.linalg.det(F))
    if J<=0: raise ValueError('nonpositive deformation Jacobian')
    Q=np.linalg.inv(F).T
    C=J*Q
    dC=J*(np.einsum('ij,kl->ijkl',Q,Q)-np.einsum('il,kj->ijkl',Q,Q))
    return J,C,dC


@dataclass(frozen=True)
class ReferenceFluid:
    V0_m3: float
    storage_m3_Pa: float
    alpha: float
    reference_pressure_Pa: float
    reference_J: float = 1.
    initial_fluid_reference_volume_m3: float = .3
    reference_density_kg_m3: float = 1000.
    minimum_pressure_Pa: float = 3000.

    def __post_init__(self):
        p=asdict(self)
        if not np.isfinite(list(p.values())).all(): raise ValueError('finite parameters required')
        if min(self.V0_m3,self.storage_m3_Pa,self.reference_J,self.initial_fluid_reference_volume_m3,self.reference_density_kg_m3)<=0: raise ValueError('positive volume/storage/density required')
        if not 0<=self.alpha<=1 or not 0<=self.minimum_pressure_Pa<self.reference_pressure_Pa: raise ValueError('invalid coupling/pressure domain')
        if self.initial_fluid_reference_volume_m3>=self.V0_m3*self.reference_J:
            raise ValueError('initial geometric pore volume must be below total cell volume')

    @property
    def signature(self):
        return hashlib.sha256(json.dumps({'law':'finite-J-reference-storage-v2-geometric-domain','parameters':asdict(self)},sort_keys=True).encode()).hexdigest()

    def content(self,F,pressure_increment_Pa):
        J,C,dC=cofactor(F);p=float(pressure_increment_Pa)
        if not np.isfinite(p): raise ValueError('finite pressure required')
        return self.storage_m3_Pa*p+self.alpha*self.V0_m3*(J-self.reference_J)

    def evaluate(self,F,zeta_m3,check_domain=True):
        J,C,dC=cofactor(F);zeta=float(zeta_m3)
        if not np.isfinite(zeta): raise ValueError('finite content required')
        a=self.alpha;V=self.V0_m3;S=self.storage_m3_Pa
        capacity=a*V*(J-self.reference_J)
        pore_volume=self.initial_fluid_reference_volume_m3+capacity
        current_volume=V*J;solid_volume=current_volume-pore_volume
        x=zeta-capacity;p=x/S;absolute=self.reference_pressure_Pa+p
        if check_domain and absolute<self.minimum_pressure_Pa: raise ValueError('one-phase pressure domain exceeded')
        if check_domain and self.initial_fluid_reference_volume_m3+zeta<=0: raise ValueError('nonpositive fluid mass inventory')
        if check_domain and pore_volume<=0: raise ValueError('nonpositive geometric pore capacity')
        if check_domain and solid_volume<=0: raise ValueError('nonpositive geometric solid volume')
        energy=.5*x*x/S+self.reference_pressure_Pa*x
        P=-a*absolute*C
        A=a*a*V/S*np.einsum('ij,kl->ijkl',C,C)-a*absolute*dC
        return dict(W=energy/V,energy_J=energy,J=J,cofactor=C,P=P,A=A,
                    A_fixed_pressure=-a*absolute*dC,
                    pressure_increment_Pa=p,absolute_pressure_Pa=absolute,
                    zeta_m3=zeta,capacity_change_m3=capacity,
                    pore_capacity_m3=pore_volume,current_cell_volume_m3=current_volume,
                    solid_volume_m3=solid_volume,porosity=pore_volume/current_volume,
                    storage_mass_anomaly_m3=x,
                    dP_dzeta=-a/S*C,dp_dF=-a*V/S*C,dp_dzeta=1/S,
                    dcontent_dF=a*V*C,dcontent_dp=S,
                    fluid_mass_kg=self.reference_density_kg_m3*(self.initial_fluid_reference_volume_m3+zeta))

    def mixed(self,F,pressure_increment_Pa,check_domain=True):
        r=self.evaluate(F,self.content(F,pressure_increment_Pa),check_domain)
        r['A_fixed_content']=r['A'];r['A']=r['A_fixed_pressure']
        r['dP_dp']=-self.alpha*r['cofactor']
        return r


def discrete_cofactor(F0,F1):
    """Exact line integral on a positive-J straight deformation path.

    Positive endpoint determinants alone do not make the intervening path
    admissible. Check extrema of its cubic determinant before integrating.
    """
    F0=np.asarray(F0,dtype=float);F1=np.asarray(F1,dtype=float);dF=F1-F0
    J0,C0,_=cofactor(F0);J1,C1,dC1=cofactor(F1)
    c1=float(np.sum(C0*dF));c3=float(np.linalg.det(dF));c2=J1-J0-c1-c3
    for s in np.roots([3*c3,2*c2,c1]):
        if abs(s.imag)<1e-12 and 0<s.real<1:
            if np.linalg.det(F0+s.real*dF)<=0:
                raise ValueError('straight deformation path crosses nonpositive Jacobian')
    _,Cm,dCm=cofactor(.5*(F0+F1))
    return (C0+4*Cm+C1)/6,(2*dCm+dC1)/6
