from datetime import datetime, timezone

import pytest

from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_evidence.contracts import Claim, Entity, EvidenceBatch, ObjectRef, ReviewDecision, ScientificUseAdmission
from vkm_evidence.journal import EvidenceJournal, ZERO
from vkm_evidence.objects import ObjectCatalogue, OriginalObject
from vkm_evidence.query import EvidenceReader
from vkm_evidence.validation import dependency_closure, policy_hash
from vkm_evidence.coverage import CoverageLedger

SHA = "a" * 64
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
PUBLIC = ResourcePolicy(access_class="PUBLIC", policy_version="p1", authority="owner")
REF = ObjectRef(source_id="source-1", source_sha256=SHA, snapshot_id="snap-1", object_id="object-1",
    object_version="v1", content_sha256=SHA, locator="page:1", extraction_generation="1")
CTX = AccessContext(principal="owner", execution="LOCAL", granted_classes={"PUBLIC", "PRIVATE_CLOUD_ALLOWED"}, allow_targets=True)


def journal(tmp_path):
    resolver = ObjectCatalogue((OriginalObject(REF, PUBLIC, "Synthetic source"),))
    return EvidenceJournal(tmp_path, object_validator=resolver.validate, reviewers=frozenset({"owner"}))


def entity(policy=PUBLIC):
    return Entity(record_id="entity-1", recorded_at=NOW, actor="owner", policy=policy, supports=(REF,),
        entity_type="MINE", label="synthetic", identity_state="RESOLVED", time={"available_from": "2026-10-01", "precision": "day"})


def test_admission_special_refs_cannot_disclose_private_target(tmp_path):
    j = journal(tmp_path)
    private = ResourcePolicy(access_class="PRIVATE_CLOUD_ALLOWED", experimental_role="TARGET", policy_version="p1", authority="owner")
    e = entity(private)
    j.publish("entity", ZERO, EvidenceBatch(records=(e,)), CTX)
    r = ReviewDecision(record_id="review", recorded_at=NOW, actor="owner", policy=private, target=e.version_ref,
        supports=(REF,), decision="SEMANTIC_REVIEWED", source_verified=True, reviewer_authority="owner", rationale="verified")
    j.publish("review", j.revision, EvidenceBatch(records=(r,)), CTX)
    a = ScientificUseAdmission(record_id="admission", recorded_at=NOW, actor="owner", policy=PUBLIC,
        purpose="test", origin="2026-10-01", targets=(e.version_ref,), dependency_versions=(e.version_ref,),
        review_versions=(r.version_ref,), status="READY", policy_sha256=policy_hash({e.record_id: e}))
    with pytest.raises(PermissionError, match="widens dependency"):
        j.publish("admit", j.revision, EvidenceBatch(records=(a,)), CTX)
    reader = EvidenceReader(j)
    public = AccessContext(principal="public", execution="CLOUD", granted_classes={"PUBLIC"})
    assert reader.page(public)["total_permitted"] == 0


def test_semantic_reference_correction_invalidates_closure():
    e = entity()
    claim = Claim(record_id="claim", recorded_at=NOW, actor="owner", policy=PUBLIC, supports=(REF,),
        references=(e.version_ref,), proposition="Synthetic attribution", attribution="source", polarity="AFFIRMED",
        modality="REPORTED", provenance={"status": "UNKNOWN"})
    corrected = e.model_copy(update={"revision": 2, "supersedes": e.version_ref, "label": "corrected"})
    _, reasons = dependency_closure((claim.version_ref,), {"entity-1": corrected, "claim": claim})
    assert "STALE_OR_MISSING:entity-1" in reasons


def test_reader_does_not_expose_mutable_nested_provenance(tmp_path):
    j = journal(tmp_path)
    claim = Claim(record_id="claim", recorded_at=NOW, actor="owner", policy=PUBLIC, supports=(REF,),
        proposition="synthetic", attribution="source", polarity="AFFIRMED", modality="REPORTED",
        provenance={"status": "UNKNOWN"})
    j.publish("claim", ZERO, EvidenceBatch(records=(claim,)), CTX)
    reader = EvidenceReader(j)
    before = reader.get("claim", CTX)
    _, exposed = reader.view()
    exposed["claim"].provenance.confidence = "INJECTED"
    exposed["claim"].time.available_from = NOW.date()
    assert reader.get("claim", CTX) == before


def test_candidate_cannot_borrow_other_candidate_attempt():
    units = tuple({"unit_id": f"p{i}", "source_id": "source-1", "source_sha256": SHA,
        "unit_kind": "PAGE", "locator": f"p{i}"} for i in (1, 2))
    attempt = {"attempt_id": "attempt-b", "unit_id": "p2", "candidate_id": "b", "stage": "test",
        "input_sha256": SHA, "config_sha256": SHA, "code_revision": SHA, "state": "SUCCEEDED",
        "outputs": (SHA,), "object_outputs": {"output-b": SHA}}
    candidates = tuple({"candidate_id": name, "unit_id": unit, "kind": "TABLE", "locator": name,
        "state": "EXTRACTED", "attempts": ("attempt-b",), "output_objects": ("output-b",)}
        for name, unit in (("a", "p1"), ("b", "p2")))
    with pytest.raises(ValueError, match="borrows"):
        CoverageLedger(campaign_sha256=SHA, units=units, inspected_units=("p1", "p2"), candidates=candidates,
                       attempts=(attempt,))


def test_correction_cannot_drop_target_policy(tmp_path):
    j = journal(tmp_path)
    private = ResourcePolicy(access_class="PRIVATE_CLOUD_ALLOWED", experimental_role="TARGET", policy_version="p1", authority="owner")
    e = entity(private)
    j.publish("entity", ZERO, EvidenceBatch(records=(e,)), CTX)
    corrected = entity().model_copy(update={"revision": 2, "supersedes": e.version_ref})
    with pytest.raises(PermissionError, match="policy downgrade"):
        j.publish("correct", j.revision, EvidenceBatch(records=(corrected,)), CTX)


def test_forged_projection_manifest_rejected(tmp_path):
    import json
    from vkm_evidence.projections import build_projection
    j = journal(tmp_path / "journal")
    j.publish("entity", ZERO, EvidenceBatch(records=(entity(),)), CTX)
    root = tmp_path / "projection"
    manifest = build_projection(EvidenceReader(j), root, CTX)
    path = root / manifest["projection_id"] / "manifest.json"
    path.write_text(json.dumps({"files": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="identity mismatch"):
        build_projection(EvidenceReader(j), root, CTX)


def test_projection_interrupted_before_manifest_can_resume(tmp_path, monkeypatch):
    import vkm_evidence.projections as projection
    j = journal(tmp_path / "journal")
    j.publish("entity", ZERO, EvidenceBatch(records=(entity(),)), CTX)
    output, reader = tmp_path / "projection", EvidenceReader(j)
    write = projection.write_bytes
    def interrupt(temp, target, data, **kw):
        if target.name == "manifest.json":
            raise RuntimeError("interrupted before manifest")
        return write(temp, target, data, **kw)
    monkeypatch.setattr(projection, "write_bytes", interrupt)
    with pytest.raises(RuntimeError, match="before manifest"):
        projection.build_projection(reader, output, CTX)
    monkeypatch.setattr(projection, "write_bytes", write)
    resumed = projection.build_projection(reader, output, CTX)
    assert resumed["record_count"] == 1
