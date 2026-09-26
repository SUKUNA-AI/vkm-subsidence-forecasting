"""Physical-process DEFINITIONS (metadata). Solver outputs never live here."""
from __future__ import annotations

from enum import Enum

from pydantic import Field

from ..core.base import WorldObject


class ExecutionStatus(str, Enum):
    """Planned treatment of a process in future computations (design/planning statuses in Phase 1)."""

    COMPUTED_3D = "COMPUTED_3D"
    COMPUTED_2D_SECTION = "COMPUTED_2D_SECTION"
    COMPUTED_LOCAL = "COMPUTED_LOCAL"
    ANALYTICAL = "ANALYTICAL"
    SEMI_ANALYTICAL = "SEMI_ANALYTICAL"
    EMPIRICAL = "EMPIRICAL"
    COUPLED_PROCESS = "COUPLED_PROCESS"
    INITIAL_CONDITION = "INITIAL_CONDITION"
    BOUNDARY_CONDITION = "BOUNDARY_CONDITION"
    OBSERVATION_OPERATOR = "OBSERVATION_OPERATOR"
    CONSTRAINT_CHECK = "CONSTRAINT_CHECK"
    CONTEXT_ONLY = "CONTEXT_ONLY"
    RESEARCH_REQUIRED = "RESEARCH_REQUIRED"
    UNKNOWN = "UNKNOWN"


class Readiness(str, Enum):
    READY_FOR_LOCAL = "READY_FOR_LOCAL"             # equations + parameters + geometry evidence exist
    PARAMETERS_MISSING = "PARAMETERS_MISSING"
    GEOMETRY_MISSING = "GEOMETRY_MISSING"
    EVIDENCE_WEAK = "EVIDENCE_WEAK"
    NOT_APPLICABLE_NORMAL_SCENARIO = "NOT_APPLICABLE_NORMAL_SCENARIO"
    UNKNOWN = "UNKNOWN"


class ProcessDefinition(WorldObject):
    domain: str = Field(..., description="mechanics|rheology|damage|interfaces|backfill|hydro|thermal|dynamics|EM|surface")
    causal_role: str
    governing_variables: tuple[str, ...] = ()
    math_models: tuple[str, ...] = Field((), description="MATHEMATICAL_MODEL_REGISTRY ids")
    parameters_needed: tuple[str, ...] = ()
    evidence: tuple[str, ...] = Field((), description="evidence ids / source ids")
    spatial_scale: str | None = None
    temporal_scale: str | None = None
    site_relevance: str | None = None
    identifiability: str | None = None
    planned_treatment: ExecutionStatus = ExecutionStatus.UNKNOWN
    readiness: Readiness = Readiness.UNKNOWN
    candidate_tools: tuple[str, ...] = Field((), description="OGS, MATLAB, FLAC, PLAXIS, gprMax, GemPy, Micromine ...")
