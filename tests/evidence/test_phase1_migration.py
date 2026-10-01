"""Synthetic Phase-1 CSVs and real in-memory CanonStore; no private corpus reads."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from vkm_corpus.api.canon import CanonStore
from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_evidence.contracts import Entity, EvidenceBatch, ObjectRef, ReviewDecision, VersionRef
from vkm_evidence.journal import EvidenceJournal, JournalConflict, ZERO
from vkm_evidence.migration import (ArchivedRow, CatalogueSpec, MigrationApproval, MigrationBlocked,
    MigrationBounds, MigrationPublisher, RowBinding, freeze_inputs, plan_migration)
from vkm_evidence.objects import canonical_resolver
import vkm_evidence.migration as migration

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
SID = "VKM-SRC-001"
SHA = "a" * 64
TEXT = "Синтетический интервал: 2–4 м; UNKNOWN не означает ноль."
POLICY = ResourcePolicy(access_class="PRIVATE_CLOUD_ALLOWED", experimental_role="INPUT", policy_version="p1", authority="owner")
CTX = AccessContext(principal="migrator", execution="LOCAL", granted_classes={"PRIVATE_CLOUD_ALLOWED"})
HEADER = ("row_id", "source_id", "locator", "quote", "status", "scope", "scale", "evidence_type",
          "value_raw", "time_raw", "old_refs", "visual_check")
BASE_ROW = ("MR-MECH-0001", SID, "PDF p.1; historical locator", TEXT, "FACT", "SKRU1", "LAB",
            "LAB_TEST", "2–4; not collapsed", "UNKNOWN", "EV-VN-S001-0001; previous-id", "PASS")
SPEC = CatalogueSpec(path="MECH_RHEO/mechanics_evidence_catalog.csv", id_column="row_id")


class Fixture:
    def __init__(self, tmp):
        self.root = tmp / "PRIVATE" / "11_evidence_vnext" / "canonical"
        self.tmp = tmp
        self.policy = POLICY
        self.catalogue_policy = POLICY
        self.con = duckdb.connect(":memory:")
        self.con.execute("CREATE SCHEMA meta")
        self.con.execute("CREATE TABLE meta.snapshot AS SELECT 'snap-1' snapshot_id, ? manifest_sha256, "
            "TIMESTAMPTZ '2026-10-01 00:00:00+00' built_at, 'synthetic' duckdb_version", [SHA])
        self.con.execute("CREATE TABLE meta.commits(commit_key VARCHAR, commit_id VARCHAR)")
        self.ref = ObjectRef(source_id=SID, source_sha256=SHA, snapshot_id="snap-1",
            object_id=SID + ":doc:b123456abcdef", object_version="native-1",
            content_sha256=hashlib.sha256(TEXT.encode()).hexdigest(), locator="body/p[1]",
            extraction_generation="1", char_start=0, char_end=len(TEXT),
            fragment_sha256=hashlib.sha256(TEXT.encode()).hexdigest())
        self.con.execute('CREATE TABLE blocks AS SELECT ? object_id, ? source_id, ? source_sha256, '
            '? content_sha256, 1 extraction_generation, ? extraction_signature, NULL raw_locator, '
            '? docx_paragraph_path, NULL page_id, ? AS "text"',
            [self.ref.object_id, SID, SHA, self.ref.content_sha256, self.ref.object_version, self.ref.locator, TEXT])
        self.canon = CanonStore(connection=self.con)
        self.journal = EvidenceJournal(tmp / "private-runtime" / "evidence",
            object_validator=canonical_resolver(self.canon, self.source_policy))
        self.write()

    def source_policy(self, sid):
        self.policy.require(CTX)
        return self.policy

    def write(self, rows=(BASE_ROW,), *, spec=SPEC, header=HEADER):
        path = self.root / spec.path
        path.parent.mkdir(parents=True, exist_ok=True)
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(header)
        writer.writerows(rows)
        path.write_bytes(output.getvalue().encode("utf-8-sig"))
        return path

    def freeze(self, specs=(SPEC,), bounds=None):
        return freeze_inputs(self.root, specs, catalogue_policy=self.catalogue_policy, context=CTX, bounds=bounds)

    def bindings(self, frozen, *, mode="VERBATIM", supports=None):
        result = []
        for file in frozen.files:
            with (self.root / file.spec.path).open(encoding="utf-8-sig", newline="") as stream:
                rows = csv.DictReader(stream)
                for n, row in enumerate(rows, 1):
                    archived = ArchivedRow(path=file.spec.path, file_sha256=file.sha256, row_number=n,
                        old_id=row.get(file.spec.id_column), values=tuple(row.values()))
                    result.append(RowBinding(row_sha256=archived.row_sha256, supports=supports or (self.ref,),
                                             historical_quote_field=mode))
        return tuple(result)

    def plan(self, *, frozen=None, bindings=None, mode="VERBATIM", supports=None, reviews=(), **kw):
        frozen = frozen or self.freeze()
        if bindings is None:
            bindings = self.bindings(frozen, mode=mode, supports=supports)
        return plan_migration(self.root, frozen, bindings, canon=self.canon, source_policy=self.source_policy,
            context=CTX, catalogue_policy=self.catalogue_policy, output_policy=POLICY, recorded_at=NOW,
            base_revision=kw.get("base_revision", ZERO), review_decisions=reviews)

    def publisher(self, plan, *, approved=True, allow_unresolved=False):
        approval = MigrationApproval(plan_sha256=plan.sha256, base_revision=plan.base_revision,
            publisher=CTX.principal, authority="owner", allow_unresolved=allow_unresolved)
        return MigrationPublisher(self.tmp / "private-runtime" / "migrations", self.journal, canon=self.canon,
            source_policy=self.source_policy, catalogue_policy=lambda: self.catalogue_policy,
            approved_plans={plan.sha256: approval} if approved else {}, owners=frozenset({"owner"}))


@pytest.fixture
def env(tmp_path):
    # The global offline runner may keep its synthetic basetemp in PUBLIC/work.
    # Exercise the real private-storage guard, rather than disabling it for tests.
    # Retain the unique synthetic package for inspection; never clean shared temp.
    if tmp_path.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        tmp_path = Path(tempfile.mkdtemp(prefix="vkm-phase1-migration-"))
    fixture = Fixture(tmp_path)
    yield fixture
    fixture.con.close()


def test_read_only_deterministic_plan_preserves_exact_ids_scope_scale_and_unknown(env):
    plan = env.plan()
    assert plan == env.plan()
    assert not env.journal.root.exists()
    row = plan.rows[0]
    assert row.outcome == "MIGRATABLE" and row.reasons == ()
    assert row.record.record_id == "MR-MECH-0001"
    assert row.record.provenance.status.value == "FACT"
    assert row.record.provenance.scope.value == "SKRU1" and row.record.provenance.scale.value == "LAB"
    assert row.record.review_state == "UNREVIEWED" and row.semantic_state == "NEEDS_SEMANTIC_REVIEW"
    assert row.record.time.available_from is None and not hasattr(row.record, "quantity")
    assert row.record.supports[0].locator == "body/p[1]"
    assert plan.archive[0].values == BASE_ROW
    assert plan.review_decisions == () and plan.scientific_admission == "NOT_ESTABLISHED"
    assert "OriginalOccurrence" not in plan.model_dump_json()


def test_paraphrase_is_not_a_verified_quote_or_automatic_semantic_review(env):
    row = list(BASE_ROW)
    row[3] = "Historical paraphrase; deliberately absent from canonical text."
    env.write((tuple(row),))
    plan = env.plan(mode="PARAPHRASE")
    assert plan.rows[0].record.proposition == row[3]
    assert plan.rows[0].record.review_state == "UNREVIEWED"
    assert "historical_quote_field:PARAPHRASE" in plan.rows[0].record.qualifiers
    assert env.plan(mode="VERBATIM").rows[0].reasons == ("HISTORICAL_QUOTE_FRAGMENT_MISMATCH",)
    assert env.plan(mode="UNSPECIFIED").rows[0].reasons == ("HISTORICAL_QUOTE_SEMANTICS_UNSPECIFIED",)


@pytest.mark.parametrize("field,value", [
    ("source_sha256", "b" * 64), ("snapshot_id", "old-snapshot"),
    ("object_id", "invented-original"), ("locator", "invented-locator"),
    ("object_version", "old-extraction"), ("content_sha256", "b" * 64),
    ("char_end", 9999), ("fragment_sha256", "b" * 64),
])
def test_missing_or_forged_original_is_unresolved_without_retargeting(env, field, value):
    ref = env.ref.model_copy(update={field: value})
    plan = env.plan(supports=(ref,))
    assert plan.rows[0].record is None
    assert plan.rows[0].reasons == ("ORIGINAL_BINDING_UNRESOLVED",)
    assert len(plan.archive) == 1 and plan.archive[0].values == BASE_ROW


def test_absent_binding_keeps_row_and_cannot_publish_generic_pass(env):
    plan = env.plan(bindings=())
    assert plan.rows[0].reasons == ("ORIGINAL_BINDING_MISSING",)
    assert plan.rows[0].outcome == "UNRESOLVED"
    with pytest.raises(MigrationBlocked, match="NO_PUBLISHABLE_RECORDS"):
        env.publisher(plan, allow_unresolved=True).publish(env.root, plan, CTX)
    assert env.journal.revision == ZERO


@pytest.mark.parametrize("column,value,reason", [
    ("row_id", "", "HISTORICAL_ID_MISSING"),
    ("row_id", "id with spaces", "HISTORICAL_ID_NOT_REPRESENTABLE"),
    ("locator", "", "HISTORICAL_LOCATOR_MISSING"),
    ("source_id", "VKM-SRC-002", "ORIGINAL_BINDING_SOURCE_MISMATCH"),
    ("source_id", "a work mentioned somewhere", "HISTORICAL_SOURCE_IDS_UNRESOLVED"),
    ("scope", "SKRU1;NON_VKM", "HISTORICAL_SCOPE_OR_SCALE_UNRESOLVED"),
    ("scale", "LAB/MASSIF", "HISTORICAL_SCOPE_OR_SCALE_UNRESOLVED"),
    ("status", "DERIVATION", "HISTORICAL_PROVENANCE_NOT_REPRESENTABLE"),
])
def test_unrepresentable_old_rows_never_disappear(env, column, value, reason):
    row = list(BASE_ROW)
    row[HEADER.index(column)] = value
    env.write((tuple(row),))
    plan = env.plan()
    assert plan.rows[0].reasons == (reason,)
    assert plan.archive[0].values == tuple(row) and plan.rows[0].outcome == "UNRESOLVED"


def test_unmapped_status_remains_raw_and_only_maps_to_unknown(env):
    row = list(BASE_ROW)
    row[4] = "PROPOSED"
    env.write((tuple(row),))
    result = env.plan().rows[0]
    assert result.record.provenance.status.value == "UNKNOWN"
    assert result.reasons == ("HISTORICAL_STATUS_UNMAPPED_RETAINED_AS_UNKNOWN",)


def test_duplicate_historical_ids_across_files_are_all_unresolved(env):
    second = SPEC.model_copy(update={"path": "MECH_RHEO/second.csv"})
    env.write(spec=second)
    plan = env.plan(frozen=env.freeze((SPEC, second)))
    assert len(plan.rows) == 2 and len(plan.archive) == 2
    assert all(r.reasons == ("HISTORICAL_ID_DUPLICATE",) for r in plan.rows)
    assert len({r.row_sha256 for r in plan.rows}) == 2


def test_shared_primary_data_does_not_become_two_independent_observations(env):
    second = ("MR-MECH-0002", *BASE_ROW[1:])
    env.write((BASE_ROW, second))
    plan = env.plan()
    assert len(plan.rows) == 2
    assert all(r.record.kind == "CLAIM" and not hasattr(r.record, "origins") for r in plan.rows)
    assert plan.rows[0].record.supports == plan.rows[1].record.supports


def test_fresh_original_hash_detects_same_size_same_mtime_change(env):
    frozen = env.freeze()
    path = env.root / SPEC.path
    stamp = path.stat()
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"MR-MECH-0001", b"MR-MECH-9999"))
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(MigrationBlocked, match="FROZEN_MIGRATION_INPUT_CHANGED"):
        env.plan(frozen=frozen, bindings=())


def test_policy_precedes_input_paths_and_source_lookup(env, monkeypatch):
    denied = CTX.model_copy(update={"granted_classes": frozenset()})
    with pytest.raises(PermissionError, match="RESOURCE_POLICY_DENIED"):
        freeze_inputs(Path("does-not-exist"), (SPEC,), catalogue_policy=POLICY, context=denied)
    calls = []
    monkeypatch.setattr(env.canon, "row", lambda *args: calls.append(args))
    env.policy = POLICY.model_copy(update={"experimental_role": "TEST_SEALED"})
    with pytest.raises(PermissionError, match="RESOURCE_POLICY_DENIED"):
        env.plan()
    assert calls == []


def test_unbound_rows_cannot_bypass_source_policy_through_private_archive(env, monkeypatch):
    frozen = env.freeze()
    env.policy = POLICY.model_copy(update={"experimental_role": "TEST_SEALED"})
    calls = []
    monkeypatch.setattr(env.canon, "row", lambda *args: calls.append(args))
    with pytest.raises(PermissionError, match="RESOURCE_POLICY_DENIED"):
        env.plan(frozen=frozen, bindings=())
    assert calls == [] and not env.journal.root.exists()


def test_current_snapshot_change_during_plan_is_detected(env, monkeypatch):
    original = env.canon.snapshot
    count = [0]
    def moving():
        count[0] += 1
        snapshot = original()
        return replace(snapshot, manifest_sha256="b" * 64) if count[0] > 4 else snapshot
    monkeypatch.setattr(env.canon, "snapshot", moving)
    with pytest.raises(MigrationBlocked, match="CANONICAL_SNAPSHOT_CHANGED"):
        env.plan()


def test_plan_stage_and_publish_are_immutable_private_and_idempotent(env):
    plan = env.plan()
    publisher = env.publisher(plan)
    stage = publisher.stage(env.root, plan, CTX)
    assert stage["status"] == "PLANNED" and env.journal.revision == ZERO
    folder = publisher.root / plan.sha256
    archived = json.loads((folder / "rows.json").read_bytes())
    assert archived[0]["values"] == list(BASE_ROW)
    file = plan.inputs.files[0]
    assert (folder / "original-catalogues" / (file.sha256 + ".csv")).read_bytes() == (env.root / SPEC.path).read_bytes()
    receipt = publisher.publish(env.root, plan, CTX)
    assert receipt == publisher.publish(env.root, plan, CTX)
    assert len(env.journal.commits()) == 1
    assert receipt["rows"][0]["outcome"] == "MIGRATED"
    assert receipt["rows"][0]["semantic_state"] == "NEEDS_SEMANTIC_REVIEW"
    assert TEXT not in json.dumps(receipt, ensure_ascii=False)
    assert receipt["scientific_admission"] == "NOT_ESTABLISHED"
    assert receipt["physical_durability"] == "NOT_QUALIFIED"
    assert receipt["directory_fsync"] == ("COMPLETED" if os.name == "posix" else "NOT_QUALIFIED")


def test_publish_needs_separate_operator_approval_not_old_flags(env):
    plan = env.plan()
    with pytest.raises(MigrationBlocked, match="OWNER_APPROVAL_REQUIRED"):
        env.publisher(plan, approved=False).publish(env.root, plan, CTX)
    assert env.journal.revision == ZERO
    publisher = env.publisher(plan)
    changed = plan.model_copy(update={"actor": "somebody-else"})
    with pytest.raises(MigrationBlocked, match="OWNER_APPROVAL_REQUIRED"):
        publisher.publish(env.root, changed, CTX)


def test_unresolved_subset_needs_explicit_ack_and_complete_map(env):
    env.write((BASE_ROW, ("MR-MECH-0002", *BASE_ROW[1:])))
    frozen = env.freeze()
    plan = env.plan(frozen=frozen, bindings=env.bindings(frozen)[:1])
    with pytest.raises(MigrationBlocked, match="UNRESOLVED_ROWS_REQUIRE_OWNER_ACK"):
        env.publisher(plan).publish(env.root, plan, CTX)
    receipt = env.publisher(plan, allow_unresolved=True).publish(env.root, plan, CTX)
    assert [r["outcome"] for r in receipt["rows"]] == ["MIGRATED", "UNRESOLVED"]


def test_persisted_map_tamper_rejected_before_journal_commit(env):
    plan = env.plan()
    publisher = env.publisher(plan)
    publisher.stage(env.root, plan, CTX)
    (publisher.root / plan.sha256 / "map.json").write_bytes(b"[]")
    with pytest.raises(MigrationBlocked, match="IMMUTABLE_MIGRATION_OUTPUT_CONFLICT"):
        publisher.publish(env.root, plan, CTX)
    assert env.journal.revision == ZERO


def test_lost_ack_replays_committed_exact_request_once(env):
    plan = env.plan()
    publisher = env.publisher(plan)
    def interrupt():
        raise ConnectionError("synthetic lost ACK")
    with pytest.raises(ConnectionError):
        publisher.publish(env.root, plan, CTX, after_commit=interrupt)
    assert env.journal.revision != ZERO
    assert not (publisher.root / plan.sha256 / "publish-receipt.json").exists()
    receipt = publisher.publish(env.root, plan, CTX)
    assert receipt["journal"]["revision"] == env.journal.revision and len(env.journal.commits()) == 1


def test_fsync_failure_cannot_return_completed_or_advance_head(env, monkeypatch):
    plan = env.plan()
    publisher = env.publisher(plan)
    real_fsync = os.fsync
    monkeypatch.setattr(migration.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("synthetic fsync")))
    with pytest.raises(OSError):
        publisher.publish(env.root, plan, CTX)
    assert env.journal.revision == ZERO
    monkeypatch.setattr(migration.os, "fsync", real_fsync)
    assert publisher.publish(env.root, plan, CTX)["status"] == "COMMITTED"


def test_replay_reverifies_originals_and_policy_before_existing_receipt(env):
    plan = env.plan()
    publisher = env.publisher(plan)
    publisher.publish(env.root, plan, CTX)
    env.policy = POLICY.model_copy(update={"experimental_role": "TEST_SEALED"})
    with pytest.raises(PermissionError, match="RESOURCE_POLICY_DENIED"):
        publisher.publish(env.root, plan, CTX)
    env.policy = POLICY
    env.con.execute("UPDATE blocks SET content_sha256=?", ["b" * 64])
    with pytest.raises(MigrationBlocked, match="NO_LONGER_REPRODUCIBLE"):
        publisher.publish(env.root, plan, CTX)
    assert len(env.journal.commits()) == 1


@pytest.mark.parametrize("attribution", [SID + ";unmapped", "unmapped|" + SID, SID + ",?"])
def test_unresolved_mixed_attribution_cannot_hide_a_denied_known_source(env, attribution):
    row = list(BASE_ROW)
    row[1] = attribution
    env.write((tuple(row),))
    env.policy = POLICY.model_copy(update={"experimental_role": "TEST_SEALED"})
    with pytest.raises(PermissionError, match="RESOURCE_POLICY_DENIED"):
        env.plan(bindings=())
    assert not env.journal.root.exists()


def test_changed_policy_version_and_changed_csv_block_existing_receipt(env):
    plan = env.plan()
    publisher = env.publisher(plan)
    publisher.publish(env.root, plan, CTX)
    env.policy = POLICY.model_copy(update={"policy_version": "p2"})
    with pytest.raises(MigrationBlocked, match="NO_LONGER_REPRODUCIBLE"):
        publisher.publish(env.root, plan, CTX)
    env.policy = POLICY
    path = env.root / SPEC.path
    path.write_bytes(path.read_bytes().replace(b"MR-MECH-0001", b"MR-MECH-0002"))
    with pytest.raises(MigrationBlocked, match="FROZEN_MIGRATION_INPUT_CHANGED"):
        publisher.publish(env.root, plan, CTX)


def test_pending_files_are_preserved_and_do_not_count_as_completed(env):
    plan = env.plan()
    publisher = env.publisher(plan)
    folder = publisher.root / plan.sha256
    folder.mkdir(parents=True)
    (folder / ".pending-interrupted").write_bytes(b"synthetic incomplete output")
    assert publisher.publish(env.root, plan, CTX)["status"] == "COMMITTED"
    assert (folder / ".pending-interrupted").read_bytes() == b"synthetic incomplete output"


def test_duplicate_or_dangling_binding_is_not_silently_ignored(env):
    frozen = env.freeze()
    bindings = env.bindings(frozen)
    for invalid in (bindings + bindings, (bindings[0].model_copy(update={"row_sha256": "b" * 64}),)):
        with pytest.raises(MigrationBlocked, match="BINDINGS_DUPLICATE_OR_DANGLING"):
            env.plan(frozen=frozen, bindings=invalid)


def test_dangling_pinned_dependency_cannot_commit(env):
    frozen = env.freeze()
    binding = env.bindings(frozen)[0].model_copy(update={"dependencies": (
        VersionRef(record_id="missing-parent", record_sha256="b" * 64),)})
    plan = env.plan(frozen=frozen, bindings=(binding,))
    with pytest.raises(JournalConflict, match="dangling or stale dependency"):
        env.publisher(plan).publish(env.root, plan, CTX)
    assert env.journal.revision == ZERO


def test_stale_base_cannot_overwrite_existing_records(env):
    plan = env.plan()
    other = Entity(record_id="other", entity_type="MINE", label="synthetic", recorded_at=NOW,
                   actor=CTX.principal, policy=POLICY)
    env.journal.publish("unrelated", ZERO, EvidenceBatch(records=(other,)), CTX)
    with pytest.raises(JournalConflict, match="stale base"):
        env.publisher(plan).publish(env.root, plan, CTX)
    assert set(env.journal.records()) == {"other"}


def explicit_review(plan):
    return ReviewDecision(record_id="review-old-row", actor=CTX.principal, recorded_at=NOW,
        policy=POLICY, supports=plan.rows[0].record.supports, target=plan.rows[0].record.version_ref,
        decision="VERIFIED_TRANSCRIPTION", rationale="Separate configured reviewer inspected the original.",
        source_verified=True, checks=("identity", "transcription"), reviewer_authority=CTX.principal)


def test_historical_review_cannot_self_declare_authority(env):
    draft = env.plan()
    plan = env.plan(reviews=(explicit_review(draft),))
    with pytest.raises(PermissionError, match="review authority not configured"):
        env.publisher(plan).publish(env.root, plan, CTX)
    assert env.journal.revision == ZERO


def test_explicit_review_has_exact_target_and_still_does_not_admit_science(env):
    draft = env.plan()
    review = explicit_review(draft)
    plan = env.plan(reviews=(review,))
    env.journal.reviewers = frozenset({CTX.principal})
    receipt = env.publisher(plan).publish(env.root, plan, CTX)
    assert receipt["journal"]["record_count"] == 2
    assert receipt["scientific_admission"] == "NOT_ESTABLISHED"
    wrong = review.model_copy(update={"target": review.target.model_copy(update={"record_sha256": "b" * 64})})
    with pytest.raises(MigrationBlocked, match="REVIEW_BINDING_INVALID"):
        env.plan(reviews=(wrong,))


def test_public_output_policy_and_public_checkout_paths_are_rejected(env):
    with pytest.raises(MigrationBlocked, match="PRIVATE_MIGRATION_STORAGE_REQUIRED"):
        plan_migration(env.root, env.freeze(), (), canon=env.canon, source_policy=env.source_policy,
            context=CTX, catalogue_policy=POLICY, output_policy=POLICY.model_copy(update={"access_class": "PUBLIC"}),
            recorded_at=NOW, base_revision=ZERO)
    with pytest.raises(MigrationBlocked, match="PRIVATE_MIGRATION_STORAGE_REQUIRED"):
        MigrationPublisher(Path(__file__).resolve().parents[2] / "work" / "no-private-migration", env.journal,
            canon=env.canon, source_policy=env.source_policy, catalogue_policy=lambda: POLICY,
            approved_plans={}, owners=frozenset({"owner"}))


@pytest.mark.parametrize("raw,reason", [
    (b"row_id,row_id\na,b\n", "HEADER_INVALID"),
    (b"row_id,source_id\na\n", "ROW_WIDTH"),
    (b'row_id,source_id\n"unterminated', "CSV_UNREADABLE"),
    (b"\xff\xfe\x80", "CSV_UNREADABLE"),
])
def test_malformed_csv_is_bounded_and_errors_have_no_raw_values(env, raw, reason):
    (env.root / SPEC.path).write_bytes(raw)
    with pytest.raises(MigrationBlocked, match=reason) as exc:
        env.freeze()
    assert "unterminated" not in str(exc.value)


@pytest.mark.parametrize("bounds", [
    MigrationBounds(max_total_bytes=10), MigrationBounds(max_columns=2), MigrationBounds(max_cell_chars=5),
])
def test_explicit_bounds_fail_closed(env, bounds):
    with pytest.raises(MigrationBlocked):
        env.freeze(bounds=bounds)


def test_retired_or_traversal_paths_are_not_current_inputs(env):
    for path in ("../legacy.csv", "10_physics_evidence/old.csv", "MINING/../../old.csv", "MINING\\old.csv"):
        with pytest.raises(ValueError):
            CatalogueSpec(path=path, id_column="row_id")
    with pytest.raises(MigrationBlocked, match="CURRENT_PHASE1_CANONICAL_ROOT_REQUIRED"):
        freeze_inputs(env.tmp / "legacy", (SPEC,), catalogue_policy=POLICY, context=CTX)
