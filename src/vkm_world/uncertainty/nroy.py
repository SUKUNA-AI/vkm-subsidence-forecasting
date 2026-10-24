"""NROY (not-ruled-out-yet) screening of worlds by real observations — DESIGN ONLY (FW §24–26, CT §30, §33–34, §43–44).

Nothing here screens real worlds or calibrates anything. The module fixes the contract of a future, explicit
comparison experiment and checks a design for the known traps:

* future leakage: every observation used for screening has ``available_from <= t0`` under the chosen availability
  mode (publication date, or the owner-approved SIMULATED_ENTERPRISE_VIEW: epoch date), and target epochs after t0
  are never screened on;
* calibration: screening rules out worlds, it never tunes parameters (``calibration_datasets`` must stay empty);
* inverse crime (FW §25, CT §33): held-out physical families, a different solver and a different resolution are
  declared for the later method test; synthetic data never enter a screening as observations;
* the tolerance is a decision rule (EPISTEMIC_RULE) built from components of DIFFERENT classes; the components are
  kept and reported separately, their combination inside the test is an explicit, owner-approved choice and is
  never reported as «the uncertainty of the forecast».

``implausibility`` is the standard history-matching statistic (Craig et al. 1997; Vernon et al. 2010) written out
for the record; tests use synthetic numbers only.
"""
from __future__ import annotations

import math
from datetime import date
from enum import Enum
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .taxonomy import UncertaintyClass


class AvailabilityMode(str, Enum):
    PUBLICATION_DATE = "PUBLICATION_DATE"                    # strict: available_from = publication
    SIMULATED_ENTERPRISE_VIEW = "SIMULATED_ENTERPRISE_VIEW"  # owner decision: observation available from its epoch


class ToleranceComponentKind(str, Enum):
    OBSERVATION_NOISE = "OBSERVATION_NOISE"
    DIGITIZATION = "DIGITIZATION"
    STATION_POSITION = "STATION_POSITION"
    EPOCH_DATING = "EPOCH_DATING"
    REPRESENTATION_ERROR = "REPRESENTATION_ERROR"
    EMULATOR = "EMULATOR"
    MODEL_DISCREPANCY = "MODEL_DISCREPANCY"


