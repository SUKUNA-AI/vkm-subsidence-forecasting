"""Complete typed update-cycle drills on synthetic originals; no production intake."""
from dataclasses import replace
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import zipfile

import pytest

from vkm_corpus.contracts.access import ResourcePolicy
from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.registry.rules import REGISTER_COLUMNS
from vkm_corpus.update.contracts import CampaignInput, CampaignManifest
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.runtime import (BoundFile, Observation, RuntimeConfig, UpdateRuntime,
    observe_components, stage_request_id, worker)
from vkm_datasets import DatasetVersion, Policy, discover
from vkm_datasets.catalogue import verify_catalogue
from vkm_datasets.manifest import canonical_bytes as manifest_bytes
from vkm_evidence.contracts import canonical_bytes

CODE = "b" * 40
SHA = "a" * 64
IDENTITY = {"code_revision": CODE, "code_dirty": False, "profile": "production", "identity_sha256": "c" * 64}


def put(path, value):
    raw = value if isinstance(value, bytes) else canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return BoundFile(path=str(path), sha256=sha256_of(path))


def workbook(path):
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Synthetic" sheetId="1" r:id="r1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData><row r="1"><c r="A1"><f>1+2</f><v>3</v></c></row></sheetData></worksheet>')


def fixture(tmp_path, monkeypatch, *, vector=False, access="PRIVATE_CLOUD_ALLOWED", role="INPUT", execution="CLOUD", allow_targets=False, linked=False):
    original = tmp_path / "originals" / "quarantine"
    original.mkdir(parents=True)
    name = "sample.gpkg" if vector else "sample.xlsx"
    if vector:
        (original / name).write_bytes(b"synthetic vector input")
    else:
        workbook(original / name)
    version = DatasetVersion("dataset-a", discover(original, (name,)), (name,), Policy(access, role, "owner-decision"),
        "synthetic-owner", "synthetic-only", "2026-10-01T00:00:00Z", source_ids=("VKM-SRC-001",) if linked else ())
    policy = ResourcePolicy(access_class=access, experimental_role=role, policy_version="1", authority="owner")
    policies = {"DATASET:dataset-a": policy.model_dump(mode="json")}
    if linked:
        policies["VKM-SRC-001"] = ResourcePolicy(access_class="PUBLIC", policy_version="1", authority="owner").model_dump(mode="json")
    cfg_policy = put(tmp_path / "policy.json", {"schema": "vkm-source-policy/1", "policies": policies})
    artifacts = {"manifest": put(tmp_path / "manifest.json", manifest_bytes(version.as_dict())),
        "context": put(tmp_path / "context.json", {"principal": "owner", "execution": execution,
            "granted_classes": [access, "PUBLIC"], "allow_targets": allow_targets})}
    item = dict(dataset_id=version.dataset_id, version_sha256=version.digest, manifest_artifact="manifest", originals_prefix="quarantine")
    if linked:
        text = io.StringIO(newline="")
        writer = csv.DictWriter(text, fieldnames=REGISTER_COLUMNS)
        writer.writeheader()
        writer.writerow({**{k: "" for k in REGISTER_COLUMNS}, "resource_id": "VKM-SRC-001", "sha256": SHA})
        artifacts["register"] = put(tmp_path / "SOURCE_REGISTER.csv", text.getvalue().encode("utf-8"))
        item.update(source_register_artifact="register", source_links=({"source_id": "VKM-SRC-001", "source_sha256": SHA, "relation": "DESCRIBED_BY"},))
    config = RuntimeConfig(runtime_root=str(tmp_path / "runtime"), originals_root=str(tmp_path / "originals"),
        policy=cfg_policy, qualification_root=str(tmp_path / "qualifications"), expected_commit=CODE,
        memory_budget_gib=3, memory_reserve_gib=1, worker_memory_gib=2, min_free_disk_gib=.001, artifacts=artifacts)
    rt = UpdateRuntime(config)
    monkeypatch.setattr(UpdateRuntime, "identity", lambda self: dict(IDENTITY))
    monkeypatch.setattr("vkm_corpus.pipeline.context.available_memory_bytes", lambda: 8 * 2**30)
    def stage(sid, operation, parents=(), **opts):
        return {"stage_id": sid, "operation": operation, "depends_on": parents,
            "options": {"context_artifact": "context", **opts}, "input_sha256": SHA, "config_sha256": SHA,
            "memory_gib": 2, "min_free_disk_gib": 0, "timeout_seconds": 30, "compute": "CPU", "attempts": 2}
    stages = [stage("verify", "DATASET_VERIFY", dataset_id=version.dataset_id),
        stage("inspect", "DATASET_INSPECT", ("verify",), dataset_id=version.dataset_id, entrypoint=name, include_values=not vector)]
    if vector:
        stages.append(stage("convert", "DATASET_CONVERT", ("inspect",), dataset_id=version.dataset_id, entrypoint=name))
    stages.append(stage("register", "DATASET_REGISTER", (stages[-1]["stage_id"],), dataset_ids=(version.dataset_id,)))
    campaign = CampaignManifest(campaign_id="synthetic", code_commit=CODE, producer_identity_sha256=IDENTITY["identity_sha256"],
        policy_sha256=cfg_policy.sha256, inputs=(), dataset_inputs=(item,), stages=stages, gates=())
    campaign = bind(rt, campaign)
    calls = []
    def child(argv, **kw):
        calls.append(argv)
        return worker(Path(argv[-1]))
    monkeypatch.setattr("vkm_corpus.update.runtime.bounded_subprocess", child)
    return rt, campaign, version, original, calls


