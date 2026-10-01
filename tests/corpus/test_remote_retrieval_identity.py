"""Synthetic loaded-byte identity and receiver lifecycle; never start a model."""
import copy
import os
import platform
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from vkm_corpus.embeddings.pack import PackHandle, build_pack
from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.retrieval_service.app import create_app
from vkm_corpus.update import remote_retrieval as rr
from vkm_evidence.contracts import record_hash

linux_receiver = pytest.mark.skipif(platform.system() != "Linux",
    reason="NOT_RUN: qualified loaded-pack receiver requires Linux change-time semantics; native Windows qualification unavailable")


def make_pack(tmp_path):
    from test_embeddings_pack import _artifacts, _units_dir
    art = _artifacts(tmp_path)
    rep = build_pack(art, _units_dir(tmp_path), publish_current=True)
    handle = PackHandle(art, check_s=9999)
    path = art / "packs" / rep["pack_id"]
    return handle, path, sha256_of(path / "pack.json")


@linux_receiver
def test_loaded_pack_hashes_once_and_observes_actual_bound_bytes(tmp_path, monkeypatch):
    from vkm_corpus.update import remote_pack
    handle, path, digest = make_pack(tmp_path)
    original = remote_pack.sha256_of
    hashes = []
    def counted(p):
        hashes.append(p)
        return original(p)
    monkeypatch.setattr(remote_pack, "sha256_of", counted)
    bound = handle.bind_qualified_identity(expected_manifest_sha256=digest)
    assert bound["manifest_sha256"] == digest
    assert bound["files"]["tokens.f16"] == original(path / "tokens.f16")
    assert handle.qualified_identity() == bound and handle.qualified_identity() == bound
    assert len(hashes) == 2


