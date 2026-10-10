"""CPU build-input tests; no model, image build, server or GPU execution."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest


_PATH = Path(__file__).resolve().parents[2] / "infra" / "edge" / "visual-owner" / "verify_inputs.py"
_SPEC = importlib.util.spec_from_file_location("visual_owner_inputs_test", _PATH)
inputs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inputs)


def _git(root, *args):
    environment = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return subprocess.check_output(["git", "-c", "core.autocrlf=false", "-C", str(root), *args],
                                   env=environment, stderr=subprocess.PIPE)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "synthetic-public-source"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "source.cpp").write_bytes(b"// SYNTHETIC compilation input, never compiled.\n")
    (root / "empty.txt").write_bytes(b"")
    (root / ".gitignore").write_text("ignored-output\n", encoding="utf-8")
    _git(root, "add", "--", "source.cpp", "empty.txt", ".gitignore")
    _git(root, "-c", "user.name=Synthetic Fixture", "-c", "user.email=synthetic@example.invalid",
         "commit", "-qm", "SYNTHETIC build input test")
    commit = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    archive = hashlib.sha256(_git(root, "archive", "--format=tar", commit)).hexdigest()
    return root, commit, archive


def test_every_actual_source_byte_and_empty_file_is_accounted(source):
    root, commit, archive = source
    report = inputs.verify_source(root, commit, archive)
    assert report["files"] == 3
    assert report["source_bytes"] == sum((root / name).stat().st_size for name in ("source.cpp", "empty.txt", ".gitignore"))


def test_source_tampering_with_restored_mtime_is_rejected(source):
    root, commit, archive = source
    path = root / "source.cpp"
    before = path.stat()
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"SYNTHETIC", b"TAMPERED_"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="working-tree bytes"):
        inputs.verify_source(root, commit, archive)


def test_git_ignored_input_is_not_hidden_from_build_guard(source):
    root, commit, archive = source
    (root / "ignored-output").write_bytes(b"unapproved compile-time input")
    with pytest.raises(ValueError, match="untracked"):
        inputs.verify_source(root, commit, archive)


def test_source_archive_and_commit_have_independent_checks(source):
    root, commit, archive = source
    with pytest.raises(ValueError, match="commit differs"):
        inputs.verify_source(root, "f" * 40, archive)
    with pytest.raises(ValueError, match="archive differs"):
        inputs.verify_source(root, commit, "f" * 64)


def test_hardlink_alias_is_not_an_immutable_software_input(tmp_path):
    path = tmp_path / "artifact"
    alias = tmp_path / "alias"
    path.write_bytes(b"public software fixture")
    os.link(path, alias)
    with pytest.raises(ValueError, match="ordinary bounded immutable"):
        inputs.ordinary(path)


@pytest.mark.parametrize("raw", [b'{"files":{},"files":{}}', b'{"files":{"x":1,"x":2}}', b'{"x":NaN}'])
def test_manifest_duplicate_and_nonfinite_fields_are_rejected(raw):
    with pytest.raises(ValueError):
        inputs.unique_json(raw)


def test_exact_manifest_hash_is_checked_before_source_commands(tmp_path, monkeypatch):
    (tmp_path / "inputs.json").write_bytes(b"{}")
    calls = []
    monkeypatch.setattr(inputs, "git", lambda *a: calls.append(a))
    with pytest.raises(ValueError, match="approved build manifest"):
        inputs.verify(tmp_path, "f" * 64)
    assert calls == []


@pytest.mark.parametrize("change", ["base", "source", "public", "status", "unknown"])
def test_mutable_or_unbound_image_plan_rejected_before_any_input(tmp_path, change, monkeypatch):
    value = {"schema_version": "vkm-visual-owner-build-inputs/1", "status": "PLANNED_NOT_BUILT_NOT_MODEL_QUALIFIED",
             "base_images": dict(inputs.BASES), "source_commit": inputs.UPSTREAM,
             "source_archive_sha256": inputs.SOURCE_ARCHIVE, "public_source_commit": "b" * 40, "files": {}}
    if change == "base": value["base_images"]["runtime"] = "nvidia/cuda:latest"
    elif change == "source": value["source_commit"] = "main"
    elif change == "public": value["public_source_commit"] = "working-tree"
    elif change == "status": value["status"] = "READY"
    else: value["skip_checks"] = True
    raw = json.dumps(value).encode()
    (tmp_path / "inputs.json").write_bytes(raw)
    calls = []
    monkeypatch.setattr(inputs, "verify_source", lambda *a: calls.append(a))
    with pytest.raises(ValueError, match="contract differs"):
        inputs.verify(tmp_path, hashlib.sha256(raw).hexdigest())
    assert calls == []


@pytest.fixture
def complete_inputs(source, tmp_path, monkeypatch):
    source_path, commit, archive = source
    raw_archive = _git(source_path, "archive", "--format=tar", commit)
    source_path.rename(tmp_path / "llama-source")
    monkeypatch.setattr(inputs, "UPSTREAM", commit)
    monkeypatch.setattr(inputs, "SOURCE_ARCHIVE", archive)
    for folder in inputs.FOLDERS:
        (tmp_path / folder).mkdir()
    paths = set(inputs.FIXED)
    paths.update("patches/" + name for name in inputs.PATCHES)
    paths.update("builder-debs/synthetic%d.deb" % i for i in range(3))
    paths.add("runtime-debs/synthetic.deb")
    paths.update("wheelhouse/synthetic%d.whl" % i for i in range(2))
    inventory = {}
    for name in paths:
        raw = raw_archive if name == "llama-source.tar" else b"SYNTHETIC software artifact, never installed or compiled: " + name.encode()
        (tmp_path / name).write_bytes(raw)
        inventory[name] = hashlib.sha256(raw).hexdigest()
    plan = {"schema_version": "vkm-visual-owner-build-inputs/1", "status": "PLANNED_NOT_BUILT_NOT_MODEL_QUALIFIED",
            "base_images": dict(inputs.BASES), "source_commit": commit,
            "source_archive_sha256": archive, "public_source_commit": "b" * 40, "files": inventory}
    raw = json.dumps(plan).encode()
    (tmp_path / "inputs.json").write_bytes(raw)
    return tmp_path, hashlib.sha256(raw).hexdigest(), plan


def test_complete_input_manifest_checks_actual_software_and_source_bytes(complete_inputs):
    root, manifest_hash, plan = complete_inputs
    report = inputs.verify(root, manifest_hash)
    assert report["status"] == "BUILD_INPUTS_VALIDATED_NOT_IMAGE_OR_MODEL_QUALIFIED"
    assert report["software_files"] == len(plan["files"])
    assert report["source"]["files"] == 3
    (root / "wheelhouse/synthetic0.whl").write_bytes(b"different package with unchanged manifest")
    with pytest.raises(ValueError, match="software input bytes"):
        inputs.verify(root, manifest_hash)


def test_software_manifest_cannot_substitute_a_different_native_archive(complete_inputs):
    root, _, plan = complete_inputs
    plan["files"]["llama-source.tar"] = "f" * 64
    raw = json.dumps(plan).encode()
    (root / "inputs.json").write_bytes(raw)
    with pytest.raises(ValueError, match="archive file must match"):
        inputs.verify(root, hashlib.sha256(raw).hexdigest())


def test_combined_source_and_software_budget_is_enforced(complete_inputs, monkeypatch):
    root, manifest_hash, _ = complete_inputs
    monkeypatch.setattr(inputs, "verify_source", lambda *a: {"files": 3, "source_bytes": 1024**3})
    with pytest.raises(ValueError, match="combined source/software"):
        inputs.verify(root, manifest_hash)


def _write_plan(root, plan):
    raw = json.dumps(plan).encode()
    (root / "inputs.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _cached_inputs(root, plan):
    (root / "native-delta").mkdir()
    for name in inputs.CACHED - {"native-cache-plan.json"}:
        content = b"SYNTHETIC cached software delta: " + name.encode()
        (root / name).write_bytes(content)
        plan["files"][name] = hashlib.sha256(content).hexdigest()
    cache = dict(schema_version="vkm-native-cache-plan/1", status="PINNED_CACHE_DELTA_NOT_BUILT_NOT_MODEL_QUALIFIED",
        cache_image_id=inputs.CACHED_IMAGE, upstream_commit=inputs.UPSTREAM,
        source_archive_sha256=inputs.SOURCE_ARCHIVE, base_images=inputs.BASES,
        candidate_source_files={"/src/tools/server/" + name: plan["files"][artifact]
            for name, artifact in (("server-context.cpp", "native-delta/server-context.cpp"),
                ("vkm-loaded-lifetime.h", "vkm-loaded-lifetime.h"),
                ("vkm-weight-placement.h", "vkm-weight-placement.h"))})
    _write_cache(root, plan, cache)
    return cache


def _write_cache(root, plan, cache):
    raw = json.dumps(cache).encode()
    (root / "native-cache-plan.json").write_bytes(raw)
    plan["files"]["native-cache-plan.json"] = hashlib.sha256(raw).hexdigest()


def test_cached_variant_binds_all_four_files_and_exact_three_native_overlays(complete_inputs):
    root, _, plan = complete_inputs
    _cached_inputs(root, plan)
    report = inputs.verify(root, _write_plan(root, plan))
    assert report["native_route"] == "PINNED_CACHE_DELTA"
    # The full immutable cache filesystem is separately qualified in its image;
    # this synthetic fixture proves only input/overlay binding, never a build.


@pytest.mark.parametrize("removed", sorted(inputs.CACHED))
def test_partial_cached_inventory_fails_before_any_source_command(complete_inputs, removed, monkeypatch):
    root, _, plan = complete_inputs
    _cached_inputs(root, plan)
    del plan["files"][removed]
    calls = []
    monkeypatch.setattr(inputs, "verify_source", lambda *a: calls.append(a))
    with pytest.raises(ValueError, match="present together"):
        inputs.verify(root, _write_plan(root, plan))
    assert calls == []


def test_cold_manifest_cannot_ignore_a_cached_delta_on_disk(complete_inputs):
    root, manifest, _ = complete_inputs
    (root / "Dockerfile.cached-native").write_bytes(b"unapproved extra context")
    with pytest.raises(ValueError, match="approved variant"):
        inputs.verify(root, manifest)


@pytest.mark.parametrize("change", ["cpp", "loaded_header", "placement_header", "image", "schema", "status"])
def test_cached_binding_cannot_substitute_different_code_or_provenance(complete_inputs, change):
    root, _, plan = complete_inputs
    cache = _cached_inputs(root, plan)
    if change in {"cpp", "loaded_header", "placement_header"}:
        name = {"cpp": "server-context.cpp", "loaded_header": "vkm-loaded-lifetime.h",
                "placement_header": "vkm-weight-placement.h"}[change]
        cache["candidate_source_files"]["/src/tools/server/" + name] = "f" * 64
    elif change == "image": cache["cache_image_id"] = "sha256:" + "f" * 64
    elif change == "schema": cache["schema_version"] = "unknown/1"
    else: cache["status"] = "READY"
    _write_cache(root, plan, cache)
    with pytest.raises(ValueError, match="not bound"):
        inputs.verify(root, _write_plan(root, plan))


def test_unlisted_cached_delta_member_is_rejected(complete_inputs):
    root, _, plan = complete_inputs
    _cached_inputs(root, plan)
    (root / "native-delta/other.cpp").write_bytes(b"unapproved cached overlay")
    with pytest.raises(ValueError, match="approved cached source delta"):
        inputs.verify(root, _write_plan(root, plan))