def bind(rt, campaign):
    return campaign.model_copy(update={"stages": tuple(s.model_copy(update=rt.stage_bindings(campaign, s)) for s in campaign.stages)})


def execute(rt, campaign):
    plan = rt.plan(campaign)
    assert plan["status"] == "READY", plan
    return rt.execute(campaign, plan["plan_sha256"])


def snapshot(rt):
    path, = (rt.root / "datasets" / "catalogues").glob("*.json")
    return path, verify_catalogue(rt.root, path)


def fake_vector(monkeypatch):
    from vkm_datasets import gis
    import vkm_datasets.update as update
    find_spec = update.importlib.util.find_spec
    monkeypatch.setattr(update.importlib.util, "find_spec", lambda name: object() if name == "osgeo" else find_spec(name))
    calls = []
    def translate(path, *args, **kwargs):
        calls.append(kwargs)
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE synthetic(id INTEGER)")
            db.commit()
        finally:
            db.close()
        return object()
    engine = SimpleNamespace(VersionInfo=lambda _: "synthetic", VectorTranslate=translate)
    monkeypatch.setattr(gis, "_engine", lambda: (engine, None))
    monkeypatch.setattr(gis, "_open", lambda _: SimpleNamespace(GetDriver=lambda: SimpleNamespace(ShortName="synthetic")))
    monkeypatch.setattr(gis, "_scan", lambda *args: ([], []))
    return calls, engine


def test_workbook_complete_cycle_replay_catalogue_and_native_observer(tmp_path, monkeypatch):
    rt, campaign, version, originals, calls = fixture(tmp_path, monkeypatch, linked=True)
    assert execute(rt, campaign)["status"] == "PASS"
    path, cat = snapshot(rt)
    assert cat.entries[0].source_links[0].source_sha256 == SHA
    assert cat.entries[0].source_register_sha256 == rt.config.artifacts["register"].sha256
    assert cat.entries[0].version_sha256 == version.digest
    assert cat.scientific_admission == "NOT_ESTABLISHED"
    assert execute(rt, campaign)["status"] == "PASS" and len(calls) == 3
    binding = put(tmp_path / "binding.json", {"status": "PASS", "policy_sha256": campaign.policy_sha256,
        "component_manifest_sha256": sha256_of(path)})
    spec = Observation(component="DATASET", native_manifest=str(path), policy_binding=binding.path,
        dataset_root=str(rt.root), dataset_policy_file=rt.config.policy.path)
    assert observe_components((spec,))["DATASET"]["revision"] == cat.sha256
    inspected = next(rt.root.glob("attempts/*/inspection.json"))
    inspected.write_bytes(b"changed")
    with pytest.raises(GenerationUnavailable):
        observe_components((spec,))
    assert rt.plan(campaign)["status"] == "BLOCKED"


def test_vector_complete_cycle_uses_existing_converter_and_rechecks_engine(tmp_path, monkeypatch):
    translations, engine = fake_vector(monkeypatch)
    rt, campaign, version, originals, calls = fixture(tmp_path, monkeypatch, vector=True)
    assert execute(rt, campaign)["status"] == "PASS"
    _, cat = snapshot(rt)
    assert set(cat.entries[0].conversions) == {"sample.gpkg"}
    assert execute(rt, campaign)["status"] == "PASS" and len(translations) == 1
    engine.GetConfigOptions = lambda: {"CHANGED_RULE": "yes"}
    assert rt.plan(campaign)["status"] == "BLOCKED"
    assert len(translations) == 1


def test_legacy_document_dataset_hash_is_rejected_not_ignored():
    with pytest.raises(ValueError, match="CampaignDatasetInput"):
        CampaignInput(source_id="VKM-SRC-001", source_sha256=SHA, logical_path="x", size_bytes=1,
                      lifecycle="ACTIVE", dataset_version_sha256=SHA)