@linux_receiver
@pytest.mark.parametrize("when", ["before_bind", "after_bind"])
def test_same_size_restored_mtime_byte_change_closes_pack_admission(tmp_path, when):
    handle, path, digest = make_pack(tmp_path)
    if when == "after_bind":
        handle.bind_qualified_identity(expected_manifest_sha256=digest)
    tokens = path / "tokens.f16"
    before = tokens.stat()
    with tokens.open("r+b") as stream:
        b = stream.read(1)
        stream.seek(0)
        stream.write(bytes([b[0] ^ 1]))
    os.utime(tokens, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert tokens.stat().st_size == before.st_size and tokens.stat().st_mtime_ns == before.st_mtime_ns
    if when == "before_bind":
        with pytest.raises(ValueError, match="changed since|content differs|native file changed"):
            handle.bind_qualified_identity(expected_manifest_sha256=digest)
    else:
        assert handle.status()["status"] == "UNAVAILABLE"
        with pytest.raises(LookupError, match="rebind"):
            handle.current()
        with pytest.raises(ValueError, match="rebind"):
            handle.qualified_identity()


@linux_receiver
def test_qualified_pointer_change_does_not_wait_for_legacy_reload_interval(tmp_path):
    handle, path, digest = make_pack(tmp_path)
    handle.bind_qualified_identity(expected_manifest_sha256=digest)
    (path.parent / "CURRENT").write_text('{"pack_id":"not-loaded"}')
    assert handle.status()["status"] == "UNAVAILABLE"
    with pytest.raises(LookupError):
        handle.current()


@linux_receiver
def test_wrong_pinned_manifest_cannot_enable_qualified_pack(tmp_path):
    handle, _, _ = make_pack(tmp_path)
    with pytest.raises(ValueError, match="pinned"):
        handle.bind_qualified_identity(expected_manifest_sha256="0" * 64)
    with pytest.raises(ValueError, match="not been qualified"):
        handle.qualified_identity()


@pytest.mark.parametrize("system", ["Windows", "Darwin"])
def test_pack_qualification_refuses_unsupported_os_before_read(monkeypatch, system):
    from vkm_corpus.update.remote_pack import QualifiedPackLease
    monkeypatch.setattr(platform, "system", lambda: system)
    with pytest.raises(ValueError, match="requires Linux"):
        QualifiedPackLease(None)


def test_identity_endpoint_does_not_promote_legacy_health_to_qualification():
    with TestClient(create_app(None, token="synthetic")) as client:
        assert client.get("/identity").status_code == 401
        response = client.get("/identity", headers={"Authorization": "Bearer synthetic"})
        assert response.status_code == 503 and response.json()["status"] == "UNAVAILABLE"


def test_native_qualification_failure_stops_owned_residency():
    events = []
    res = SimpleNamespace(start=lambda: events.append("start"), stop=lambda: events.append("stop"))
    def fail(**kw):
        raise ValueError("synthetic wrong pack")
    store = SimpleNamespace(bind_qualified_identity=fail)
    cfg = SimpleNamespace(search=SimpleNamespace(qualified_pack_manifest_sha256="a" * 64), token="synthetic")
    with pytest.raises(ValueError, match="wrong pack"):
        with TestClient(create_app(cfg, residency=res, store=store)):
            pass
    assert events == ["start", "stop"]


def model_fixture(tmp_path, monkeypatch):
    files = {}
    for key in ("weights.gguf", "llama-server", "tokenizer.json"):
        p = tmp_path / key
        p.write_bytes(("synthetic " + key).encode())
        files[key] = p
    slot = SimpleNamespace(gguf=str(files["weights.gguf"]), gguf_sha256=sha256_of(files["weights.gguf"]),
        tokenizer_dir=str(tmp_path), heads=None, role="late")
    q = {"tokenizer_sha256": sha256_of(files["tokenizer.json"]), "dimension": 2}
    qconfig = SimpleNamespace(tokenizer_sha256=q["tokenizer_sha256"], as_dict=lambda: dict(q), signature=lambda: record_hash(q))
    native = {"pid": 123, "start_ticks": 42, "command_sha256": "a" * 64, "endpoint_sha256": "b" * 64}
    model = SimpleNamespace(client=object(), slot=slot, _load_files=rr.capture_load_files([files["weights.gguf"], files["llama-server"]]))
    # This fixture already replaces native process observation. File-event
    # semantics have separate Linux filesystem tests below.
    model._load_watch = SimpleNamespace(check=lambda: None)
    encoder = SimpleNamespace(backend=model.client, slot=slot, spec=SimpleNamespace(tokenizer_file="tokenizer.json",
        model_id="synthetic", model_revision="revision"), qconfig=qconfig, signature=record_hash(q),
        _load_files=rr.capture_load_files([files["tokenizer.json"]]))
    encoder._load_watch = SimpleNamespace(check=lambda: None)
    monkeypatch.setattr(rr, "process_identity", lambda m: copy.deepcopy(native))
    return encoder, model, native, files


@pytest.mark.parametrize("fault", ["restart", "backend", "weights", "config"])
def test_owned_model_identity_rechecks_actual_resources_and_selected_process(tmp_path, monkeypatch, fault):
    enc, model, native, files = model_fixture(tmp_path, monkeypatch)
    lease = rr.OwnedModelLease(enc, model)
    assert lease.observe()["weights_sha256"] == sha256_of(files["weights.gguf"])
    if fault == "restart":
        native["start_ticks"] += 1
    elif fault == "backend":
        enc.backend = object()
    elif fault == "weights":
        files["weights.gguf"].write_bytes(b"changed")
    else:
        enc.qconfig = SimpleNamespace(as_dict=lambda: {"dimension": 7})
    with pytest.raises(ValueError):
        lease.observe()


def test_configuration_only_external_model_cannot_qualify(tmp_path, monkeypatch):
    enc, model, _, _ = model_fixture(tmp_path, monkeypatch)
    model._load_files = None
    with pytest.raises(ValueError, match="load-time"):
        rr.OwnedModelLease(enc, model)


@linux_receiver
def test_pack_watch_closes_even_when_all_stat_fields_repeat(tmp_path, monkeypatch):
    from vkm_corpus.update import remote_pack
    handle, path, digest = make_pack(tmp_path)
    handle.bind_qualified_identity(expected_manifest_sha256=digest)
    signature = remote_pack.file_signatures(path)
    monkeypatch.setattr(remote_pack, "file_signatures", lambda p: signature)
    tokens = path / "tokens.f16"
    with tokens.open("r+b") as stream:
        original = stream.read(1)
        stream.seek(0)
        stream.write(bytes([original[0] ^ 1]))
    assert handle.status()["status"] == "UNAVAILABLE"
