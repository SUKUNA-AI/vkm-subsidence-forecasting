"""Conservative origin, dependency and original-review admission gates."""
from __future__ import annotations

from datetime import date
import re
from typing import Mapping

from vkm_corpus.contracts.access import AccessContext
from vkm_evidence.contracts import (EvidenceRecord, ReviewDecision, ScientificUseAdmission, EvidenceTransfer,
                                    VersionRef, record_hash)


def version_references(record):
    refs = [*record.depends_on, *record.references]
    if isinstance(record, (ReviewDecision, EvidenceTransfer)):
        refs.append(record.target)
    if isinstance(record, ScientificUseAdmission):
        refs.extend((*record.targets, *record.dependency_versions, *record.review_versions))
        if record.use_context is not None:
            for binding in record.use_context.transfers:
                refs.extend((binding.target, binding.transfer_record))
    return tuple(refs)


def semantic_references(record: EvidenceRecord) -> set[str]:
    ids = {r.record_id for r in version_references(record)}
    for field in ("entity_id", "subject", "object"):
        if value := getattr(record, field, None):
            ids.add(value)
    for field in ("observation_ids", "participants", "entity_candidates", "conflicting_versions", "revealed_by"):
        ids.update(getattr(record, field, ()))
    if isinstance(record, ReviewDecision):
        ids.add(record.target.record_id)
    return ids


def validate_reference_graph(records: Mapping[str, EvidenceRecord]) -> None:
    """Only computational DERIVED_FROM edges and depends_on must be acyclic.

    Citation/support/conflict graphs can have cycles. Logical references must also
    have a version pin in depends_on, so re-extraction cannot silently retarget them.
    """
    edges = {}
    for rid, record in records.items():
        pinned = {r.record_id for r in version_references(record)}
        needed = semantic_references(record)
        if not needed.issubset(records):
            raise ValueError("dangling evidence reference")
        if not needed.issubset(pinned):
            raise ValueError("semantic reference lacks exact dependency version")
        edges[rid] = [r.record_id for r in record.depends_on]
    visiting, visited = set(), set()
    def visit(rid):
        if rid in visiting:
            raise ValueError("cyclic computational provenance")
        if rid in visited:
            return
        visiting.add(rid)
        for child in edges[rid]:
            visit(child)
        visiting.remove(rid)
        visited.add(rid)
    for rid in edges:
        visit(rid)


def dependency_closure(targets: tuple[VersionRef, ...], records: Mapping[str, EvidenceRecord]) -> tuple[dict, list[str]]:
    closure, reasons, todo = {}, [], list(targets)
    while todo:
        ref = todo.pop()
        record = records.get(ref.record_id)
        if record is None or record.version_ref != ref:
            reasons.append("STALE_OR_MISSING:" + ref.record_id)
            continue
        if ref.record_id in closure:
            continue
        closure[ref.record_id] = record
        # Entity bindings, occurrence lineage and symbol associations also affect
        # scientific meaning, even when they are not computation DAG edges.
        todo.extend(version_references(record))
    return closure, reasons


def lineage_overlap(left, right) -> str:
    """INDEPENDENT means reviewed distinct primary origins, never distinct papers."""
    if not left or not right or not all(o.verified for o in (*left, *right)):
        return "UNKNOWN"
    shared_origin = False
    for a in left:
        for b in right:
            if a.origin_id == b.origin_id:
                shared_origin = True
                if not a.members or not b.members or set(a.members) & set(b.members):
                    return "OVERLAP"
    return "DISJOINT_SHARED_ORIGIN" if shared_origin else "INDEPENDENT"


def policy_hash(records: Mapping[str, EvidenceRecord]) -> str:
    return record_hash({rid: r.policy.model_dump(mode="json") for rid, r in sorted(records.items())})


def _support_pdf_page(support) -> int | None:
    """Use explicit physical-page identities only, never invent pages for native XML."""
    candidates = []
    if match := re.search(r":p([0-9]+)(?=:|$)", support.object_id):
        candidates.append(int(match[1]))
    if match := re.match(r"^page:([0-9]+)(?=[:/]|$)", support.locator):
        candidates.append(int(match[1]))
    return candidates[0] if candidates and candidates[0] > 0 and len(set(candidates)) == 1 else None


