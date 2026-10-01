"""Synthetic producer -> receiving admission/snapshot -> backup closure; no real OCR."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_pipeline_e2e import env, _run_all, RUN
from test_backup_manifest import M
from vkm_corpus.coverage import publication as P
from vkm_corpus.contracts.access import AccessContext
from vkm_corpus.parquet.layout import init_root, open_root
from vkm_corpus.parquet.commits import list_markers
from vkm_corpus.parquet.admit import admit
from vkm_corpus.parquet.snapshot import build_snapshot
from vkm_corpus.parquet.reader import load_manifest, current_snapshot_id
from vkm_corpus.publish import transfer
from vkm_evidence.contracts import canonical_bytes, record_hash


def copier(cmd, **kwargs):
    """Faithful immutable files-from transport, used on hosts without rsync."""
    if cmd[0] == "ssh":
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    source, dest = Path(cmd[-2]), Path(cmd[-1])
    listing = next(Path(c.split("=", 1)[1]) for c in cmd if c.startswith("--files-from="))
    if "--dry-run" not in cmd:
        for name in listing.read_bytes().split(b"\0"):
            if not name:
                continue
            relative = name.decode("utf-8")
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(source / relative, target)
    return SimpleNamespace(returncode=0, stdout="Number of regular files transferred: 0", stderr="")


def make_prepared(env):
    import csv
    from vkm_corpus.extract.synthetic import make_register
    from vkm_corpus.registry.rules import QUICK_LOOK_MARKER
    from vkm_corpus.registry.importer import import_registry
    from vkm_corpus.parquet.runs import RunRecorder, describe_file
    layout = open_root(env.data_root)
    with (env.resources_root / "00_registry/SOURCE_REGISTER.csv").open(encoding="utf-8-sig", newline="") as stream:
        registered = list(csv.DictReader(stream))
    for row in registered:
        row["notes"] = QUICK_LOOK_MARKER
    make_register(env.resources_root, registered)
    from vkm_corpus.registry.works import WORK_REGISTER_COLUMNS, WORK_LINKS_COLUMNS, write_csv
    from vkm_corpus.testing.synthetic import _work, _link
    work = env.resources_root / "00_registry/work_registry"
    work.mkdir()
    write_csv(work / "WORK_REGISTER.csv", WORK_REGISTER_COLUMNS,
        [_work(int(r["resource_id"][-3:]), "MONOGRAPH", "Synthetic", "", None) for r in registered])
    write_csv(work / "WORK_LINKS.csv", WORK_LINKS_COLUMNS,
        [_link("SOURCE_WORK", r["resource_id"], "FULL_COPY", r["resource_id"].replace("SRC", "WRK"),
               is_primary="true") for r in registered])
    import_registry(layout, env.resources_root, workers=1)
    run = RunRecorder(layout, run_kind="EXTRACTION", cli_command="synthetic", host_role="WORKSTATION", run_id=RUN).start()
    sources, _, receipts = _run_all(env, RUN)
    for dataset in ("processing_steps", "errors"):
        for path in (layout.canonical / dataset / f"run={RUN}").glob("*.parquet"):
            run.files.append(describe_file(layout, dataset, layout.rel(path)))
    run.end()
    markers = {m["key"]: m for m in list_markers(layout)}
    policy_path = env.data_root.parent / "policy.json"
    policy_path.write_bytes(canonical_bytes({"schema": "vkm-source-policy/1", "policies": {
        s.source_id: {"access_class": "PRIVATE_CLOUD_ALLOWED", "experimental_role": "INPUT",
                      "policy_version": "synthetic1", "authority": "test"} for s in sources}}))
    context = AccessContext(principal="synthetic-operator", execution="LOCAL", granted_classes={"PRIVATE_CLOUD_ALLOWED"})
    source_items = []
    for source in sources:
        if source.source_id not in receipts:
            continue
        marker = markers[source.source_id]
        source_items.append(P.PublicationSource(source_id=source.source_id, source_sha256=source.sha256,
            commit_id=marker["commit_id"], marker=P.file_ref(layout.root, "canonical/" + marker["_path"]),
            binding=P.file_ref(layout.root, receipts[source.source_id]["accounting_receipt"]["path"])))
    request = P.PublicationRequest(campaign_sha256="a" * 64, policy_sha256=P.sha256_of(policy_path),
        max_total_bytes=128 * 1024 * 1024, sources=tuple(source_items),
        registry=P.file_ref(layout.root, "canonical/" + markers["REGISTRY"]["_path"]))
    ref = P.freeze_publication(layout.root, request, policy_path=policy_path, context=context)
    approval = P.PublicationApproval(descriptor_sha256=ref.sha256, campaign_sha256=request.campaign_sha256,
        policy_sha256=request.policy_sha256, source_ids=tuple(s.source_id for s in source_items), context=context)
    target = init_root(env.data_root.parent / "core", "CANONICAL")
    return SimpleNamespace(cfg=env, layout=layout, request=request, ref=ref, approval=approval,
                           policy=policy_path, context=context, target=target, receipts=receipts)


@pytest.fixture(scope="module")
def publication_seed(tmp_path_factory):
    # Generate the synthetic corpus once; each test gets independent byte copies.
    return make_prepared(env.__wrapped__(tmp_path_factory.mktemp("publication-seed")))


@pytest.fixture
def prepared(publication_seed, tmp_path):
    from copy import deepcopy
    seed = publication_seed
    stage = tmp_path / "staging"
    shutil.copytree(seed.layout.root, stage)
    policy = tmp_path / "policy.json"
    shutil.copyfile(seed.policy, policy)
    cfg = deepcopy(seed.cfg)
    cfg.data_root = stage
    return SimpleNamespace(cfg=cfg, layout=open_root(stage), request=seed.request, ref=seed.ref,
        approval=seed.approval, policy=policy, context=seed.context,
        target=init_root(tmp_path / "core", "CANONICAL"), receipts=seed.receipts)


def publish(p):
    return transfer.publish(p.layout.root, str(p.target.root), publication_approval=p.approval,
                            policy_path=p.policy, runner=copier)


def admitted_snapshot(p):
    publish(p)
    result = admit(p.target, publication_approval=p.approval, policy_path=p.policy)
    assert not result["rejected"] and not result["pending"], result
    snap = build_snapshot(p.target, publication_approval=p.approval, policy_path=p.policy)
    assert snap["status"] == "PASS", [c for c in snap["report"]["checks"] if c["status"] == "FAIL"]
    return load_manifest(p.target)


def test_real_producer_publication_snapshot_and_backup_restore_closure(prepared, tmp_path):
    p = prepared
    snapshot = admitted_snapshot(p)
    assert snapshot["accounting"]["status"] == "VERIFIED"
    assert P.snapshot_accounting(p.target.root, snapshot, approval=p.approval, policy_path=p.policy)["status"] == "VERIFIED"
    files = list(M.iter_files(p.target.root, M.CORE_SET))
    _, entries = M.build(p.target.root, files)
    restored = tmp_path / "restored"
    for rel in entries:
        target = restored / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p.target.root / rel, target)
    proof = M.verify(restored, entries)
    assert proof["verdict"] == "PASS" and proof["full_accounting_recovery"]
    assert P.snapshot_accounting(restored, snapshot, approval=p.approval, policy_path=p.policy)["status"] == "VERIFIED"
    old = current_snapshot_id(p.target)
    publish(p)  # same immutable bytes; prior CURRENT is a permitted lost-ACK retry
    assert not admit(p.target, publication_approval=p.approval, policy_path=p.policy)["admitted"]
    assert current_snapshot_id(p.target) == old
    replay = build_snapshot(p.target, publication_approval=p.approval, policy_path=p.policy)
    assert replay["noop"] and replay["snapshot_id"] == old


@pytest.mark.parametrize("missing", ["entire_tree", "descriptor", "binding", "report", "event", "blob"])
def test_missing_delivery_stays_pending_and_retry_works(prepared, missing):
    p = prepared
    publish(p)
    desc = P.verify_publication(p.target.root, p.approval, policy_path=p.policy)
    wanted = {"descriptor": p.ref.path, "binding": p.request.sources[0].binding.path,
        "report": next(f.path for f in desc.files if f.path.startswith("accounting/reports/")),
        "event": next(f.path for f in desc.files if f.path.startswith("accounting/events/")),
        "blob": next(f.path for f in desc.files if f.path.startswith("artifacts/"))}
    removed = []
    if missing == "entire_tree":
        removed = [f for f in (p.target.root / "accounting").rglob("*.json")]
    else:
        removed = [p.target.root / wanted[missing]]
    for path in removed:
        path.unlink()
    pending = admit(p.target, publication_approval=p.approval, policy_path=p.policy)
    assert pending["accounting"] == "PENDING" and not pending["rejected"]
    assert current_snapshot_id(p.target) is None
    publish(p)
    assert not admit(p.target, publication_approval=p.approval, policy_path=p.policy)["pending"]


def test_omitting_descriptor_cannot_admit_modern_commit_or_snapshot(prepared):
    p = prepared
    with pytest.raises(transfer.TransferError, match="APPROVAL_REQUIRED"):
        transfer.publish(p.layout.root, str(p.target.root), runner=copier)
    publish(p)
    result = admit(p.target)
    assert result["accounting"] == "PENDING" and result["pending"]
    admit(p.target, publication_approval=p.approval, policy_path=p.policy)
    snap = build_snapshot(p.target)
    assert snap["status"] == "FAIL" and not snap["current_moved"]


def test_policy_denial_precedes_any_descriptor_or_private_sidecar_read(prepared, monkeypatch):
    p = prepared
    approval = p.approval.model_copy(update={"context": AccessContext(principal="denied", execution="CLOUD")})
    original = P._json
    reads = []
    def observe(path, **kw):
        reads.append(path)
        assert path == p.policy
        return original(path, **kw)
    monkeypatch.setattr(P, "_json", observe)
    with pytest.raises(PermissionError):
        P.verify_publication(p.layout.root, approval, policy_path=p.policy)
    assert reads == [p.policy]


def test_corrupt_existing_target_is_not_hidden_by_ignore_existing(prepared):
    p = prepared
    publish(p)
    bad = p.target.root / p.request.sources[0].binding.path
    bad.write_bytes(b"synthetic corrupt")
    with pytest.raises((ValueError, KeyError)):
        publish(p)
    assert admit(p.target, publication_approval=p.approval, policy_path=p.policy)["accounting"] == "PENDING"


def test_backup_does_not_claim_full_recovery_without_accounting(prepared, tmp_path):
    p = prepared
    admitted_snapshot(p)
    _, entries = M.build(p.target.root, list(M.iter_files(p.target.root, M.CORE_SET)))
    restored = tmp_path / "only-canonical"
    for rel in entries:
        if rel.startswith("canonical/"):
            path = restored / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p.target.root / rel, path)
    proof = M.verify(restored, entries, ["canonical"])
    assert proof["verdict"] == "FAIL" and not proof["full_accounting_recovery"]


def test_remote_sync_failure_is_not_success(prepared):
    p = prepared
    def runner(cmd, **kw):
        return SimpleNamespace(returncode=1 if cmd[0] == "ssh" else 0, stdout="", stderr="")
    with pytest.raises(transfer.TransferError, match="REMOTE_SYNC_FAILED"):
        transfer.publish(p.layout.root, "synthetic:/not-contacted", publication_approval=p.approval,
                         policy_path=p.policy, runner=runner)


def test_new_event_after_freeze_never_silently_enters_descriptor(prepared):
    p = prepared
    before = P.verify_publication(p.layout.root, p.approval, policy_path=p.policy)
    extra = p.layout.root / "accounting/events/unused.json"
    extra.write_text("{}", encoding="utf-8")
    after = P.verify_publication(p.layout.root, p.approval, policy_path=p.policy)
    assert before.files == after.files and all(f.path != "accounting/events/unused.json" for f in after.files)


@pytest.mark.parametrize("path", ["../secret", "a/../secret", "/secret", "C:/secret", "a\\b", "a\nsecret"])
def test_unsafe_closure_paths_rejected_without_reads(path):
    with pytest.raises(ValueError):
        P.FileRef(path=path, sha256="a" * 64, size_bytes=0)


@pytest.mark.parametrize("change", ["source_hash", "foreign_binding", "byte_budget"])
def test_wrong_source_binding_and_byte_budget_fail_before_freeze(prepared, change):
    p = prepared
    sources = list(p.request.sources)
    update = {}
    if change == "source_hash":
        sources[0] = sources[0].model_copy(update={"source_sha256": "f" * 64})
    elif change == "foreign_binding":
        sources[0] = sources[0].model_copy(update={"binding": sources[1].binding})
    else:
        update["max_total_bytes"] = 1
    request = p.request.model_copy(update={"sources": tuple(sources), **update})
    with pytest.raises(ValueError):
        P.freeze_publication(p.layout.root, request, policy_path=p.policy, context=p.context)


@pytest.mark.parametrize("stage", ["content", "run-markers", "commit-markers", "publication-marker"])
def test_interrupted_transfer_never_admits_until_exact_retry(prepared, stage):
    p = prepared
    calls = []
    def fail(cmd, **kw):
        calls.append(cmd)
        # Groups are ordered and all are populated by the real producer fixture.
        name = ["content", "run-markers", "commit-markers", "publication-marker"][len(calls) - 1]
        if name == stage:
            return SimpleNamespace(returncode=23, stdout="", stderr="synthetic interrupted")
        return copier(cmd, **kw)
    with pytest.raises(transfer.TransferError):
        transfer.publish(p.layout.root, str(p.target.root), publication_approval=p.approval,
                         policy_path=p.policy, runner=fail)
    pending = admit(p.target, publication_approval=p.approval, policy_path=p.policy)
    assert pending["accounting"] == "PENDING" and not pending["rejected"]
    assert current_snapshot_id(p.target) is None
    publish(p)
    assert not admit(p.target, publication_approval=p.approval, policy_path=p.policy)["pending"]


def test_snapshot_head_substitution_and_stripped_binding_are_blocked(prepared):
    p = prepared
    snapshot = admitted_snapshot(p)
    from copy import deepcopy
    forged = deepcopy(snapshot)
    forged["source_heads"]["VKM-SRC-901"] = "CMT-" + "f" * 16
    with pytest.raises(P.PublicationBlocked, match="HEADS_MISMATCH"):
        P.snapshot_accounting(p.target.root, forged, approval=p.approval, policy_path=p.policy)
    forged = deepcopy(snapshot)
    forged["inputs"] = {}
    forged["head_commits"] = {}
    with pytest.raises(P.PublicationBlocked, match="BINDING_REQUIRED"):
        P.snapshot_accounting(p.target.root, forged)
    forged = deepcopy(snapshot)
    forged["datasets"]["blocks"]["files"] = []
    with pytest.raises(P.PublicationBlocked, match="PARTITIONS_MISMATCH"):
        P.snapshot_accounting(p.target.root, forged, approval=p.approval, policy_path=p.policy)


def test_changed_operator_policy_and_descriptor_pin_fail(prepared):
    p = prepared
    with pytest.raises(P.PublicationBlocked):
        P.verify_publication(p.layout.root, p.approval.model_copy(update={"descriptor_sha256": "f" * 64}), policy_path=p.policy)
    p.policy.write_bytes(p.policy.read_bytes() + b" ")
    with pytest.raises(P.PublicationBlocked, match="POLICY_CHANGED"):
        P.verify_publication(p.layout.root, p.approval, policy_path=p.policy)


def test_symlink_check_precedes_read_even_when_target_stays_inside_root(prepared, monkeypatch):
    p = prepared
    wanted = p.layout.root / p.ref.path
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == wanted or original(path))
    with pytest.raises(ValueError, match="PATH_LINK"):
        P.verify_publication(p.layout.root, p.approval, policy_path=p.policy)


def test_legacy_accounting_is_explicitly_not_available(tmp_path):
    from vkm_corpus.testing import synthetic_canon
    canon = synthetic_canon(tmp_path / "legacy")
    assert P.snapshot_accounting(canon.layout.root, canon.manifest)["status"] == "NOT_AVAILABLE"
    assert canon.manifest["accounting"]["status"] == "NOT_AVAILABLE"


def test_accounted_chain_cannot_downgrade_through_legacy_writer(prepared):
    from vkm_corpus.parquet.commits import commit_source
    from vkm_corpus.contracts.datasets import DOCUMENT_DATASETS
    p = prepared
    source = p.request.sources[0]
    with pytest.raises(ValueError, match="DOWNGRADE_FORBIDDEN"):
        commit_source(p.layout, source_id=source.source_id, source_sha256=source.source_sha256,
            run_id="RUN-20261001T120000Z-000000ff", tables={k: [] for k in DOCUMENT_DATASETS}, require_lease=False)


def test_receiver_downgrade_and_preexisting_bad_admission_cannot_move_current(prepared):
    from vkm_corpus import ids
    from vkm_corpus.parquet.atomic import write_json
    p = prepared
    snapshot = admitted_snapshot(p)
    old = current_snapshot_id(p.target)
    source = p.request.sources[0]
    marker = json.loads((p.target.root / source.marker.path).read_bytes())
    marker.pop("accounting_required")
    marker["parent_commit_id"] = marker["commit_id"]
    marker["commit_id"] = ids.commit_id(marker)
    relative = f"_commits/run={marker['processing_run_id']}/{source.source_id}__{marker['commit_id']}.json"
    write_json(p.target.tmp, p.target.path(relative), marker)
    result = admit(p.target)
    assert marker["commit_id"] in result["pending"] and result["accounting"] == "PENDING"
    assert not result["admitted"] and not result["rejected"]
    # A previously deployed buggy receiver may already have written ADMITTED.
    # Read-time validation must independently protect CURRENT and recovery.
    write_json(p.target.tmp, p.target.path(p.target.admission(marker["commit_id"])), {
        "commit_id": marker["commit_id"], "key": source.source_id, "status": "ADMITTED",
        "parent_commit_id": marker["parent_commit_id"], "marker_path": relative})
    blocked = build_snapshot(p.target)
    assert blocked["status"] == "FAIL" and current_snapshot_id(p.target) == old
    from copy import deepcopy
    forged = deepcopy(snapshot)
    forged["inputs"] = {}
    forged["source_heads"][source.source_id] = marker["commit_id"]
    forged["head_commits"][source.source_id].update(commit_id=marker["commit_id"], marker_path=relative)
    with pytest.raises(ValueError, match="DOWNGRADE_FORBIDDEN"):
        P.snapshot_accounting(p.target.root, forged)
    snapshot_path = p.target.path(p.target.snapshot_manifest(old))
    snapshot_path.write_bytes(canonical_bytes(forged))
    _, entries = M.build(p.target.root, list(M.iter_files(p.target.root, M.CORE_SET)))
    assert M.verify_accounting_closure(p.target.root, entries)["status"] == "FAIL"


@pytest.mark.parametrize("dataset,attribution", [
    ("processing_steps", "source_id"), ("artifacts", "registered_source_id"),
    ("sources", "source_id"), ("works", "anchor_source_id")])
def test_actual_partition_inventory_denies_unclassified_sources_before_payload_hash(prepared, dataset, attribution, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from vkm_corpus import ids
    p = prepared
    request = p.request
    if dataset in {"sources", "works"}:
        marker = json.loads((p.layout.root / request.registry.path).read_bytes())
        path = p.layout.canonical / marker["datasets"][dataset]["path"]
    else:
        original = next((p.layout.canonical / dataset / f"run={RUN}").glob("**/*.parquet"))
        path = original.with_name("part-foreign.parquet")
        shutil.copyfile(original, path)
    table = pq.ParquetFile(path).read()
    assert table.num_rows
    index = table.schema.get_field_index(attribution)
    table = table.set_column(index, table.schema.field(index), pa.array(["VKM-SRC-999"] * table.num_rows))
    pq.write_table(table, path)
    if dataset in {"sources", "works"}:
        marker["datasets"][dataset].update(sha256=P.sha256_of(path), bytes=path.stat().st_size)
        marker["commit_id"] = ids.commit_id(marker)
        relative = f"canonical/_commits/run={marker['processing_run_id']}/REGISTRY__{marker['commit_id']}.json"
        (p.layout.root / relative).write_bytes(canonical_bytes(marker))
        request = request.model_copy(update={"registry": P.file_ref(p.layout.root, relative)})
    original_hash = P.sha256_of
    def spy(candidate):
        assert Path(candidate) != path, "private payload hashed before actual attribution policy check"
        return original_hash(candidate)
    monkeypatch.setattr(P, "sha256_of", spy)
    with pytest.raises(PermissionError, match="UNCLASSIFIED"):
        P.freeze_publication(p.layout.root, request, policy_path=p.policy, context=p.context)


def test_snapshot_cannot_include_unapproved_run_partition(prepared):
    from copy import deepcopy
    p = prepared
    snapshot = admitted_snapshot(p)
    forged = deepcopy(snapshot)
    entry = deepcopy(forged["datasets"]["processing_steps"]["files"][0])
    entry["path"] = "processing_steps/run=RUN-20261001T120000Z-ffffffff/part-000000.parquet"
    forged["datasets"]["processing_steps"]["files"].append(entry)
    with pytest.raises(P.PublicationBlocked, match="UNAPPROVED_PARTITION"):
        P.snapshot_accounting(p.target.root, forged, approval=p.approval, policy_path=p.policy)


def test_lost_ack_retry_cannot_reuse_tampered_current_partition_map(prepared):
    p = prepared
    snapshot = admitted_snapshot(p)
    current = current_snapshot_id(p.target)
    snapshot["datasets"]["blocks"]["files"] = []
    path = p.target.path(p.target.snapshot_manifest(current))
    path.write_bytes(canonical_bytes(snapshot))
    with pytest.raises(P.PublicationBlocked, match="RETRY_MANIFEST_CHANGED"):
        build_snapshot(p.target, publication_approval=p.approval, policy_path=p.policy)
    assert current_snapshot_id(p.target) == current  # no automatic replacement/repair


def test_pinned_base_closure_is_carried_without_reprocessing_and_restorable(prepared):
    p = prepared
    snapshot = admitted_snapshot(p)
    relative = "canonical/" + p.target.snapshot_manifest(snapshot["snapshot_id"])
    destination = p.layout.root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(p.target.root / relative, destination)
    base = P.SnapshotBase(snapshot_id=snapshot["snapshot_id"], manifest_sha256=P.sha256_of(destination),
        source_heads=snapshot["source_heads"], registry_head=snapshot["registry_head"])
    request = p.request.model_copy(update={"base": base, "campaign_sha256": "b" * 64})
    ref = P.freeze_publication(p.layout.root, request, policy_path=p.policy, context=p.context)
    approval = p.approval.model_copy(update={"descriptor_sha256": ref.sha256, "campaign_sha256": "b" * 64})
    descriptor = P.verify_publication(p.layout.root, approval, policy_path=p.policy)
    assert relative in {f.path for f in descriptor.files}
    transfer.publish(p.layout.root, str(p.target.root), publication_approval=approval, policy_path=p.policy, runner=copier)
    assert not admit(p.target, publication_approval=approval, policy_path=p.policy)["pending"]
    result = build_snapshot(p.target, publication_approval=approval, policy_path=p.policy)
    assert result["status"] == "PASS", result["report"]
    _, entries = M.build(p.target.root, list(M.iter_files(p.target.root, M.CORE_SET)))
    assert M.verify(p.target.root, entries)["full_accounting_recovery"]
    (p.target.root / relative).unlink()
    _, entries = M.build(p.target.root, list(M.iter_files(p.target.root, M.CORE_SET)))
    assert M.verify_accounting_closure(p.target.root, entries)["status"] == "FAIL"


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed: NOT_RUN")
def test_actual_local_rsync_transfers_only_pinned_closure(prepared):
    p = prepared
    transfer.publish(p.layout.root, str(p.target.root), publication_approval=p.approval, policy_path=p.policy)
    assert P.verify_publication(p.target.root, p.approval, policy_path=p.policy, receiving=True)
    assert not (p.target.root / "accounting/tmp").exists()
    assert not any((p.target.root / "cache").rglob("*"))  # init_root creates an empty cache directory


def test_windows_drive_is_local_receiving_target_not_ssh_host():
    for value in ("C:/synthetic/core", "C:\\synthetic\\core"):
        assert transfer._split_remote(value) == (None, value)
    assert transfer._split_remote("core:/synthetic") == ("core", "/synthetic")


def test_empty_self_consistent_ledger_cannot_hide_actual_pages_or_outputs(prepared):
    from vkm_corpus.coverage import accounting as A
    p = prepared
    source = p.request.sources[0]
    receipt = json.loads((p.layout.root / source.binding.path).read_bytes())
    report = json.loads((p.layout.root / receipt["report"]["path"]).read_bytes())
    for key in ("units", "inspected_units", "candidates", "attempts"):
        report["ledger"][key] = []
    ledger = A.CoverageLedger.model_validate(report["ledger"])
    report["report"].update(ledger.report())
    new_report = A.publish_report(p.layout.root, report)
    new_receipt = A._publish(p.layout.root, "commits", {**receipt, "report": new_report})
    altered = source.model_copy(update={"binding": P.file_ref(p.layout.root, new_receipt["path"])})
    request = p.request.model_copy(update={"sources": (altered, *p.request.sources[1:])})
    with pytest.raises(P.PublicationBlocked, match="DENOMINATOR_MISMATCH"):
        P.freeze_publication(p.layout.root, request, policy_path=p.policy, context=p.context)


def test_cli_freeze_reuses_exact_descriptor_and_missing_approval_is_nonzero(prepared, tmp_path, capsys, monkeypatch):
    from vkm_corpus.cli import main
    p = prepared
    request, context = tmp_path / "request.json", tmp_path / "context.json"
    request.write_bytes(canonical_bytes(p.request))
    context.write_bytes(canonical_bytes(p.context))
    assert main(["core", "publication-freeze", "--staging", str(p.layout.root), "--request", str(request),
        "--request-sha256", P.sha256_of(request), "--context", str(context), "--context-sha256", P.sha256_of(context),
        "--policy", str(p.policy)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["descriptor"]["sha256"] == p.ref.sha256 and output["publication"] == "NOT_RUN"
    assert main(["core", "publish", "--staging", str(p.layout.root), "--target", str(p.target.root)]) != 0
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    publish(p)
    monkeypatch.setenv("VKM_DATA_ROOT", str(p.target.root))
    monkeypatch.setenv("VKM_DATA_ROLE", "canonical")
    assert main(["canon", "admit"]) != 0
    assert json.loads(capsys.readouterr().out)["accounting"] == "PENDING"
    approved = tmp_path / "approved.json"
    approved.write_bytes(canonical_bytes(p.approval))
    flags = ["--approval", str(approved), "--approval-sha256", P.sha256_of(approved), "--policy", str(p.policy)]
    assert main(["canon", "admit", *flags]) == 0
    capsys.readouterr()
    assert main(["canon", "snapshot", *flags]) == 0
    assert json.loads(capsys.readouterr().out)["accounting"]["status"] == "VERIFIED"
    assert main(["canon", "validate", *flags]) == 0
    assert json.loads(capsys.readouterr().out)["accounting"]["status"] == "VERIFIED"


def test_cli_invalid_operator_input_never_echoes_private_values(prepared, tmp_path, capsys):
    from vkm_corpus.cli import main
    p = prepared
    bad = tmp_path / "invalid-approval.json"
    bad.write_text('{"private_value":"synthetic-secret-sentinel"}', encoding="utf-8")
    code = main(["core", "publish", "--staging", str(p.layout.root), "--target", str(p.target.root),
        "--approval", str(bad), "--approval-sha256", P.sha256_of(bad), "--policy", str(p.policy)])
    captured = capsys.readouterr()
    assert code != 0 and "synthetic-secret-sentinel" not in captured.out + captured.err
