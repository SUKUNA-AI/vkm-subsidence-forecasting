"""The production gate must inspect the selected service, not a prepared sidecar."""
import json
import os
import platform
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from vkm_corpus.api.app import ApiConfig
from vkm_corpus.api.canon import CanonStore
from vkm_corpus.api.production import (bind_generation_guard, require_serving_profile, read_tool_names,
    serving_code_identity, SERVING_CHECKS)
from vkm_corpus.api.production import serving_dependencies_identity, serving_access_identity
from vkm_corpus.contracts.access import AccessContext
from vkm_corpus.contracts.policy_store import SourcePolicyStore
from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update.contracts import GenerationManifest
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.runtime import BoundFile, Observation, RuntimeConfig, UpdateRuntime, observe_components
from vkm_evidence.contracts import canonical_bytes, record_hash

CODE = "b" * 40
SNAP = "snap-20261001T000000Z-12345678"

requires_linux_receiver = pytest.mark.skipif(
    platform.system() != "Linux",
    reason=("NOT_RUN: qualified production receiver requires Linux filesystem change-time semantics; "
            "native Windows ChangeTime/reparse-handle qualification unavailable"),
)


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(obj))
    return BoundFile(path=str(path), sha256=sha256_of(path))


def edit_database_copy(path, edit):
    """Simulate an external publisher while the receiver holds read-only state."""
    candidate = Path(path).with_suffix(".edited")
    candidate.write_bytes(Path(path).read_bytes())
    with duckdb.connect(str(candidate)) as con:
        edit(con)
    candidate.replace(path)


def setup(tmp_path):
    root = tmp_path / "canon"
    write(root / ".vkm_root.json", {"root_kind": "CANONICAL"})
    document = write(root / "canonical/_snapshots" / (SNAP + ".json"), {"snapshot_id": SNAP})
    (root / "canonical/CURRENT").write_text(SNAP)
    policy = write(tmp_path / "policy.json", {"schema": "vkm-source-policy/1", "policies": {}})
    db = root / "duckdb/vkm_corpus.duckdb"
    db.parent.mkdir()
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE SCHEMA meta")
        con.execute("CREATE TABLE meta.snapshot AS SELECT ?::VARCHAR AS snapshot_id, ?::VARCHAR AS manifest_sha256, "
                    "TIMESTAMPTZ '2026-10-01 00:00:00+00' AS built_at, 'synthetic' AS duckdb_version", [SNAP, document.sha256])
        con.execute("CREATE TABLE meta.commits(commit_key VARCHAR, commit_id VARCHAR)")
        con.execute("CREATE TABLE sources(source_id VARCHAR)")
    specs = []
    for key in ("DOCUMENT", "DUCKDB"):
        binding = write(tmp_path / (key + "-binding.json"), {"status": "PASS",
            "component_manifest_sha256": document.sha256, "policy_sha256": policy.sha256})
        specs.append(Observation(component=key, native_manifest=document.path, policy_binding=binding.path,
                     runtime_database=str(db) if key == "DUCKDB" else None))
    q = tmp_path / "qualifications"
    q.mkdir()
    runtime = UpdateRuntime(RuntimeConfig(runtime_root=str(tmp_path / "runtime"), originals_root=str(tmp_path / "originals"),
        policy=policy, qualification_root=str(q), expected_commit=CODE, memory_budget_gib=4, memory_reserve_gib=1,
        worker_memory_gib=2, min_free_disk_gib=.001, observations=tuple(specs)))
    observed = observe_components(tuple(specs))
    components = sorted(observed.values(), key=lambda c: c["component"])
    cfg = ApiConfig(read_tokens={"read": "reader"}, access_contexts={"reader": AccessContext(principal="reader", execution="CLOUD")})
    acceptance = write(tmp_path / "acceptance.json", {"schema": "vkm-serving-acceptance/1",
        "status": "PASS", "scope": "SHADOW_PRODUCTION", "code_commit": CODE,
        "code_tree_sha256": serving_code_identity(), "policy_sha256": policy.sha256,
        "dependencies_sha256": serving_dependencies_identity(),
        "access_config_sha256": serving_access_identity(cfg),
        "duckdb_file_sha256": sha256_of(db),
        "components_sha256": record_hash(components), "services_sha256": record_hash([]),
        "checks": dict.fromkeys(SERVING_CHECKS, "PASS"),
        "tools": dict.fromkeys(read_tool_names(), "PASS")})
    (q / (acceptance.sha256 + ".json")).write_bytes(Path(acceptance.path).read_bytes())
    manifest = GenerationManifest(code_commit=CODE, policy_sha256=policy.sha256,
        acceptance_sha256=acceptance.sha256, components=tuple(observed.values()))
    write(runtime.root / "served" / (manifest.sha256 + ".json"), manifest)
    (runtime.root / "served/CURRENT").write_text(manifest.sha256)
    deps = SimpleNamespace(canon=CanonStore(db), access_policy=SourcePolicyStore(Path(policy.path), lambda: ()),
                           nav=None, evidence=None, graph=None, search=None, hybrid=None, generation_guard=None)
    return deps, runtime, cfg, root


