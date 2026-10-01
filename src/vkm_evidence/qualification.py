"""Frozen, independently reviewed extraction qualification; no corpus execution.

Metrics describe the pinned gold/prediction population. They never establish
scientific admission or field validation. Values are not copied into reports.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from math import sqrt
from statistics import NormalDist
from typing import Annotated, Callable, Literal, Union

from pydantic import Field, field_validator, model_validator

from vkm_evidence.contracts import Identifier, OriginSlice, Sha256, StrictModel, record_hash
from vkm_evidence.validation import lineage_overlap
from vkm_world.core.provenance import TemporalSupport

Metric = Literal["DISCOVERY_RECALL", "DISCOVERY_PRECISION", "CLASSIFICATION_ACCURACY", "EMPTY_UNIT_ACCURACY",
                 "NUMERIC_EXACT", "GRID_EXACT", "FORMULA_EXACT", "ENTITY_EXACT", "TEMPORAL_EXACT"]
EXACT = ("NUMERIC_EXACT", "GRID_EXACT", "FORMULA_EXACT", "ENTITY_EXACT", "TEMPORAL_EXACT")


def _unique(values, label):
    if len(set(values)) != len(values):
        raise ValueError("duplicate " + label)


def _actors(values):
    return {v.casefold() for v in values}


class NumericField(StrictModel):
    metric: Literal["NUMERIC_EXACT"] = "NUMERIC_EXACT"
    key: Identifier
    literal: str
    state: Literal["VALUE", "EMPTY", "MISSING", "UNREADABLE", "NOT_APPLICABLE"]
    decimal_value: str | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def _number(self):
        if self.state == "VALUE":
            try:
                finite = self.decimal_value is not None and Decimal(self.decimal_value).is_finite()
            except InvalidOperation:
                finite = False
            if not finite:
                raise ValueError("finite explicit decimal required")
        elif self.decimal_value is not None:
            raise ValueError("non-value cannot silently become zero")
        return self


class GridCell(StrictModel):
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    rowspan: int = Field(1, ge=1)
    colspan: int = Field(1, ge=1)
    text: str
    state: Literal["VALUE", "EMPTY", "UNREADABLE"]


class GridField(StrictModel):
    metric: Literal["GRID_EXACT"] = "GRID_EXACT"
    key: Identifier
    rows: int = Field(ge=1)
    columns: int = Field(ge=1)
    cells: tuple[GridCell, ...]

    @field_validator("cells")
    @classmethod
    def _order(cls, values):
        return tuple(sorted(values, key=lambda c: (c.row, c.col)))

    @model_validator(mode="after")
    def _complete_grid(self):
        if sum(c.rowspan * c.colspan for c in self.cells) != self.rows * self.columns:
            raise ValueError("grid must explicitly account for empty and merged cells")
        events = {0: [], self.rows: []}
        for index, cell in enumerate(self.cells):
            if cell.row + cell.rowspan > self.rows or cell.col + cell.colspan > self.columns:
                raise ValueError("cell outside grid")
            events.setdefault(cell.row, []).append((index, True))
            events.setdefault(cell.row + cell.rowspan, []).append((index, False))
        active = {}
        # Sweep span boundaries; a huge merged cell must not allocate one object
        # per covered coordinate just to verify a small gold representation.
        for row in sorted(events)[:-1]:
            for index, starts in events[row]:
                if starts:
                    cell = self.cells[index]
                    active[index] = (cell.col, cell.col + cell.colspan)
                else:
                    del active[index]
            end = 0
            for start, stop in sorted(active.values()):
                if start != end:
                    raise ValueError("overlap or hole in grid")
                end = stop
            if end != self.columns:
                raise ValueError("incomplete row band in grid")
        return self


class FormulaField(StrictModel):
    metric: Literal["FORMULA_EXACT"] = "FORMULA_EXACT"
    key: Identifier
    representation: Literal["LATEX", "OMML", "MATHML", "TEXT", "IMAGE_ONLY"]
    literal: str
    # symbol, meaning, scope; preserves ambiguity rather than proving algebra.
    bindings: tuple[tuple[str, str, str], ...] = ()


class EntityField(StrictModel):
    metric: Literal["ENTITY_EXACT"] = "ENTITY_EXACT"
    key: Identifier
    entity_id: Identifier | None
    scope: str = Field(min_length=1)
    state: Literal["RESOLVED", "AMBIGUOUS", "UNKNOWN"]
    candidates: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _resolution(self):
        if (self.state == "RESOLVED") != (self.entity_id is not None):
            raise ValueError("entity identity and resolution state disagree")
        _unique(self.candidates, "entity candidate")
        return self


class TemporalField(StrictModel):
    metric: Literal["TEMPORAL_EXACT"] = "TEMPORAL_EXACT"
    key: Identifier
    support: TemporalSupport


ExactField = Annotated[Union[NumericField, GridField, FormulaField, EntityField, TemporalField], Field(discriminator="metric")]


class EvalObject(StrictModel):
    object_id: Identifier
    unit_id: Identifier
    locator: str = Field(min_length=1)
    classification: str = Field(min_length=1)
    fields: tuple[ExactField, ...] = ()

    @model_validator(mode="after")
    def _keys(self):
        _unique([(f.metric, f.key) for f in self.fields], "object field")
        return self


class StratumKey(StrictModel):
    source_id: Identifier
    source_sha256: Sha256
    format: str = Field(min_length=1)  # e.g. PDF_NATIVE, PDF_SCAN, DOCX, XLSX, TAB
    quality: str = Field(min_length=1)  # UNKNOWN is an explicit stratum, never omitted
    object_kind: str = Field(min_length=1)


class Threshold(StrictModel):
    metric: Metric
    minimum: float = Field(ge=0, le=1)
    minimum_denominator: int = Field(ge=1)
    decision_statistic: Literal["POINT", "WILSON_LOWER"]


class StratumPlan(StrictModel):
    stratum_id: Identifier
    key: StratumKey
    unit_ids: tuple[Identifier, ...] = Field(min_length=1)
    design: Literal["POSITIVE_OBJECTS", "NEGATIVE_UNITS"]
    sampling_basis: str = Field(min_length=1)
    thresholds: tuple[Threshold, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _frame(self):
        _unique(self.unit_ids, "sampling unit")
        _unique([t.metric for t in self.thresholds], "threshold")
        required = {"EMPTY_UNIT_ACCURACY"} if self.design == "NEGATIVE_UNITS" else {
            "DISCOVERY_RECALL", "DISCOVERY_PRECISION", "CLASSIFICATION_ACCURACY"}
        if not required.issubset(t.metric for t in self.thresholds):
            raise ValueError("discovery/classification or negative-unit gates are required")
        return self


class FrozenPlan(StrictModel):
    schema_version: Literal["vkm-extraction-qualification/1"] = "vkm-extraction-qualification/1"
    plan_id: Identifier
    population: Literal["SYNTHETIC", "CORPUS"]
    purpose: Literal["INDEPENDENT_EXTRACTION_VALIDATION", "SHARED_ORIGIN_HOLDOUT", "TRANSFER_CONSISTENCY"]
    gold_sha256: Sha256
    extractor_identity_sha256: Sha256  # exact code/config/model/tokenizer/chunking identity artifact
    producer_actors: tuple[Identifier, ...] = Field(min_length=1)
    tuning_actors: tuple[Identifier, ...] = ()
    tuning_state: Literal["DECLARED", "NO_TUNING_ATTESTED", "UNKNOWN"]
    tuning_origins: tuple[OriginSlice, ...] = ()
    tuning_declaration_sha256: Sha256 | None = None
    strata: tuple[StratumPlan, ...] = Field(min_length=1)
    confidence: float = Field(gt=0, lt=1)
    matching_rule: Literal["INDEPENDENT_ORIGINAL_ADJUDICATION_V1"] = "INDEPENDENT_ORIGINAL_ADJUDICATION_V1"
    exact_rule: Literal["EXACT_TYPED_VALUE_V1"] = "EXACT_TYPED_VALUE_V1"

    @model_validator(mode="after")
    def _plan(self):
        _unique([s.stratum_id for s in self.strata], "stratum")
        _unique([record_hash(s.key) for s in self.strata], "source-version stratum key")
        _unique([a.casefold() for a in self.producer_actors], "producer actor")
        _unique([a.casefold() for a in self.tuning_actors], "tuning actor")
        if self.tuning_state == "DECLARED" and not self.tuning_origins:
            raise ValueError("declared tuning requires original provenance")
        if self.tuning_state == "NO_TUNING_ATTESTED" and (self.tuning_origins or not self.tuning_declaration_sha256):
            raise ValueError("no-tuning claim requires separate attestation and no tuning origins")
        return self

    @property
    def sha256(self):
        return record_hash(self)


class StratumResult(StrictModel):
    stratum_id: Identifier
    key: StratumKey
    unit_ids: tuple[Identifier, ...]
    status: Literal["COMPLETE", "FAILED", "NOT_RUN"]
    objects: tuple[EvalObject, ...] = ()
    reason: str | None = None

    @model_validator(mode="after")
    def _objects(self):
        _unique(self.unit_ids, "evaluated unit")
        _unique([o.object_id for o in self.objects], "evaluation object")
        if any(o.unit_id not in self.unit_ids for o in self.objects):
            raise ValueError("object outside inspected unit inventory")
        if self.status != "COMPLETE" and not self.reason:
            raise ValueError("failed/not-run stratum requires reason")
        return self


class GoldStratum(StratumResult):
    origins: tuple[OriginSlice, ...]
    original_review: Literal["VERIFIED_FROM_ORIGINAL", "NOT_RUN", "FAILED"]
    review_receipt_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _review(self):
        if self.original_review == "VERIFIED_FROM_ORIGINAL" and not self.review_receipt_sha256:
            raise ValueError("original gold review requires pinned receipt")
        return self


class GoldSet(StrictModel):
    schema_version: Literal["vkm-qualification-gold/1"] = "vkm-qualification-gold/1"
    population: Literal["SYNTHETIC", "CORPUS"]
    authors: tuple[Identifier, ...] = Field(min_length=1)
    reviewers: tuple[Identifier, ...] = Field(min_length=1)
    strata: tuple[GoldStratum, ...]


class PredictionSet(StrictModel):
    schema_version: Literal["vkm-qualification-prediction/1"] = "vkm-qualification-prediction/1"
    plan_sha256: Sha256
    extractor_identity_sha256: Sha256
    producer_actors: tuple[Identifier, ...] = Field(min_length=1)
    started_at: datetime
    strata: tuple[StratumResult, ...]

    @field_validator("started_at")
    @classmethod
    def _utc(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone-aware timestamp required")
        return value.astimezone(timezone.utc)


class Match(StrictModel):
    gold_object_id: Identifier
    prediction_object_id: Identifier


class MatchStratum(StrictModel):
    stratum_id: Identifier
    status: Literal["COMPLETE", "FAILED", "NOT_RUN"]
    matches: tuple[Match, ...] = ()
    reason: str | None = None

    @model_validator(mode="after")
    def _matching(self):
        _unique([m.gold_object_id for m in self.matches], "matched gold object")
        _unique([m.prediction_object_id for m in self.matches], "matched prediction object")
        if self.status != "COMPLETE" and not self.reason:
            raise ValueError("missing adjudication requires reason")
        return self


class MatchAdjudication(StrictModel):
    schema_version: Literal["vkm-qualification-matches/1"] = "vkm-qualification-matches/1"
    plan_sha256: Sha256
    gold_sha256: Sha256
    prediction_sha256: Sha256
    reviewers: tuple[Identifier, ...] = Field(min_length=1)
    reviewed_at: datetime
    strata: tuple[MatchStratum, ...]

    _utc = field_validator("reviewed_at")(PredictionSet._utc.__func__)


class FrozenRegistration(StrictModel):
    schema_version: Literal["vkm-qualification-registration/1"] = "vkm-qualification-registration/1"
    plan_sha256: Sha256
    gold_sha256: Sha256
    registered_at: datetime
    registrar: Identifier
    durable_receipt_sha256: Sha256

    _utc = field_validator("registered_at")(PredictionSet._utc.__func__)


def wilson_interval(numerator: int, denominator: int, confidence: float) -> tuple[float, float] | None:
    """Two-sided Wilson interval; zero denominator is undefined, never perfect."""
    if type(numerator) is not int or type(denominator) is not int or not 0 <= numerator <= denominator or not 0 < confidence < 1:
        raise ValueError("invalid binomial proportion")
    if denominator == 0:
        return None
    z = -NormalDist().inv_cdf((1 - confidence) / 2)
    p, n = numerator / denominator, denominator
    divisor = 1 + z * z / n
    center = (p + z * z / (2 * n)) / divisor
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / divisor
    return max(0.0, center - half), min(1.0, center + half)


def _metric(k, n, confidence, basis, **counts):
    interval = wilson_interval(k, n, confidence)
    return {"status": "MEASURED" if n else "NOT_RUN", "numerator": k, "denominator": n,
            "errors": n - k, "point": k / n if n else None, "wilson": list(interval) if interval else None,
            "confidence": confidence, "denominator_basis": basis, **counts}


def _measure(plan, gold, prediction, matches):
    gs, ps = {o.object_id: o for o in gold.objects}, {o.object_id: o for o in prediction.objects}
    pairs = {}
    for match in matches.matches:
        if match.gold_object_id not in gs or match.prediction_object_id not in ps:
            raise ValueError("adjudication references unknown object")
        a, b = gs[match.gold_object_id], ps[match.prediction_object_id]
        if a.unit_id != b.unit_id:
            raise ValueError("adjudication crosses original units")
        pairs[a.object_id] = b.object_id
    n = len(pairs)
    confidence = plan.confidence
    metrics = {
        "DISCOVERY_RECALL": _metric(n, len(gs), confidence, "ALL_GOLD_OBJECTS"),
        "DISCOVERY_PRECISION": _metric(n, len(ps), confidence, "ALL_PREDICTED_OBJECTS"),
        "CLASSIFICATION_ACCURACY": _metric(sum(gs[g].classification == ps[p].classification for g, p in pairs.items()),
            n, confidence, "MATCHED_GOLD_OBJECTS", unmatched_gold=len(gs) - n),
    }
    empty = set(gold.unit_ids) - {g.unit_id for g in gold.objects}
    predicted_units = {p.unit_id for p in prediction.objects}
    metrics["EMPTY_UNIT_ACCURACY"] = _metric(len(empty - predicted_units), len(empty), confidence, "GOLD_EMPTY_UNITS")
    for kind in EXACT:
        slots = {(g.object_id, f.key): f for g in gold.objects for f in g.fields if f.metric == kind}
        proposed = {(p.object_id, f.key): f for p in prediction.objects for f in p.fields if f.metric == kind}
        used, correct, missing, incorrect = set(), 0, 0, 0
        for (gid, key), value in slots.items():
            pkey = (pairs.get(gid), key)
            if pkey not in proposed:
                missing += 1
            else:
                used.add(pkey)
                if record_hash(value) == record_hash(proposed[pkey]):
                    correct += 1
                else:
                    incorrect += 1
        unexpected = len(set(proposed) - used)
        metrics[kind] = _metric(correct, len(slots) + unexpected, confidence, "GOLD_FIELDS_PLUS_UNEXPECTED_PREDICTED_FIELDS",
            gold_fields=len(slots), missing=missing, incorrect=incorrect, unexpected=unexpected)
    return metrics


def evaluate_qualification(plan: FrozenPlan, gold: GoldSet | None, predictions: PredictionSet | None,
                           adjudication: MatchAdjudication | None, registration: FrozenRegistration | None, *,
                           verify_registration: Callable[[FrozenRegistration], bool] | None = None) -> dict:
    """Evaluate frozen artifacts. Verification callback reads trusted durable registration.

    No threshold tuning, edits, corpus lookup or producer invocation occurs here.
    Raw values and source text are deliberately absent from the returned receipt.
    """
    reasons, reports = [], {}
    gh = record_hash(gold) if gold is not None else None
    ph = record_hash(predictions) if predictions is not None else None
    report = {"schema": "vkm-qualification-report/1", "plan_sha256": plan.sha256, "gold_sha256": gh,
              "prediction_sha256": ph, "adjudication_sha256": record_hash(adjudication) if adjudication else None,
              "registration_sha256": record_hash(registration) if registration else None,
              "population": plan.population, "purpose": plan.purpose, "strata": reports,
              "evaluator_rule": "vkm-qualification-metrics/1",
              "qualified_scope": {s.stratum_id: [t.metric for t in s.thresholds] for s in plan.strata},
              "interval_interpretation": "DESCRIPTIVE_BERNOULLI_NOT_CLUSTER_ADJUSTED",
              "scientific_admission": "NOT_ESTABLISHED", "field_validation": "NOT_ESTABLISHED"}
    if gold is None or predictions is None or adjudication is None:
        reasons.append("EVALUATION_ARTIFACT_NOT_RUN")
    if gh != plan.gold_sha256:
        reasons.append("FROZEN_GOLD_MISSING_OR_CHANGED")
    if gold and gold.population != plan.population:
        reasons.append("POPULATION_MISMATCH")
    if predictions and (predictions.plan_sha256 != plan.sha256 or
            predictions.extractor_identity_sha256 != plan.extractor_identity_sha256 or
            _actors(predictions.producer_actors) != _actors(plan.producer_actors)):
        reasons.append("PREDICTION_BINDING_MISMATCH")
    if adjudication and (adjudication.plan_sha256 != plan.sha256 or adjudication.gold_sha256 != gh or
                         adjudication.prediction_sha256 != ph):
        reasons.append("ADJUDICATION_BINDING_MISMATCH")
    if registration is None or registration.plan_sha256 != plan.sha256 or registration.gold_sha256 != gh:
        reasons.append("PREREGISTRATION_BINDING_MISSING")
    else:
        try:
            verified = verify_registration is not None and verify_registration(registration) is True
        except Exception:
            verified = False
        if not verified:
            reasons.append("PREREGISTRATION_NOT_VERIFIED")
        if predictions and registration.registered_at >= predictions.started_at:
            reasons.append("PLAN_NOT_FROZEN_BEFORE_PREDICTION")
    if adjudication and predictions and adjudication.reviewed_at < predictions.started_at:
        reasons.append("ADJUDICATION_PRECEDES_PREDICTION")
    evaluators = _actors((gold.authors + gold.reviewers if gold else ()) + (adjudication.reviewers if adjudication else ()))
    if evaluators & _actors(plan.producer_actors + plan.tuning_actors + (predictions.producer_actors if predictions else ())):
        reasons.append("EVALUATOR_PRODUCER_ACTOR_OVERLAP")

    def indexed(values, label):
        mapping = {s.stratum_id: s for s in values}
        if len(mapping) != len(values):
            reasons.append("DUPLICATE_STRATUM:" + label)
        if set(mapping) - {s.stratum_id for s in plan.strata}:
            reasons.append("UNPLANNED_STRATUM:" + label)
        return mapping

    gm = indexed(gold.strata if gold else (), "gold")
    pm = indexed(predictions.strata if predictions else (), "prediction")
    am = indexed(adjudication.strata if adjudication else (), "adjudication")
    for spec in plan.strata:
        sid, local = spec.stratum_id, []
        g, p, a = gm.get(sid), pm.get(sid), am.get(sid)
        item = {"key": spec.key.model_dump(mode="json"),
                "metrics": {t.metric: {"status": "NOT_RUN", "numerator": None, "denominator": None,
                                       "errors": None, "point": None, "wilson": None} for t in spec.thresholds},
                "gates": [{**t.model_dump(mode="json"), "status": "NOT_RUN"} for t in spec.thresholds], "reasons": local}
        reports[sid] = item
        if g is None or p is None or a is None:
            local.append("STRATUM_NOT_RUN")
        elif (g.status != "COMPLETE" or p.status != "COMPLETE" or a.status != "COMPLETE" or
              g.original_review != "VERIFIED_FROM_ORIGINAL"):
            local.append("STRATUM_OR_ORIGINAL_REVIEW_INCOMPLETE")
        elif (g.key != spec.key or p.key != spec.key or set(g.unit_ids) != set(spec.unit_ids) or set(p.unit_ids) != set(spec.unit_ids)):
            local.append("STRATUM_SOURCE_VERSION_OR_DENOMINATOR_CHANGED")
        else:
            if plan.tuning_state == "NO_TUNING_ATTESTED" and g.origins and all(o.verified for o in g.origins):
                lineage = "NO_TUNING_ATTESTED"
            else:
                lineage = lineage_overlap(plan.tuning_origins, g.origins) if plan.tuning_state == "DECLARED" else "UNKNOWN"
            item["primary_lineage"] = lineage
            allowed = {"INDEPENDENT", "NO_TUNING_ATTESTED"}
            if plan.purpose == "SHARED_ORIGIN_HOLDOUT":
                allowed.add("DISJOINT_SHARED_ORIGIN")
            if plan.purpose == "TRANSFER_CONSISTENCY":
                allowed.update({"DISJOINT_SHARED_ORIGIN", "OVERLAP"})
            if lineage not in allowed:
                local.append("PRIMARY_ORIGIN_INDEPENDENCE_NOT_ESTABLISHED")
            if spec.design == "NEGATIVE_UNITS" and g.objects:
                local.append("NEGATIVE_GOLD_CONTAINS_OBJECTS")
            try:
                item["metrics"] = _measure(plan, g, p, a)
            except ValueError:
                local.append("INVALID_ONE_TO_ONE_ADJUDICATION")
            thresholds = {t.metric: t for t in spec.thresholds}
            for name, metric in item["metrics"].items():
                if name in EXACT and metric["denominator"] and name not in thresholds:
                    local.append("EXACT_METRIC_WITHOUT_FROZEN_THRESHOLD:" + name)
            item["gates"] = []
            for threshold in spec.thresholds:
                metric = item["metrics"].get(threshold.metric)
                result = "NOT_RUN"
                if metric and metric["status"] == "MEASURED":
                    value = metric["point"] if threshold.decision_statistic == "POINT" else metric["wilson"][0]
                    result = "PASS" if (metric["denominator"] >= threshold.minimum_denominator and value >= threshold.minimum) else "FAIL"
                item["gates"].append({**threshold.model_dump(mode="json"), "status": result})
                if result != "PASS":
                    local.append("METRIC_GATE_" + result + ":" + threshold.metric)
        item["status"] = "BLOCKED" if local else "PASS"
        reasons.extend(sid + ":" + r for r in local)
    report["reasons"] = sorted(set(reasons))
    report["status"] = "NOT_RUN" if gold is None or predictions is None or adjudication is None else "BLOCKED" if reasons else "PASS"
    report["qualification"] = ("SYNTHETIC_ONLY" if plan.population == "SYNTHETIC" else "QUALIFIED_FOR_FROZEN_SCOPE") if report["status"] == "PASS" else "NOT_QUALIFIED"
    report["report_sha256"] = record_hash(report)
    return report


def qualification_gate(report: dict, plan: FrozenPlan, expected_prediction_sha256: str, *,
                       require_corpus: bool = True, required_scope: dict[str, tuple[Metric, ...]] | None = None) -> bool:
    """Read a receipt without promoting synthetic PASS or unmeasured scope.

    Callers must obtain ``plan`` and the expected prediction hash from approved
    immutable artifacts, not from this report. This is integrity verification,
    not authentication of arbitrary writable local files.
    """
    try:
        body = {k: v for k, v in report.items() if k != "report_sha256"}
        if report.get("report_sha256") != record_hash(body):
            return False
        if (report["status"] != "PASS" or report["reasons"] or report["plan_sha256"] != plan.sha256 or
                report["gold_sha256"] != plan.gold_sha256 or report["prediction_sha256"] != expected_prediction_sha256 or
                report["population"] != plan.population or report["purpose"] != plan.purpose or
                report["evaluator_rule"] != "vkm-qualification-metrics/1" or
                report["scientific_admission"] != "NOT_ESTABLISHED" or report["field_validation"] != "NOT_ESTABLISHED"):
            return False
        expected_qualification = "SYNTHETIC_ONLY" if plan.population == "SYNTHETIC" else "QUALIFIED_FOR_FROZEN_SCOPE"
        if report["qualification"] != expected_qualification or (require_corpus and plan.population != "CORPUS"):
            return False
        scope = {s.stratum_id: [t.metric for t in s.thresholds] for s in plan.strata}
        if report["qualified_scope"] != scope or set(report["strata"]) != set(scope):
            return False
        if required_scope and any(sid not in scope or not set(metrics).issubset(scope[sid])
                                  for sid, metrics in required_scope.items()):
            return False
        for spec in plan.strata:
            row = report["strata"][spec.stratum_id]
            if (row["status"] != "PASS" or row["reasons"] or row["key"] != spec.key.model_dump(mode="json") or
                    row["gates"] != [{**t.model_dump(mode="json"), "status": "PASS"} for t in spec.thresholds]):
                return False
            for threshold in spec.thresholds:
                metric = row["metrics"][threshold.metric]
                k, n = metric["numerator"], metric["denominator"]
                interval = wilson_interval(k, n, plan.confidence)
                if (metric["status"] != "MEASURED" or n < threshold.minimum_denominator or interval is None or
                        metric["point"] != k / n or metric["errors"] != n - k or
                        metric["wilson"] != list(interval) or metric["confidence"] != plan.confidence):
                    return False
                value = k / n if threshold.decision_statistic == "POINT" else interval[0]
                if value < threshold.minimum:
                    return False
        return True
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False