#: class each tolerance component belongs to (they are never merged into one class)
COMPONENT_CLASS = {
    ToleranceComponentKind.OBSERVATION_NOISE: UncertaintyClass.OBSERVATION,
    ToleranceComponentKind.DIGITIZATION: UncertaintyClass.OBSERVATION,
    ToleranceComponentKind.STATION_POSITION: UncertaintyClass.OBSERVATION,
    ToleranceComponentKind.EPOCH_DATING: UncertaintyClass.OBSERVATION,
    ToleranceComponentKind.REPRESENTATION_ERROR: UncertaintyClass.REPRESENTATION_CHOICE,
    ToleranceComponentKind.EMULATOR: UncertaintyClass.REPRESENTATION_CHOICE,
    # how much structural inadequacy of the world families is tolerated: a rule, not a measured property
    ToleranceComponentKind.MODEL_DISCREPANCY: UncertaintyClass.EPISTEMIC_RULE,
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ScreeningData(_Strict):
    dataset_id: str
    dataset_type: Literal["REAL", "SYNTHETIC", "SEMI_SYNTHETIC"]
    role: Literal["SCREENING", "HELD_OUT_TARGET"]
    observation_world_id: str
    epochs: list[date] = Field(min_length=1)
    publication_date: date
    station_ids: list[str] = []
    source_ids: list[str] = []


class ToleranceComponent(_Strict):
    kind: ToleranceComponentKind
    uncertainty_class: UncertaintyClass
    sigma: float | None = Field(None, ge=0)
    bound: float | None = Field(None, ge=0)
    unit: str
    relative: bool = Field(False, description="sigma / bound in % of the predicted value (e.g. a representation error)")
    metric_ids: list[str] = Field([], description="metrics the component applies to; empty = every metric")
    status: str = Field(description="EVIDENCE / DERIVATION / MODEL_CHOICE ...")
    element_ids: list[str] = []

    def scale(self, prediction: float | None, which: str) -> float | None:
        v = self.sigma if which == "sigma" else self.bound
        if v is None:
            return None
        if self.relative:
            if prediction is None:
                raise ValueError(f"{self.kind.value}: relative tolerance needs the predicted value")
            return v / 100.0 * abs(prediction)
        return v


class Metric(_Strict):
    metric_id: str
    definition_ru: str
    unit: str
    epochs_used: Literal["SCREENING_ONLY"] = "SCREENING_ONLY"


class Rule(_Strict):
    combination: Literal["VARIANCE_SUM_IMPLAUSIBILITY", "BOUND_SUM", "PER_COMPONENT_ALL"]
    threshold: float = Field(gt=0)
    threshold_status: Literal["MODEL_CHOICE"] = "MODEL_CHOICE"
    owner_approved: bool = False
    aggregate_over_metrics: Literal["MAX", "SECOND_MAX", "PER_METRIC"] = "MAX"


class InverseCrimeGuard(_Strict):
    generator_families: list[str] = Field(min_length=1)
    generator_solver: str
    generator_resolution: str
    held_out_families: list[str] = []
    test_solver: str | None = None
    test_resolution: str | None = None
    calibration_datasets: list[str] = []


class NroyDesign(_Strict):
    design_id: str
    status: Literal["DESIGN_ONLY"] = "DESIGN_ONLY"
    t0: date
    availability_mode: AvailabilityMode
    availability_mode_owner_approved: bool = False
    data: list[ScreeningData] = Field(min_length=1)
    metrics: list[Metric] = Field(min_length=1)
    tolerance: list[ToleranceComponent] = Field(min_length=1)
    rule: Rule
    guard: InverseCrimeGuard


def _available(d: ScreeningData, epoch: date, mode: AvailabilityMode) -> date:
    return epoch if mode is AvailabilityMode.SIMULATED_ENTERPRISE_VIEW else max(epoch, d.publication_date)


def check_design(d: NroyDesign) -> list[str]:
    """Problems of a design (empty = consistent). Owner decisions still open are reported with 'OWNER:'."""
    p: list[str] = []
    for x in d.data:
        if x.role == "SCREENING":
            if x.dataset_type != "REAL":
                p.append(f"{x.dataset_id}: {x.dataset_type} data cannot screen worlds (synthetic is never an observation)")
            for ep in x.epochs:
                av = _available(x, ep, d.availability_mode)
                if av > d.t0:
                    p.append(f"{x.dataset_id}: epoch {ep} available from {av} > t0 {d.t0} (future leakage)")
        else:
            if any(ep <= d.t0 for ep in x.epochs):
                p.append(f"{x.dataset_id}: held-out target has epochs <= t0")
    # the same observation world at the same epoch on both sides is a leak, whatever the dataset ids
    scr = {(x.observation_world_id, ep) for x in d.data if x.role == "SCREENING" for ep in x.epochs}
    tgt = {(x.observation_world_id, ep) for x in d.data if x.role == "HELD_OUT_TARGET" for ep in x.epochs}
    if scr & tgt:
        p.append(f"screening and target share epochs: {sorted(scr & tgt)}")
    if not scr:
        p.append("no screening data")
    if not tgt:
        p.append("no held-out target data: the post-t0 method test is impossible")
    metric_ids = {m.metric_id for m in d.metrics}
    for c in d.tolerance:
        if COMPONENT_CLASS[c.kind] is not c.uncertainty_class:
            p.append(f"tolerance {c.kind.value}: class {c.uncertainty_class.value} != {COMPONENT_CLASS[c.kind].value}")
        if c.sigma is None and c.bound is None:
            p.append(f"tolerance {c.kind.value}: needs sigma or bound")
        if d.rule.combination == "VARIANCE_SUM_IMPLAUSIBILITY" and c.sigma is None:
            p.append(f"tolerance {c.kind.value}: VARIANCE_SUM needs sigma (a bound is not silently converted)")
        unknown = sorted(set(c.metric_ids) - metric_ids)
        if unknown:
            p.append(f"tolerance {c.kind.value}: unknown metric ids {unknown}")
    for m in d.metrics:
        comps = components_for(d, m.metric_id)
        if not comps:
            p.append(f"metric {m.metric_id}: no tolerance component applies")
        for c in comps:
            if not _unit_ok(c, m.unit):
                p.append(f"metric {m.metric_id} [{m.unit}]: tolerance {c.kind.value} in {c.unit} (units differ)")
    kinds = {c.kind for c in d.tolerance}
    for need in (ToleranceComponentKind.REPRESENTATION_ERROR, ToleranceComponentKind.MODEL_DISCREPANCY):
        if need not in kinds:
            p.append(f"tolerance lacks {need.value}")
    if not kinds & {ToleranceComponentKind.OBSERVATION_NOISE, ToleranceComponentKind.DIGITIZATION}:
        p.append("tolerance lacks an observation-error component")
    if d.guard.calibration_datasets:
        p.append(f"calibration datasets declared {d.guard.calibration_datasets}: NROY rules worlds out, it does not calibrate")
    if not d.guard.held_out_families:
        p.append("inverse crime: no held-out physical family declared for the method test")
    shared = sorted(set(d.guard.held_out_families) & set(d.guard.generator_families))
    if shared:
        p.append(f"inverse crime: held-out families {shared} are also generator families (review Codex, PR #32)")
    if not d.guard.test_solver or d.guard.test_solver == d.guard.generator_solver:
        p.append("inverse crime: the method test needs a solver different from the generator")
    if not d.guard.test_resolution or d.guard.test_resolution == d.guard.generator_resolution:
        p.append("inverse crime: the method test needs a resolution different from the generator")
    if not d.rule.owner_approved:
        p.append("OWNER: threshold and combination rule are MODEL_CHOICE and not yet approved")
    if d.availability_mode is AvailabilityMode.SIMULATED_ENTERPRISE_VIEW and not d.availability_mode_owner_approved:
        p.append("OWNER: SIMULATED_ENTERPRISE_VIEW availability mode needs the owner's decision")
    return p


def _unit_ok(c: ToleranceComponent, unit: str) -> bool:
    return (c.relative and c.unit == "%") or (not c.relative and c.unit == unit)


def components_for(d: NroyDesign, metric_id: str) -> list[ToleranceComponent]:
    """Tolerance components that apply to one metric (empty metric_ids = every metric)."""
    return [c for c in d.tolerance if not c.metric_ids or metric_id in c.metric_ids]


def implausibility(residual: float, components: Sequence[ToleranceComponent],
                   combination: str = "VARIANCE_SUM_IMPLAUSIBILITY", prediction: float | None = None,
                   unit: str | None = None) -> float:
    """|z - f(w)| scaled by the tolerance. VARIANCE_SUM: / sqrt(sum sigma^2); BOUND_SUM: / sum bounds;
    PER_COMPONENT_ALL: min over components of |r| / scale_i (rule out only if every single component is exceeded).
    Relative components (%, e.g. representation error) are scaled by the predicted value. With `unit`, every absolute
    component must be in that unit. VARIANCE_SUM refuses bound-only components (no silent conversion)."""
    if unit is not None:
        bad = [c.kind.value for c in components if not _unit_ok(c, unit)]
        if bad:
            raise ValueError(f"tolerance units differ from the residual unit {unit}: {bad}")
    r = abs(residual)
    sig = [c.scale(prediction, "sigma") for c in components]
    bnd = [c.scale(prediction, "bound") for c in components]
    if combination == "VARIANCE_SUM_IMPLAUSIBILITY":
        missing = [c.kind.value for c, s in zip(components, sig) if s is None]
        if missing:
            raise ValueError(f"VARIANCE_SUM needs sigma for every component; bound-only: {missing}")
        v = sum(s ** 2 for s in sig)
        return math.inf if v == 0 else r / math.sqrt(v)
    if combination == "BOUND_SUM":
        b = sum(bb if bb is not None else 2.0 * (s or 0.0) for s, bb in zip(sig, bnd))
        return math.inf if b == 0 else r / b
    if combination == "PER_COMPONENT_ALL":
        scales = [bb if bb is not None else s for s, bb in zip(sig, bnd)]
        scales = [s for s in scales if s]
        return math.inf if not scales else min(r / s for s in scales)
    raise ValueError(combination)


def json_schema() -> str:
    import json
    schema = NroyDesign.model_json_schema()
    schema["$id"] = "https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/schemas/nroy_design.schema.json"
    schema["title"] = "VKM/SKRU-1 NROY screening design (design only)"
    return json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=1) + "\n"


def verdict(implausibilities: Sequence[float], rule: Rule) -> Literal["NOT_RULED_OUT_YET", "RULED_OUT"]:
    """Aggregate per-metric implausibilities with the (owner-approved) rule."""
    xs = sorted(implausibilities, reverse=True)
    if rule.aggregate_over_metrics == "SECOND_MAX" and len(xs) >= 2:
        stat = xs[1]
    else:
        stat = xs[0]
    return "RULED_OUT" if stat > rule.threshold else "NOT_RULED_OUT_YET"