def replace_acceptance(runtime, **changes):
    generation_root = runtime.root / "served"
    old = GenerationManifest.model_validate_json((generation_root / ((generation_root / "CURRENT").read_text() + ".json")).read_bytes())
    q = Path(runtime.config.qualification_root)
    proof = json.loads((q / (old.acceptance_sha256 + ".json")).read_bytes())
    proof.update(changes)
    staged = write(q / "replacement.json", proof)
    (q / (staged.sha256 + ".json")).write_bytes(Path(staged.path).read_bytes())
    changed = old.model_copy(update={"acceptance_sha256": staged.sha256})
    write(generation_root / (changed.sha256 + ".json"), changed)
    (generation_root / "CURRENT").write_text(changed.sha256)


@requires_linux_receiver
def test_operator_receiver_starts_closed_without_durable_record_but_proves_metadata(tmp_path):
    import asyncio
    from dataclasses import replace
    from vkm_corpus.update.admission import AdmissionState, STATE_FILE
    deps, runtime, cfg, root = setup(tmp_path)
    cfg = replace(cfg, deployment_token="x" * 64)
    guard = bind_generation_guard(deps, runtime, cfg, root)
    served = runtime.root / "served"
    try:
        assert guard()["status"] == "UNAVAILABLE"
        proof = asyncio.run(deps.receiver_identity("1" * 64))
        assert not proof.admission_open  # metadata proof does not open content
        current = (served / "CURRENT").read_text()
        event = {"schema": "vkm-deployment-event/1", "request_key": "a" * 64,
                 "parent_sha256": None, "phase": "RECEIVERS_VERIFIED", "detail": {
                     "generation_sha256": current, "native_sha256": "b" * 64,
                     "receiver_proofs": {"synthetic-test-receiver": "c" * 64}}}
        data = canonical_bytes(event)
        import hashlib
        sha = hashlib.sha256(data).hexdigest()
        (served / "journals").mkdir()
        (served / "journals" / (sha + ".json")).write_bytes(data)
        (served / STATE_FILE).write_bytes(canonical_bytes(AdmissionState(status="OPEN", request_key="a" * 64,
            generation_sha256=current, verified_event_sha256=sha)))
        assert guard()["status"] == "READY"
        (served / STATE_FILE).unlink()
        assert guard()["status"] == "UNAVAILABLE"
        assert not asyncio.run(deps.receiver_identity("2" * 64)).admission_open
    finally:
        deps.serving_file_lease.close()


@requires_linux_receiver
def test_receiver_proof_observes_actual_bound_generation_and_native_process(tmp_path):
    import asyncio
    from vkm_corpus.update.receiver import linux_process_identity, verify_identity, ReceiverChallenge, sign_identity
    deps, runtime, cfg, root = setup(tmp_path)
    bind_generation_guard(deps, runtime, cfg, root)
    proof = asyncio.run(deps.receiver_identity("1" * 64))
    assert {k: getattr(proof, k) for k in linux_process_identity()} == linux_process_identity()
    served = runtime.root / "served"
    manifest = GenerationManifest.model_validate_json((served / ((served / "CURRENT").read_text() + ".json")).read_bytes())
    assert verify_identity(canonical_bytes(sign_identity(proof, "x" * 64)), token="x" * 64,
        challenge=ReceiverChallenge(nonce="1" * 64), manifest=manifest,
        runtime_sha256=record_hash(runtime.config), code_sha256=serving_code_identity(),
        dependencies_sha256=serving_dependencies_identity(), access_sha256=serving_access_identity(cfg),
        gate=deps.admission_barrier._gate_identity, admission_open=True) == proof
    deps.serving_file_lease.close()