@pytest.mark.parametrize("access,role,execution,targets", [("PRIVATE_LOCAL_ONLY", "INPUT", "CLOUD", False),
    ("PRIVATE_CLOUD_ALLOWED", "TARGET", "CLOUD", False), ("PRIVATE_CLOUD_ALLOWED", "TEST_SEALED", "LOCAL", True)])
def test_policy_denied_before_manifest_or_originals_are_opened(tmp_path, monkeypatch, access, role, execution, targets):
    rt, campaign, *_ = fixture(tmp_path, monkeypatch, access=access, role=role, execution=execution, allow_targets=targets)
    import vkm_datasets.update as update
    def no_inspection(*args):
        pytest.fail("denied dataset was inspected")
    monkeypatch.setattr(update, "verify_members", no_inspection)
    real_bound = update._bound
    def no_manifest(runtime, alias):
        if alias == "manifest":
            pytest.fail("denied dataset manifest was inspected")
        return real_bound(runtime, alias)
    monkeypatch.setattr(update, "_bound", no_manifest)
    assert rt.plan(campaign)["status"] == "BLOCKED"
    assert not rt.root.exists()


@pytest.mark.parametrize("field", ["version", "source"])
def test_wrong_dataset_or_source_version_blocked(tmp_path, monkeypatch, field):
    rt, campaign, *_ = fixture(tmp_path, monkeypatch, linked=True)
    item = campaign.dataset_inputs[0]
    changes = {"version_sha256": "d" * 64} if field == "version" else {
        "source_links": (item.source_links[0].model_copy(update={"source_sha256": "d" * 64}),)}
    campaign = bind(rt, campaign.model_copy(update={"dataset_inputs": (item.model_copy(update=changes),)}))
    assert rt.plan(campaign)["status"] == "BLOCKED"


def test_source_bytes_or_policy_change_invalidates_completed_replay(tmp_path, monkeypatch):
    rt, campaign, version, originals, calls = fixture(tmp_path, monkeypatch)
    assert execute(rt, campaign)["status"] == "PASS"
    path = originals / "sample.xlsx"
    old = path.stat(); raw = path.read_bytes()
    path.write_bytes(b"X" + raw[1:]); os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert rt.plan(campaign)["status"] == "BLOCKED"
    path.write_bytes(raw)
    policy = Path(rt.config.policy.path)
    policy.write_bytes(policy.read_bytes() + b" ")
    assert rt.plan(campaign)["status"] == "BLOCKED" and len(calls) == 3


def test_missing_inspection_dependency_never_registers_complete_dataset(tmp_path, monkeypatch):
    rt, campaign, *_ = fixture(tmp_path, monkeypatch)
    stages = tuple(s.model_copy(update={"depends_on": ("verify",)}) if s.stage_id == "register" else s for s in campaign.stages)
    campaign = bind(rt, campaign.model_copy(update={"stages": stages}))
    assert "DATASET_DEPENDENCY_MISSING_OR_AMBIGUOUS" in rt.plan(campaign)["reasons"]


def test_arbitrary_dataset_option_is_schema_error(tmp_path, monkeypatch):
    _, campaign, *_ = fixture(tmp_path, monkeypatch)
    raw = campaign.model_dump(mode="json")
    raw["stages"][0]["options"]["command"] = "untrusted"
    with pytest.raises(ValueError):
        CampaignManifest.model_validate(raw)


def test_unsupported_project_is_not_silent_empty_inspection(tmp_path, monkeypatch):
    rt, campaign, version, originals, _ = fixture(tmp_path, monkeypatch)
    (originals / "project.qgs").write_bytes(b"synthetic QGIS project")
    version = replace(version, files=discover(originals, ("project.qgs",)), entrypoints=("project.qgs",))
    changed = rt.config.artifacts.copy()
    changed["manifest"] = put(tmp_path / "manifest-qgs.json", manifest_bytes(version.as_dict()))
    rt = UpdateRuntime(rt.config.model_copy(update={"artifacts": changed}))
    item = campaign.dataset_inputs[0].model_copy(update={"version_sha256": version.digest})
    stages = tuple(s.model_copy(update={"options": {**s.options, "entrypoint": "project.qgs"}}) if s.operation == "DATASET_INSPECT" else s for s in campaign.stages)
    campaign = bind(rt, campaign.model_copy(update={"dataset_inputs": (item,), "stages": stages}))
    assert "DATASET_INSPECTOR_UNQUALIFIED" in rt.plan(campaign)["reasons"]


