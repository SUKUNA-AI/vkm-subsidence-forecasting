"""CPU-only publication faults: the vector engine is synthetic, bytes are real."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from vkm_datasets import DatasetVersion, Policy, Registry, discover
from vkm_datasets import gis
from vkm_datasets.manifest import canonical_bytes, fsync_directory


@pytest.fixture
def attempt(tmp_path, monkeypatch):
    root = tmp_path / "original"
    root.mkdir()
    source = root / "a.gpkg"
    source.write_bytes(b"synthetic source bytes, never a production input")
    version = DatasetVersion("SYNTHETIC", discover(root, ("a.gpkg",)), ("a.gpkg",),
                             Policy("PRIVATE_CLOUD_ALLOWED", "INPUT", "synthetic-test"),
                             "synthetic", "synthetic-only", "2026-10-01T00:00:00Z")
    translations = []
    def translate(path, *args, **kwargs):
        translations.append(kwargs)
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE synthetic(id INTEGER)")
            db.commit()
        finally:
            db.close()
        return object()
    monkeypatch.setattr(gis, "_engine", lambda: (SimpleNamespace(
        VersionInfo=lambda _: "synthetic", VectorTranslate=translate), None))
    monkeypatch.setattr(gis, "_open", lambda _: object())
    monkeypatch.setattr(gis, "_scan", lambda *args: ([], []))
    out = tmp_path / "bundle"
    return root, version, out, translations


def run(attempt, **kwargs):
    root, version, out, _ = attempt
    return gis.convert_to_gpkg(root, version, "a.gpkg", out, **kwargs)


def test_identical_completed_retry_verifies_bytes_without_second_translation(attempt):
    root, version, out, calls = attempt
    first = run(attempt)
    assert first["status"] == "PASS_CONVERSION"
    assert first["request"]["limits"]["max_features"] == 200000
    assert first["request"]["implementation_sha256"]["gis.py"]
    assert first["durability"]["power_loss_qualification"] == "NOT_RUN"
    assert first["durability"]["directories"] == ("FSYNC" if os.name == "posix" else "NOT_QUALIFIED")
    assert run(attempt) == first and len(calls) == 1
    assert calls[0]["preserveFID"] is True
    proof = gis.verify_conversion_bundle(out, version.digest, expected_request=first["request_sha256"])
    assert proof["receipt_sha256"] == hashlib.sha256((out / "receipt.json").read_bytes()).hexdigest()
    assert list(out.glob(".dataset-pending-*"))  # own staging is retained, no cleanup


@pytest.mark.parametrize("kwargs", [
    {"destination": "cloud"}, {"keys": {"declared": ("id",)}}, {"limits": gis.VectorLimits(max_features=1)},
])
def test_changed_context_or_config_cannot_reuse_or_overwrite(attempt, kwargs):
    first = run(attempt)
    before = (attempt[2] / "receipt.json").read_bytes()
    with pytest.raises(ValueError, match="different input, context, rules or config"):
        run(attempt, **kwargs)
    assert (attempt[2] / "receipt.json").read_bytes() == before and len(attempt[3]) == 1
    assert first["request_sha256"]


def test_rule_or_engine_change_cannot_reuse(attempt, monkeypatch):
    run(attempt)
    monkeypatch.setattr(gis, "RULE_VERSION", "synthetic-next-rule")
    with pytest.raises(ValueError, match="different input, context, rules or config"):
        run(attempt)


def test_engine_build_change_cannot_reuse(attempt, monkeypatch):
    run(attempt)
    engine, ogr = gis._engine()
    engine.VersionInfo = lambda _: "different-build"
    monkeypatch.setattr(gis, "_engine", lambda: (engine, ogr))
    with pytest.raises(ValueError, match="different input, context, rules or config"):
        run(attempt)
    assert len(attempt[3]) == 1


def test_original_same_size_and_mtime_tamper_prevents_completed_retry(attempt):
    run(attempt)
    source = attempt[0] / "a.gpkg"
    original = source.read_bytes()
    stamp = source.stat()
    source.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(ValueError, match="hash mismatch"):
        run(attempt)
    assert len(attempt[3]) == 1


@pytest.mark.parametrize("name", ["data.gpkg", "native_metadata.json"])
def test_output_tamper_prevents_completed_retry(attempt, name):
    run(attempt)
    path = attempt[2] / name
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="output hash mismatch"):
        run(attempt)


def test_manifest_policy_change_cannot_reuse(attempt):
    run(attempt)
    root, version, out, calls = attempt
    restricted = replace(version, policy=Policy("PRIVATE_LOCAL_ONLY", "INPUT", "new-policy"))
    with pytest.raises(ValueError, match="another version"):
        gis.convert_to_gpkg(root, restricted, "a.gpkg", out)
    assert len(calls) == 1


def test_source_policy_denied_before_any_existing_bundle_or_source_lookup(attempt):
    root, version, out, calls = attempt
    sealed = replace(version, policy=Policy("PRIVATE_CLOUD_ALLOWED", "TEST_SEALED", "experiment"))
    (root / "a.gpkg").unlink()
    with pytest.raises(PermissionError):
        gis.convert_to_gpkg(root, sealed, "a.gpkg", out)
    assert not out.exists() and not calls


def test_missing_commit_marker_is_typed_blocked_and_preserves_attempt(attempt):
    out = attempt[2]
    out.mkdir()
    pending = out / ".dataset-pending-old"
    pending.mkdir()
    (pending / "candidate.gpkg").write_bytes(b"partial")
    with pytest.raises(gis.ConversionBlocked, match="NEW_ATTEMPT"):
        run(attempt)
    assert (pending / "candidate.gpkg").read_bytes() == b"partial" and not attempt[3]


def test_cli_emits_typed_blocked_without_cleanup(attempt, tmp_path, capsys):
    from vkm_datasets.cli import main
    root, version, out, _ = attempt
    out.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(canonical_bytes(version.as_dict()))
    assert main(["convert", "--root", str(root), "--manifest", str(manifest),
                 "--entrypoint", "a.gpkg", "--output-directory", str(out)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED" and result["reason"] == "INCOMPLETE_BUNDLE_REQUIRES_NEW_ATTEMPT"
    assert out.exists()


def test_receipt_rename_interruption_never_returns_pass(attempt, monkeypatch):
    rename = gis.os.rename
    def interrupted(source, destination):
        if destination.name == "receipt.json":
            raise OSError("synthetic commit interruption")
        return rename(source, destination)
    monkeypatch.setattr(gis.os, "rename", interrupted)
    with pytest.raises(gis.ConversionBlocked, match="NEW_ATTEMPT"):
        run(attempt)
    out = attempt[2]
    assert not (out / "receipt.json").exists()
    assert list(out.glob(".dataset-pending-*/receipt.json"))
    with pytest.raises(gis.ConversionBlocked, match="NEW_ATTEMPT"):
        run(attempt)


def test_file_fsync_failure_cannot_publish_pass(attempt, monkeypatch):
    def failing(path):
        raise OSError("synthetic flush failure")
    monkeypatch.setattr(gis, "fsync_file", failing)
    with pytest.raises(gis.ConversionBlocked):
        run(attempt)
    assert not (attempt[2] / "receipt.json").exists()


def test_receipt_fsync_failure_keeps_marker_private_to_staging(attempt, monkeypatch):
    write = gis.durable_write_new
    def failing(path, raw):
        write(path, raw)
        if path.name == "receipt.json":
            raise OSError("synthetic receipt flush failure")
    monkeypatch.setattr(gis, "durable_write_new", failing)
    with pytest.raises(gis.ConversionBlocked):
        run(attempt)
    assert not (attempt[2] / "receipt.json").exists()
    assert list(attempt[2].glob(".dataset-pending-*/receipt.json"))


def test_uncertain_commit_ack_recovers_identical_published_receipt(attempt, monkeypatch):
    sync = gis.fsync_directory
    fired = []
    def failing_once(path):
        if path == attempt[2] and (path / "receipt.json").exists() and not fired:
            fired.append(True)
            raise OSError("synthetic post-commit directory flush failure")
        return sync(path)
    monkeypatch.setattr(gis, "fsync_directory", failing_once)
    with pytest.raises(gis.ConversionBlocked, match="ACK_UNCERTAIN"):
        run(attempt)
    assert (attempt[2] / "receipt.json").exists()
    assert run(attempt)["status"] == "PASS_CONVERSION" and len(attempt[3]) == 1


def test_file_sync_precedes_receipt_publication(attempt, monkeypatch):
    events = []
    sync, write, rename = gis.fsync_file, gis.durable_write_new, gis.os.rename
    def synced(path):
        sync(path)
        events.append("synced:" + path.name)
    def written(path, raw):
        write(path, raw)
        events.append("written-and-synced:" + path.name)
    def renamed(source, destination):
        rename(source, destination)
        events.append("published:" + destination.name)
    monkeypatch.setattr(gis, "fsync_file", synced)
    monkeypatch.setattr(gis, "durable_write_new", written)
    monkeypatch.setattr(gis.os, "rename", renamed)
    assert run(attempt)["status"] == "PASS_CONVERSION"
    marker = events.index("published:receipt.json")
    assert events.index("synced:data.gpkg") < events.index("written-and-synced:receipt.json") < marker
    assert events.index("synced:native_metadata.json") < events.index("written-and-synced:receipt.json")
    assert events.index("published:data.gpkg") < marker
    assert events.index("published:native_metadata.json") < marker


def test_embedded_native_metadata_is_verified_not_just_outer_file_hash(attempt):
    first = run(attempt)
    out = attempt[2]
    db = sqlite3.connect(out / "data.gpkg")
    try:
        db.execute("UPDATE _vkm_native_metadata SET json='{}'")
        db.commit()
    finally:
        db.close()
    first["outputs"]["data.gpkg"] = hashlib.sha256((out / "data.gpkg").read_bytes()).hexdigest()
    (out / "receipt.json").write_bytes(canonical_bytes(first))
    with pytest.raises(ValueError, match="embedded native metadata"):
        run(attempt)


def test_fid_loss_is_an_explicit_roundtrip_failure():
    layer = {"name": "example", "feature_count": 2, "content_sha256": "a" * 64,
             "geometry_type": 1, "fid_sha256": "b" * 64, "styles_sha256": "c" * 64, "fields": [],
             "crs": {"status": "UNKNOWN", "authority": None}}
    changed = {**layer, "fid_sha256": "d" * 64}
    errors, _ = gis._compare([layer], [changed])
    assert errors == ["example:fid_sha256:MISMATCH"]


def test_posix_directory_fsync_failure_is_not_swallowed(tmp_path, monkeypatch):
    if os.name != "posix":
        assert fsync_directory(tmp_path) is False
        return
    def failing(fd):
        raise OSError("synthetic directory sync failure")
    monkeypatch.setattr(os, "fsync", failing)
    with pytest.raises(OSError, match="directory sync failure"):
        fsync_directory(tmp_path)


def test_registry_replay_resyncs_existing_manifest(attempt, monkeypatch, tmp_path):
    from vkm_datasets import manifest
    registry = Registry(tmp_path / "registry")
    path = registry.register(attempt[1])
    flushed = []
    monkeypatch.setattr(manifest, "fsync_file", lambda file: flushed.append(file))
    assert registry.register(attempt[1]) == path and flushed == [path]