def quantity_admission_errors(record: EvidenceRecord, records: Mapping[str, EvidenceRecord]) -> list[str]:
    """Structural numeric-use gates, in addition to an authorized semantic review.

    Faithful archival UNKNOWN, unresolved units and historical unbound provenance
    remain valid records. They do not become numerically usable through review
    alone. This checks source-context use; no target site/scale is inferred from
    ScientificUseAdmission.purpose or silently added to the quantity. Requested
    site/scale application is checked separately by scientific_context_errors.
    """
    from vkm_world.core.provenance import EpistemicStatus, UncertaintyKind, quantity_errors
    from vkm_world.core.units import check_unit
    q = getattr(record, "quantity", None)
    if q is None:
        return []
    errors = []
    explicit_set = bool(q.uncertainty.values) and q.uncertainty.kind == UncertaintyKind.DISCRETE_SET
    has_numeric = q.value is not None or (q.low is not None and q.high is not None) or explicit_set
    if q.provenance.status == EpistemicStatus.UNKNOWN or not has_numeric:
        errors.append("QUANTITY_VALUE_NOT_ESTABLISHED")
    if quantity_errors(q):
        errors.append("QUANTITY_INVALID")
    # Ambiguous source units also need explicit interpretation; do not turn an
    # archival QA warning into a conversion assumption during scientific use.
    if check_unit(q.name, q.unit):
        errors.append("QUANTITY_UNIT_UNRESOLVED")
    refs = version_references(record)
    pinned = {ref.record_id: records[ref.record_id] for ref in refs
              if ref.record_id in records and records[ref.record_id].version_ref == ref}
    def supports(parent):
        return (*parent.supports, *(s for binding in getattr(parent, "symbols", ()) for s in binding.supports))
    if any(item not in pinned for item in q.provenance.inputs):
        errors.append("QUANTITY_INPUT_NOT_PINNED")
    for source in q.provenance.sources:
        evidence = tuple(pinned.get(rid) for rid in source.evidence_ids)
        if any(parent is None for parent in evidence):
            errors.append("QUANTITY_EVIDENCE_NOT_PINNED")
        # A claimed evidence ID must actually carry original support from that
        # source; another dependency with unrelated content cannot certify it.
        evidence_supported = bool(evidence) and all(parent is not None and
            any(s.source_id == source.source_id for s in supports(parent)) for parent in evidence)
        candidates = tuple(s for s in supports(record) if s.source_id == source.source_id)
        candidates += tuple(s for parent in evidence if parent is not None
                            for s in supports(parent) if s.source_id == source.source_id)
        has_location = source.locator is not None or source.pdf_page is not None
        def matches(support):
            if source.pdf_page is not None and (source.pdf_page < 1 or _support_pdf_page(support) != source.pdf_page):
                return False
            if source.locator is not None:
                if not source.locator.strip():
                    return False
                if source.locator != support.locator:
                    page = re.fullmatch(r"page:([0-9]+)", source.locator)
                    if page is None or _support_pdf_page(support) != int(page[1]):
                        return False
            return True
        bound = any(matches(s) for s in candidates) if has_location else evidence_supported
        if not bound or (source.evidence_ids and not evidence_supported):
            errors.append("QUANTITY_SOURCE_NOT_BOUND")
    # Codes contain no number, unit literal, locator or source text.
    return sorted(set(errors))