def test_interrupted_inspection_has_no_catalogue_and_requires_new_campaign(tmp_path, monkeypatch):
    rt, campaign, *_ = fixture(tmp_path, monkeypatch)
    from vkm_datasets import catalogue
    write = catalogue.write_once
    def fail(path, raw):
        if path.name == "inspection-receipt.json":
            raise OSError("synthetic interruption")
        return write(path, raw)
    monkeypatch.setattr(catalogue, "write_once", fail)
    assert execute(rt, campaign)["status"] == "FAIL"
    assert not list(rt.root.glob("datasets/catalogues/*.json"))
    assert "INCOMPLETE_INSPECTION_REQUIRES_NEW_CAMPAIGN" in rt.plan(campaign)["reasons"]


def test_conversion_without_receipt_is_blocked_before_second_translation(tmp_path, monkeypatch):
    translations, _ = fake_vector(monkeypatch)
    rt, campaign, *_ = fixture(tmp_path, monkeypatch, vector=True)
    stage = next(s for s in campaign.stages if s.operation == "DATASET_CONVERT")
    pending = rt.root / "attempts" / stage_request_id(campaign, stage) / "conversion"
    pending.mkdir(parents=True)
    (pending / "candidate.gpkg").write_bytes(b"interrupted")
    assert "INCOMPLETE_CONVERSION_REQUIRES_NEW_CAMPAIGN" in rt.plan(campaign)["reasons"]
    assert translations == []


def successor(rt, campaign, version, tmp_path, previous, *, lifecycle="ACTIVE", policy=None):
    artifacts = dict(rt.config.artifacts)
    artifacts["manifest"] = put(tmp_path / (version.digest + ".json"), manifest_bytes(version.as_dict()))
    artifacts["previous"] = BoundFile(path=str(previous), sha256=sha256_of(previous))
    cfg = rt.config.model_copy(update={"artifacts": artifacts, **({"policy": policy} if policy else {})})
    newer = UpdateRuntime(cfg)
    item = campaign.dataset_inputs[0].model_copy(update={"version_sha256": version.digest, "lifecycle": lifecycle,
        "reason": "synthetic lifecycle decision" if lifecycle != "ACTIVE" else None})
    stages = []
    for stage in campaign.stages:
        if lifecycle != "ACTIVE" and stage.operation in {"DATASET_INSPECT", "DATASET_CONVERT"}:
            continue
        if stage.operation == "DATASET_REGISTER":
            stage = stage.model_copy(update={"options": {**stage.options, "previous_snapshot_artifact": "previous"},
                "depends_on": (stages[-1].stage_id,)})
        stages.append(stage)
    plan = campaign.model_copy(update={"campaign_id": "successor", "dataset_inputs": (item,), "stages": tuple(stages),
        "policy_sha256": cfg.policy.sha256})
    return newer, bind(newer, plan)


def test_new_version_selects_explicit_parent_and_preserves_previous_catalogue(tmp_path, monkeypatch):
    rt, campaign, version, originals, _ = fixture(tmp_path, monkeypatch)
    assert execute(rt, campaign)["status"] == "PASS"
    old_path, old = snapshot(rt)
    original_snapshot = old_path.read_bytes()
    new_version = replace(version, parents=(version.digest,), data_valid_at="2026-10-02")
    newer, new_campaign = successor(rt, campaign, new_version, tmp_path, old_path)
    assert execute(newer, new_campaign)["status"] == "PASS"
    selected = [verify_catalogue(rt.root, p) for p in old_path.parent.glob("*.json") if p != old_path]
    assert len(selected) == 1 and selected[0].previous_snapshot_sha256 == old.sha256
    assert selected[0].entries[0].version_sha256 == new_version.digest
    assert old_path.read_bytes() == original_snapshot
    assert execute(newer, new_campaign)["status"] == "PASS"


def test_unrelated_replacement_is_blocked_before_native_work(tmp_path, monkeypatch):
    rt, campaign, version, _, calls = fixture(tmp_path, monkeypatch)
    assert execute(rt, campaign)["status"] == "PASS"
    old_path, _ = snapshot(rt)
    newer, updated = successor(rt, campaign, replace(version, data_valid_at="2026-10-02"), tmp_path, old_path)
    assert "DATASET_PARENT_SELECTION_MISSING" in newer.plan(updated)["reasons"]
    assert len(calls) == 3


