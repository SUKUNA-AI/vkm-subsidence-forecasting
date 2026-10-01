"""Synthetic local runtime drills: no remote store, private content or accelerator."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update.contracts import CampaignManifest, ComponentIdentity, GenerationManifest
from vkm_corpus.update.runtime import (BoundFile, Observation, RuntimeConfig, UpdateRuntime, bounded_subprocess,
    QualityGateBinding, late_pack_compatibility, nav_compatibility, observe_components, stage_request_id, worker)
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_evidence.contracts import canonical_bytes, record_hash

CODE = "b" * 40
SHA = "a" * 64
IDENTITY = {"code_revision": CODE, "code_dirty": False, "profile": "production", "verified": True,
            "identity_sha256": "c" * 64}


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value))
    return BoundFile(path=str(path), sha256=sha256_of(path))


def runtime_fixture(tmp_path, monkeypatch, *, operation="VERIFY_ORIGINALS", options=None, artifacts=None, policy_overrides=None):
    originals = tmp_path / "originals"
    originals.mkdir(exist_ok=True)
    source = originals / "source.bin"
    if not source.exists():
        source.write_bytes(b"synthetic")
    policy = put(tmp_path / "policy.json", {"schema": "vkm-source-policy/1", "policies": {"VKM-SRC-001": {
        "access_class": "PUBLIC", "experimental_role": "INPUT", "policy_version": "1", "authority": "synthetic",
        **(policy_overrides or {})}}})
    config = RuntimeConfig(runtime_root=str(tmp_path / "runtime"), originals_root=str(originals),
        policy=policy, qualification_root=str(tmp_path / "qualifications"), expected_commit=CODE,
        memory_budget_gib=3, memory_reserve_gib=1, worker_memory_gib=2, min_free_disk_gib=.001,
        artifacts=artifacts or {})
    rt = UpdateRuntime(config)
    monkeypatch.setattr(UpdateRuntime, "identity", lambda self: dict(IDENTITY))
    campaign = CampaignManifest(campaign_id="synthetic", code_commit=CODE,
        producer_identity_sha256=IDENTITY["identity_sha256"], policy_sha256=policy.sha256,
        inputs=({"source_id": "VKM-SRC-001", "source_sha256": sha256_of(source), "logical_path": "source.bin",
                 "size_bytes": source.stat().st_size, "lifecycle": "ACTIVE"},), gates=(),
        stages=({"stage_id": "stage", "operation": operation, "options": options or {},
                 "input_sha256": SHA, "config_sha256": SHA, "memory_gib": 2, "min_free_disk_gib": 0,
                 "timeout_seconds": 20, "compute": "CPU", "attempts": 2},))
    stage = campaign.stages[0].model_copy(update=rt.stage_bindings(campaign, campaign.stages[0]))
    return rt, campaign.model_copy(update={"stages": (stage,)})


def fake_child(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return worker(Path(argv[-1]))
    monkeypatch.setattr("vkm_corpus.update.runtime.bounded_subprocess", run)
    return calls


def test_plan_and_status_have_no_writes_changed_same_size_mtime_blocked(tmp_path, monkeypatch):
    rt, campaign = runtime_fixture(tmp_path, monkeypatch)
    first = rt.plan(campaign)
    assert first["status"] == "READY"
    assert rt.status(campaign)["campaign"]["status"] == "NOT_RUN"
    assert not rt.root.exists()
    source = tmp_path / "originals/source.bin"
    before = source.stat()
    source.write_bytes(b"replaced!")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert rt.plan(campaign)["status"] == "BLOCKED"
    with pytest.raises(ValueError):
        rt.execute(campaign, first["plan_sha256"])
    assert not rt.root.exists()


def test_execute_resume_worker_receipts_and_tamper(tmp_path, monkeypatch):
    rt, campaign = runtime_fixture(tmp_path, monkeypatch)
    calls = fake_child(monkeypatch)
    plan = rt.plan(campaign)
    assert rt.execute(campaign, plan["plan_sha256"])["status"] == "PASS"
    assert rt.execute(campaign, plan["plan_sha256"])["status"] == "PASS"
    assert len(calls) == 1
    assert calls[0][0][1:4] == ["-m", "vkm_corpus.update.runtime", "--worker"]
    result = next(rt.root.glob("attempts/*/operation.json"))
    result.write_bytes(b"corrupt")
    assert rt.status(campaign)["campaign"]["stages"]["stage"]["status"] == "INVALIDATED"
    assert rt.execute(campaign, plan["plan_sha256"])["status"] == "BLOCKED"
    assert len(calls) == 1


@pytest.mark.parametrize("changes", [{"operation": "SWITCH"}, {"compute": "GPU"},
                                      {"options": {"command": "echo injected"}}])
def test_unqualified_operation_and_arbitrary_command_are_blocked(tmp_path, monkeypatch, changes):
    rt, campaign = runtime_fixture(tmp_path, monkeypatch)
    stage = campaign.stages[0].model_copy(update=changes)
    stage = stage.model_copy(update=rt.stage_bindings(campaign, stage))
    assert rt.plan(campaign.model_copy(update={"stages": (stage,)}))["status"] == "BLOCKED"
    assert rt.rollback()["status"] == "BLOCKED"
    assert not rt.root.exists()


@pytest.mark.parametrize("execution,access,role,targets", [("LOCAL", "PUBLIC", "INPUT", False),
    ("CLOUD", "PRIVATE_CLOUD_ALLOWED", "TARGET", True)])
def test_native_prepare_real_synthetic_pdf_binds_blobs_and_cache(tmp_path, monkeypatch, execution, access, role, targets):
    import fitz
    originals = tmp_path / "originals"
    originals.mkdir()
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 50), "Synthetic source with original glyphs and locators.")
        pdf.save(originals / "source.bin")
    context = put(tmp_path / "context.json", {"principal": "synthetic-worker", "execution": execution,
        "granted_classes": [access], "allow_targets": targets})
    rt, campaign = runtime_fixture(tmp_path, monkeypatch, operation="EXTRACT",
        options={"mode": "native_prepare", "context_artifact": "context"}, artifacts={"context": context},
        policy_overrides={"access_class": access, "experimental_role": role})
    fake_child(monkeypatch)
    result = rt.execute(campaign, rt.plan(campaign)["plan_sha256"])
    assert result["status"] == "PASS"
    receipt = json.loads((rt.root / (result["stages"]["stage"]["receipt_sha256"] + ".receipt.json")).read_bytes())
    paths = [r["path"] for r in receipt["outputs"]]
    assert any(p.startswith("staging/artifacts/") for p in paths)
    assert any(p.startswith("staging/cache/prep/") for p in paths)
    blob = next(rt.root / p for p in paths if p.startswith("staging/artifacts/"))
    blob.unlink()
    assert rt.status(campaign)["campaign"]["stages"]["stage"]["status"] == "INVALIDATED"


@pytest.mark.parametrize("access", ["PRIVATE_LOCAL_ONLY", "SEALED"])
def test_extraction_cloud_context_never_upgraded_by_local_cpu_worker(tmp_path, monkeypatch, access):
    context = put(tmp_path / "context.json", {"principal": "synthetic-worker", "execution": "CLOUD",
        "granted_classes": [access], "allow_targets": True})
    rt, campaign = runtime_fixture(tmp_path, monkeypatch, operation="EXTRACT",
        options={"mode": "native_prepare", "context_artifact": "context"}, artifacts={"context": context},
        policy_overrides={"access_class": access, "experimental_role": "TARGET"})
    assert any("SOURCE_ADMISSION_BLOCKED" in s for s in rt.plan(campaign)["reasons"])
    assert not rt.root.exists()


def test_worker_rejects_injected_request_id_before_output(tmp_path, monkeypatch):
    rt, campaign = runtime_fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="request identity"):
        rt.execute_stage(campaign, campaign.stages[0], "d" * 64)
    assert not rt.root.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="production process-group enforcement is Linux qualified")
def test_hard_timeout_kills_owned_process_group(tmp_path):
    # Child and grandchild are synthetic sleep processes; no runtime bypass flag exists.
    marker = tmp_path / "escaped"
    child_code = "import time,pathlib;time.sleep(2);pathlib.Path(" + repr(str(marker)) + ").write_text('escaped')"
    parent_code = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c'," + repr(child_code) + "]);time.sleep(30)"
    with pytest.raises(TimeoutError):
        bounded_subprocess([sys.executable, "-c", parent_code], timeout=1, memory_gib=2, cwd=tmp_path)
    import time
    time.sleep(1.5)
    assert not marker.exists()


def test_native_observer_reads_actual_duckdb_and_fails_closed(tmp_path):
    import duckdb
    manifest = put(tmp_path / "canonical.json", {"snapshot_id": "doc-1"})
    binding = put(tmp_path / "binding.json", {"status": "PASS", "component_manifest_sha256": manifest.sha256,
                                           "policy_sha256": SHA})
    db = tmp_path / "canonical.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE SCHEMA meta")
        con.execute("CREATE TABLE meta.snapshot AS SELECT 'doc-1' snapshot_id, ? manifest_sha256", [manifest.sha256])
    spec = Observation(component="DUCKDB", native_manifest=manifest.path, policy_binding=binding.path,
                       runtime_database=str(db))
    assert observe_components((spec,))["DUCKDB"]["built_from"] == {"DOCUMENT": "doc-1"}
    with duckdb.connect(str(db)) as con:
        con.execute("UPDATE meta.snapshot SET snapshot_id='doc-2'")
    with pytest.raises(GenerationUnavailable):
        observe_components((spec,))


def test_nav_legacy_33_datasets_needs_attestation_not_embedding_recompute(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from vkm_corpus.navigation.store import pack
    entries = {}
    for i in range(33):
        path = tmp_path / f"dataset_{i}.parquet"
        pq.write_table(pa.table({"id": [i]}), path)
        entries[path.stem] = {"path": path.name, "sha256": sha256_of(path), "rows": 1, "columns": ["id"]}
    manifest = tmp_path / "manifest.json"
    put(manifest, {"snapshot_id": "doc-1", "datasets": entries})
    report = nav_compatibility(manifest, canonical_snapshot="doc-1", canonical_manifest_sha256=SHA)
    assert report["status"] == "BLOCKED" and report["datasets"] == 33 and report["rebuild_required"] is False
    put(manifest, {"format": "vkm-nav-manifest-v1", "snapshot": {"snapshot_id": "doc-1", "manifest_sha256": SHA},
                   "datasets": entries})
    report = nav_compatibility(manifest, canonical_snapshot="doc-1", canonical_manifest_sha256=SHA)
    assert report["repack_required"] and report["embeddings_recompute_required"] is False
    pack(tmp_path)
    assert nav_compatibility(manifest, canonical_snapshot="doc-1", canonical_manifest_sha256=SHA,
                             packed_path=tmp_path / "nav.duckdb")["repack_required"] is False
    (tmp_path / "dataset_0.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash"):
        nav_compatibility(manifest, canonical_snapshot="doc-1", canonical_manifest_sha256=SHA)


def test_late_pack_reuse_checks_encoder_and_byte_identity(tmp_path):
    from vkm_corpus.embeddings.pack import COMPATIBLE_FIELDS, PACK_SCHEMA
    (tmp_path / "tokens.bin").write_bytes(b"synthetic")
    encoder = {k: "synthetic-" + k for k in COMPATIBLE_FIELDS}
    encoder["dimension"] = 4
    manifest = put(tmp_path / "pack.json", {"schema": PACK_SCHEMA, "pack_id": "doc-1-test", "snapshot_id": "doc-1", "config": encoder,
        "files": {"tokens.bin": {"bytes": 9, "sha256": sha256_of(tmp_path / "tokens.bin")}}})
    kwargs = {"canonical_snapshot": "doc-1", "encoder": encoder}
    assert late_pack_compatibility(Path(manifest.path), **kwargs)["embeddings_recompute_required"] is False
    assert late_pack_compatibility(Path(manifest.path), canonical_snapshot="doc-1", encoder={**encoder, "model_id": "other"})["status"] == "BLOCKED"
    assert late_pack_compatibility(Path(manifest.path), canonical_snapshot="doc-1", encoder={"model_id": "toy"})["status"] == "BLOCKED"
    (tmp_path / "tokens.bin").write_bytes(b"replaced!")
    with pytest.raises(ValueError):
        late_pack_compatibility(Path(manifest.path), **kwargs)


def test_cli_dry_run_and_status_are_executable_read_only(tmp_path, monkeypatch, capsys):
    from vkm_corpus.cli import main
    rt, campaign = runtime_fixture(tmp_path, monkeypatch)
    config = put(tmp_path / "runtime.json", rt.config)
    manifest = put(tmp_path / "campaign.json", campaign)
    for command in ("plan", "dry-run", "status"):
        assert main(["update", command, "--config", config.path, "--campaign", manifest.path]) == 0
        assert json.loads(capsys.readouterr().out)
    assert not rt.root.exists()
    with pytest.raises(GenerationUnavailable):
        rt.require_startup()


def test_synthetic_canon_validation_and_shadow_database_preserve_original_selector(tmp_path, monkeypatch):
    from vkm_corpus.testing.synthetic import synthetic_canon
    import duckdb
    canon = synthetic_canon(tmp_path / "canon")
    manifest = put(tmp_path / "canonical-manifest.json", canon.manifest)
    selector = canon.layout.path(canon.layout.CURRENT)
    before = selector.read_bytes()
    rt, campaign = runtime_fixture(tmp_path, monkeypatch, operation="BUILD_SHADOW",
        options={"kind": "DUCKDB", "artifact": "canonical"}, artifacts={"canonical": manifest})
    rt = UpdateRuntime(rt.config.model_copy(update={"canonical_root": str(canon.layout.root)}))
    stage = campaign.stages[0].model_copy(update=rt.stage_bindings(campaign, campaign.stages[0]))
    campaign = campaign.model_copy(update={"stages": (stage,)})
    fake_child(monkeypatch)
    state = rt.execute(campaign, rt.plan(campaign)["plan_sha256"])
    assert state["status"] == "PASS"
    db = next(rt.root.glob("attempts/*/shadow.duckdb"))
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT snapshot_id, manifest_sha256 FROM meta.snapshot").fetchall() == [(canon.snapshot_id, manifest.sha256)]
    assert selector.read_bytes() == before


def test_native_nav_observer_requires_packed_metadata_not_just_manifest(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from vkm_corpus.navigation.store import pack
    file = tmp_path / "sections.parquet"
    pq.write_table(pa.table({"id": [1]}), file)
    manifest = put(tmp_path / "manifest.json", {"format": "vkm-nav-manifest-v1",
        "snapshot": {"snapshot_id": "doc-1", "manifest_sha256": SHA},
        "datasets": {"sections": {"path": file.name, "sha256": sha256_of(file), "rows": 1, "columns": ["id"]}}})
    binding = put(tmp_path / "binding.json", {"component_manifest_sha256": manifest.sha256,
                                            "policy_sha256": SHA, "status": "PASS"})
    spec = Observation(component="NAV", native_manifest=manifest.path, policy_binding=binding.path,
                       runtime_database=str(tmp_path / "nav.duckdb"))
    with pytest.raises(GenerationUnavailable):
        observe_components((spec,))
    pack(tmp_path)
    assert observe_components((spec,))["NAV"]["revision"] == "doc-1"
    assert nav_compatibility(Path(manifest.path), canonical_snapshot="doc-1", canonical_manifest_sha256=SHA,
        required_datasets=("missing",))["status"] == "BLOCKED"


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS qualification is Linux-specific")
def test_child_memory_limit_is_enforced_before_work(tmp_path):
    assert bounded_subprocess([sys.executable, "-c", "bytearray(256*1024*1024)"], timeout=5,
                              memory_gib=0.064, cwd=tmp_path) != 0


def test_registry_metadata_closure_and_campaign_identity_are_required(tmp_path, monkeypatch):
    from vkm_corpus.registry.rules import REGISTER_COLUMNS
    from vkm_corpus.registry.works import write_csv
    rt, campaign = runtime_fixture(tmp_path, monkeypatch)
    source = campaign.inputs[0]
    registry = tmp_path / "originals/00_registry/SOURCE_REGISTER.csv"
    row = {k: "" for k in REGISTER_COLUMNS}
    row.update(resource_id=source.source_id, canonical_path=source.logical_path, sha256=source.source_sha256,
               size_bytes=str(source.size_bytes), evidence_scope="GENERAL_METHOD", migration_status="ADDED_BY_USER_EXACT")
    write_csv(registry, REGISTER_COLUMNS, [row])
    context = put(tmp_path / "context.json", {"principal": "synthetic", "execution": "LOCAL"})
    metadata = put(tmp_path / "metadata.json", {"files": {"00_registry/SOURCE_REGISTER.csv": sha256_of(registry)}})
    rt = UpdateRuntime(rt.config.model_copy(update={"artifacts": {"context": context, "metadata": metadata}}))
    stage = campaign.stages[0].model_copy(update={"operation": "BUILD_SHADOW",
        "options": {"kind": "REGISTRY", "context_artifact": "context", "metadata_artifact": "metadata"}})
    stage = stage.model_copy(update=rt.stage_bindings(campaign, stage))
    campaign = campaign.model_copy(update={"stages": (stage,)})
    assert rt.blockers(campaign) == []
    (registry.parent / "unapproved.csv").write_text("new")
    assert "SOURCE_ADMISSION_BLOCKED:stage" in rt.blockers(campaign)
    assert not rt.root.exists()


def test_cli_compatibility_artifact_binding(tmp_path, monkeypatch, capsys):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from vkm_corpus.cli import main
    rt, _ = runtime_fixture(tmp_path, monkeypatch)
    document = put(tmp_path / "document.json", {"snapshot_id": "doc-1"})
    file = tmp_path / "section.parquet"
    pq.write_table(pa.table({"id": [1]}), file)
    nav = put(tmp_path / "manifest.json", {"format": "vkm-nav-manifest-v1",
        "snapshot": {"snapshot_id": "doc-1", "manifest_sha256": document.sha256},
        "datasets": {"section": {"path": file.name, "sha256": sha256_of(file)}}})
    config = put(tmp_path / "runtime.json", rt.config.model_copy(update={"artifacts": {"document": document, "nav": nav}}))
    assert main(["update", "compatibility", "--config", config.path, "--document-artifact", "document",
                 "--nav-artifact", "nav", "--required-dataset", "section"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "PASS" and report["nav"]["repack_required"] is True
    assert not rt.root.exists()


def test_runtime_rejects_unbound_quality_or_synthetic_pass_as_production_gate(tmp_path, monkeypatch):
    import hashlib
    rt, _ = runtime_fixture(tmp_path, monkeypatch)
    report = {"schema": "vkm-qualification-report/1", "status": "PASS", "qualification": "SYNTHETIC_ONLY"}
    data = canonical_bytes(report)
    digest = hashlib.sha256(data).hexdigest()
    qroot = Path(rt.config.qualification_root)
    qroot.mkdir()
    (qroot / (digest + ".json")).write_bytes(data)
    assert rt._gate(digest) is False
    # Infrastructure receipts have a different trust policy. Declaring this one
    # as a quality gate cannot bypass its typed report contract.
    infrastructure = canonical_bytes({"status": "PASS"})
    infra_digest = hashlib.sha256(infrastructure).hexdigest()
    (qroot / (infra_digest + ".json")).write_bytes(infrastructure)
    assert rt._gate(infra_digest) is True
    binding = QualityGateBinding(plan_artifact="plan", prediction_artifact="prediction",
                                 required_scope={"stratum": ("NUMERIC_EXACT",)})
    bound = UpdateRuntime(rt.config.model_copy(update={"quality_gates": {infra_digest: binding}}))
    assert bound._gate(infra_digest) is False