def scientific_context_errors(admission, closure, records):
    from vkm_world.core.provenance import Scope, Scale, EpistemicStatus, EvidenceType, transfer_use_errors

    use = admission.use_context
    if use is None:
        return ["SCIENTIFIC_USE_CONTEXT_MISSING"]
    reasons, consumed = [], set()
    target_ids = {ref.record_id for ref in admission.targets}
    numeric_uses = {"MATERIAL_PARAMETER", "CALIBRATION_INPUT", "PREDICTION_INPUT", "BENCHMARK_INPUT", "SCENARIO_INPUT"}
    for ref in admission.targets:
        target = closure.get(ref.record_id)
        if target is None:
            continue  # the exact-version closure check reports the stale target
        qualified = use.use == "SOURCE_INTERPRETATION"
        if use.use == "IDENTITY":
            qualified = ((target.kind == "ENTITY" and target.identity_state == "RESOLVED") or
                (target.kind == "ENTITY_RESOLUTION" and target.decision in {"SAME_ENTITY", "DISTINCT", "SPLIT"}))
        elif use.use in numeric_uses:
            qualified = target.kind == "OBSERVATION" and target.quantity is not None
            if target.kind == "OBSERVATION_SET":
                pins = {p.record_id: p for p in version_references(target)}
                qualified = bool(target.observation_ids) and target.lineage_state == "VERIFIED"
                for rid in target.observation_ids:
                    child = closure.get(rid)
                    qualified = qualified and (child is not None and child.kind == "OBSERVATION" and
                        child.quantity is not None and child.version_ref == pins.get(rid))
        elif use.use == "VALIDATION_OBSERVATION":
            qualified = target.kind == "OBSERVATION" and target.quantity is not None
        elif use.use == "GEOMETRY_INPUT":
            # No current EvidenceRecord carries a typed WorldSpec geometry.
            # Numeric-looking text or one scalar must never stand in for it.
            qualified = False
        if not qualified:
            reasons.append("SCIENTIFIC_TARGET_KIND_NOT_QUALIFIED:" + target.record_id)
    bindings = {b.target.record_id: b for b in use.transfers}
    for rid, record in closure.items():
        quantity = getattr(record, "quantity", None)
        provenance = quantity.provenance if quantity is not None else getattr(record, "provenance", None)
        if use.use == "VALIDATION_OBSERVATION" and rid in target_ids:
            if (record.kind != "OBSERVATION" or provenance is None or
                    provenance.status != EpistemicStatus.FACT or
                    (provenance.scale, provenance.scope) != (use.scale, use.site) or
                    provenance.evidence_type not in {EvidenceType.MEASURED, EvidenceType.FIELD_OBSERVATION} or
                    record.policy.experimental_role != "VALIDATION"):
                reasons.append("SCIENTIFIC_VALIDATION_OBSERVATION_NOT_ESTABLISHED:" + rid)
        if provenance is not None:
            if provenance.status == EpistemicStatus.UNKNOWN:
                reasons.append("SCIENTIFIC_EPISTEMIC_STATUS_UNKNOWN:" + rid)
            if provenance.scope == Scope.UNSTATED or provenance.scale == Scale.UNSTATED:
                reasons.append("SCIENTIFIC_SOURCE_CONTEXT_UNKNOWN:" + rid)
            if use.use == "MATERIAL_PARAMETER" and use.scale == Scale.NOT_APPLICABLE:
                reasons.append("SCIENTIFIC_MATERIAL_SCALE_REQUIRED:" + rid)
            errors = transfer_use_errors(provenance, use.scale, use.site)
            reasons.extend(error + ":" + rid for error in errors)
            moved = (provenance.scale, provenance.scope) != (use.scale, use.site)
            if moved:
                binding = bindings.get(rid)
                transfer = records.get(binding.transfer_record.record_id) if binding else None
                if (binding is None or binding.target != record.version_ref or
                        not isinstance(transfer, EvidenceTransfer) or transfer.version_ref != binding.transfer_record or
                        transfer.target != record.version_ref or provenance.transfer is None or
                        record_hash(provenance.transfer) != binding.transfer_sha256 or
                        record_hash(transfer.transfer) != binding.transfer_sha256):
                    reasons.append("SCIENTIFIC_TRANSFER_NOT_PINNED:" + rid)
                else:
                    consumed.add(rid)
        elif record.kind == "ENTITY":
            if record.site_scope != use.site.value:
                reasons.append("SCIENTIFIC_ENTITY_SITE_MISMATCH:" + rid)
        elif record.kind == "FORMULA_INTERPRETATION" and use.use != "SOURCE_INTERPRETATION":
            # A parsed formula plus an application label does not establish its
            # physical validity at a site/scale. An application model needs its
            # own reviewed provenance; this record remains source interpretation.
            reasons.append("SCIENTIFIC_FORMULA_APPLICATION_NOT_ESTABLISHED:" + rid)
    if set(bindings) != consumed:
        reasons.append("SCIENTIFIC_TRANSFER_BINDING_UNUSED_OR_INVALID")
    return reasons