def test_authoritative_policy_cannot_silently_widen_prior_dataset(tmp_path, monkeypatch):
    rt, campaign, version, _, _ = fixture(tmp_path, monkeypatch)
    assert execute(rt, campaign)["status"] == "PASS"
    old_path, _ = snapshot(rt)
    policy = ResourcePolicy(access_class="PUBLIC", experimental_role="INPUT", policy_version="2", authority="owner")
    bound = put(tmp_path / "policy-v2.json", {"schema": "vkm-source-policy/1", "policies": {"DATASET:dataset-a": policy.model_dump(mode="json")}})
    changed = replace(version, policy=Policy("PUBLIC", "INPUT", "synthetic-new-decision"), parents=(version.digest,))
    newer, updated = successor(rt, campaign, changed, tmp_path, old_path, policy=bound)
    assert "DATASET_POLICY_DOWNGRADE" in newer.plan(updated)["reasons"]


def test_explicit_retirement_does_not_read_original_or_serve_old_facets(tmp_path, monkeypatch):
    rt, campaign, version, _, _ = fixture(tmp_path, monkeypatch)
    assert execute(rt, campaign)["status"] == "PASS"
    old_path, _ = snapshot(rt)
    newer, updated = successor(rt, campaign, version, tmp_path, old_path, lifecycle="RETIRED")
    monkeypatch.setattr("vkm_datasets.update.verify_members", lambda *args: pytest.fail("retirement read originals"))
    assert execute(newer, updated)["status"] == "PASS"
    cat = next(verify_catalogue(rt.root, p) for p in old_path.parent.glob("*.json") if p != old_path)
    assert cat.entries[0].lifecycle == "RETIRED" and cat.entries[0].reason
    assert cat.entries[0].inspections == cat.entries[0].conversions == {}
    verification = newer.root / "attempts" / stage_request_id(updated, updated.stages[0]) / "operation.json"
    assert json.loads(verification.read_bytes())["gate"] == "DATASET_LIFECYCLE_METADATA_ONLY"


def test_linked_source_policy_cannot_be_bypassed_by_catalogue_observer(tmp_path, monkeypatch):
    rt, campaign, _, _, _ = fixture(tmp_path, monkeypatch, linked=True)
    assert execute(rt, campaign)["status"] == "PASS"
    path, _ = snapshot(rt)
    from vkm_corpus.contracts.policy_store import SourcePolicyStore
    policies = SourcePolicyStore(Path(rt.config.policy.path), lambda: ()).read()
    policies["VKM-SRC-001"] = ResourcePolicy(access_class="PRIVATE_LOCAL_ONLY", policy_version="2", authority="owner")
    from vkm_datasets.catalogue import DatasetBlocked
    with pytest.raises(DatasetBlocked, match="SOURCE_POLICY_WIDENING"):
        verify_catalogue(rt.root, path, policies=policies)


@pytest.mark.parametrize("mutation", ["register", "policy", "orphan"])
def test_invalid_source_link_never_reaches_inspector(tmp_path, monkeypatch, mutation):
    rt, campaign, version, _, calls = fixture(tmp_path, monkeypatch, linked=True)
    if mutation == "register":
        path = Path(rt.config.artifacts["register"].path)
        path.write_bytes(path.read_bytes().replace(SHA.encode(), ("f" * 64).encode()))
    elif mutation == "policy":
        body = json.loads(Path(rt.config.policy.path).read_bytes())
        body["policies"].pop("VKM-SRC-001")
        cfg = rt.config.model_copy(update={"policy": put(tmp_path / "policy-no-source.json", body)})
        rt = UpdateRuntime(cfg)
        campaign = bind(rt, campaign.model_copy(update={"policy_sha256": cfg.policy.sha256}))
    else:
        changed = replace(version, source_ids=())
        cfg = rt.config.model_copy(update={"artifacts": {**rt.config.artifacts,
            "manifest": put(tmp_path / "orphan-manifest.json", manifest_bytes(changed.as_dict()))}})
        rt = UpdateRuntime(cfg)
        campaign = bind(rt, campaign.model_copy(update={"dataset_inputs": (
            campaign.dataset_inputs[0].model_copy(update={"version_sha256": changed.digest}),)}))
    assert rt.plan(campaign)["status"] == "BLOCKED" and not calls


