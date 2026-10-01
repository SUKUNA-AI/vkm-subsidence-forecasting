"""Offline, selected-use scientific contracts. READY is consistency with supplied review inputs.

This module neither reviews evidence nor evaluates a formula. ReviewIndex is an explicit authoritative
input supplied by the reviewer; hashes detect changes, not truth. Unsupported exact forms remain archival.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
import json
import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..core.base import WorldObject
from ..core.io import sha256_bytes
from ..core.provenance import (SITE_SPECIFIC_SKRU1_SCOPES, EpistemicStatus, Quantity, Scale, Scope,
                               TemporalSupport, Provenance, transfer_use_errors)
from ..core.units import Dimension, lookup
from ..materials.parameters import TestMethod
from ..worldspec.io import content_hash
from ..worldspec.model import WorldSpec, _nested_models

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
EXACT_FORMS = {
    "creep-power-pa-s/1": {
        "equation": "strain_rate = A * stress ** n",
        "variables": "strain_rate:creep_rate[1/s];A:coefficient[Pa^-n/s];stress:stress[Pa];n:exponent[1]",
        "roles": ("A", "n"),
    },
    "creep-normalized-pa-s/1": {
        "equation": "strain_rate = B * (stress / stress0) ** n",
        "variables": "strain_rate:creep_rate[1/s];B:coefficient[1/s];stress:stress[Pa];stress0:reference[Pa];n:exponent[1]",
        "roles": ("B", "n", "stress0"),
    },
}


class FormulaReview(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    record_sha256: Sha256
    evidence_id: str
    time: TemporalSupport


class FamilyReview(BaseModel):
    """One exact experiment/calibration family, not independent numbers from the same book."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    model_id: str
    law_id: str
    parameter_hashes: dict[str, Sha256]
    material: str
    test_method: TestMethod
    scope: Scope
    scale: Scale
    conditions: str
    applicability: dict[str, Quantity]
    time: TemporalSupport
    formula_specialization: str | None = None


