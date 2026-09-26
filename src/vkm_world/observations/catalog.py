"""Observation systems, datasets and observation-operator SPECIFICATIONS (design only).

latent world state → observation operator → predicted measurement → comparison with the actual
measurement. Modalities are kept separate: levelling ≠ InSAR ≠ GNSS ≠ GPR ≠ seismic ≠ logs.
"""
from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from ..core.base import WorldObject
from ..core.provenance import TemporalSupport


class Modality(str, Enum):
    LEVELLING = "levelling"                    # геометрическое нивелирование
    PROFILE_LINE = "profile_line"              # профильные линии наблюдательной станции
    HORIZONTAL_DEFORMATION = "horizontal_deformation"  # линейные измерения между реперами
    GNSS = "gnss"
    INSAR = "insar"
    HYDROSTATIC_LEVELLING = "hydrostatic_levelling"
    UNDERGROUND_STATION = "underground_station"  # подземные замерные станции (конвергенция, деформации целиков)
    CONVERGENCE = "convergence"
    GPR = "gpr"
    BOREHOLE_LOGGING = "borehole_logging"
    SEISMIC_SURVEY = "seismic_survey"
    SEISMIC_MONITORING = "seismic_monitoring"
    ULTRASONIC = "ultrasonic"
    LAB_TEST = "lab_test"
    OTHER = "other"


class ObservationSystem(WorldObject):
    modality: Modality
    observable: str = Field(..., description="what is actually measured, e.g. height difference, LOS phase, TWT")
    equipment: str | None = None
    datum: str | None = Field(None, description="reference benchmark / reference point / height system")
    crs_id: str | None = None
    precision: str | None = Field(None, description="as printed, with conditions (e.g. II class, mm per km)")
    systematic_errors: str | None = None
    spatial_support: str | None = Field(None, description="points/lines/pixels; spacing; footprint")
    temporal_support: str | None = Field(None, description="campaign frequency / revisit / continuous")
    site_scope: str | None = None
    latent_state_link: str | None = Field(None, description="which latent state variable it observes (u_z, u vector, geometry+EM, ...)")


class ObservationDataset(WorldObject):
    system_id: str
    spatial_nodes: tuple[str, ...] = ()
    time: TemporalSupport
    values_location: str | None = Field(None, description="where the actual values are (source figure/table, private file); None if not available")
    values_available_in_corpus: bool = False
    is_digitized_from_figure: bool = False

    @model_validator(mode="after")
    def _availability(self) -> "ObservationDataset":
        if self.is_digitized_from_figure and not self.values_location:
            raise ValueError(f"{self.id}: digitized dataset must point to the figure it was digitized from")
        return self


class OperatorStatus(str, Enum):
    DESIGN = "DESIGN"
    IMPLEMENTED_LOCAL = "IMPLEMENTED_LOCAL"
    VALIDATED = "VALIDATED"


class ObservationOperatorSpec(WorldObject):
    """Conceptual interface H: state → predicted measurement. No forward model in Phase 1."""

    modality: Modality
    state_inputs: tuple[str, ...] = Field(..., description="latent variables consumed (u_z, u, geometry, eps_r, sigma, Vp ...)")
    world_inputs: tuple[str, ...] = Field((), description="WorldSpec objects consumed (benchmarks, LOS geometry, profiles)")
    output: str
    math_models: tuple[str, ...] = ()
    noise_model: str | None = None
    support_transform: str | None = Field(None, description="point/area/footprint averaging, reference differencing")
    status: OperatorStatus = OperatorStatus.DESIGN
    candidate_tools: tuple[str, ...] = ()
