"""Evidence-backed material parameter records (no aggregation, no scale transfer by default)."""
from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from ..core.base import WorldObject
from ..core.provenance import Quantity, Scale, check_scale_use, check_site_use, Scope
from ..core.units import check_unit, is_hard_error


class TestMethod(str, Enum):
    UNIAXIAL_COMPRESSION = "uniaxial_compression"
    TENSILE_BRAZILIAN = "tensile_brazilian"
    DIRECT_TENSION = "direct_tension"
    TRIAXIAL = "triaxial"
    CREEP_CONSTANT_LOAD = "creep_constant_load"
    RELAXATION = "relaxation"
    SHEAR = "shear"
    ULTRASONIC = "ultrasonic"
    DYNAMIC = "dynamic"
    IN_SITU = "in_situ"
    BACK_ANALYSIS = "back_analysis"          # calibrated from field observations → CALIBRATED_EFFECTIVE_MODEL
    NORMATIVE = "normative"
    LITERATURE = "literature"
    UNKNOWN = "unknown"


class MaterialParameter(WorldObject):
    variable: str = Field(..., description="density, youngs_modulus, poisson_ratio, ucs, creep_param_<law>_<name>, ...")
    material: str = Field(..., description="lithology as printed (галит, сильвинит, карналлит, глина, ...)")
    unit_id: str | None = Field(None, description="stratigraphic unit/seam id if stated")
    quantity: Quantity
    test_method: TestMethod = TestMethod.UNKNOWN
    n_samples: int | None = None
    conditions: str | None = Field(None, description="stress level, strain rate, temperature, moisture, specimen size")
    law_id: str | None = Field(None, description="math registry id if the parameter belongs to a constitutive law")

    @model_validator(mode="after")
    def _units(self) -> "MaterialParameter":
        probs = check_unit(self.variable, self.quantity.unit)
        if is_hard_error(probs) and not self.variable.startswith("creep_param"):
            raise ValueError(f"{self.id}: " + "; ".join(probs))
        return self

    def use_errors(self, as_scale: Scale = Scale.MASSIF, for_scope: Scope = Scope.SKRU1) -> list[str]:
        return check_scale_use(self.quantity, as_scale) + check_site_use(self.quantity, for_scope)
