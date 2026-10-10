from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_evidence.contracts import (Entity, EvidenceBatch, ObjectRef, OriginSlice, ReviewDecision,
                                    ScientificUseAdmission, record_hash)
from vkm_evidence.coverage import CoverageLedger
from vkm_evidence.journal import EvidenceJournal, JournalConflict, JournalCorruption, ZERO
from vkm_evidence.objects import ObjectCatalogue, OriginalObject
from vkm_evidence.query import EvidenceReader
from vkm_evidence.validation import admission_state, lineage_overlap, policy_hash

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
SHA = "a" * 64
POLICY = ResourcePolicy(access_class="PUBLIC", experimental_role="INPUT", policy_version="p1", authority="owner")
CTX = AccessContext(principal="reviewer", execution="LOCAL", granted_classes={"PUBLIC"})
REF = ObjectRef(source_id="VKM-SRC-001", source_sha256=SHA, snapshot_id="snap-1",
                object_id="VKM-SRC-001:p0001:b123456abcdef", object_version="v1", content_sha256=SHA,
                locator="page:1:block:1", extraction_generation="1")


@pytest.fixture
def journal(tmp_path):
    catalogue = ObjectCatalogue((OriginalObject(REF, POLICY, "Исходный текст"),))
    return EvidenceJournal(tmp_path / "evidence", object_validator=catalogue.validate, reviewers=frozenset({"reviewer"}))


def entity(rid="entity-1", **kw):
    return Entity(record_id=rid, recorded_at=NOW, actor=CTX.principal, policy=POLICY, supports=(REF,),
                  time={"available_from": "2026-10-01", "precision": "day"},
                  entity_type="MINE", label="Синтетический рудник", identity_state="RESOLVED", site_scope="SKRU1", **kw)


def publish(journal, *records, request="request-1"):
    return journal.publish(request, journal.revision, EvidenceBatch(records=records), CTX)


def test_ack_loss_is_idempotent_and_changed_payload_rejected(journal):
    e = entity()
    batch = EvidenceBatch(records=(e,))
    with pytest.raises(RuntimeError, match="lost ack"):
        journal.publish("req", ZERO, batch, CTX, after_commit=lambda: (_ for _ in ()).throw(RuntimeError("lost ack")))
    receipt = journal.publish("req", ZERO, batch, CTX)
    assert receipt["revision"] == journal.revision and len(journal.commits()) == 1
    with pytest.raises(JournalConflict, match="different content"):
        journal.publish("req", ZERO, EvidenceBatch(records=(entity("other"),)), CTX)


def test_corrections_preserve_history_and_stale_base_fails(journal):
    e = entity()
    first = publish(journal, e)
    correction = entity(revision=2, supersedes=e.version_ref).model_copy(update={"label": "Исправленный рудник"})
    second = publish(journal, correction, request="correct")
    assert journal.records(first["revision"])[e.record_id].label == e.label
    assert journal.records()[e.record_id].revision == 2
    with pytest.raises(JournalConflict, match="stale base"):
        journal.publish("stale", first["revision"], EvidenceBatch(records=(entity("new"),)), CTX)
    assert journal.revision == second["revision"]


def test_original_locator_and_span_are_authoritative(journal):
    e = entity()
    wrong = e.model_copy(update={"supports": (REF.model_copy(update={"locator": "invented"}),)})
    with pytest.raises(ValueError, match="identity mismatch"):
        publish(journal, wrong)
    text = "Исходный текст"
    ref = REF.model_copy(update={"char_start": 0, "char_end": 8,
                      "fragment_sha256": hashlib.sha256(text[:8].encode()).hexdigest()})
    publish(journal, e.model_copy(update={"supports": (ref,)}))


def test_access_cannot_be_widened_or_inferred_from_role(tmp_path):
    private = POLICY.model_copy(update={"access_class": "PRIVATE_CLOUD_ALLOWED"})
    catalogue = ObjectCatalogue((OriginalObject(REF, private),))
    j = EvidenceJournal(tmp_path, object_validator=catalogue.validate)
    ctx = CTX.model_copy(update={"granted_classes": frozenset({"PUBLIC", "PRIVATE_CLOUD_ALLOWED"})})
    with pytest.raises(PermissionError, match="widens source"):
        j.publish("req", ZERO, EvidenceBatch(records=(entity(),)), ctx)
    assert not private.model_copy(update={"experimental_role": "TARGET"}).permits(ctx)


def test_tampered_partition_is_not_read(journal):
    receipt = publish(journal, entity())
    path = journal.root / "partitions" / (receipt["partition_sha256"] + ".parquet")
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(JournalCorruption, match="partition hash"):
        journal.records()


def test_single_writer_and_eight_concurrent_reads(journal):
    records = tuple(entity(f"entity-{i:03}") for i in range(80))
    publish(journal, *records)
    reader = EvidenceReader(journal)
    with ThreadPoolExecutor(max_workers=8) as pool:
        pages = list(pool.map(lambda _: reader.page(CTX, limit=10), range(8)))
    assert all(p["total_permitted"] == 80 and len(p["items"]) == 10 for p in pages)
    with journal._writer():
        with pytest.raises(JournalConflict, match="publisher busy"):
            publish(journal, entity("busy"), request="busy")


