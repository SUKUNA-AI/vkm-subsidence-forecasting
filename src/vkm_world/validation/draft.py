"""Executable checks of a DRAFT experiment contract; no predictions, scoring, training or test access."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..core.base import WorldObject
from ..core.io import sha256_file
from ..core.units import lookup
from ..core import base, io, provenance, units
from ..core.provenance import EpistemicStatus, Provenance, TemporalSupport
from ..materials import parameters
from ..mathmeta import models as math_models
from ..observations import catalog as observations
from ..observations.catalog import ObservationDataset, ObservationSystem
from ..worldspec import io as world_io, model as world_model
from ..worldspec.model import _nested_models
from . import leakage, metrics, scientific, splits
from .leakage import (PLANNED_INFORMATION_FIELDS, assert_available_at, assert_disjoint_sample_sets,
                      assert_feature_fields_safe, assert_planned_target, assert_positive_horizon,
                      assert_time_alignment)
from .metrics import point_metrics
from .scientific import (ReviewIndex, ScientificUseBinding, admit_scientific_use, record_hash,
                         receipt_matches_current, _bounds, _fresh)
from .splits import (FORWARD_ONLY_SPLITTERS, UnsafeSplitError, assert_forward_only, leave_one_line_out, rolling_origin_assignments,
                     validate_splitter_name)

# These declarations refer to the existing implementation; this consumer never calls it.
SUPPORTED_METRICS = {"mae": point_metrics, "rmse": point_metrics, "bias": point_metrics}
DATA_ROLES = frozenset({"calibration", "nroy", "filtering", "validation", "test"})


def implementation_hashes() -> dict[str, str]:
    """Actual public implementation identities, with logical names rather than machine paths."""
    modules = {"scientific": scientific, "leakage": leakage, "splits": splits, "metrics": metrics,
               "provenance": provenance, "units": units, "materials": parameters,
               "math_models": math_models, "observations": observations,
               "worldspec_model": world_model, "worldspec_io": world_io, "core_base": base, "core_io": io}
    return {"draft": sha256_file(__file__), **{name: sha256_file(module.__file__) for name, module in modules.items()}}


class DraftDataRole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["USED", "NOT_APPLICABLE"] | None = None
    dataset_ids: tuple[str, ...] = ()
    sample_ids: tuple[str, ...] = ()
    rationale: str = ""


class DraftSample(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    sample_id: str
    role: Literal["development"] = "development"
    origin: date
    target: date
    target_campaign_id: str
    horizon_days: int
    target_available_from: date
    group: str


class DraftProtocol(BaseModel):
    """An incomplete draft is archival; declarations become mandatory at the use boundary."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    experiment_id: str = Field(min_length=1)
    scientific_question: str = ""
    experiment_version: str = ""
    status: Literal["DRAFT"] = "DRAFT"
    t0: date | None = None
    sample_origin_policy: Literal["SINGLE_T0", "AT_OR_AFTER_T0"] | None = None
    horizon_rule: Literal["PLANNED_EPOCH"] | None = None
    horizon_steps: int | None = Field(None, ge=1)
    input_availability_rule: Literal["KNOWN_AT_ORIGIN"] | None = None
    binding_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    data_kind: Literal["SYNTHETIC"] | None = None
    data_roles: dict[str, DraftDataRole] = Field(default_factory=dict)
    independence_rule: Literal["DISJOINT_DATA_AND_LINEAGE"] | None = None
    code_hashes: dict[str, str] = Field(default_factory=dict)
    data_hashes: dict[str, str] = Field(default_factory=dict)
    observation_dataset_id: str = ""
    dataset_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    feature_fields: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    acceptance_limits: dict[str, float] = Field(default_factory=dict)
    splitter: str = ""
    minimum_train_dates: int | None = Field(None, ge=1)
    samples: list[DraftSample] = Field(default_factory=list)
    schedule: list[tuple[str, date]] = Field(default_factory=list)