@requires_linux_receiver
def test_receiver_proof_rejects_mutation_and_gate_replacement(tmp_path):
    import asyncio
    deps, runtime, cfg, root = setup(tmp_path)
    bind_generation_guard(deps, runtime, cfg, root)
    assert asyncio.run(deps.receiver_identity("1" * 64)).generation_sha256
    gate = deps.admission_barrier.gate_path
    replacement = gate.with_suffix(".replacement")
    replacement.touch()
    replacement.replace(gate)
    with pytest.raises((ValueError, GenerationUnavailable), match="gate"):
        asyncio.run(deps.receiver_identity("2" * 64))
    deps.serving_file_lease.close()


@requires_linux_receiver
def test_selected_database_change_and_policy_change_close_admission(tmp_path):
    deps, runtime, cfg, root = setup(tmp_path)
    guard = bind_generation_guard(deps, runtime, cfg, root)
    assert guard()["status"] == "READY"
    edit_database_copy(deps.canon.path, lambda con: con.execute("UPDATE meta.snapshot SET snapshot_id='other'"))
    assert guard()["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("before_bind", [False, True])
@requires_linux_receiver
def test_data_change_with_unchanged_snapshot_metadata_is_unqualified(tmp_path, before_bind):
    deps, runtime, cfg, root = setup(tmp_path)
    guard = None if before_bind else bind_generation_guard(deps, runtime, cfg, root)
    def change(con):
        original_meta = con.execute("SELECT * FROM meta.snapshot").fetchall()
        con.execute("CREATE TABLE synthetic_corruption_probe AS SELECT 123 AS changed_data")
        assert con.execute("SELECT * FROM meta.snapshot").fetchall() == original_meta
    edit_database_copy(deps.canon.path, change)
    if before_bind:
        with pytest.raises(GenerationUnavailable):
            bind_generation_guard(deps, runtime, cfg, root)
    else:
        assert guard()["status"] == "UNAVAILABLE"


@requires_linux_receiver
def test_same_size_rewrite_with_restored_mtime_invalidates_immutable_file(tmp_path):
    deps, runtime, cfg, root = setup(tmp_path)
    guard = bind_generation_guard(deps, runtime, cfg, root)
    path = deps.canon.path
    before = path.stat()
    before_sha256 = sha256_of(path)
    # Actual byte corruption preserves size and restores mtime. Admission must
    # close on Linux ctime before attempting to read the now corrupted database.
    with path.open("r+b") as stream:
        first = stream.read(1)
        stream.seek(0)
        stream.write(bytes([first[0] ^ 1]))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert sha256_of(path) != before_sha256
    assert after.st_ctime_ns != before.st_ctime_ns
    assert guard()["status"] == "UNAVAILABLE"


@requires_linux_receiver
def test_atomic_replacement_with_same_bytes_size_and_mtime_requires_rebind(tmp_path):
    deps, runtime, cfg, root = setup(tmp_path)
    guard = bind_generation_guard(deps, runtime, cfg, root)
    path = deps.canon.path
    before = path.stat()
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes())
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_ino != before.st_ino
    assert guard()["status"] == "UNAVAILABLE"


@requires_linux_receiver
def test_wrong_file_hash_cannot_reuse_otherwise_qualified_acceptance(tmp_path):
    deps, runtime, cfg, root = setup(tmp_path)
    replace_acceptance(runtime, duckdb_file_sha256="0" * 64)
    with pytest.raises(GenerationUnavailable):
        bind_generation_guard(deps, runtime, cfg, root)


@requires_linux_receiver
def test_memory_canon_is_not_a_qualified_production_file(tmp_path):
    deps, runtime, cfg, root = setup(tmp_path)
    deps.canon.path = None
    with pytest.raises(GenerationUnavailable):
        bind_generation_guard(deps, runtime, cfg, root)


@pytest.mark.parametrize("shape", ["symlink", "directory", "fifo"])
@requires_linux_receiver
def test_qualified_duckdb_must_be_an_ordinary_nonindirect_file(tmp_path, shape):
    deps, runtime, cfg, root = setup(tmp_path)
    path = deps.canon.path
    original = path.with_suffix(".original")
    path.rename(original)
    if shape == "symlink":
        path.symlink_to(original)
    elif shape == "directory":
        path.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("NOT_RUN: FIFO fixture requires POSIX")
        os.mkfifo(path)
    with pytest.raises(GenerationUnavailable):
        bind_generation_guard(deps, runtime, cfg, root)


