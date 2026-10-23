"""Taxonomy of uncertainty along the chain of theory v0.2 (FW §2–5, §12–14; AG §43–44; MS §30; CT §1, §17, §29–30).

Five classes are kept apart and never summed into one error (FW §5, AG §44, MS §30):

* PHYSICAL_HYPOTHESIS — world uncertainty: which geometry / laws / parameters / history / initial state is real
  (it distinguishes PhysicalWorlds, FW §5.1);
* REPRESENTATION_CHOICE — representation error: how accurately a chosen world is computed (solver, mesh, time
  step, 2D reduction, equivalent layer, microkernels, surrogate; FW §5.2, §19);
* FORECAST_METHOD — error of a forecast method with respect to world trajectories (FW §5.3);
* EPISTEMIC_RULE — rules of admission, transfer, calibration, validation, weighting (FW §5.4, CT §27);
* OBSERVATION — ObservationWorld: noise, observation operator, station positions, datum, epochs, missingness,
  availability (FW §12–14, CT §22–24). It is outside the PhysicalWorld.
"""
from __future__ import annotations

from enum import Enum


class UncertaintyClass(str, Enum):
    PHYSICAL_HYPOTHESIS = "PHYSICAL_HYPOTHESIS"
    REPRESENTATION_CHOICE = "REPRESENTATION_CHOICE"
    FORECAST_METHOD = "FORECAST_METHOD"
    EPISTEMIC_RULE = "EPISTEMIC_RULE"
    OBSERVATION = "OBSERVATION"


#: element id prefix per class: UE-W-..., UE-R-..., UE-F-..., UE-E-..., UE-O-...
CLASS_PREFIX: dict[UncertaintyClass, str] = {
    UncertaintyClass.PHYSICAL_HYPOTHESIS: "W",
    UncertaintyClass.REPRESENTATION_CHOICE: "R",
    UncertaintyClass.FORECAST_METHOD: "F",
    UncertaintyClass.EPISTEMIC_RULE: "E",
    UncertaintyClass.OBSERVATION: "O",
}

CLASS_TITLE_RU: dict[UncertaintyClass, str] = {
    UncertaintyClass.PHYSICAL_HYPOTHESIS: "неопределённость мира (какой мир реален)",
    UncertaintyClass.REPRESENTATION_CHOICE: "ошибка представления (насколько точно посчитан выбранный мир)",
    UncertaintyClass.FORECAST_METHOD: "ошибка метода прогноза (насколько метод воспроизводит траектории миров)",
    UncertaintyClass.EPISTEMIC_RULE: "правила допуска, переноса и проверки (не законы природы)",
    UncertaintyClass.OBSERVATION: "мир наблюдений: шум, оператор, положение, отсчёт, даты, пропуски",
}


class ChainStage(str, Enum):
    """Where along the chain an element arises / which later stages it affects (FW §36, CT §63)."""

    REAL_OBJECT = "REAL_OBJECT"            # the real SKRU-1 (one; full state inaccessible)
    EVIDENCE = "EVIDENCE"                  # corpus, digitization, evidence records
    WORLDSPEC = "WORLDSPEC"                # FACT / RANGE / DISCRETE_SET / UNKNOWN / MODEL_CHOICE / ANALOGUE / TRANSFER
    PHYSICAL_WORLD = "PHYSICAL_WORLD"      # completion of WorldSpec into an executable hypothesis
    REPRESENTATION = "REPRESENTATION"      # Ansys / OGS / World-0 microkernels / surrogate
    SOLVER_RUN = "SOLVER_RUN"
    OBSERVATION_WORLD = "OBSERVATION_WORLD"
    FORECAST_METHOD = "FORECAST_METHOD"
    VALIDATION = "VALIDATION"              # coverage, NROY, benchmark


