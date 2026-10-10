from datetime import datetime, timezone

import pytest

from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_evidence.contracts import Entity, EvidenceBatch, ObjectRef, ObservationSet, OriginSlice
from vkm_evidence.journal import EvidenceJournal
from vkm_evidence.objects import ObjectCatalogue, OriginalObject
from vkm_evidence.projections import build_projection
from vkm_evidence.query import EvidenceReader

CTX = AccessContext(principal="synthetic", execution="CLOUD")
POLICY = ResourcePolicy(access_class="PUBLIC", policy_version="1", authority="synthetic")
REF = ObjectRef(source_id="VKM-SRC-001", source_sha256="a" * 64, snapshot_id="synthetic",
    object_id="object", object_version="1", content_sha256="a" * 64, locator="page:1", extraction_generation="1")


def fixture(tmp_path):
    catalogue = ObjectCatalogue((OriginalObject(REF, POLICY),))
    journal = EvidenceJournal(tmp_path / "journal", object_validator=catalogue.validate)
    for i in range(3):
        item = Entity(record_id="entity-" + str(i), recorded_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                      actor=CTX.principal, policy=POLICY, supports=(REF,), entity_type="MINE", label="synthetic")
        journal.publish(str(i), journal.revision, EvidenceBatch(records=(item,)), CTX)
    return journal


def stale_fixture(tmp_path):
    journal = fixture(tmp_path)
    records = journal.records()
    parent, child = records["entity-0"], records["entity-1"]
    dependent = child.model_copy(update={"revision": 2, "supersedes": child.version_ref,
                                         "references": (parent.version_ref,)})
    journal.publish("dependent", journal.revision, EvidenceBatch(records=(dependent,)), CTX)
    corrected = parent.model_copy(update={"revision": 2, "supersedes": parent.version_ref, "label": "correction"})
    journal.publish("corrected", journal.revision, EvidenceBatch(records=(corrected,)), CTX)
    return journal