def validate_admission(admission: ScientificUseAdmission, records: Mapping[str, EvidenceRecord],
                       context: AccessContext) -> list[str]:
    transfer_refs = tuple(b.transfer_record for b in admission.use_context.transfers) if admission.use_context else ()
    closure, reasons = dependency_closure((*admission.targets, *transfer_refs), records)
    reasons.extend(scientific_context_errors(admission, closure, records))
    expected = {r.version_ref for r in closure.values()}
    if set(admission.dependency_versions) != expected:
        reasons.append("DEPENDENCY_CLOSURE_MISMATCH")
    if admission.policy_sha256 != policy_hash(closure):
        reasons.append("POLICY_VERSION_MISMATCH")
    reviews = []
    for ref in admission.review_versions:
        review = records.get(ref.record_id)
        if not isinstance(review, ReviewDecision) or review.version_ref != ref:
            reasons.append("STALE_REVIEW:" + ref.record_id)
        else:
            reviews.append(review)
    origin = date.fromisoformat(admission.origin)
    for rid, record in closure.items():
        if not record.policy.permits(context):
            reasons.append("ACCESS_DENIED:" + rid)
        # Only records with a temporal meaning need availability; unknown cannot
        # become available through today's ingestion date.
        from vkm_evidence.temporal import usable_at, temporal_conflicts
        reasons.extend(reason + ":" + rid for reason in temporal_conflicts(record))
        if usable_at(record, origin) is not True:
            reasons.append("AVAILABILITY_NOT_PROVEN:" + rid)
        # Mapping order is journal commit order, not the user-supplied timestamp.
        latest = [r for r in records.values() if isinstance(r, ReviewDecision) and r.target == record.version_ref]
        if not latest or latest[-1] not in reviews or latest[-1].decision != "SEMANTIC_REVIEWED":
            reasons.append("NO_CURRENT_SEMANTIC_REVIEW:" + rid)
        if getattr(record, "value_state", "VALUE") != "VALUE":
            reasons.append("NON_VALUE:" + rid)
        if record.kind == "OBSERVATION" and record.quantity is None:
            reasons.append("QUANTITY_NOT_INTERPRETED:" + rid)
        reasons.extend(reason + ":" + rid for reason in quantity_admission_errors(record, records))
        if record.kind in {"OBSERVATION", "OBSERVATION_SET"}:
            origins = record.origins
            if not origins or not all(o.verified for o in origins):
                reasons.append("PRIMARY_ORIGIN_NOT_PROVEN:" + rid)
        if getattr(record, "parse_state", "PARSED") != "PARSED":
            reasons.append("FORMULA_UNRESOLVED:" + rid)
        if getattr(record, "identity_state", "RESOLVED") != "RESOLVED":
            reasons.append("ENTITY_UNRESOLVED:" + rid)
        if record.review_state in {"CONFLICT", "REJECTED"}:
            reasons.append("UNRESOLVED:" + rid)
    return sorted(set(reasons))


def admission_state(admission: ScientificUseAdmission, records: Mapping[str, EvidenceRecord],
                    context: AccessContext) -> dict:
    reasons = validate_admission(admission, records, context)
    if admission.record_id in records and records[admission.record_id].version_ref != admission.version_ref:
        reasons.append("ADMISSION_VERSION_NOT_CURRENT")
    if admission.status != "READY":
        reasons.append(admission.status)
    return {"status": "READY" if not reasons else "NOT_READY", "reasons": sorted(set(reasons)),
            "purpose": admission.purpose, "record_sha256": record_hash(admission),
            "use_context": admission.use_context.model_dump(mode="json") if admission.use_context else None,
            "use_context_sha256": record_hash(admission.use_context) if admission.use_context else None,
            "admission_scope": "DECLARED_DATA_USE_ONLY", "field_validation": "NOT_ESTABLISHED"}