class ReviewIndex(BaseModel):
    """Ephemeral reviewed identities and applicability; never inferred from record.status."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    registered_sources: set[str]
    registered_evidence: set[str]
    registered_laws: set[str]
    formula_reviews: dict[str, FormulaReview]
    families: dict[str, FamilyReview]


class ScientificUseBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    world_sha256: Sha256
    model_id: str
    model_sha256: Sha256
    review_sha256: Sha256
    adapter_id: str
    parameter_ids: dict[str, str]
    family_evidence_id: str
    as_scale: Scale
    for_scope: Scope
    origin: date
    conditions: dict[str, Quantity]


class AdmissionDiagnostic(BaseModel):
    """A stable logical reference and identity; values, source text and machine paths are excluded."""
    model_config = ConfigDict(extra="forbid")
    code: str
    severity: Literal["BLOCKED", "UNDETERMINED"]
    field_ref: str
    input_sha256: Sha256 | None = None


def record_hash(record) -> str:
    """Exact canonical record identity. Non-finite JSON and permissive NaN encoding are refused."""
    payload = record.model_dump(mode="json", warnings=False) if isinstance(record, BaseModel) else record
    # Set serialization is canonicalized explicitly (Pydantic's JSON set order is not stable).
    if isinstance(record, ReviewIndex):
        for key in ("registered_sources", "registered_evidence", "registered_laws"):
            payload[key] = sorted(payload[key])
    return sha256_bytes(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                                  allow_nan=False).encode("utf-8"))


def _fresh(record):
    if not isinstance(record, BaseModel):
        raise TypeError("expected typed scientific input")
    return type(record).model_validate(record.model_dump(mode="python", round_trip=True, warnings=False))


def _bounds(q: Quantity):
    if q.provenance.status is EpistemicStatus.UNKNOWN:
        return None
    if q.is_range:
        return (q.low, q.high) if q.low is not None and q.high is not None else None
    if q.uncertainty.values:
        return min(q.uncertainty.values), max(q.uncertainty.values)
    return (q.value, q.value) if q.value is not None else None


def _result(problems, hashes=None, statuses=None):
    problems = [item if isinstance(item, AdmissionDiagnostic) else AdmissionDiagnostic(
        code=item[0], severity=item[1], field_ref="input") for item in problems]
    status = "BLOCKED" if any(item.severity == "BLOCKED" for item in problems) else (
        "UNDETERMINED" if problems else "READY")
    body = {"schema_version": 1, "status": status, "readiness_scope": "CONTRACT_CONSISTENCY_ONLY",
            "reasons": sorted({item.code for item in problems}), "reason_counts": dict(sorted(Counter(
                item.code for item in problems).items())), "diagnostics": [item.model_dump(mode="json") for item in problems],
            "unit_policy": "KNOWN_ALIASES_TO_PA_S_K;EXPONENT_UNITY_FACTOR;SOURCE_VALUES_PRESERVED",
            "input_hashes": hashes or {},
            "effective_statuses": statuses or {}}
    return {**body, "receipt_sha256": record_hash(body)}


def admit_scientific_use(world: WorldSpec, binding: ScientificUseBinding | None,
                         review: ReviewIndex | None) -> dict:
    """Freshly check one use of existing records; never assign a source parameter a new value/status.

    Missing review/binding, unknown applicability/availability and unsupported exact forms fail closed.
    Reasons contain fixed codes, so callers can publish aggregates without exposing private values.
    """
    problems = []
    context = {"field_ref": "binding", "input_sha256": None}
    def flag(code, unknown=False):
        problems.append(AdmissionDiagnostic(code=code, severity="UNDETERMINED" if unknown else "BLOCKED", **context))
    if binding is None: flag("BINDING_MISSING", True)
    if review is None: flag("REVIEW_MISSING", True)
    if problems: return _result(problems)
    if not isinstance(world, WorldSpec): return _result([("INVALID_INPUT", "BLOCKED")])
    try:
        world, binding, review = _fresh(world), _fresh(binding), _fresh(review)
        hashes = {"world": content_hash(world), "binding": record_hash(binding), "review": record_hash(review),
                  "parameters": {}}
    except (ValidationError, ValueError, TypeError):
        return _result([("INVALID_INPUT", "BLOCKED")])
    if hashes["world"] != binding.world_sha256 or hashes["review"] != binding.review_sha256:
        flag("INPUT_CHANGED")
    context.update(field_ref="binding.target", input_sha256=hashes["binding"])
    if binding.for_scope is Scope.UNSTATED or binding.as_scale in (Scale.UNSTATED, Scale.NOT_APPLICABLE):
        flag("USE_CONTEXT_UNKNOWN", True)
    context.update(field_ref="world.references", input_sha256=hashes["world"])
    if world.validate_world(review.registered_sources, registered_evidence=review.registered_evidence,
                            registered_laws=review.registered_laws):
        flag("STRUCTURAL_REFERENCES")
    model = next((m for m in world.math_models if m.model_id == binding.model_id), None)
    family = review.families.get(binding.family_evidence_id)
    formula_review = review.formula_reviews.get(binding.model_id)
    if model is None: flag("FORMULA_MISSING")
    if family is None: flag("FAMILY_UNREVIEWED", True)
    if formula_review is None: flag("FORMULA_UNREVIEWED", True)
    if model is None or family is None or formula_review is None: return _result(problems, hashes)
    hashes["formula"] = record_hash(model)
    context.update(field_ref="formula.conflicting_forms", input_sha256=hashes["formula"])
    if model.conflicting_forms and model.conflicting_forms.strip():
        # This narrow profile has no conflict-resolution contract; do not infer resolution from text.
        flag("FORMULA_CONFLICT_UNRESOLVED", True)
    context.update(field_ref="formula.review", input_sha256=hashes["formula"])
    if hashes["formula"] != binding.model_sha256 or hashes["formula"] != formula_review.record_sha256:
        flag("FORMULA_CHANGED")
    vn_ids = {value.strip() for value in str(getattr(model, "vn_ids", "")).split(";") if value.strip()}
    if formula_review.evidence_id not in review.registered_evidence or formula_review.evidence_id not in vn_ids:
        flag("FORMULA_REVIEW_REFERENCE")
    context.update(field_ref="review.family", input_sha256=hashes["review"])
    if binding.family_evidence_id not in review.registered_evidence or family.model_id != binding.model_id:
        flag("FAMILY_REFERENCE")
    if family.law_id not in review.registered_laws | {m.model_id for m in world.math_models}:
        flag("LAW_UNREGISTERED")
    if not family.conditions.strip() or family.test_method is TestMethod.UNKNOWN:
        flag("CALIBRATION_CONTEXT_UNKNOWN", True)
    if family.scope is Scope.UNSTATED or family.scale in (Scale.UNSTATED, Scale.NOT_APPLICABLE):
        flag("CALIBRATION_CONTEXT_UNKNOWN", True)
    for key, declared, expected, codes in (
            ("scale", model.scale, family.scale.value, {item.value for item in Scale}),
            ("site_applicability", model.site_applicability, family.scope.value, {item.value for item in Scope})):
        context.update(field_ref=f"formula.{key}", input_sha256=hashes["formula"])
        if not declared or not declared.strip():
            continue  # explicit reviewed calibration family supplies the absent applicability context
        if declared == "GENERAL":
            if not family.formula_specialization or not family.formula_specialization.strip():
                flag("FORMULA_GENERAL_SPECIALIZATION_REQUIRED", True)
        elif declared not in codes or declared != expected:
            flag("FORMULA_APPLICABILITY_UNRESOLVED", True)
    context.update(field_ref="formula.status", input_sha256=hashes["formula"])
    if binding.for_scope in SITE_SPECIFIC_SKRU1_SCOPES and model.status and model.status.strip():
        if model.status == EpistemicStatus.UNKNOWN.value or model.status not in {item.value for item in EpistemicStatus}:
            flag("FORMULA_SITE_APPLICABILITY_UNRESOLVED", True)

    def available(time):
        usability = time.usable_at(binding.origin)
        if usability is None: flag("UNKNOWN_AVAILABILITY", True)
        elif not usability: flag("FUTURE_INFORMATION")

    objects = {obj.id: obj for _, obj in _nested_models(world) if isinstance(obj, WorldObject)}
    math_records = {record.model_id: record for record in world.math_models}
    spatial_ids = {node.id for node in world.spatial_nodes}
    def consumed(provenance, ref, identity):
        """Check only explicitly consumed world dependencies of this selected provenance."""
        context.update(field_ref=ref, input_sha256=identity)
        available(provenance.temporal)
        if provenance.status is EpistemicStatus.UNKNOWN: flag("CONSUMED_INPUT_UNKNOWN", True)
        dependencies, pending = set(), list(provenance.inputs)
        if provenance.spatial.entity_id:
            if provenance.spatial.entity_id not in spatial_ids: flag("CONDITION_SPATIAL_REFERENCE")
            pending.append(provenance.spatial.entity_id)
        while pending:
            oid = pending.pop()
            if oid in dependencies: continue
            dependencies.add(oid)
            obj = objects.get(oid)
            math_record = math_records.get(oid)
            if obj is not None and math_record is not None:
                flag("CONSUMED_REFERENCE_AMBIGUOUS", True)
                continue
            if math_record is not None:
                context.update(field_ref=f"{ref}.inputs", input_sha256=record_hash(math_record))
                reviewed = review.formula_reviews.get(oid)
                if reviewed is None: flag("CONSUMED_FORMULA_UNREVIEWED", True)
                elif reviewed.record_sha256 != record_hash(math_record): flag("CONSUMED_FORMULA_CHANGED")
                else:
                    available(reviewed.time)
                    evidence_ids = {value.strip() for value in str(getattr(math_record, "vn_ids", "")).split(";")
                                    if value.strip()}
                    if reviewed.evidence_id not in review.registered_evidence or reviewed.evidence_id not in evidence_ids:
                        flag("CONSUMED_FORMULA_REVIEW_REFERENCE")
                if math_record.conflicting_forms and math_record.conflicting_forms.strip():
                    flag("CONSUMED_FORMULA_CONFLICT_UNRESOLVED", True)
                continue
            if obj is None:
                context.update(field_ref=f"{ref}.inputs", input_sha256=identity)
                flag("CONSUMED_REFERENCE_MISSING")
                continue
            context.update(field_ref=f"{ref}.inputs", input_sha256=record_hash(obj))
            for _, nested in _nested_models(obj):
                if isinstance(nested, TemporalSupport): available(nested)
                elif isinstance(nested, Provenance):
                    if nested.status is EpistemicStatus.UNKNOWN: flag("CONSUMED_INPUT_UNKNOWN", True)
                    pending.extend(nested.inputs)
                    if nested.spatial.entity_id: pending.append(nested.spatial.entity_id)
        for unknown in world.unknowns:
            if dependencies & set(unknown.blocks):
                context.update(field_ref=f"{ref}.inputs", input_sha256=record_hash(unknown))
                flag("CONSUMED_INPUT_BLOCKED_BY_UNKNOWN", True)
        context.update(field_ref=ref, input_sha256=identity)

    context.update(field_ref="formula.availability", input_sha256=hashes["formula"])
    available(formula_review.time)
    context.update(field_ref="review.family.availability", input_sha256=hashes["review"])
    available(family.time)
    context.update(field_ref="formula.exact_form", input_sha256=hashes["formula"])
    form = EXACT_FORMS.get(binding.adapter_id)
    if form is None or model.equation_plain != form["equation"] or model.variables != form["variables"]:
        flag("UNSUPPORTED_EXACT_FORM", True)
        return _result(problems, hashes)
    if set(binding.parameter_ids) != set(form["roles"]) or len(set(binding.parameter_ids.values())) != len(form["roles"]):
        flag("PARAMETER_ROLES")
        return _result(problems, hashes)
    if set(binding.parameter_ids.values()) != set(family.parameter_hashes):
        flag("PARAMETER_SET_MISMATCH")
    parameters, statuses = {}, {}
    by_id = {m.id: m for m in world.materials}
    for role, parameter_id in binding.parameter_ids.items():
        for unknown in world.unknowns:
            if parameter_id in unknown.blocks:
                context.update(field_ref=f"parameter.{role}.blockers", input_sha256=record_hash(unknown))
                flag("SELECTED_INPUT_BLOCKED_BY_UNKNOWN", True)
        context.update(field_ref=f"parameter.{role}", input_sha256=None)
        parameter = by_id.get(parameter_id)
        if parameter is None:
            flag("PARAMETER_MISSING")
            continue
        parameters[role] = parameter
        hashes["parameters"][role] = record_hash(parameter)
        context.update(input_sha256=hashes["parameters"][role])
        if hashes["parameters"][role] != family.parameter_hashes.get(parameter_id): flag("PARAMETER_UNREVIEWED")
        p = parameter.quantity.provenance
        if (parameter.law_id != family.law_id or parameter.material != family.material or
            parameter.test_method != family.test_method or parameter.conditions != family.conditions or
            parameter.variable != f"creep_param_{role}" or p.scope != family.scope or p.scale != family.scale):
            flag("INCOHERENT_PARAMETER")
        if not any(binding.family_evidence_id in source.evidence_ids and bool(source.locator and source.locator.strip())
                   for source in p.sources):
            flag("CALIBRATION_FAMILY_MISMATCH")
        consumed(parameter.provenance, f"parameter.{role}.provenance", hashes["parameters"][role])
        consumed(p, f"parameter.{role}.quantity.provenance", hashes["parameters"][role])
        if _bounds(parameter.quantity) is None: flag("UNKNOWN_PARAMETER_VALUE", True)
        transfer = p.transfer
        moved = (p.scale, p.scope) != (binding.as_scale, binding.for_scope)
        for error in transfer_use_errors(p, binding.as_scale, binding.for_scope): flag(error)
        statuses[role] = (transfer.status if moved and transfer is not None else p.status).value
    if set(parameters) != set(form["roles"]): return _result(problems, hashes, statuses)

    def dimension(q, expected):
        info = lookup(q.unit)
        if info is None or info.factor is None or info.ambiguous_with:
            flag("UNSUPPORTED_UNIT", True)
            return None
        if info.dimension is not expected:
            flag("DIMENSION_MISMATCH")
            return None
        return info

    exponent = parameters["n"].quantity
    context.update(field_ref="parameter.n.quantity.unit", input_sha256=hashes["parameters"]["n"])
    exponent_unit = dimension(exponent, Dimension.DIMENSIONLESS)
    if exponent_unit is not None and exponent_unit.factor != 1:
        # Do not reinterpret a printed exponent or round it to a different coefficient dimension.
        flag("EXPONENT_UNIT_FACTOR_UNSUPPORTED", True)
    n = exponent.value
    if n is None or exponent.is_range or exponent.uncertainty.values or n <= 0:
        flag("EXPONENT_UNRESOLVED", True)
    if binding.adapter_id == "creep-power-pa-s/1":
        context.update(field_ref="parameter.A.quantity.unit", input_sha256=hashes["parameters"]["A"])
        unit = parameters["A"].quantity.unit
        if n is not None:
            expected = f"Pa^-{n:.17g}/s"
            if unit != expected:
                if lookup(unit) is not None or re.fullmatch(r"Pa\^-[0-9]+(?:\.[0-9]+)?/s", unit):
                    flag("DIMENSION_MISMATCH")
                else: flag("UNSUPPORTED_UNIT", True)
    else:
        context.update(field_ref="parameter.B.quantity.unit", input_sha256=hashes["parameters"]["B"])
        dimension(parameters["B"].quantity, Dimension.STRAIN_RATE)
        reference = parameters["stress0"].quantity
        context.update(field_ref="parameter.stress0.quantity.unit", input_sha256=hashes["parameters"]["stress0"])
        dimension(reference, Dimension.STRESS)
        if reference.value is None or reference.is_range or reference.uncertainty.values or reference.value <= 0:
            flag("REFERENCE_STRESS_UNRESOLVED", True)
    for key, expected_dimension in (("stress", Dimension.STRESS), ("temperature", Dimension.TEMPERATURE),
                                    ("duration", Dimension.TIME)):
        context.update(field_ref=f"binding.conditions.{key}", input_sha256=hashes["binding"])
        actual, limit = binding.conditions.get(key), family.applicability.get(key)
        if actual is None or limit is None:
            flag("CONDITIONS_UNKNOWN", True)
            continue
        if limit.uncertainty.values:
            # Competing reviewed hypotheses do not establish the continuous range between them.
            flag("CONDITIONS_DISCRETE_SUPPORT_UNSUPPORTED", True)
            continue
        pairs = []
        for quantity, ref, identity in ((actual, f"binding.conditions.{key}", hashes["binding"]),
                                       (limit, f"review.family.applicability.{key}", hashes["review"])):
            context.update(field_ref=ref, input_sha256=identity)
            consumed(quantity.provenance, ref, identity)
            target_scale, target_scope = ((binding.as_scale, binding.for_scope) if quantity is actual
                                          else (family.scale, family.scope))
            provenance = quantity.provenance
            if (provenance.scope is Scope.UNSTATED or provenance.scale in (Scale.UNSTATED, Scale.NOT_APPLICABLE) or
                target_scope is Scope.UNSTATED or target_scale in (Scale.UNSTATED, Scale.NOT_APPLICABLE)):
                flag("CONDITION_CONTEXT_UNKNOWN", True)
            else:
                for error in transfer_use_errors(provenance, target_scale, target_scope): flag(error)
            for source in quantity.provenance.sources:
                if source.source_id not in review.registered_sources or not set(source.evidence_ids) <= review.registered_evidence:
                    flag("CONDITION_REFERENCES")
            if quantity is limit and not any(binding.family_evidence_id in source.evidence_ids and
                    bool(source.locator and source.locator.strip()) for source in quantity.provenance.sources):
                flag("CONDITION_FAMILY_MISMATCH")
            bounds, info = _bounds(quantity), dimension(quantity, expected_dimension)
            if bounds is None: flag("CONDITIONS_UNKNOWN", True)
            if bounds is None or info is None: break
            converted = tuple(value * info.factor for value in bounds)
            if not all(math.isfinite(value) for value in converted):
                flag("INVALID_CONDITIONS")
                break
            pairs.append(converted)
        if len(pairs) == 2 and not (pairs[1][0] <= pairs[0][0] <= pairs[0][1] <= pairs[1][1]):
            context.update(field_ref=f"binding.conditions.{key}", input_sha256=hashes["binding"])
            flag("CONDITIONS_OUT_OF_RANGE")
    return _result(problems, hashes, statuses)


def receipt_matches_current(receipt, world, binding, review) -> bool:
    """A historical READY bit is insufficient: recompute the complete content-bound result."""
    fresh = admit_scientific_use(world, binding, review)
    return fresh["status"] == "READY" and fresh == receipt