@requires_linux_receiver
def test_full_database_hash_is_only_computed_once_at_bind(tmp_path, monkeypatch):
    from vkm_corpus.parquet import atomic
    deps, runtime, cfg, root = setup(tmp_path)
    original = atomic.sha256_of
    hashes = []

    def counted_hash(path, *args, **kwargs):
        if Path(path) == deps.canon.path:
            hashes.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(atomic, "sha256_of", counted_hash)
    guard = bind_generation_guard(deps, runtime, cfg, root)
    assert guard()["status"] == "READY"
    assert guard()["status"] == "READY"
    assert hashes == [deps.canon.path]


@requires_linux_receiver
def test_native_watch_closes_even_when_all_stat_fields_repeat(tmp_path, monkeypatch):
    import vkm_corpus.api.production as production
    deps, runtime, cfg, root = setup(tmp_path)
    signature = production._duckdb_file_signature(deps.canon.path)
    guard = bind_generation_guard(deps, runtime, cfg, root)
    assert guard()["status"] == "READY"
    # A real filesystem write, while the cheap signature deliberately reports
    # timestamp coalescing. The independent native watch must close permanently.
    monkeypatch.setattr(production, "_duckdb_file_signature", lambda path: signature)
    with deps.canon.path.open("r+b") as stream:
        first = stream.read(1)
        stream.seek(0)
        stream.write(bytes([first[0] ^ 1]))
    assert guard()["status"] == "UNAVAILABLE"
    assert guard()["status"] == "UNAVAILABLE"