def test_cursor_full_traversal_stale_policy_and_generation(journal):
    publish(journal, *(entity(f"entity-{i:03}") for i in range(31)))
    reader, seen, cursor = EvidenceReader(journal), [], None
    while True:
        page = reader.page(CTX, limit=7, cursor=cursor)
        seen.extend(r["record_id"] for r in page["items"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 31
    token = reader.page(CTX, limit=1)["next_cursor"]
    with pytest.raises(ValueError, match="stale"):
        reader.page(CTX.model_copy(update={"allow_targets": True}), cursor=token)
    publish(journal, entity("new"), request="new")
    with pytest.raises(ValueError, match="stale"):
        reader.page(CTX, cursor=token)


def review(e, rid="review-1", decision="SEMANTIC_REVIEWED"):
    return ReviewDecision(record_id=rid, recorded_at=NOW, actor=CTX.principal, policy=POLICY, supports=e.supports,
        references=(e.version_ref,), target=e.version_ref, decision=decision, source_verified=True,
        reviewer_authority=CTX.principal, rationale="Synthetic source verified", checks=("identity",))


def test_admission_requires_current_authorized_review_and_invalidates_on_correction(journal):
    e = entity()
    publish(journal, e)
    r = review(e)
    publish(journal, r, request="review")
    a = ScientificUseAdmission(record_id="admission", recorded_at=NOW, actor=CTX.principal, policy=POLICY,
        use_context={"use": "IDENTITY", "site": "SKRU1", "scale": "NOT_APPLICABLE"},
        purpose="synthetic identity test", origin="2026-10-01", targets=(e.version_ref,),
        dependency_versions=(e.version_ref,), review_versions=(r.version_ref,), policy_sha256=policy_hash({e.record_id: e}),
        status="READY")
    publish(journal, a, request="admit")
    assert EvidenceReader(journal).get("admission", CTX)["current_admission"]["status"] == "READY"
    corrected = entity(revision=2, supersedes=e.version_ref).model_copy(update={"label": "New identity reading"})
    publish(journal, corrected, request="correct")
    state = EvidenceReader(journal).get("admission", CTX)["current_admission"]
    assert state["status"] == "NOT_READY" and "STALE_OR_MISSING:entity-1" in state["reasons"]


def test_unauthorized_self_review_is_rejected(journal):
    e = entity()
    publish(journal, e)
    ctx = CTX.model_copy(update={"principal": "model"})
    r = review(e).model_copy(update={"actor": "model", "reviewer_authority": "model"})
    with pytest.raises(PermissionError, match="not configured"):
        journal.publish("selfreview", journal.revision, EvidenceBatch(records=(r,)), ctx)


def test_revoked_review_cannot_be_hidden_by_backdated_timestamp(journal):
    e = entity()
    publish(journal, e)
    r = review(e)
    publish(journal, r, request="review")
    revoke = review(e, "revocation", "REVOKED").model_copy(update={"recorded_at": NOW.replace(year=2020)})
    publish(journal, revoke, request="revoke")
    a = ScientificUseAdmission(record_id="admission", recorded_at=NOW, actor=CTX.principal, policy=POLICY,
        use_context={"use": "IDENTITY", "site": "SKRU1", "scale": "NOT_APPLICABLE"},
        purpose="identity", origin="2026-10-01", targets=(e.version_ref,), dependency_versions=(e.version_ref,),
        review_versions=(r.version_ref,), policy_sha256=policy_hash({e.record_id: e}), status="READY")
    with pytest.raises(ValueError, match="NO_CURRENT_SEMANTIC_REVIEW"):
        publish(journal, a, request="admit")


def test_primary_origin_overlap_not_publication_count():
    first = OriginSlice(origin_id="survey-1", members=("point-1",), independence_basis="reviewed original", verified=True)
    second = first.model_copy(update={"members": ("point-2",)})
    assert lineage_overlap((first,), (first,)) == "OVERLAP"
    assert lineage_overlap((first,), (second,)) == "DISJOINT_SHARED_ORIGIN"
    assert lineage_overlap((first,), (first.model_copy(update={"verified": False}),)) == "UNKNOWN"


def test_empty_extraction_does_not_become_complete():
    ledger = CoverageLedger(campaign_sha256=SHA, units=({"unit_id": "page-1", "source_id": "source-1",
        "source_sha256": SHA, "unit_kind": "PAGE", "locator": "page:1"},), inspected_units=(), candidates=(), attempts=())
    assert ledger.report()["status"] == "INCOMPLETE"
    assert ledger.report()["detector_recall"] == "NOT_ESTABLISHED"
    with pytest.raises(ValueError, match="output identities"):
        CoverageLedger(**{**ledger.model_dump(), "candidates": ({"candidate_id": "table-1", "unit_id": "page-1",
            "kind": "TABLE", "locator": "p1:r1", "state": "EXTRACTED"},)})


def test_projection_restore_build_and_hash_integrity(journal, tmp_path):
    from vkm_evidence.projections import build_projection
    publish(journal, entity())
    reader = EvidenceReader(journal)
    manifest = build_projection(reader, tmp_path / "projections", CTX)
    assert manifest["record_count"] == 1
    assert build_projection(reader, tmp_path / "projections", CTX) == manifest
    import duckdb
    db = tmp_path / "projections" / manifest["projection_id"] / "evidence.duckdb"
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT count(*) FROM evidence").fetchone()[0] == 1
    con.close()
