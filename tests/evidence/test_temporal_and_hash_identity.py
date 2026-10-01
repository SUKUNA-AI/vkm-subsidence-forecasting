"""Regressions for forecast availability, exact pins and process-independent hashes."""
from datetime import date, datetime, timezone
import os
from pathlib import Path
import subprocess
import sys

import pytest

from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_evidence.contracts import Claim, Entity, EvidenceBatch, FormulaInterpretation, ObjectRef, ReviewDecision
from vkm_evidence.coverage import ExtractionAttempt
from vkm_evidence.journal import EvidenceJournal
from vkm_evidence.objects import ObjectCatalogue, OriginalObject
from vkm_evidence.query import EvidenceReader
from vkm_evidence.temporal import available_latest, temporal_conflicts, usable_at

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
POLICY = ResourcePolicy(access_class="PUBLIC", policy_version="1", authority="owner")
CONTEXT = AccessContext(principal="owner", execution="LOCAL")
REF = ObjectRef(source_id="source-1", source_sha256="a" * 64, snapshot_id="synthetic",
    object_id="object-1", object_version="1", content_sha256="a" * 64, locator="page:1", extraction_generation="1")


def entity(rid, *, available="2026-10-01", **kwargs):
    return Entity(record_id=rid, recorded_at=NOW, actor="owner", policy=POLICY, supports=(REF,),
        entity_type="MINE", label="synthetic", time={"available_from": available, "precision": "day"}, **kwargs)


class JournalView:
    revision = "a" * 64

    def __init__(self, *records):
        self.items = {r.record_id: r for r in records}

    def records(self, revision):
        assert revision == self.revision
        return self.items


@pytest.mark.parametrize("field", ["references", "depends_on"])
@pytest.mark.parametrize("available", [None, "2026-10-02"])
def test_as_of_closes_transitive_future_or_unknown_dependencies(field, available):
    parent = entity("parent", available=available)
    child = entity("child", **{field: (parent.version_ref,)})
    grandchild = entity("grandchild", references=(child.version_ref,))
    reader = EvidenceReader(JournalView(parent, child, grandchild))
    assert reader.page(CONTEXT, as_of=date(2026, 10, 1))["total_permitted"] == 0
    assert reader.page(CONTEXT)["total_permitted"] == 3


def test_as_of_stale_pin_and_review_target_cannot_survive_parent_filter():
    parent = entity("parent")
    child = entity("child", references=(parent.version_ref,))
    corrected = parent.model_copy(update={"label": "corrected", "revision": 2, "supersedes": parent.version_ref})
    review = ReviewDecision(record_id="review", recorded_at=NOW, actor="owner", policy=POLICY,
        target=child.version_ref, decision="CONFLICT", reviewer_authority="owner", rationale="synthetic",
        time={"available_from": "2026-10-01", "precision": "day"})
    reader = EvidenceReader(JournalView(corrected, child, review))
    page = reader.page(CONTEXT, as_of=date(2026, 10, 1))
    assert [r["record_id"] for r in page["items"]] == ["parent"]


def test_empty_evidence_time_uses_stated_worldspec_time_without_ingestion_inference():
    claim = Claim(record_id="claim", recorded_at=NOW, actor="owner", policy=POLICY, supports=(REF,),
        proposition="synthetic", attribution="source", polarity="AFFIRMED", modality="REPORTED",
        provenance={"status": "UNKNOWN", "temporal": {"available_from": "1999-01-01", "precision": "year"}})
    assert available_latest(claim) == date(1999, 12, 31)
    assert usable_at(claim, date(1999, 1, 1)) is False
    assert usable_at(claim, date(2000, 1, 1)) is True
    unknown = claim.model_copy(deep=True)
    unknown.provenance.temporal.available_from = None
    assert usable_at(unknown, NOW.date()) is None


def test_conflicting_explicit_times_close_known_at_view():
    claim = Claim(record_id="claim", recorded_at=NOW, actor="owner", policy=POLICY, supports=(REF,),
        proposition="synthetic", attribution="source", polarity="AFFIRMED", modality="REPORTED",
        time={"available_from": "2026-10-01", "precision": "day"},
        provenance={"status": "UNKNOWN", "temporal": {"available_from": "2026-10-02", "precision": "day"}})
    assert temporal_conflicts(claim) == ["CONFLICTING_AVAILABLE_FROM"]
    assert usable_at(claim, date(2026, 10, 3)) is None
    assert EvidenceReader(JournalView(claim)).page(CONTEXT, as_of=date(2026, 10, 3))["items"] == []