#: allowed kinds (subclasses) per class
KINDS: dict[UncertaintyClass, frozenset[str]] = {
    UncertaintyClass.PHYSICAL_HYPOTHESIS: frozenset({
        "GEOMETRY", "GEOLOGY", "SURFACE_RELIEF", "MATERIAL_PARAMETER", "CONSTITUTIVE_LAW", "INITIAL_STATE",
        "BOUNDARY_CONDITION", "MINING_HISTORY", "BACKFILL", "STRUCTURE", "COUPLING"}),
    UncertaintyClass.REPRESENTATION_CHOICE: frozenset({
        "SOLVER_IMPLEMENTATION", "SPATIAL_DISCRETIZATION", "TIME_DISCRETIZATION", "DIMENSION_REDUCTION",
        "CONSTITUTIVE_REPRESENTATION", "REDUCED_MODEL", "SURROGATE", "DOMAIN_TRUNCATION", "GEOMETRY_OPERATOR"}),
    UncertaintyClass.FORECAST_METHOD: frozenset({
        "METHOD_STRUCTURE", "METHOD_PARAMETERS", "TRAINING_DISTRIBUTION", "EXTRAPOLATION_HORIZON",
        "INPUT_INFORMATION"}),
    UncertaintyClass.EPISTEMIC_RULE: frozenset({
        "TRANSFER_LAB_MASSIF", "TRANSFER_ANALOGUE_SITE", "CALIBRATED_EFFECTIVE_REUSE", "NORMATIVE_AS_PARAMETER",
        "ADMISSION_TOLERANCE", "AVAILABILITY_RULE", "PRIOR_WEIGHTING", "VALIDATION_INDEPENDENCE",
        "SITE_ATTRIBUTION", "SELECTION_BIAS"}),
    UncertaintyClass.OBSERVATION: frozenset({
        "MEASUREMENT_NOISE", "DIGITIZATION", "STATION_POSITION", "REFERENCE_DATUM", "EPOCH_DATING",
        "MISSINGNESS", "OPERATOR", "SOURCE_DEPENDENCE"}),
}


class RepresentationKind(str, Enum):
    """How an element is represented in the ensemble / analysis."""

    RANGE = "RANGE"                        # [low, high] (optionally a central level)
    DISCRETE_SET = "DISCRETE_SET"          # competing hypotheses / scenarios (>= 2 options)
    DISTRIBUTION = "DISTRIBUTION"          # a probability law (MODEL_CHOICE unless it has an objective basis)
    ENSEMBLE = "ENSEMBLE"                  # a sample of realizations (e.g. conditional geology simulation)
    BOUND = "BOUND"                        # one-sided limit
    POINT_ASSUMPTION = "POINT_ASSUMPTION"  # one value fixed for now: the uncertainty exists but is NOT represented
    UNKNOWN = "UNKNOWN"                    # not represented at all (no number)


class Subjectivity(str, Enum):
    EVIDENCE = "EVIDENCE"                  # bounds / options read from evidence
    MODEL_CHOICE = "MODEL_CHOICE"          # subjective: scenarios, weights, distributions without objective basis
    MIXED = "MIXED"                        # evidence bounds, subjective level / selection inside them
    NOT_REPRESENTED = "NOT_REPRESENTED"    # for UNKNOWN


class MeasurementStatus(str, Enum):
    """Has the project already measured the effect of the element on a quantity of interest?"""

    MEASURED = "MEASURED"
    PARTIALLY_MEASURED = "PARTIALLY_MEASURED"
    NOT_MEASURED = "NOT_MEASURED"
    PENDING_PARALLEL_WORK = "PENDING_PARALLEL_WORK"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class NarrowingRoute(str, Enum):
    CORPUS = "CORPUS"                      # search / review / digitization in the existing corpus
    NEW_DATA = "NEW_DATA"                  # data not in the corpus (survey, enterprise, GIS, tests)
    COMPUTATION = "COMPUTATION"            # a run that measures or reduces a representation error
    OWNER_DECISION = "OWNER_DECISION"      # a choice only the owner can make (rules, thresholds, scope)
    PARALLEL_SESSION = "PARALLEL_SESSION"  # being worked on in another session


def class_of_prefix(prefix: str) -> UncertaintyClass:
    for c, p in CLASS_PREFIX.items():
        if p == prefix:
            return c
    raise KeyError(prefix)