def check_draft_protocol(world, binding: ScientificUseBinding, review: ReviewIndex,
                         protocol: DraftProtocol, admission=None) -> dict:
    """Consume current admission and existing guards; a complete draft always remains DRAFT.

    Only declared synthetic development metadata is accepted. Values_location is hashed but never opened.
    Metric limits declare maximum MAE/RMSE/absolute bias; no scores are calculated here.
    """
    result = {"schema_version": 1, "status": "BLOCKED", "protocol_status": "DRAFT",
              "readiness_scope": "CONTRACT_CONSISTENCY_ONLY", "reasons": [], "fold_count": 0}
    try:
        protocol = DraftProtocol.model_validate(protocol.model_dump(mode="python", warnings=False))
    except (ValidationError, ValueError, TypeError, AttributeError):
        result["reasons"] = ["INVALID_DRAFT_CONTRACT"]
        return result
    mandatory = set(DraftProtocol.model_fields) - {"experiment_id", "status"}
    missing = [key for key in sorted(mandatory) if getattr(protocol, key) is None or
               not (getattr(protocol, key).strip() if isinstance(getattr(protocol, key), str)
                    else getattr(protocol, key))]
    if missing:
        result.update(status="NOT_READY", reasons=[f"DRAFT_MISSING_{key.upper()}" for key in missing])
        return result
    try:
        world, binding, review = _fresh(world), _fresh(binding), _fresh(review)
    except (ValidationError, ValueError, TypeError):
        result["reasons"] = ["INVALID_SCIENTIFIC_INPUT"]
        return result
    current = admit_scientific_use(world, binding, review)
    if admission is None: admission = current
    if not receipt_matches_current(admission, world, binding, review):
        result["reasons"] = ["SCIENTIFIC_ADMISSION_REQUIRED"]
        return result
    try:
        result["protocol_sha256"] = record_hash(protocol)
        result["admission_sha256"] = current["receipt_sha256"]
        if (not protocol.scientific_question.strip() or not protocol.experiment_version.strip() or
            protocol.t0 != binding.origin or protocol.binding_sha256 != record_hash(binding) or
            protocol.code_hashes != implementation_hashes()):
            result["reasons"] = ["DRAFT_IDENTITY_OR_SCOPE"]
            return result
        dataset = next((d for d in world.observation_datasets if d.id == protocol.observation_dataset_id), None)
        if dataset is None or record_hash(dataset) != protocol.dataset_sha256:
            result["reasons"] = ["DATASET_CHANGED_OR_MISSING"]
            return result
        if set(protocol.data_roles) != DATA_ROLES:
            result.update(status="NOT_READY", reasons=["DATA_ROLES_UNDECLARED"])
            return result
        selected_ids = {dataset.id}
        for role in protocol.data_roles.values():
            if (role.state is None or not role.rationale.strip() or
                (role.state == "USED" and (not role.dataset_ids or not role.sample_ids))):
                result.update(status="NOT_READY", reasons=["DATA_ROLE_INCOMPLETE"])
                return result
            if (len(set(role.dataset_ids)) != len(role.dataset_ids) or
                len(set(role.sample_ids)) != len(role.sample_ids) or
                (role.state == "NOT_APPLICABLE" and (role.dataset_ids or role.sample_ids))):
                result["reasons"] = ["DATA_ROLE_CONFLICT"]
                return result
            selected_ids.update(role.dataset_ids)
        datasets = {d.id: d for d in world.observation_datasets}
        if (not selected_ids <= datasets.keys() or set(protocol.data_hashes) != selected_ids or
            any(protocol.data_hashes[key] != record_hash(datasets[key]) for key in selected_ids)):
            result["reasons"] = ["DATA_IDENTITY"]
            return result
        objects = {obj.id: obj for _, obj in _nested_models(world) if isinstance(obj, WorldObject)}
        first_origin = min(sample.origin for sample in protocol.samples)
        dependencies = {}
        pending = [(dataset.id, first_origin)]
        for name, role in protocol.data_roles.items():
            pending.extend((oid, first_origin if name == "validation" else protocol.t0)
                           for oid in role.dataset_ids)
        while pending:
            oid, required_at = pending.pop()
            if oid in dependencies and dependencies[oid] <= required_at: continue
            dependencies[oid] = required_at
            obj = objects.get(oid)
            if obj is None: continue
            pending.extend((ref, required_at) for ref in obj.provenance.inputs)
            if isinstance(obj, ObservationDataset):
                pending.extend((ref, protocol.t0) for ref in (obj.system_id, *obj.spatial_nodes))
            elif isinstance(obj, ObservationSystem) and obj.crs_id:
                pending.append((obj.crs_id, protocol.t0))
        if any(dependencies.keys() & set(unknown.blocks) for unknown in world.unknowns):
            result.update(status="NOT_READY", reasons=["SELECTED_DATASET_BLOCKED_BY_UNKNOWN"])
            return result
        for oid, required_at in dependencies.items():
            obj = objects.get(oid)
            if obj is None:
                formula_review = review.formula_reviews.get(oid)
                math_record = next((record for record in world.math_models if record.model_id == oid), None)
                if formula_review is not None and math_record is not None:
                    evidence_ids = {value.strip() for value in str(getattr(math_record, "vn_ids", "")).split(";")
                                    if value.strip()}
                    if (formula_review.record_sha256 != record_hash(math_record) or
                        formula_review.evidence_id not in review.registered_evidence or
                        formula_review.evidence_id not in evidence_ids):
                        result["reasons"] = ["DATA_DEPENDENCY_FORMULA_IDENTITY"]
                        return result
                support = formula_review.time if formula_review else TemporalSupport()
                supports, unknown = [support], formula_review is None
            else:
                nested = list(_nested_models(obj))
                supports = [item for _, item in nested if isinstance(item, TemporalSupport)]
                unknown = any(item.status is EpistemicStatus.UNKNOWN for _, item in nested if isinstance(item, Provenance))
            availability = [support.usable_at(required_at) for support in supports]
            if unknown or not availability or any(value is None for value in availability):
                result.update(status="NOT_READY", reasons=["DATA_DEPENDENCY_AVAILABILITY_UNKNOWN"])
                return result
            if not all(availability):
                result["reasons"] = ["DATA_DEPENDENCY_UNAVAILABLE"]
                return result
        def lineage(ids):
            seen, tokens, pending = set(), set(), list(ids)
            while pending:
                oid = pending.pop()
                if oid in seen: continue
                seen.add(oid)
                tokens.add(f"object:{oid}")
                obj = objects.get(oid)
                if obj is None: continue
                pending.extend(obj.provenance.inputs)
                for source in obj.provenance.sources:
                    tokens.update(f"evidence:{ref}" for ref in source.evidence_ids)
                    tokens.add(("source", source.source_id, source.locator, source.pdf_page, source.printed_page))
            return tokens
        used = {name: role for name, role in protocol.data_roles.items() if role.state == "USED"}
        validation_role = used.get("validation")
        if (validation_role is None or dataset.id not in validation_role.dataset_ids or
            set(validation_role.sample_ids) != {sample.sample_id for sample in protocol.samples}):
            result["reasons"] = ["DRAFT_VALIDATION_ROLE"]
            return result
        assert_disjoint_sample_sets({name: role.sample_ids for name, role in used.items()})
        lineages = {name: lineage(role.dataset_ids) for name, role in used.items()}
        names = list(lineages)
        if any(lineages[a] & lineages[b] for i, a in enumerate(names) for b in names[i+1:]):
            result["reasons"] = ["DATA_LINEAGE_OVERLAP"]
            return result
        if (not protocol.samples or len({s.sample_id for s in protocol.samples}) != len(protocol.samples) or
            not protocol.feature_fields or len(set(protocol.feature_fields)) != len(protocol.feature_fields)):
            result["reasons"] = ["DRAFT_INPUTS_INCOMPLETE"]
            return result
        if (not protocol.metrics or len(set(protocol.metrics)) != len(protocol.metrics) or
            not set(protocol.metrics) <= SUPPORTED_METRICS.keys() or
            set(protocol.acceptance_limits) != set(protocol.metrics) or
            any(value < 0 for value in protocol.acceptance_limits.values())):
            result["reasons"] = ["METRIC_CONTRACT"]
            return result
        assert_feature_fields_safe(protocol.feature_fields)
        sources = {role: next(m.quantity.provenance.temporal for m in world.materials if m.id == mid)
                   for role, mid in binding.parameter_ids.items()}
        sources.update({key: quantity.provenance.temporal for key, quantity in binding.conditions.items()})
        if not set(protocol.feature_fields) <= sources.keys() | PLANNED_INFORMATION_FIELDS:
            result["reasons"] = ["FEATURE_SOURCE_MISSING"]
            return result
        origins = [s.origin for s in protocol.samples]
        targets = [s.target for s in protocol.samples]
        horizons = [s.horizon_days for s in protocol.samples]
        assert_positive_horizon(horizons)
        assert_time_alignment(origins, targets, horizons)
        duration = binding.conditions["duration"]
        duration_bounds, duration_unit = _bounds(duration), lookup(duration.unit)
        allowed_seconds = tuple(value * duration_unit.factor for value in duration_bounds)
        discrete_seconds = tuple(value * duration_unit.factor for value in duration.uncertainty.values)
        for sample in protocol.samples:
            if (sample.origin < protocol.t0 or sample.target_available_from < sample.target or
                (protocol.sample_origin_policy == "SINGLE_T0" and sample.origin != protocol.t0)):
                result["reasons"] = ["DRAFT_TIME_CONTRACT"]
                return result
            horizon_seconds = sample.horizon_days * 86400
            if (not allowed_seconds[0] <= horizon_seconds <= allowed_seconds[1] or
                (discrete_seconds and horizon_seconds not in discrete_seconds)):
                result["reasons"] = ["HORIZON_OUTSIDE_ADMITTED_DURATION"]
                return result
            assert_available_at({"dataset": dataset.time, "dataset_source": dataset.provenance.temporal,
                                 **{key: sources[key] for key in protocol.feature_fields
                                                            if key in sources}}, sample.origin)
            assert_planned_target(sample.origin, sample.target_campaign_id, protocol.schedule, steps=protocol.horizon_steps)
            planned_date = dict(protocol.schedule)[sample.target_campaign_id]
            if planned_date != sample.target:
                result["reasons"] = ["PLANNED_TARGET_DATE"]
                return result
        splitter = validate_splitter_name(protocol.splitter)
        if splitter in FORWARD_ONLY_SPLITTERS:
            folds = rolling_origin_assignments({s.sample_id: s.origin for s in protocol.samples},
                {s.sample_id: s.target_available_from for s in protocol.samples},
                minimum_train_dates=protocol.minimum_train_dates, sealed=protocol.data_roles["test"].sample_ids)
        elif splitter == "leaveonelineout":
            folds = leave_one_line_out({s.sample_id: s.group for s in protocol.samples},
                                      sealed=protocol.data_roles["test"].sample_ids)
        else:
            result["reasons"] = ["DRAFT_SPLITTER_UNSUPPORTED"]
            return result
        try:
            assert_forward_only(folds, {sample.sample_id: sample.origin for sample in protocol.samples},
                                {sample.sample_id: sample.target_available_from for sample in protocol.samples})
        except UnsafeSplitError:
            result["reasons"] = ["TRAINING_LABEL_TEMPORAL_LEAKAGE"]
            return result
        roles = defaultdict(lambda: defaultdict(list))
        for fold in folds:
            roles[fold.fold_id][fold.role].append(fold.sample_id)
        for split in roles.values(): assert_disjoint_sample_sets(split)
        result.update(status="READY_DRAFT", fold_count=len(roles), sample_count=len(protocol.samples),
                      split_manifest_sha256=record_hash([fold.__dict__ for fold in folds]))
    except (ValidationError, ValueError, TypeError, StopIteration):
        # Exceptions may embed private numbers, ids and paths. Publish a stable aggregate code only.
        result["reasons"] = ["INVALID_DRAFT_CONTRACT"]
    return result