@pytest.mark.parametrize("changes", [
    {"record_count": 999}, {"relation_count": -1}, {"occurrence_count": True}, {"primary_origin_count": False},
    {"missing_referenced_versions": []}, {"load_readiness": "PINNED_INPUTS_PREPARED"},
    {"remote_load": "PASS"}, {"status": "PASS"},
])
def test_cached_manifest_cannot_forge_derived_summary_or_remote_acceptance(tmp_path, changes):
    import json
    journal = stale_fixture(tmp_path)
    reader, output = EvidenceReader(journal), tmp_path / "projections"
    first = build_projection(reader, output, CTX)
    assert first["load_readiness"] == "BLOCKED_MISSING_REFERENCED_VERSIONS"
    path = output / first["projection_id"] / "manifest.json"
    path.write_text(json.dumps({**first, **changes}), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        build_projection(reader, output, CTX)


@pytest.mark.parametrize("change", ["policy", "revision"])
def test_cache_hit_rechecks_policy_and_revision_after_file_hashing(tmp_path, monkeypatch, change):
    import vkm_evidence.projections as projection
    journal = fixture(tmp_path)
    current = {"policy": POLICY}
    reader = EvidenceReader(journal, source_policy=lambda sid: current["policy"])
    output = tmp_path / "projections"
    build_projection(reader, output, CTX)
    original = projection.sha256_of
    changed = False

    def mutate_after_file_hash(path):
        nonlocal changed
        value = original(path)
        if path.name == "evidence.parquet" and not changed:
            changed = True
            if change == "policy":
                current["policy"] = ResourcePolicy(access_class="PRIVATE_LOCAL_ONLY", policy_version="2", authority="synthetic")
            else:
                record = journal.records()["entity-0"]
                correction = record.model_copy(update={"revision": 2, "supersedes": record.version_ref, "label": "new"})
                journal.publish("during-reuse", journal.revision, EvidenceBatch(records=(correction,)), CTX)
        return value

    monkeypatch.setattr(projection, "sha256_of", mutate_after_file_hash)
    with pytest.raises(ValueError, match="changed during projection"):
        build_projection(reader, output, CTX)


@pytest.mark.parametrize("reuse", [False, True])
def test_builder_identity_is_rechecked_before_fresh_publication_or_cache_return(tmp_path, monkeypatch, reuse):
    import vkm_evidence.projections as projection
    reader, output = EvidenceReader(fixture(tmp_path)), tmp_path / "projections"
    original_identity, original_write, original_hash = projection.projection_builder_identity, projection.write_with, projection.sha256_of
    current, seen_producers = ["before"], []

    def identity(producer=None):
        seen_producers.append(producer)
        return {**original_identity(producer), "injected_recipe": current[0]}

    monkeypatch.setattr(projection, "projection_builder_identity", identity)
    producer = "a" * 64
    if reuse:
        build_projection(reader, output, CTX, producer_identity=producer)

        def changed_hash(path):
            value = original_hash(path)
            if path.name == "evidence.parquet":
                current[0] = "after"
            return value

        monkeypatch.setattr(projection, "sha256_of", changed_hash)
    else:
        def changed_write(*args, **kwargs):
            value = original_write(*args, **kwargs)
            current[0] = "after"
            return value

        monkeypatch.setattr(projection, "write_with", changed_write)
    with pytest.raises(ValueError, match="builder.*changed"):
        build_projection(reader, output, CTX, producer_identity=producer)
    assert set(seen_producers) == {producer}
    if not reuse:
        assert list(output.glob("*/manifest.json")) == []


def test_stale_semantic_subject_does_not_report_the_current_object_as_missing(tmp_path):
    import pyarrow.parquet as pq
    from vkm_evidence.contracts import EvidenceRelation
    journal = fixture(tmp_path)
    subject, target = journal.records()["entity-0"], journal.records()["entity-1"]
    relation = EvidenceRelation(record_id="semantic-relation", actor=CTX.principal,
        recorded_at=datetime(2026, 10, 1, tzinfo=timezone.utc), policy=POLICY, supports=(REF,),
        subject=subject.record_id, object=target.record_id, predicate="SUPPORTS", rationale="synthetic",
        references=(subject.version_ref, target.version_ref))
    journal.publish("relation", journal.revision, EvidenceBatch(records=(relation,)), CTX)
    correction = subject.model_copy(update={"revision": 2, "supersedes": subject.version_ref, "label": "new"})
    journal.publish("correction", journal.revision, EvidenceBatch(records=(correction,)), CTX)
    manifest = build_projection(EvidenceReader(journal), tmp_path / "projection", CTX)
    root = tmp_path / "projection" / manifest["projection_id"]
    edge = next(row for row in pq.read_table(root / "relations.parquet").to_pylist() if row["predicate"] == "SUPPORTS")
    assert manifest["missing_referenced_versions"] == [edge["subject_node_key"]]
    assert edge["object_node_key"] not in manifest["missing_referenced_versions"]
    assert manifest["load_readiness"] == "BLOCKED_MISSING_REFERENCED_VERSIONS"


def test_cached_readiness_checks_actual_endpoint_presence_not_only_declared_pin_state(tmp_path):
    import json
    import pyarrow as pa
    import pyarrow.parquet as pq
    from vkm_corpus.parquet.atomic import sha256_of
    reader, output = EvidenceReader(stale_fixture(tmp_path)), tmp_path / "projections"
    first = build_projection(reader, output, CTX)
    root = output / first["projection_id"]
    path = root / "relations.parquet"
    table = pq.read_table(path)
    rows = table.to_pylist()
    for row in rows:
        if row["pin_state"] == "STALE":
            row["pin_state"] = "CURRENT"
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    first["files"]["relations.parquet"] = sha256_of(path)
    first.update(missing_referenced_versions=[], load_readiness="PINNED_INPUTS_PREPARED")
    (root / "manifest.json").write_text(json.dumps(first), encoding="utf-8")
    with pytest.raises(ValueError, match="pin state"):
        build_projection(reader, output, CTX)


def test_builder_and_dependencies_key_reuse(tmp_path):
    journal = fixture(tmp_path)
    reader = EvidenceReader(journal)
    first = build_projection(reader, tmp_path / "projections", CTX, producer_identity="a" * 64)
    assert first == build_projection(reader, tmp_path / "projections", CTX, producer_identity="a" * 64)
    second = build_projection(reader, tmp_path / "projections", CTX, producer_identity="b" * 64)
    assert first["projection_id"] != second["projection_id"]
    assert second["builder_identity"]["dependencies"]["duckdb"]
    assert second["builder_identity"]["code"]["vkm_evidence.query"]


def test_live_source_revocation_changes_cursor_and_projection(tmp_path):
    journal = fixture(tmp_path)
    current = {"policy": POLICY}
    reader = EvidenceReader(journal, source_policy=lambda sid: current["policy"])
    page = reader.page(CTX, limit=1)
    before = build_projection(reader, tmp_path / "projections", CTX)
    current["policy"] = ResourcePolicy(access_class="PRIVATE_LOCAL_ONLY", policy_version="2", authority="synthetic")
    with pytest.raises(ValueError, match="cursor"):
        reader.page(CTX, cursor=page["next_cursor"], limit=1)
    with pytest.raises(KeyError):
        reader.get("entity-0", CTX)
    after = build_projection(reader, tmp_path / "projections", CTX)
    assert after["record_count"] == 0 and after["projection_id"] != before["projection_id"]


def test_retained_dependency_original_policy_is_checked_after_parent_correction(tmp_path):
    old_ref = REF.model_copy(update={"source_id": "old-source", "object_id": "old-object"})
    new_ref = REF.model_copy(update={"source_id": "new-source", "object_id": "new-object"})
    catalogue = ObjectCatalogue((OriginalObject(old_ref, POLICY), OriginalObject(new_ref, POLICY)))
    journal = EvidenceJournal(tmp_path / "journal", object_validator=catalogue.validate)
    parent = Entity(record_id="parent", recorded_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        actor=CTX.principal, policy=POLICY, supports=(old_ref,), entity_type="MINE", label="old")
    child = parent.model_copy(update={"record_id": "child", "supports": (new_ref,), "references": (parent.version_ref,)})
    journal.publish("first", journal.revision, EvidenceBatch(records=(parent, child)), CTX)
    corrected = parent.model_copy(update={"revision": 2, "supersedes": parent.version_ref,
        "supports": (new_ref,), "label": "new"})
    journal.publish("corrected", journal.revision, EvidenceBatch(records=(corrected,)), CTX)
    policies = {"old-source": POLICY, "new-source": POLICY}
    reader = EvidenceReader(journal, source_policy=policies.__getitem__)
    before = reader.page(CTX, limit=1)
    assert before["total_permitted"] == 2
    policies["old-source"] = ResourcePolicy(access_class="PRIVATE_LOCAL_ONLY", policy_version="2", authority="owner")
    assert reader.page(CTX)["total_permitted"] == 1
    with pytest.raises(KeyError):
        reader.get("child", CTX)
    with pytest.raises(ValueError, match="cursor"):
        reader.page(CTX, limit=1, cursor=before["next_cursor"])


def test_projection_preserves_semantic_edges_occurrences_primary_origins_and_stale_pins(tmp_path):
    import json
    import duckdb
    journal = fixture(tmp_path)
    parent = journal.records()["entity-0"]
    observation_set = ObservationSet(record_id="survey", actor=CTX.principal,
        recorded_at=datetime(2026, 10, 1, tzinfo=timezone.utc), policy=POLICY, supports=(REF,),
        references=(parent.version_ref,), origins=(OriginSlice(origin_id="primary-campaign", members=("point-1",),
            verified=True, independence_basis="synthetic reviewed original"),), lineage_state="VERIFIED")
    journal.publish("survey", journal.revision, EvidenceBatch(records=(observation_set,)), CTX)
    corrected = parent.model_copy(update={"revision": 2, "supersedes": parent.version_ref, "label": "correction"})
    journal.publish("correction", journal.revision, EvidenceBatch(records=(corrected,)), CTX)
    manifest = build_projection(EvidenceReader(journal), tmp_path / "projections", CTX)
    assert manifest["load_readiness"] == "BLOCKED_MISSING_REFERENCED_VERSIONS"
    assert manifest["remote_load"] == "NOT_RUN"
    root = tmp_path / "projections" / manifest["projection_id"]
    con = duckdb.connect(str(root / "evidence.duckdb"), read_only=True)
    try:
        edge = con.execute("SELECT object_sha256, pin_state, object_node_key FROM relations WHERE predicate='REFERENCES'").fetchone()
        assert edge[:2] == (parent.version_ref.record_sha256, "STALE")
        assert edge[2] in manifest["missing_referenced_versions"]
        assert con.execute("SELECT count(*) FROM occurrences").fetchone()[0] == 1
        assert con.execute("SELECT origin_id, verified FROM origins").fetchone() == ("primary-campaign", True)
    finally:
        con.close()
    nodes = [json.loads(line) for line in (root / "neo4j_nodes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {n["layer"] for n in nodes} == {"EVIDENCE", "ORIGINAL_OCCURRENCE", "PRIMARY_ORIGIN"}
    current_parent = next(n for n in nodes if n.get("record_id") == "entity-0")
    assert current_parent["node_key"] != edge[2]