def test_added_unmanifested_companion_blocks_before_engine(tmp_path, monkeypatch):
    rt, campaign, version, originals, calls = fixture(tmp_path, monkeypatch, vector=True)
    (originals / "map.tab").write_bytes(b'!table\n!version 300\nDefinition Table\n Type NATIVE Charset "WindowsLatin1"\n')
    (originals / "map.dat").write_bytes(b"synthetic")
    (originals / "map.map").write_bytes(b"synthetic")
    (originals / "map.id").write_bytes(b"synthetic")
    replacement = replace(version, files=discover(originals, ("map.tab",)), entrypoints=("map.tab",))
    artifacts = {**rt.config.artifacts, "manifest": put(tmp_path / "map.json", manifest_bytes(replacement.as_dict()))}
    rt = UpdateRuntime(rt.config.model_copy(update={"artifacts": artifacts}))
    stages = tuple(s.model_copy(update={"options": {**s.options, "entrypoint": "map.tab"}})
        if s.operation in {"DATASET_INSPECT", "DATASET_CONVERT"} else s for s in campaign.stages)
    campaign = bind(rt, campaign.model_copy(update={"stages": stages, "dataset_inputs": (
        campaign.dataset_inputs[0].model_copy(update={"version_sha256": replacement.digest}),)}))
    (originals / "map.ind").write_bytes(b"arrived after manifest")
    assert "DATASET_COMPANION_CLOSURE_MISMATCH" in rt.plan(campaign)["reasons"] and not calls


def test_inspection_fsync_failure_never_commits_pass(tmp_path, monkeypatch):
    rt, campaign, *_ = fixture(tmp_path, monkeypatch)
    from vkm_datasets import catalogue
    local_os = SimpleNamespace(name=os.name, fdopen=os.fdopen, link=os.link,
        fsync=lambda *_: (_ for _ in ()).throw(OSError("synthetic fsync error")))
    monkeypatch.setattr(catalogue, "os", local_os)
    assert execute(rt, campaign)["status"] == "FAIL"
    assert not list(rt.root.glob("attempts/*/inspection-receipt.json"))
    assert not list(rt.root.glob("datasets/catalogues/*.json"))


def test_inspection_lost_directory_ack_can_retry_without_rereading_workbook(tmp_path, monkeypatch):
    rt, campaign, *_ = fixture(tmp_path, monkeypatch)
    from vkm_datasets import catalogue, workbooks
    sync, inspect = catalogue.fsync_directory, workbooks.inspect_workbook
    faults, reads = [], []
    def fail_once(path):
        if (path / "inspection-receipt.json").exists() and not faults:
            faults.append(path)
            raise OSError("synthetic lost directory ack")
        return sync(path)
    def count(*args, **kw):
        reads.append(1)
        return inspect(*args, **kw)
    monkeypatch.setattr(catalogue, "fsync_directory", fail_once)
    monkeypatch.setattr(workbooks, "inspect_workbook", count)
    assert execute(rt, campaign)["status"] == "FAIL"
    assert not list(rt.root.glob("datasets/catalogues/*.json"))
    assert execute(rt, campaign)["status"] == "PASS" and reads == [1]


def test_foreign_operation_without_authoritative_receipt_is_not_registration_proof(tmp_path, monkeypatch):
    rt, campaign, *_ = fixture(tmp_path, monkeypatch)
    from vkm_datasets.update import run_operation
    from vkm_datasets.catalogue import DatasetBlocked
    for stage in campaign.stages[:-1]:
        folder = rt.root / "attempts" / stage_request_id(campaign, stage)
        put(folder / "operation.json", {"status": "PASS", "dataset_version": campaign.dataset_inputs[0].version_sha256})
    stage = campaign.stages[-1]
    folder = rt.root / "attempts" / stage_request_id(campaign, stage)
    with pytest.raises(DatasetBlocked, match="DEPENDENCY_NOT_COMPLETE"):
        run_operation(rt, campaign, stage, folder)
    assert not list(rt.root.glob("datasets/catalogues/*.json"))


def test_workbook_memory_limit_failure_does_not_publish_partial_inspection(tmp_path, monkeypatch):
    rt, campaign, *_ = fixture(tmp_path, monkeypatch)
    stages = tuple(s.model_copy(update={"options": {**s.options, "workbook_limits": {"max_file_bytes": 1}}})
        if s.operation == "DATASET_INSPECT" else s for s in campaign.stages)
    campaign = bind(rt, campaign.model_copy(update={"stages": stages}))
    assert execute(rt, campaign)["status"] == "FAIL"
    assert not list(rt.root.glob("attempts/*/inspection-receipt.json"))
    assert not list(rt.root.glob("datasets/catalogues/*.json"))


