"""Metadata of mathematical models (catalogue schema of MATHEMATICAL_MODEL_REGISTRY.csv).

Phase 1 catalogues exact source forms; it does not solve anything.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MathClass(str, Enum):
    ALGEBRAIC = "ALGEBRAIC"
    SYSTEM_ALGEBRAIC = "SYSTEM_ALGEBRAIC"
    ODE = "ODE"
    DAE = "DAE"
    PDE_ELLIPTIC = "PDE_ELLIPTIC"
    PDE_PARABOLIC = "PDE_PARABOLIC"
    PDE_HYPERBOLIC = "PDE_HYPERBOLIC"
    PDE_SYSTEM = "PDE_SYSTEM"
    INTEGRAL = "INTEGRAL"
    INTEGRO_DIFFERENTIAL = "INTEGRO_DIFFERENTIAL"
    HEREDITARY = "HEREDITARY"
    FRACTIONAL = "FRACTIONAL"
    CONVOLUTION = "CONVOLUTION"
    GREEN_FUNCTION = "GREEN_FUNCTION"
    VARIATIONAL = "VARIATIONAL"
    STOCHASTIC = "STOCHASTIC"
    STATISTICAL = "STATISTICAL"
    INVERSE = "INVERSE"
    GEOMETRIC = "GEOMETRIC"
    EMPIRICAL_TABLE = "EMPIRICAL_TABLE"
    OTHER = "OTHER"


class Origin(str, Enum):
    ORIGINAL_SOURCE = "ORIGINAL_SOURCE"        # the source's own model
    CITED_IN_SOURCE = "CITED_IN_SOURCE"        # the source reproduces someone else's model
    EXTERNAL_PRIMARY = "EXTERNAL_PRIMARY"      # verified in the original publication
    TEXTBOOK_STANDARD = "TEXTBOOK_STANDARD"
    DERIVED_THIS_PROJECT = "DERIVED_THIS_PROJECT"


class MathModelRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    model_id: str
    name_ru: str
    physical_meaning_ru: str | None = None
    math_class: MathClass
    equation_plain: str
    equation_latex: str | None = None
    variables: str = Field(..., description="symbol:meaning[unit]; …")
    assumptions: str | None = None
    scale: str | None = None               # LAB / MASSIF / FIELD / DESIGN / GENERAL
    site_applicability: str | None = None
    source_ids: str
    locator: str
    origin: Origin
    known_variants: str | None = None
    conflicting_forms: str | None = None
    intended_role: str | None = None       # WorldSpec role / future use
    future_execution: str | None = None    # planned ExecutionStatus
    status: str | None = None              # FACT/…/UNKNOWN of its applicability to SKRU-1

    @field_validator("locator", "source_ids")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("formula without source/locator is not allowed")
        return v
