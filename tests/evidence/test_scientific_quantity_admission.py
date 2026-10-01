"""Scientific numeric use is stricter than faithful archival transcription."""
from datetime import datetime, timezone

import pytest

from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_evidence.contracts import (Entity, EvidenceBatch, Observation, ObjectRef, ReviewDecision, ScientificUseAdmission)
from vkm_evidence.validation import admission_state, policy_hash
from vkm_world.core.provenance import Quantity, Scale, check_scale_use

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
POLICY = ResourcePolicy(access_class="PUBLIC", policy_version="1", authority="owner")
CONTEXT = AccessContext(principal="reviewer", execution="LOCAL")
BASE = dict(recorded_at=NOW, actor="reviewer", policy=POLICY)
REF = ObjectRef(source_id="source-1", source_sha256="a" * 64, snapshot_id="snap-1",
    object_id="source-1:p0001:b123456abcdef", object_version="1", content_sha256="b" * 64,
    locator="page:1:block:1", extraction_generation="1")
TIME = {"available_from": "2026-10-01", "precision": "day"}


def quantity(**changes):
    value = dict(name="subsidence", unit="m", value=12,
        provenance={"status": "FACT", "scope": "SKRU1", "scale": "FIELD",
                    "sources": ({"source_id": "source-1", "pdf_page": 1},)})
    value.update(changes)
    return Quantity.model_validate(value)


def parent(support=REF):
    return Entity(**BASE, record_id="parent", entity_type="MEASUREMENT", label="synthetic",
                  identity_state="RESOLVED", site_scope="SKRU1", time=TIME, supports=(support,))


def assess(q, *, support=REF, parents=(), computational=False, use_context=None):
    edges = {"depends_on" if computational else "references": tuple(p.version_ref for p in parents)}
    obs = Observation(**BASE, record_id="observation", original_value="synthetic original", value_state="VALUE",
        quantity=q, supports=(support,), time=TIME, origins=({"origin_id": "synthetic-primary",
            "verified": True, "independence_basis": "synthetic original review"},), **edges)
    closure = {r.record_id: r for r in (*parents, obs)}
    reviews = tuple(ReviewDecision(**BASE, record_id="review-" + r.record_id, target=r.version_ref,
        decision="SEMANTIC_REVIEWED", source_verified=True, supports=r.supports, reviewer_authority="reviewer",
        rationale="synthetic configured reviewer", checks=("identity",)) for r in closure.values())
    admission = ScientificUseAdmission(**BASE, record_id="admission", purpose="scientific numeric use",
        use_context=use_context or {"use": "SOURCE_INTERPRETATION", "site": "SKRU1", "scale": "FIELD"},
        origin="2026-10-01", targets=(obs.version_ref,), dependency_versions=tuple(r.version_ref for r in closure.values()),
        review_versions=tuple(r.version_ref for r in reviews), status="READY", policy_sha256=policy_hash(closure))
    records = {**closure, **{r.record_id: r for r in reviews}}
    return admission_state(admission, records, CONTEXT), obs


@pytest.mark.parametrize("unit,name", [("UNKNOWN", "subsidence"), ("", "subsidence"),
    ("m", "youngs_modulus"), ("t/m3", "density")])
def test_unknown_missing_or_incompatible_units_close_numeric_admission(unit, name):
    state, _ = assess(quantity(unit=unit, name=name))
    assert state["status"] == "NOT_READY"
    assert "QUANTITY_UNIT_UNRESOLVED:observation" in state["reasons"]


def test_ambiguous_original_unit_needs_explicit_resolution_before_numeric_use():
    state, _ = assess(quantity(unit="т/м3", name="density"))
    assert state["status"] == "NOT_READY"
    assert "QUANTITY_UNIT_UNRESOLVED:observation" in state["reasons"]


def test_unknown_quantity_remains_valid_archival_record_but_not_numeric_ready():
    q = quantity(value=None, provenance={"status": "UNKNOWN"})
    state, obs = assess(q)
    assert obs.quantity.provenance.status == "UNKNOWN" and obs.quantity.value is None
    assert state["status"] == "NOT_READY"
    assert "QUANTITY_VALUE_NOT_ESTABLISHED:observation" in state["reasons"]


def test_half_interval_is_not_a_complete_numeric_payload():
    state, _ = assess(quantity(value=None, low=10))
    assert state["status"] == "NOT_READY"
    assert "QUANTITY_VALUE_NOT_ESTABLISHED:observation" in state["reasons"]


@pytest.mark.parametrize("changes", [{}, {"value": None, "low": 10, "high": 20},
    {"value": None, "uncertainty": {"kind": "DISCRETE_SET", "values": (10, 20)}}])
def test_explicit_point_interval_and_set_with_matching_source_are_ready(changes):
    state, _ = assess(quantity(**changes))
    assert state["status"] == "READY", state


@pytest.mark.parametrize("source", [{"source_id": "uninspected", "pdf_page": 1},
    {"source_id": "source-1", "pdf_page": 999}, {"source_id": "source-1"},
    {"source_id": "source-1", "locator": "uninspected fragment"},
    {"source_id": "source-1", "locator": REF.locator, "pdf_page": 999}])
