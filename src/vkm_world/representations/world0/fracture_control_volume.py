"""Finite, saturated fracture carrier in reference-volume mass coordinates.

The input pressure_increment_Pa is a reference-volume mass potential p [Pa],
not the mechanical pressure increment Pi=p+p*p/(2*K). The equilibrated
reference confinement defines physical absolute pressure P_ref+Pi.
The fluid free energy is the compressible quadratic perspective in (m,V).
No pressure clipping: cavitation/out-of-domain raises before state acceptance.
"""
from dataclasses import dataclass
import math


class CavitationDomainError(ValueError):
    pass


@dataclass(frozen=True)
class FractureControlVolume:
    area_m2: float
    residual_aperture_m: float
    fluid_bulk_Pa: float
    reference_gap_m: float
    reference_absolute_pressure_Pa: float
    minimum_absolute_pressure_Pa: float
    reference_density_kg_m3: float = 1000.

    def __post_init__(self):
        a=[self.area_m2,self.residual_aperture_m,self.fluid_bulk_Pa,self.reference_absolute_pressure_Pa,self.reference_density_kg_m3]
        if any(not math.isfinite(v) or v<=0 for v in a): raise ValueError('positive finite fracture parameters required')
        if not math.isfinite(self.reference_gap_m) or not 0<=self.minimum_absolute_pressure_Pa<self.reference_absolute_pressure_Pa:
            raise ValueError('invalid reference/cavitation domain')

    @property
    def reference_volume_m3(self):
        return self.area_m2*self.aperture(self.reference_gap_m)[0]

    def aperture(self,gap_m):
        """Smooth rough-interface void aperture: g=a-a0ВІ/a.

        a0 is the MODEL_CHOICE roughness aperture at nominal face contact.
        g<0 is asperity compliance, not negative water volume. This positive
        map keeps pressure work continuous through nominal contact; it is not
        a measured SKRU-1 roughness relation.
        """
        r=math.hypot(gap_m,2*self.residual_aperture_m)
        a=.5*(gap_m+r) if gap_m>=0 else 2*self.residual_aperture_m**2/(r-gap_m)
        derivative=.5*(1+gap_m/r)
        second=2*self.residual_aperture_m**2/r**3
        return a,derivative,second

    def evaluate(self,gap_m,pressure_increment_Pa,check_domain=True):
        """Evaluate p=rho_ref*(mu-mu_ref), in Pa.

        Legacy argument name is retained for keyword callers.
        absolute_pressure_Pa now reports physical mechanical pressure;
        pressure_conjugate_Pa is its increment (compatibility alias).
        """
        if not math.isfinite(gap_m) or not math.isfinite(pressure_increment_Pa): raise ValueError('finite state required')
        p=pressure_increment_Pa
        conjugate=p+.5*p*p/self.fluid_bulk_Pa
        absolute=self.reference_absolute_pressure_Pa+conjugate
        if check_domain and absolute<self.minimum_absolute_pressure_Pa:
            raise CavitationDomainError('saturated fracture reached cavitation bound; gas phase required')
        if check_domain and abs(p)/self.fluid_bulk_Pa>.01:
            raise ValueError('reference-density expansion range exceeded (1 percent)')
        aperture,da,d2a=self.aperture(gap_m)
        V=self.area_m2*aperture
        dVdg=self.area_m2*da
        mass=V*(1+p/self.fluid_bulk_Pa)-self.reference_volume_m3
        energy=.5*V/self.fluid_bulk_Pa*p*p
        # -dPsi/dV at fixed m, not simply p when C=V/K varies.
        return dict(volume_m3=V,mass_anomaly_ref_m3=mass,mass_anomaly_kg=self.reference_density_kg_m3*mass,
                    storage_m3_Pa=V/self.fluid_bulk_Pa,dV_dgap_m2=dVdg,
                    d2V_dgap2_m=self.area_m2*d2a,hydraulic_aperture_m=aperture,daperture_dgap=da,
                    dmass_dgap_m2=(1+p/self.fluid_bulk_Pa)*dVdg,
                    pressure_conjugate_Pa=conjugate,dconjugate_dp=1+p/self.fluid_bulk_Pa,
                    mass_potential_increment_Pa=p,
                    mechanical_pressure_increment_Pa=conjugate,
                    absolute_mechanical_pressure_Pa=absolute,
                    stored_energy_J=energy,absolute_pressure_Pa=absolute,
                    density_kg_m3=self.reference_density_kg_m3*(1+p/self.fluid_bulk_Pa))

    def energy_from_mass_volume(self,mass_anomaly_ref_m3,volume_m3):
        if volume_m3<=0: raise ValueError('positive fluid volume required')
        return .5*self.fluid_bulk_Pa/volume_m3*(mass_anomaly_ref_m3-volume_m3+self.reference_volume_m3)**2