def test_current_policy_change_of_carried_dataset_blocks_before_new_inspection(tmp_path, monkeypatch):
    rt, campaign, version, _, calls = fixture(tmp_path, monkeypatch, linked=True)
    assert execute(rt, campaign)["status"] == "PASS"
    old_path, _ = snapshot(rt)
    changed = replace(version, dataset_id="dataset-b", source_ids=())
    policies = json.loads(Path(rt.config.policy.path).read_bytes())
    policies["policies"]["DATASET:dataset-b"] = policies["policies"]["DATASET:dataset-a"]
    policies["policies"]["VKM-SRC-001"]["access_class"] = "PRIVATE_LOCAL_ONLY"
    new_policy = put(tmp_path / "carried-policy.json", policies)
    newer, updated = successor(rt, campaign, changed, tmp_path, old_path, policy=new_policy)
    item = updated.dataset_inputs[0].model_copy(update={"dataset_id": "dataset-b", "source_links": (), "source_register_artifact": None})
    stages = tuple(s.model_copy(update={"options": {**s.options,
        **({"dataset_ids": ("dataset-b",)} if s.operation == "DATASET_REGISTER" else {"dataset_id": "dataset-b"})}})
        for s in updated.stages)
    updated = bind(newer, updated.model_copy(update={"dataset_inputs": (item,), "stages": stages}))
    assert "DATASET_SOURCE_POLICY_WIDENING" in newer.plan(updated)["reasons"] and len(calls) == 3


def test_removed_source_link_cannot_drop_current_parent_policy(tmp_path, monkeypatch):
    rt, campaign, version, _, calls = fixture(tmp_path, monkeypatch, linked=True)
    assert execute(rt, campaign)["status"] == "PASS"
    old_path, _ = snapshot(rt)
    changed = replace(version, source_ids=(), parents=(version.digest,))
    policies = json.loads(Path(rt.config.policy.path).read_bytes())
    policies["policies"]["VKM-SRC-001"]["access_class"] = "PRIVATE_LOCAL_ONLY"
    new_policy = put(tmp_path / "removed-link-policy.json", policies)
    newer, updated = successor(rt, campaign, changed, tmp_path, old_path, policy=new_policy)
    item = updated.dataset_inputs[0].model_copy(update={"source_links": (), "source_register_artifact": None})
    updated = bind(newer, updated.model_copy(update={"dataset_inputs": (item,)}))
    monkeypatch.setattr("vkm_datasets.catalogue.verify_inspection", lambda *a, **kw: pytest.fail("denied old facets read before policy"))
    assert "DATASET_SOURCE_POLICY_WIDENING" in newer.plan(updated)["reasons"] and len(calls) == 3


def test_removed_direct_link_is_retained_as_policy_ancestry_in_future_snapshot(tmp_path, monkeypatch):
    rt, campaign, version, _, _ = fixture(tmp_path, monkeypatch, linked=True)
    assert execute(rt, campaign)["status"] == "PASS"
    old_path, _ = snapshot(rt)
    changed = replace(version, source_ids=(), parents=(version.digest,))
    newer, updated = successor(rt, campaign, changed, tmp_path, old_path)
    item = updated.dataset_inputs[0].model_copy(update={"source_links": (), "source_register_artifact": None})
    updated = bind(newer, updated.model_copy(update={"dataset_inputs": (item,)}))
    assert execute(newer, updated)["status"] == "PASS"
    path = next(p for p in old_path.parent.glob("*.json") if p != old_path)
    cat = verify_catalogue(rt.root, path)
    assert cat.entries[0].source_links == ()
    assert cat.entries[0].inherited_source_links == campaign.dataset_inputs[0].source_links
    from vkm_corpus.contracts.policy_store import SourcePolicyStore
    policies = SourcePolicyStore(Path(rt.config.policy.path), lambda: ()).read()
    policies["VKM-SRC-001"] = ResourcePolicy(access_class="PRIVATE_LOCAL_ONLY", policy_version="2", authority="owner")
    with pytest.raises(ValueError, match="DATASET_SOURCE_POLICY_WIDENING"):
        verify_catalogue(rt.root, path, policies=policies)


def test_parent_version_cannot_reset_policy_ancestry_with_a_new_catalogue_scope(tmp_path, monkeypatch):
    rt, campaign, version, _, calls = fixture(tmp_path, monkeypatch, linked=True)
    assert execute(rt, campaign)["status"] == "PASS"
    old_path, _ = snapshot(rt)
    changed = replace(version, parents=(version.digest,), source_ids=())
    newer, updated = successor(rt, campaign, changed, tmp_path, old_path)
    item = updated.dataset_inputs[0].model_copy(update={"source_links": (), "source_register_artifact": None})
    stages = tuple(s.model_copy(update={"options": {k: v for k, v in s.options.items() if k != "previous_snapshot_artifact"}})
        if s.operation == "DATASET_REGISTER" else s for s in updated.stages)
    updated = bind(newer, updated.model_copy(update={"dataset_inputs": (item,), "stages": stages}))
    assert "DATASET_PARENT_SELECTION_REQUIRED" in newer.plan(updated)["reasons"] and len(calls) == 3