def test_compatible_year_and_day_keep_conservative_availability():
    claim = Claim(record_id="claim", recorded_at=NOW, actor="owner", policy=POLICY, supports=(REF,),
        proposition="synthetic", attribution="source", polarity="AFFIRMED", modality="REPORTED",
        time={"available_from": "2026-01-01", "precision": "year"},
        provenance={"status": "UNKNOWN", "temporal": {"available_from": "2026-05-15", "precision": "day"}})
    assert temporal_conflicts(claim) == []
    assert available_latest(claim) == date(2026, 12, 31)
    assert usable_at(claim, date(2026, 10, 1)) is False
    assert usable_at(claim, date(2027, 1, 1)) is True


def test_worldspec_available_year_can_follow_known_measurement_day():
    from vkm_world.core.provenance import TemporalSupport
    time = TemporalSupport(measurement_date="2026-05-15", precision="day", available_from="2026-01-01",
        available_from_precision="year")
    assert time.available_latest() == date(2026, 12, 31)
    assert time.usable_at(date(2026, 10, 1)) is False
    with pytest.raises(ValueError, match="precedes"):
        TemporalSupport(measurement_date="2027-05-15", precision="day", available_from="2026-01-01",
            available_from_precision="year")


def test_availability_cannot_precede_measurement_in_another_provenance_layer():
    claim = Claim(record_id="claim", recorded_at=NOW, actor="owner", policy=POLICY, supports=(REF,),
        proposition="synthetic", attribution="source", polarity="AFFIRMED", modality="REPORTED",
        time={"available_from": "2026-10-01", "precision": "day"},
        provenance={"status": "UNKNOWN", "temporal": {"measurement_date": "2026-10-02", "precision": "day"}})
    assert "AVAILABILITY_PRECEDES_MEASUREMENT_DATE" in temporal_conflicts(claim)
    assert usable_at(claim, date(2026, 10, 3)) is None


def test_context_hash_is_stable_in_independent_hash_seed_processes():
    source = Path(__file__).resolve().parents[2] / "src"
    program = ("from vkm_corpus.contracts.access import AccessContext; "
        "from vkm_evidence.contracts import record_hash; "
        "print(record_hash(AccessContext(principal='synthetic',execution='LOCAL',"
        "granted_classes={'PUBLIC','PRIVATE_CLOUD_ALLOWED','PRIVATE_LOCAL_ONLY','RESTRICTED','SEALED'})))")
    hashes = []
    for seed in ("1", "2", "3", "4"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(source)}
        hashes.append(subprocess.run([sys.executable, "-c", program], env=env, check=True,
            capture_output=True, text=True, timeout=15).stdout.strip())
    assert len(set(hashes)) == 1


def test_durable_coverage_hash_must_prove_the_claimed_object_output():
    with pytest.raises(ValueError, match="absent from durable outputs"):
        ExtractionAttempt(attempt_id="a", unit_id="p", stage="synthetic", input_sha256="a" * 64,
            config_sha256="a" * 64, code_revision="a" * 64, state="SUCCEEDED", outputs=("a" * 64,),
            object_outputs={"object": "b" * 64})


def test_formula_review_must_include_original_symbol_definitions(tmp_path):
    definition = REF.model_copy(update={"object_id": "symbol-definition", "locator": "page:2"})
    catalogue = ObjectCatalogue((OriginalObject(REF, POLICY), OriginalObject(definition, POLICY)))
    journal = EvidenceJournal(tmp_path / "journal", object_validator=catalogue.validate, reviewers=frozenset({"owner"}))
    formula = FormulaInterpretation(record_id="formula", recorded_at=NOW, actor="owner", policy=POLICY,
        supports=(REF,), original_form="x=1", representation="TEXT", parse_state="PARSED", normalized_form="x=1",
        symbols=({"symbol": "x", "definition": "synthetic", "scope": "equation-1", "supports": (definition,)},))
    journal.publish("formula", journal.revision, EvidenceBatch(records=(formula,)), CONTEXT)
    review = ReviewDecision(record_id="review", recorded_at=NOW, actor="owner", policy=POLICY, supports=(REF,),
        target=formula.version_ref, decision="SEMANTIC_REVIEWED", source_verified=True,
        reviewer_authority="owner", rationale="synthetic", checks=("identity",))
    with pytest.raises(ValueError, match="support"):
        journal.publish("review", journal.revision, EvidenceBatch(records=(review,)), CONTEXT)
