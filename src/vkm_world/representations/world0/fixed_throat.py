"""Fixed circular, incompressible hydraulic carrier; SI throughout.

I*dQ/dt + R*Q + F*Q*abs(Q) = p_in-p_out, Q in m3/s.
Additive linear/quadratic resistance is a declared lumped MODEL_CHOICE,
not an all-Re exact pipe solution. Geometry is independent of fracture gap.
Inventory is rho*A*L and must be accounted once, outside fracture storage.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math


@dataclass(frozen=True)
class FixedThroat:
    length_m: float
    diameter_m: float
    density_kg_m3: float
    dynamic_viscosity_Pa_s: float
    darcy_friction_factor: float = 0.02
    minor_loss_coefficient: float = 1.0
    kinetic_shape_factor: float = 1.0

    def __post_init__(self):
        for key, value in asdict(self).items():
            if not math.isfinite(value):
                raise ValueError(f"nonfinite {key}")
            lower_allowed = key in ("darcy_friction_factor", "minor_loss_coefficient")
            if value < 0 or (value == 0 and not lower_allowed):
                raise ValueError(f"invalid {key}")

        if self.kinetic_shape_factor < 1:
            raise ValueError("kinetic shape factor must be at least one")

    @property
    def area_m2(self):
        return math.pi * self.diameter_m**2 / 4

    @property
    def volume_m3(self):
        return self.area_m2 * self.length_m

    @property
    def fluid_mass_kg(self):
        return self.density_kg_m3 * self.volume_m3

    @property
    def inertance(self):
        return self.kinetic_shape_factor * self.density_kg_m3 * self.length_m / self.area_m2

    @property
    def linear_resistance(self):
        return 8 * math.pi * self.dynamic_viscosity_Pa_s * self.length_m / self.area_m2**2

    @property
    def quadratic_resistance(self):
        return self.density_kg_m3 * (self.minor_loss_coefficient + self.darcy_friction_factor * self.length_m / self.diameter_m) / (2 * self.area_m2**2)

    @property
    def signature(self):
        payload = {"law": "fixed-throat-I-R-F-v1.1", "parameters": asdict(self)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()

    @staticmethod
    def _finite(value):
        if not math.isfinite(value):
            raise ValueError("nonfinite flow or forcing")
        return value

    def energy(self, flow_m3_s):
        return 0.5 * self.inertance * self._finite(flow_m3_s)**2

    def pressure_loss(self, flow_m3_s):
        q = self._finite(flow_m3_s)
        return self.linear_resistance * q + self.quadratic_resistance * q * abs(q)

    def pressure_loss_derivative(self, flow_m3_s):
        return self.linear_resistance + 2 * self.quadratic_resistance * abs(self._finite(flow_m3_s))

    def dissipation_power(self, flow_m3_s):
        return self.pressure_loss(flow_m3_s) * flow_m3_s

    def backward_euler_step(self, flow_old_m3_s, pressure_difference_Pa, dt_s):
        self._finite(dt_s)
        if dt_s <= 0:
            raise ValueError("dt must be positive")
        a = self.inertance / dt_s + self.linear_resistance
        b = self.inertance / dt_s * self._finite(flow_old_m3_s) + self._finite(pressure_difference_Pa)
        return 2 * b / (a + math.sqrt(a*a + 4*self.quadratic_resistance*abs(b)))
