"""Production admission uses real bytes and explicit identities; every fixture is synthetic."""
from __future__ import annotations

import hashlib
import json
import csv
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest

from vkm_corpus.pipeline import context as C
from vkm_corpus.pipeline.config import PipelineConfig
from vkm_corpus.registry import sources as R

SHA = "a" * 40


@pytest.fixture()
def production(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    (checkout / "requirements").mkdir(parents=True)
    (checkout / "requirements/corpus.lock.txt").write_bytes(b"synthetic-package==1.2.3\n")
    monkeypatch.setattr(C, "REPO_ROOT", checkout)
    monkeypatch.setattr(C, "_git", lambda *args: SHA if args[0] == "rev-parse" else "")
    monkeypatch.setattr(C.importlib.metadata, "version", lambda name: "1.2.3")
    return PipelineConfig(tmp_path / "data", tmp_path / "private", profile="production", expected_commit=SHA,
                          use_gpu_layout=False, memory_budget_gb=16, source_memory_gb=4, memory_reserve_gb=2)


@pytest.mark.parametrize("command", ["rev-parse", "status", "ls-files"])
def test_git_failure_cannot_be_reported_clean(monkeypatch, command):
    seen = []

    def run(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(returncode=1 if args[3] == command else 0,
                               stdout=SHA if args[3] == "rev-parse" else "")

    monkeypatch.setattr(C.subprocess, "run", run)
    monkeypatch.setenv("GIT_DIR", "synthetic-foreign-repository")
    with pytest.raises(C.ProducerGuardError):
        C.code_revision(strict=True)
    assert C.code_revision() == ("unknown", True)
    assert all("GIT_DIR" not in kwargs["env"] for _, kwargs in seen)


@pytest.mark.parametrize("dirty", [" M src/a.py", "?? src/new.py", "?? scripts/run.sh", "?? infra/worker.py",
                                    " M requirements/corpus.lock.txt", " M pyproject.toml"])
def test_dirty_and_untracked_executable_tree_is_rejected(production, monkeypatch, dirty):
    def git(*args):
        if args[0] == "rev-parse":
            return SHA
        if args[0] == "status":
            assert "--untracked-files=all" in args and "scripts" in args and "infra" in args
            return dirty
        return ""

    monkeypatch.setattr(C, "_git", git)
    with pytest.raises(C.ProducerGuardError, match="clean relevant"):
        C.producer_identity(production)


def test_ignored_executable_also_blocks_production(production, monkeypatch):
    monkeypatch.setattr(C, "_git", lambda *args: SHA if args[0] == "rev-parse" else
                        "src/module/__pycache__/untracked.pyc" if args[0] == "ls-files" else "")
    with pytest.raises(C.ProducerGuardError):
        C.producer_identity(production)


@pytest.mark.parametrize("expected", [None, "aaaaaaa", "b" * 40])
def test_expected_commit_is_mandatory_and_exact(production, expected):
    production.expected_commit = expected
    with pytest.raises(C.ProducerGuardError):
        C.producer_identity(production)


def test_dependency_identity_binds_versions_lock_bytes_and_config(production, monkeypatch):
    first = C.producer_identity(production)
    clone = C.config_from_json(json.loads(json.dumps(C.config_to_json(production))))
    assert C.producer_identity(clone) == first
    assert first["dependencies"]["installed_versions"] == {"synthetic-package": "1.2.3"}
    production.workers += 1
    assert C.producer_identity(production)["identity_sha256"] != first["identity_sha256"]
    production.workers -= 1
    lock = C.REPO_ROOT / "requirements/corpus.lock.txt"
    lock.write_bytes(lock.read_bytes() + b"# qualified provenance changed\n")
    assert C.producer_identity(production)["identity_sha256"] != first["identity_sha256"]
    monkeypatch.setattr(C.importlib.metadata, "version", lambda name: "1.2.4")
    with pytest.raises(C.ProducerGuardError, match="version differs"):
        C.producer_identity(production)


def test_missing_dependency_and_unpinned_lock_fail_closed(production, monkeypatch):
    def missing(name):
        raise C.importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(C.importlib.metadata, "version", missing)
    with pytest.raises(C.ProducerGuardError, match="missing"):
        C.producer_identity(production)
    (C.REPO_ROOT / "requirements/corpus.lock.txt").write_bytes(b"synthetic-package>=1\n")
    with pytest.raises(C.ProducerGuardError, match="exact pins"):
        C.producer_identity(production)


def source(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "source.pdf"
    original = b"%PDF-1.4 original"
    path.write_bytes(original)
    row = {"resource_id": "VKM-SRC-901", "canonical_path": path.name, "sha256": hashlib.sha256(original).hexdigest(),
           "size_bytes": str(len(original))}
    return path, row


def test_same_size_same_mtime_replacement_is_detected_despite_sha_cache(tmp_path):
    path, row = source(tmp_path / "private")
    cache = tmp_path / "cache.json"
    assert R.verify_files(path.parent, [row], cache_path=cache)[row["resource_id"]].status.value == "PRESENT_VERIFIED"
    before = path.stat()
    path.write_bytes(path.read_bytes().replace(b"original", b"replaced"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert R.verify_files(path.parent, [row], cache_path=cache)[row["resource_id"]].status.value == "SHA256_MISMATCH"
    with pytest.raises(R.RegisterError, match="fresh source bytes"):
        R.fresh_source_identity(path.parent, path.name, row["sha256"], int(row["size_bytes"]))


def test_registry_verify_exits_nonzero_for_active_failures_and_counts_retired(tmp_path, monkeypatch, capsys):
    from vkm_corpus.registry import cli

    path, base = source(tmp_path / "private")
    active = {key: "" for key in R.REGISTER_COLUMNS}
    active.update(base, migration_status="ADDED_BY_USER_EXACT", evidence_scope="GENERAL_METHOD")
    retired = {**active, "resource_id": "VKM-SRC-902", "canonical_path": "intentionally-absent.pdf",
               "migration_status": "RETIRED_FROM_CURRENT_RESEARCH", "evidence_scope": "LEGACY_RETIRED"}
    registry = path.parent / R.REGISTER_REL
    registry.parent.mkdir()
    with registry.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=R.REGISTER_COLUMNS)
        writer.writeheader()
        writer.writerows([active, retired])
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(data_root=None,
                                                                     require_resources_root=lambda: path.parent))
    assert cli.cmd_verify(SimpleNamespace(workers=2)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["active"] == 1 and report["status"] == "PASS_WITH_SKIPPED"
    assert report["skipped_by_register"][0]["lifecycle"] == "RETIRED"
    path.write_bytes(b"changed")
    assert cli.cmd_verify(SimpleNamespace(workers=2)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "FAIL" and len(report["not_verified"]) == 1


@pytest.mark.parametrize("bad_id", ["VKM-SRC-901", "../escape"])
def test_pipeline_registry_rejects_duplicates_and_unsafe_ids_with_utf8_bom(tmp_path, bad_id):
    from vkm_corpus.pipeline.sources import load_sources

    path, row = source(tmp_path / "private")
    registry = path.parent / R.REGISTER_REL
    registry.parent.mkdir()
    with registry.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerows([row, {**row, "resource_id": bad_id}])
    with pytest.raises(R.RegisterError):
        load_sources(path.parent)


@pytest.mark.parametrize("relative", ["../outside.pdf", "/outside.pdf", "C:/outside.pdf", "a/../outside.pdf",
                                       "a\\outside.pdf", "./source.pdf"])
def test_resource_path_rejects_escaping_or_noncanonical_names(tmp_path, relative):
    with pytest.raises(R.RegisterError):
        R.resource_path(tmp_path, relative)


def test_resource_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable: NOT_RUN")
    with pytest.raises(R.RegisterError, match="escapes"):
        R.resource_path(root, "linked/source.pdf")


def test_production_plan_checks_fresh_source_before_reading_cache(production, monkeypatch):
    from vkm_corpus.extract.model import SourceInput
    from vkm_corpus.pipeline import plan as P

    path, row = source(production.resources_root)
    src = SourceInput(row["resource_id"], row["canonical_path"], row["sha256"], int(row["size_bytes"]))
    monkeypatch.setattr(P.cm, "load_ledger", lambda cfg: {})
    cache = SimpleNamespace(calls={})
    first = P.build_plan(production, [src], cache, None)
    assert first["sources"][0]["source_identity"]["verification"] == "FRESH_SHA256"
    assert P.build_plan(production, [src], cache, None, no_ocr=True)["plan_sha256"] != first["plan_sha256"]
    production.normalize_tag = "changed-rule"
    assert P.build_plan(production, [src], cache, None)["plan_sha256"] != first["plan_sha256"]
    before = path.stat()
    path.write_bytes(path.read_bytes().replace(b"original", b"replaced"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    monkeypatch.setattr(P.cm, "load_ledger", lambda cfg: pytest.fail("cache consulted before fresh source check"))
    with pytest.raises(R.RegisterError):
        P.build_plan(production, [src], cache, None)


def test_registry_replacement_after_confirmation_cannot_change_approved_bytes(production):
    from vkm_corpus.extract.model import SourceInput

    path, row = source(production.resources_root)
    original = R.fresh_source_identity(path.parent, path.name, row["sha256"], int(row["size_bytes"]))
    plan = {"producer_identity": C.producer_identity(production),
            "sources": [{"source_id": row["resource_id"], "source_identity": original}]}
    path.write_bytes(b"different bytes now valid in a different register")
    changed = SourceInput(row["resource_id"], path.name, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
    with pytest.raises(C.ProducerGuardError, match="approved plan"):
        C.verify_approved_sources(production, [changed], plan)


def test_budget_bounds_workers_and_rejects_unobservable_or_insufficient_resources(production, monkeypatch):
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")
    monkeypatch.setitem(sys.modules, "resource", SimpleNamespace(RLIMIT_AS=1, RLIM_INFINITY=-1,
                                                                getrlimit=lambda kind: (-1, -1)))
    monkeypatch.setattr(C, "available_memory_bytes", lambda: 32 * 1024 ** 3)
    monkeypatch.setattr(C.shutil, "disk_usage", lambda path: SimpleNamespace(free=40 * 1024 ** 3))
    guard = C.resource_guard(production)
    assert guard["verified"] and guard["effective_workers"] == 3  # floor((16 - 2) / 4), not requested 8
    monkeypatch.setattr(C, "available_memory_bytes", lambda: 8 * 1024 ** 3)
    with pytest.raises(C.ProducerGuardError, match="exceeds available"):
        C.resource_guard(production)
    monkeypatch.setattr(C, "available_memory_bytes", lambda: 32 * 1024 ** 3)
    monkeypatch.setattr(C.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    with pytest.raises(C.ProducerGuardError, match="disk"):
        C.resource_guard(production)


def test_cgroup_budget_caps_host_available_memory(monkeypatch):
    files = {"/proc/meminfo": "MemAvailable: 33554432 kB\n", "/proc/self/cgroup": "0::/\n",
             "/sys/fs/cgroup/memory.max": str(16 * 1024 ** 3),
             "/sys/fs/cgroup/memory.current": str(12 * 1024 ** 3)}
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: files[self.as_posix()])
    monkeypatch.setattr(Path, "is_file", lambda self: self.as_posix() in files)
    assert C.available_memory_bytes() == 4 * 1024 ** 3


def test_memory_enforcement_failure_cannot_start_worker(monkeypatch, production):
    from vkm_corpus.pipeline import runner

    orch = object.__new__(runner.Orchestrator)
    orch.cfg, orch.cfg_path, orch.run_id = production, Path("synthetic.json"), "RUN-SYNTHETIC"
    orch.producer_identity = C.producer_identity(production)
    orch.dir = production.data_root
    orch.dir.mkdir()
    (orch.dir / "approved_plan.json").write_text(json.dumps({"producer_identity": orch.producer_identity,
                                                           "sources": []}), encoding="utf-8")
    monkeypatch.setattr(runner, "load_sources", lambda root: [])
    monkeypatch.setattr(runner, "select", lambda sources, wanted: [])
    called = []
    def reject(cmd, **kwargs):
        called.append(cmd)
        assert kwargs["preexec_fn"] is None
        assert "resource.setrlimit" in cmd[2] and "resource.getrlimit" in cmd[2]
        return SimpleNamespace(returncode=78, stderr=b"PRODUCER_MEMORY_GUARD_REJECTED")
    monkeypatch.setattr(runner.subprocess, "run", reject)
    with pytest.raises(C.ProducerGuardError, match="enforcement failed"):
        orch._worker("prepare", [], timeout=1)
    assert len(called) == 1


@pytest.mark.skipif(sys.platform != "linux", reason="Linux RLIMIT_AS synthetic enforcement: NOT_RUN")
def test_real_child_memory_wrapper_applies_limit_without_running_pipeline():
    from vkm_corpus.pipeline.runner import guarded_worker_command

    command = [sys.executable, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0])"]
    result = subprocess.run(guarded_worker_command(command, 1), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stdout.strip() == str(1024 ** 3)
