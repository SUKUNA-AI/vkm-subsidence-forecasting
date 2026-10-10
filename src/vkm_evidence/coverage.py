"""A loss-accounting ledger: accounted candidates are not detector recall."""
from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import Field, model_validator

from vkm_evidence.contracts import Identifier, Sha256, StrictModel, record_hash

Disposition = Literal["EXTRACTED", "IMAGE_PRESERVED", "UNREADABLE", "NEEDS_REVIEW", "UNSUPPORTED",
                      "FAILED", "FALSE_DETECTION", "DUPLICATE", "SUPPRESSED"]


class ExpectedUnit(StrictModel):
    unit_id: Identifier
    source_id: Identifier
    source_sha256: Sha256
    unit_kind: Literal["PAGE", "SHEET", "ATTACHMENT", "NATIVE_OBJECT"]
    locator: str = Field(min_length=1)


class ExtractionAttempt(StrictModel):
    attempt_id: Identifier
    unit_id: Identifier
    candidate_id: Identifier | None = None
    stage: str = Field(min_length=1)
    input_sha256: Sha256
    config_sha256: Sha256
    code_revision: Sha256 | str
    state: Literal["SUCCEEDED", "FAILED", "NOT_RUN", "INTERRUPTED"]
    outputs: tuple[Sha256, ...] = ()
    object_outputs: dict[Identifier, Sha256] = Field(default_factory=dict)
    reason: str | None = None

    @model_validator(mode="after")
    def _result(self):
        if self.state != "SUCCEEDED" and not self.reason:
            raise ValueError("non-successful attempt needs a reason")
        if not set(self.object_outputs.values()).issubset(self.outputs):
            raise ValueError("object output hash is absent from durable outputs")
        if len(set(self.outputs)) != len(self.outputs):
            raise ValueError("duplicate durable output hash")
        return self


class CandidateDisposition(StrictModel):
    candidate_id: Identifier
    unit_id: Identifier
    kind: Literal["TEXT", "TABLE", "FORMULA", "FIGURE", "GIS_FEATURE", "CELL", "OTHER"]
    locator: str = Field(min_length=1)
    state: Disposition
    attempts: tuple[Identifier, ...] = ()
    output_objects: tuple[Identifier, ...] = ()
    preserved_artifacts: tuple[Sha256, ...] = ()
    duplicate_of: Identifier | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _output(self):
        if self.state == "EXTRACTED" and (not self.output_objects or not self.attempts):
            raise ValueError("EXTRACTED needs output identities and extraction attempts")
        if self.state == "IMAGE_PRESERVED" and not self.preserved_artifacts:
            raise ValueError("preserved image needs its hash")
        if self.state == "DUPLICATE" and not self.duplicate_of:
            raise ValueError("duplicate needs an exact candidate")
        if self.state != "EXTRACTED" and not self.reason:
            raise ValueError("non-extracted candidate needs explicit disposition reason")
        return self


class CoverageLedger(StrictModel):
    schema_version: Literal["vkm-object-coverage/1"] = "vkm-object-coverage/1"
    campaign_sha256: Sha256
    units: tuple[ExpectedUnit, ...]
    inspected_units: tuple[Identifier, ...]
    candidates: tuple[CandidateDisposition, ...]
    attempts: tuple[ExtractionAttempt, ...]
    detection_assessment: Literal["NOT_RUN", "INDEPENDENT_ASSESSED"] = "NOT_RUN"
    detection_report_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _references(self):
        units = {u.unit_id for u in self.units}
        candidates = {c.candidate_id: c for c in self.candidates}
        attempts = {a.attempt_id: a for a in self.attempts}
        for values, unique in ((self.units, units), (self.candidates, candidates), (self.attempts, attempts)):
            if len(values) != len(unique):
                raise ValueError("duplicate ledger identity")
        if len(set(self.inspected_units)) != len(self.inspected_units) or not set(self.inspected_units).issubset(units):
            raise ValueError("invalid inspected-unit identity")
        for c in self.candidates:
            if c.unit_id not in units or not set(c.attempts).issubset(attempts):
                raise ValueError("dangling candidate/attempt reference")
            if any(attempts[a].unit_id != c.unit_id or attempts[a].candidate_id not in {None, c.candidate_id}
                   for a in c.attempts):
                raise ValueError("candidate borrows another unit/candidate's attempt")
            if c.state == "EXTRACTED" and not any(attempts[a].state == "SUCCEEDED" and attempts[a].outputs
                                                 for a in c.attempts):
                raise ValueError("extracted candidate lacks durable output")
            available = {oid for a in c.attempts if attempts[a].state == "SUCCEEDED"
                         for oid in attempts[a].object_outputs}
            if not set(c.output_objects).issubset(available):
                raise ValueError("candidate output is not linked to its own attempt")
            if c.duplicate_of and (c.duplicate_of not in candidates or c.duplicate_of == c.candidate_id):
                raise ValueError("dangling or self duplicate")
            seen, current = set(), c
            while current.duplicate_of:
                if current.candidate_id in seen:
                    raise ValueError("cyclic duplicate disposition")
                seen.add(current.candidate_id)
                current = candidates[current.duplicate_of]
        for a in self.attempts:
            if a.unit_id not in units or (a.candidate_id is not None and a.candidate_id not in candidates):
                raise ValueError("dangling extraction attempt")
            if a.candidate_id and candidates[a.candidate_id].unit_id != a.unit_id:
                raise ValueError("attempt belongs to a different unit")
        if self.detection_assessment == "INDEPENDENT_ASSESSED" and self.detection_report_sha256 is None:
            raise ValueError("independent assessment requires immutable report")
        return self

    def report(self) -> dict:
        counts = Counter(c.state for c in self.candidates)
        inspected = len(set(self.inspected_units))
        failed = sum(a.state != "SUCCEEDED" for a in self.attempts)
        return {"ledger_sha256": record_hash(self), "expected_units": len(self.units), "inspected_units": inspected,
                "unit_accounting_complete": inspected == len(self.units), "detected_candidates": len(self.candidates),
                "dispositions": dict(counts), "failed_or_not_run_attempts": failed,
                "detector_recall": "NOT_ESTABLISHED" if self.detection_assessment == "NOT_RUN" else "SEE_REPORT",
                "scientific_admission": "NOT_ESTABLISHED",
                "status": "ACCOUNTED" if inspected == len(self.units) else "INCOMPLETE"}