@pytest.mark.parametrize("kind", ["no_history", "multiple_parents"])
def test_existing_logical_id_cannot_reset_or_merge_unqualified_history(tmp_path, monkeypatch, kind):
    rt, campaign, version, _, calls = fixture(tmp_path, monkeypatch)
    assert execute(rt, campaign)["status"] == "PASS"
    old_path, _ = snapshot(rt)
    changed = replace(version, data_valid_at="2026-10-02",
        parents=() if kind == "no_history" else (version.digest, "f" * 64))
    newer, updated = successor(rt, campaign, changed, tmp_path, old_path)
    if kind == "no_history":
        stages = tuple(s.model_copy(update={"options": {k: v for k, v in s.options.items() if k != "previous_snapshot_artifact"}})
            if s.operation == "DATASET_REGISTER" else s for s in updated.stages)
        updated = bind(newer, updated.model_copy(update={"stages": stages}))
    expected = "DATASET_PREVIOUS_CATALOGUE_REQUIRED" if kind == "no_history" else "DATASET_MULTIPARENT_UPDATE_UNQUALIFIED"
    assert expected in newer.plan(updated)["reasons"] and len(calls) == 3


def test_duplicate_source_links_and_missing_lifecycle_reason_are_schema_errors(tmp_path, monkeypatch):
    _, campaign, *_ = fixture(tmp_path, monkeypatch, linked=True)
    raw = campaign.model_dump(mode="json")
    raw["dataset_inputs"][0]["source_links"] *= 2
    with pytest.raises(ValueError, match="duplicate dataset source link"):
        CampaignManifest.model_validate(raw)
    raw = campaign.model_dump(mode="json")
    raw["dataset_inputs"][0]["lifecycle"] = "RETIRED"
    with pytest.raises(ValueError, match="inactive dataset requires reason"):
        CampaignManifest.model_validate(raw)


def test_missing_gdal_is_explicit_blocker_before_vector_read(tmp_path, monkeypatch):
    rt, campaign, *_ = fixture(tmp_path, monkeypatch, vector=True)
    import vkm_datasets.update as update
    find = update.importlib.util.find_spec
    monkeypatch.setattr(update.importlib.util, "find_spec", lambda name: None if name == "osgeo" else find(name))
    assert "GDAL_RUNTIME_UNAVAILABLE" in rt.plan(campaign)["reasons"]
    assert not rt.root.exists()


def test_final_catalogue_directory_sync_failure_is_not_completed_until_retry(tmp_path, monkeypatch):
    rt, campaign, *_ = fixture(tmp_path, monkeypatch)
    from vkm_datasets import catalogue
    sync = catalogue.fsync_directory
    faults = []
    def fail_once(path):
        if path.name == "catalogues" and list(path.glob("*.json")) and not faults:
            faults.append(path)
            raise OSError("synthetic lost catalogue ack")
        return sync(path)
    monkeypatch.setattr(catalogue, "fsync_directory", fail_once)
    first = execute(rt, campaign)
    assert first["status"] == "FAIL" and first["stages"]["register"]["status"] == "FAIL"
    assert execute(rt, campaign)["status"] == "PASS"
    path, cat = snapshot(rt)
    assert path.name == cat.sha256 + ".json" and faults


def test_all_entrypoints_need_inspection_before_logical_register(tmp_path, monkeypatch):
    rt, campaign, version, originals, calls = fixture(tmp_path, monkeypatch)
    workbook(originals / "second.xlsx")
    version = replace(version, files=discover(originals, ("sample.xlsx", "second.xlsx")), entrypoints=("sample.xlsx", "second.xlsx"))
    cfg = rt.config.model_copy(update={"artifacts": {**rt.config.artifacts,
        "manifest": put(tmp_path / "two-entrypoints.json", manifest_bytes(version.as_dict()))}})
    rt = UpdateRuntime(cfg)
    campaign = bind(rt, campaign.model_copy(update={"dataset_inputs": (
        campaign.dataset_inputs[0].model_copy(update={"version_sha256": version.digest}),)}))
    assert "DATASET_DEPENDENCY_MISSING_OR_AMBIGUOUS" in rt.plan(campaign)["reasons"] and not calls
    extra = campaign.stages[1].model_copy(update={"stage_id": "inspect-second", "options": {
        **campaign.stages[1].options, "entrypoint": "second.xlsx"}})
    register = campaign.stages[-1].model_copy(update={"depends_on": ("inspect", "inspect-second")})
    campaign = bind(rt, campaign.model_copy(update={"stages": (*campaign.stages[:-1], extra, register)}))
    assert execute(rt, campaign)["status"] == "PASS"
    assert set(snapshot(rt)[1].entries[0].inspections) == {"sample.xlsx", "second.xlsx"}
