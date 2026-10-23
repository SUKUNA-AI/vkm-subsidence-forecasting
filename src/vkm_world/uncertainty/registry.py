"""Machine-readable registry of uncertainty elements (CT §1 «WorldUncertainty != RepresentationError», §29, §48).

One element = one source of uncertainty with: its class (taxonomy.UncertaintyClass), where along the chain it arises,
its evidence (source + locator) or an explicit assumption status, how it is represented (range, discrete set of
hypotheses, distribution, ensemble, point assumption, UNKNOWN — subjective representations marked MODEL_CHOICE),
what the project has already measured about its effect on the quantities of interest, what remains unknown, and
what would narrow it.

Validation rules (``UncertaintyRegistry.problems`` returns them as text; ``validate_registry`` raises):
* id prefix matches the class (UE-W- world, UE-R- representation, UE-F- forecast method, UE-E- epistemic rule,
  UE-O- observation) and the kind belongs to the class;
* UNKNOWN representation carries no number; a distribution or weights are MODEL_CHOICE unless an objective basis
  is written; an element with no source must say why (MODEL_CHOICE rationale or UNKNOWN);
* an element completed from UNKNOWN by a scenario is marked MODEL_CHOICE / MIXED;
* MEASURED / PARTIALLY_MEASURED need at least one measurement with an effect; NOT_MEASURED / PENDING have none;
* effects name a declared quantity of interest in its unit;
* a measurement of world sensitivity names the representation it was computed in (a world effect is measured
  inside one representation; its accuracy is a separate element).
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..core.provenance import EpistemicStatus, EvidenceType, Scale, Scope
from .taxonomy import (CLASS_PREFIX, KINDS, ChainStage, MeasurementStatus, NarrowingRoute, RepresentationKind,
                       Subjectivity, UncertaintyClass)

SCHEMA_ID = "vkm.uncertainty_registry/1"
ID_RE = re.compile(r"^UE-([WRFEO])-[A-Z0-9]+-\d{2}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SourceRef(_Strict):
    source_id: str = Field(description="VKM-SRC-xxx, project artifact id, catalogue id or session deliverable")
    locator: str = Field("", description="page / table / figure / file path (logical, no machine paths)")
    scope: Scope | None = None
    note: str = ""


class ValueRange(_Strict):
    low: float | None = None
    high: float | None = None
    central: float | None = None
    unit: str

    @model_validator(mode="after")
    def _order(self):
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError(f"range low {self.low} > high {self.high}")
        if self.central is not None:
            if self.low is not None and self.central < self.low or self.high is not None and self.central > self.high:
                raise ValueError("central level outside [low, high]")
        return self


class Option(_Strict):
    option_id: str
    label_ru: str
    value: float | str | None = None
    unit: str | None = None
    status: EpistemicStatus
    source_ids: list[str] = []


class Distribution(_Strict):
    family: str
    parameters: dict[str, float]
    subjectivity: Subjectivity
    basis_ru: str = ""


class EnsembleRef(_Strict):
    n_realizations: int = Field(ge=2)
    reference: str = Field(description="logical location of the realizations")
    generator: str
    nature: Literal["SYNTHETIC"] = "SYNTHETIC"


class Representation(_Strict):
    kind: RepresentationKind
    subjectivity: Subjectivity
    range: ValueRange | None = None
    options: list[Option] = []
    distribution: Distribution | None = None
    ensemble: EnsembleRef | None = None
    weights: dict[str, float] | None = None
    rationale_ru: str = ""

    @model_validator(mode="after")
    def _coherent(self):
        k, s = self.kind, self.subjectivity
        if k is RepresentationKind.UNKNOWN:
            if self.range or self.options or self.distribution or self.ensemble or self.weights:
                raise ValueError("UNKNOWN representation must carry no number, option, distribution or weight")
            if s is not Subjectivity.NOT_REPRESENTED:
                raise ValueError("UNKNOWN representation has subjectivity NOT_REPRESENTED")
            return self
        if s is Subjectivity.NOT_REPRESENTED:
            raise ValueError("only UNKNOWN is NOT_REPRESENTED")
        if k is RepresentationKind.RANGE and not (self.range and self.range.low is not None and self.range.high is not None):
            raise ValueError("RANGE needs range.low and range.high")
        if k is RepresentationKind.BOUND and not (self.range and (self.range.low is not None) != (self.range.high is not None)):
            raise ValueError("BOUND needs exactly one of range.low / range.high")
        if k is RepresentationKind.POINT_ASSUMPTION and not (self.range and self.range.central is not None):
            raise ValueError("POINT_ASSUMPTION needs range.central")
        if k is RepresentationKind.DISCRETE_SET and len(self.options) < 2:
            raise ValueError("DISCRETE_SET needs at least two options (competing hypotheses are not collapsed)")
        if k is RepresentationKind.DISTRIBUTION and self.distribution is None:
            raise ValueError("DISTRIBUTION needs distribution")
        if k is RepresentationKind.ENSEMBLE and self.ensemble is None:
            raise ValueError("ENSEMBLE needs ensemble")
        if self.distribution is not None and self.distribution.subjectivity is Subjectivity.EVIDENCE \
                and not self.distribution.basis_ru.strip():
            raise ValueError("an evidence-based distribution needs basis_ru; otherwise mark it MODEL_CHOICE")
        if self.weights is not None:
            if s is Subjectivity.EVIDENCE:
                raise ValueError("weights over hypotheses are MODEL_CHOICE (CT §29) unless an objective basis is given (MIXED)")
            if not self.rationale_ru.strip():
                raise ValueError("weights need rationale_ru (prior source and rationale, CT §29)")
        if s in (Subjectivity.MODEL_CHOICE, Subjectivity.MIXED) and not self.rationale_ru.strip():
            raise ValueError("MODEL_CHOICE / MIXED representation needs rationale_ru")
        return self


class QoIEffect(_Strict):
    """Effect of the element on one quantity of interest, measured inside one representation."""
    qoi: str
    unit: str
    central: float | None = None
    low: float | None = None
    high: float | None = None
    time_yr: float | None = None
    note: str = ""

    @model_validator(mode="after")
    def _has_number(self):
        if self.low is None and self.high is None:
            raise ValueError("an effect needs low and/or high")
        return self


class Measurement(_Strict):
    experiment_id: str
    by_ru: str
    where: str = Field(description="logical location: PRIVATE:12_work_exports/..., PUBLIC:..., session name")
    result_status: Literal["MODEL_RESULT", "DERIVATION", "DIAGNOSTIC_COMPARISON"]
    representation_context: str = Field(description="representation the effect was computed in")
    effects: list[QoIEffect] = []


class Narrowing(_Strict):
    action_ru: str
    route: NarrowingRoute
    expected_ru: str = ""
    priority: int = Field(2, ge=1, le=3)


class Links(_Strict):
    passport_prior_ids: list[str] = []
    conflict_ids: list[str] = []
    transfer_ids: list[str] = []
    unknown_ids: list[str] = []
    related_elements: list[str] = []
    sessions: list[str] = []


class UncertaintyElement(_Strict):
    element_id: str
    title_ru: str
    uncertainty_class: UncertaintyClass
    kind: str
    arises_at: ChainStage
    affects: list[ChainStage]
    description_ru: str
    quantity: str | None = None
    unit: str | None = None
    site_scope: Scope
    scale: Scale
    evidence_status: list[EpistemicStatus] = Field(min_length=1)
    evidence_types: list[EvidenceType] = []
    sources: list[SourceRef] = []
    representation: Representation
    measured: MeasurementStatus
    measurements: list[Measurement] = []
    unknown_ru: str = Field(min_length=1, description="what remains unknown")
    narrowing: list[Narrowing] = []
    links: Links = Links()

    @model_validator(mode="after")
    def _rules(self):
        m = ID_RE.match(self.element_id)
        if not m:
            raise ValueError(f"bad element_id {self.element_id!r} (UE-<W|R|F|E|O>-<TAG>-<NN>)")
        if m.group(1) != CLASS_PREFIX[self.uncertainty_class]:
            raise ValueError(f"{self.element_id}: prefix {m.group(1)} does not match class {self.uncertainty_class.value}")
        if self.kind not in KINDS[self.uncertainty_class]:
            raise ValueError(f"{self.element_id}: kind {self.kind} not allowed for {self.uncertainty_class.value}")
        r = self.representation
        if not self.sources and r.kind is not RepresentationKind.UNKNOWN \
                and r.subjectivity not in (Subjectivity.MODEL_CHOICE, Subjectivity.MIXED):
            raise ValueError(f"{self.element_id}: no source — mark the representation MODEL_CHOICE with a rationale or UNKNOWN")
        if EpistemicStatus.UNKNOWN in self.evidence_status and r.kind is not RepresentationKind.UNKNOWN \
                and r.subjectivity is Subjectivity.EVIDENCE:
            raise ValueError(f"{self.element_id}: UNKNOWN completed by a scenario must be MODEL_CHOICE / MIXED, not EVIDENCE")
        has_effect = any(ms.effects for ms in self.measurements)
        if self.measured in (MeasurementStatus.MEASURED, MeasurementStatus.PARTIALLY_MEASURED) and not has_effect:
            raise ValueError(f"{self.element_id}: {self.measured.value} needs a measurement with an effect")
        if self.measured in (MeasurementStatus.NOT_MEASURED, MeasurementStatus.PENDING_PARALLEL_WORK,
                             MeasurementStatus.NOT_APPLICABLE) and has_effect:
            raise ValueError(f"{self.element_id}: {self.measured.value} cannot carry effects")
        if self.measured is MeasurementStatus.PENDING_PARALLEL_WORK and not self.links.sessions:
            raise ValueError(f"{self.element_id}: PENDING_PARALLEL_WORK names the session in links.sessions")
        return self


class QoIDefinition(_Strict):
    qoi_id: str
    name_ru: str
    unit: str
    definition_ru: str


class UncertaintyRegistry(_Strict):
    schema_id: Literal["vkm.uncertainty_registry/1"] = SCHEMA_ID
    registry_id: str
    created: date
    target_ru: str
    central_world_ru: str = ""
    qois: list[QoIDefinition]
    elements: list[UncertaintyElement]
    notes_ru: list[str] = []

    def element(self, element_id: str) -> UncertaintyElement:
        for e in self.elements:
            if e.element_id == element_id:
                return e
        raise KeyError(element_id)

    def problems(self) -> list[str]:
        out: list[str] = []
        ids = [e.element_id for e in self.elements]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            out.append(f"duplicate element ids: {dup}")
        qoi_unit = {q.qoi_id: q.unit for q in self.qois}
        known = set(ids)
        for e in self.elements:
            for ms in e.measurements:
                for ef in ms.effects:
                    if ef.qoi not in qoi_unit:
                        out.append(f"{e.element_id}: effect on undeclared qoi {ef.qoi}")
                    elif ef.unit != qoi_unit[ef.qoi]:
                        out.append(f"{e.element_id}: effect unit {ef.unit} != qoi unit {qoi_unit[ef.qoi]} ({ef.qoi})")
            for rel in e.links.related_elements:
                if rel not in known:
                    out.append(f"{e.element_id}: related element {rel} not in the registry")
        return out

    def summary(self) -> dict:
        by_class: dict[str, dict[str, int]] = {}
        for e in self.elements:
            d = by_class.setdefault(e.uncertainty_class.value, {})
            d[e.measured.value] = d.get(e.measured.value, 0) + 1
            d["total"] = d.get("total", 0) + 1
        return {"n_elements": len(self.elements), "by_class": by_class}


class RegistryError(ValueError):
    pass


def validate_registry(data: dict | str | Path) -> UncertaintyRegistry:
    """Parse and validate; raise RegistryError listing every problem."""
    if isinstance(data, Path):
        data = data.read_text(encoding="utf-8")
    try:
        reg = UncertaintyRegistry.model_validate_json(data) if isinstance(data, str) else UncertaintyRegistry.model_validate(data)
    except ValidationError as exc:
        raise RegistryError(str(exc)) from exc
    probs = reg.problems()
    if probs:
        raise RegistryError("; ".join(probs))
    return reg


def json_schema() -> str:
    schema = UncertaintyRegistry.model_json_schema()
    schema["$id"] = "https://github.com/SUKUNA-AI/vkm-subsidence-forecasting/schemas/uncertainty_registry.schema.json"
    schema["title"] = "VKM/SKRU-1 uncertainty registry (theory v0.2 classes)"
    return json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=1) + "\n"


CSV_COLUMNS = ["element_id", "uncertainty_class", "kind", "title_ru", "arises_at", "affects", "quantity", "unit",
               "site_scope", "scale", "evidence_status", "representation_kind", "subjectivity", "range_low",
               "range_central", "range_high", "range_unit", "options", "measured", "n_measurements", "effects",
               "source_ids", "locators", "unknown_ru", "narrowing_top", "links"]


def _fmt(x):
    return "" if x is None else (f"{x:.6g}" if isinstance(x, float) else str(x))


def to_csv_rows(reg: UncertaintyRegistry) -> list[dict[str, str]]:
    """Flat view of the registry (the JSON is canonical; the CSV is a projection)."""
    rows = []
    for e in reg.elements:
        r = e.representation
        eff = []
        for ms in e.measurements:
            for ef in ms.effects:
                t = f"@{ef.time_yr:g}yr" if ef.time_yr is not None else ""
                eff.append(f"{ef.qoi}{t}: {_fmt(ef.low)}..{_fmt(ef.high)} {ef.unit} (central {_fmt(ef.central)})")
        top = sorted(e.narrowing, key=lambda n: n.priority)
        rows.append({
            "element_id": e.element_id, "uncertainty_class": e.uncertainty_class.value, "kind": e.kind,
            "title_ru": e.title_ru, "arises_at": e.arises_at.value, "affects": ";".join(a.value for a in e.affects),
            "quantity": e.quantity or "", "unit": e.unit or "", "site_scope": e.site_scope.value, "scale": e.scale.value,
            "evidence_status": ";".join(s.value for s in e.evidence_status), "representation_kind": r.kind.value,
            "subjectivity": r.subjectivity.value,
            "range_low": _fmt(r.range.low) if r.range else "", "range_central": _fmt(r.range.central) if r.range else "",
            "range_high": _fmt(r.range.high) if r.range else "", "range_unit": r.range.unit if r.range else "",
            "options": " | ".join(f"{o.option_id}={_fmt(o.value)}{(' ' + o.unit) if o.unit else ''} [{o.status.value}]" for o in r.options),
            "measured": e.measured.value, "n_measurements": str(len(e.measurements)), "effects": " | ".join(eff),
            "source_ids": ";".join(s.source_id for s in e.sources),
            "locators": " | ".join(f"{s.source_id}: {s.locator}" for s in e.sources if s.locator),
            "unknown_ru": e.unknown_ru, "narrowing_top": top[0].action_ru if top else "",
            "links": json.dumps(e.links.model_dump(exclude_defaults=True), ensure_ascii=False),
        })
    return rows


def to_csv(reg: UncertaintyRegistry) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    w.writeheader()
    for row in to_csv_rows(reg):
        w.writerow(row)
    return buf.getvalue()