def test_quantity_source_must_bind_inspected_source_and_specific_location(source):
    p = quantity().provenance.model_dump()
    p["sources"] = (source,)
    state, _ = assess(quantity(provenance=p))
    assert state["status"] == "NOT_READY"
    assert "QUANTITY_SOURCE_NOT_BOUND:observation" in state["reasons"]


def test_native_xml_locator_remains_exact_and_never_gets_fake_page():
    ref = REF.model_copy(update={"object_id": "native-object", "locator": "body/tbl[1]/tr[2]/tc[3]"})
    p = quantity().provenance.model_dump()
    p["sources"] = ({"source_id": ref.source_id, "locator": ref.locator},)
    assert assess(quantity(provenance=p), support=ref)[0]["status"] == "READY"
    p["sources"] = ({"source_id": ref.source_id, "pdf_page": 1},)
    assert assess(quantity(provenance=p), support=ref)[0]["status"] == "NOT_READY"


def test_fake_fact_input_cannot_replace_missing_sources():
    state, _ = assess(quantity(provenance={"status": "FACT", "inputs": ("unbound-input",)}))
    assert state["status"] == "NOT_READY"
    assert "QUANTITY_INPUT_NOT_PINNED:observation" in state["reasons"]


def test_fact_input_requires_exact_dependency_pin():
    state, _ = assess(quantity(provenance={"status": "FACT", "scope": "SKRU1", "scale": "FIELD", "inputs": ("parent",)}),
                      parents=(parent(),), computational=True)
    assert state["status"] == "READY", state


def test_explicit_evidence_version_binds_locatorless_source():
    p = quantity().provenance.model_dump()
    p["sources"] = ({"source_id": "source-1", "evidence_ids": ("parent",)},)
    assert assess(quantity(provenance=p), parents=(parent(),))[0]["status"] == "READY"
    state, _ = assess(quantity(provenance=p))
    assert "QUANTITY_EVIDENCE_NOT_PINNED:observation" in state["reasons"]


def test_pinned_evidence_must_actually_support_the_declared_source():
    other = REF.model_copy(update={"source_id": "different-source"})
    p = quantity().provenance.model_dump()
    p["sources"] = ({"source_id": "source-1", "evidence_ids": ("parent",)},)
    state, _ = assess(quantity(provenance=p), parents=(parent(other),))
    assert "QUANTITY_SOURCE_NOT_BOUND:observation" in state["reasons"]


def test_admission_never_promotes_lab_scale_into_massif():
    p = quantity().provenance.model_dump()
    p["scale"] = "LAB"
    state, obs = assess(quantity(name="youngs_modulus", unit="MPa", provenance=p),
        use_context={"use": "SOURCE_INTERPRETATION", "site": "SKRU1", "scale": "LAB"})
    assert state["status"] == "READY"  # explicit original LAB context only
    assert obs.quantity.provenance.scale == Scale.LAB
    assert check_scale_use(obs.quantity, Scale.MASSIF)


@pytest.mark.parametrize("variant", ["unknown", "unit", "source", "valid"])
def test_real_publisher_does_not_commit_numeric_false_ready(tmp_path, variant):
    pytest.importorskip("pyarrow")
    from vkm_evidence.journal import EvidenceJournal
    from vkm_evidence.objects import ObjectCatalogue, OriginalObject
    from vkm_evidence.query import EvidenceReader
    q = quantity()
    if variant == "unknown":
        q = quantity(value=None, provenance={"status": "UNKNOWN"})
    elif variant == "unit":
        q = quantity(unit="UNKNOWN")
    elif variant == "source":
        q = quantity(provenance={"status": "FACT", "sources": ({"source_id": "uninspected", "pdf_page": 99},)})
    _, obs = assess(q)
    catalogue = ObjectCatalogue((OriginalObject(REF, POLICY, "synthetic only"),))
    journal = EvidenceJournal(tmp_path / "journal", object_validator=catalogue.validate, reviewers=frozenset({"reviewer"}))
    journal.publish("observation", journal.revision, EvidenceBatch(records=(obs,)), CONTEXT)
    review = ReviewDecision(**BASE, record_id="review", target=obs.version_ref, supports=(REF,),
        decision="SEMANTIC_REVIEWED", source_verified=True, reviewer_authority="reviewer",
        rationale="synthetic configured reviewer", checks=("identity",))
    journal.publish("review", journal.revision, EvidenceBatch(records=(review,)), CONTEXT)
    admission = ScientificUseAdmission(**BASE, record_id="admission", purpose="numeric source-context use",
        use_context={"use": "SOURCE_INTERPRETATION", "site": "SKRU1", "scale": "FIELD"},
        origin="2026-10-01", targets=(obs.version_ref,), dependency_versions=(obs.version_ref,),
        review_versions=(review.version_ref,), status="READY", policy_sha256=policy_hash({obs.record_id: obs}))
    if variant == "valid":
        journal.publish("admission", journal.revision, EvidenceBatch(records=(admission,)), CONTEXT)
        reader = EvidenceReader(journal, source_policy=lambda _: POLICY)
        assert reader.get("admission", CONTEXT)["current_admission"]["status"] == "READY"
    else:
        with pytest.raises(ValueError, match="QUANTITY_"):
            journal.publish("admission", journal.revision, EvidenceBatch(records=(admission,)), CONTEXT)
        assert len(journal.commits()) == 2 and "admission" not in journal.records()