@requires_linux_receiver
def test_mutation_during_full_hash_closes_startup(tmp_path, monkeypatch):
    from vkm_corpus.parquet import atomic
    deps, runtime, cfg, root = setup(tmp_path)
    original = atomic.sha256_of

    def replaced_after_hash(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if Path(path) == deps.canon.path:
            replacement = Path(path).with_suffix(".replacement")
            replacement.write_bytes(Path(path).read_bytes())
            replacement.replace(path)
        return value

    monkeypatch.setattr(atomic, "sha256_of", replaced_after_hash)
    with pytest.raises(GenerationUnavailable):
        bind_generation_guard(deps, runtime, cfg, root)
    assert deps.generation_guard is None


@pytest.mark.parametrize("when", ["during_observer", "after_startup"])
@requires_linux_receiver
def test_replacement_at_startup_observer_boundary_never_installs_guard(tmp_path, monkeypatch, when):
    import vkm_corpus.api.production as production
    deps, runtime, cfg, root = setup(tmp_path)

    def replace_db():
        replacement = deps.canon.path.with_suffix(".replacement")
        replacement.write_bytes(deps.canon.path.read_bytes())
        replacement.replace(deps.canon.path)

    if when == "during_observer":
        original = production.observe_components

        def replaced_observation(*args, **kwargs):
            result = original(*args, **kwargs)
            replace_db()
            return result

        monkeypatch.setattr(production, "observe_components", replaced_observation)
    else:
        original = runtime.require_startup

        def replaced_after_startup(*args, **kwargs):
            original(*args, **kwargs)
            replace_db()

        monkeypatch.setattr(runtime, "require_startup", replaced_after_startup)
    with pytest.raises(GenerationUnavailable):
        bind_generation_guard(deps, runtime, cfg, root)
    assert deps.generation_guard is None


@requires_linux_receiver
def test_prepared_database_is_not_served_database(tmp_path):
    deps, runtime, cfg, root = setup(tmp_path)
    deps.canon.path = tmp_path / "other.duckdb"
    with pytest.raises(GenerationUnavailable):
        bind_generation_guard(deps, runtime, cfg, root)


@pytest.mark.parametrize("field", ["graph", "search", "hybrid", "rerank", "control"])
@requires_linux_receiver
def test_remote_service_cannot_be_omitted_from_generation(tmp_path, field):
    deps, runtime, cfg, root = setup(tmp_path)
    setattr(deps, field, object())
    with pytest.raises(GenerationUnavailable, match="live graph/search/pack"):
        bind_generation_guard(deps, runtime, cfg, root)


@requires_linux_receiver
def test_contexts_and_current_policy_are_required(tmp_path):
    deps, runtime, cfg, root = setup(tmp_path)
    with pytest.raises(GenerationUnavailable, match="contexts"):
        bind_generation_guard(deps, runtime, ApiConfig(read_tokens={"read": "unknown"}), root)
    guard = bind_generation_guard(deps, runtime, cfg, root)
    Path(runtime.config.policy.path).write_text(json.dumps({"schema": "vkm-source-policy/1", "policies": {"extra": {}}}))
    assert guard()["status"] == "UNAVAILABLE"


@requires_linux_receiver
def test_qualified_principal_permissions_cannot_change_under_old_acceptance(tmp_path):
    deps, runtime, cfg, root = setup(tmp_path)
    guard = bind_generation_guard(deps, runtime, cfg, root)
    assert guard()["status"] == "READY"
    cfg.access_contexts["reader"] = AccessContext(principal="reader", execution="LOCAL",
        granted_classes=frozenset({"PUBLIC", "SEALED"}), allow_targets=True)
    assert guard()["status"] == "UNAVAILABLE"


@requires_linux_receiver
def test_token_rotation_preserves_roles_but_write_permission_invalidates_acceptance(tmp_path):
    deps, runtime, cfg, root = setup(tmp_path)
    guard = bind_generation_guard(deps, runtime, cfg, root)
    cfg.read_tokens = {"new-secret": "reader"}
    assert guard()["status"] == "READY"
    cfg.write_tokens = {"new-write-secret": "reader"}
    assert guard()["status"] == "UNAVAILABLE"


@requires_linux_receiver
def test_acceptance_cannot_validate_one_file_and_parse_replacement_bytes(tmp_path, monkeypatch):
    deps, runtime, cfg, root = setup(tmp_path)
    generation_root = runtime.root / "served"
    old = GenerationManifest.model_validate_json((generation_root / ((generation_root / "CURRENT").read_text() + ".json")).read_bytes())
    qualification_root = Path(runtime.config.qualification_root)
    qualified_bytes = (qualification_root / (old.acceptance_sha256 + ".json")).read_bytes()
    bogus = write(qualification_root / "bogus.json", {"status": "PASS", "scope": "SYNTHETIC"})
    target = qualification_root / (bogus.sha256 + ".json")
    target.write_bytes(Path(bogus.path).read_bytes())
    changed = old.model_copy(update={"acceptance_sha256": bogus.sha256})
    write(generation_root / (changed.sha256 + ".json"), changed)
    (generation_root / "CURRENT").write_text(changed.sha256)
    original_read = Path.read_bytes

    def replaced_read(path):
        # Simulate replacement after an independent sha256_of(open(...)) read.
        return qualified_bytes if path == target else original_read(path)

    monkeypatch.setattr(Path, "read_bytes", replaced_read)
    with pytest.raises(GenerationUnavailable):
        bind_generation_guard(deps, runtime, cfg, root)


def test_default_serving_cannot_silently_bypass_production_controls():
    from vkm_corpus.config import ConfigError
    with pytest.raises(ConfigError, match="production serving"):
        require_serving_profile({})
    assert require_serving_profile({"VKM_API_PROFILE": "compatibility"}) == "compatibility"
    assert require_serving_profile({"VKM_SOURCE_POLICY_FILE": "policy", "VKM_ACCESS_CONTEXT_FILE": "contexts",
                                   "VKM_UPDATE_RUNTIME_FILE": "runtime"}) == "production"
    with pytest.raises(ConfigError, match="unsupported"):
        require_serving_profile({"VKM_API_PROFILE": "anything"})


@pytest.mark.parametrize("system", ["Windows", "Darwin", "FreeBSD"])
def test_unsupported_receiver_os_closes_before_accessing_any_artifact(monkeypatch, system):
    monkeypatch.setattr(platform, "system", lambda: system)
    # No dependencies/artifacts are needed to reject an unqualified OS; in
    # particular Windows must never fall back to birth-time based file leases.
    with pytest.raises(GenerationUnavailable, match="requires Linux"):
        bind_generation_guard(None, None, None, None)
    assert require_serving_profile({"VKM_API_PROFILE": "compatibility"}) == "compatibility"


@requires_linux_receiver
def test_generic_pass_is_not_serving_qualification(tmp_path):
    deps, runtime, cfg, root = setup(tmp_path)
    generation_root = runtime.root / "served"
    old = GenerationManifest.model_validate_json((generation_root / ((generation_root / "CURRENT").read_text() + ".json")).read_bytes())
    bogus = write(Path(runtime.config.qualification_root) / "bogus.json", {"status": "PASS", "scope": "SYNTHETIC"})
    (Path(runtime.config.qualification_root) / (bogus.sha256 + ".json")).write_bytes(Path(bogus.path).read_bytes())
    changed = old.model_copy(update={"acceptance_sha256": bogus.sha256})
    write(generation_root / (changed.sha256 + ".json"), changed)
    (generation_root / "CURRENT").write_text(changed.sha256)
    with pytest.raises(GenerationUnavailable):
        bind_generation_guard(deps, runtime, cfg, root)
