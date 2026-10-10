"""CPU-only owner-boundary qualification; no real weights, GPU or servers."""
from __future__ import annotations

import os
import platform
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update import model_owner as mo
from vkm_corpus.update import native_files, remote_models


class FakeModel:
    def __init__(self, tokenizer):
        self._tokenizer = tokenizer

    def rerank(self):
        return "synthetic"


class FakeRuntime:
    def __init__(self, model, tokenizer, prompt_formatter):
        self._model, self._tokenizer, self._prompt_formatter = model, tokenizer, prompt_formatter


def formatter():
    return "synthetic"


def _text(tmp_path, monkeypatch, *, load_change=False):
    monkeypatch.setattr(remote_models, "own_process", lambda: {"pid": 1, "start_ticks": 2})
    monkeypatch.setattr(native_files, "NativeFileWatch", lambda files: SimpleNamespace(check=lambda: None, close=lambda: None))
    paths = {role: tmp_path / role for role in ("weights", "tokenizer")}
    for role, path in paths.items():
        path.write_bytes(role.encode())
    holder, called, config = {}, [], {"mode": "synthetic"}

    def loader():
        called.append("load")
        if load_change:
            paths["weights"].write_bytes(b"replaced")
        tokenizer = object()
        return FakeRuntime(FakeModel(tokenizer), tokenizer, formatter)

    owner = mo.TextRuntimeOwner.load(resources=paths, implementation_files=[Path(__file__)],
        dependency_packages=["pydantic"], config=config, runtime_loader=loader,
        serving_runtime_getter=lambda: holder.get("runtime"),
        install_runtime=lambda runtime: holder.update(runtime=runtime))
    return owner, holder, paths, called, config


def test_text_hook_wraps_actual_loader_and_serving_runtime(tmp_path, monkeypatch):
    owner, holder, paths, called, config = _text(tmp_path, monkeypatch)
    assert called == ["load"]
    proof = owner.observe()
    assert proof.kind == "text" and proof.resources["weights"] == sha256_of(paths["weights"])
    with owner.serving() as runtime:
        assert runtime is holder["runtime"]
        assert runtime._model.rerank() == "synthetic"


@pytest.mark.parametrize("change", ["runtime", "model", "tokenizer", "model_tokenizer", "formatter", "bytes", "config"])
def test_text_hook_rejects_actual_serving_changes(tmp_path, monkeypatch, change):
    owner, holder, paths, called, config = _text(tmp_path, monkeypatch)
    if change == "runtime": holder["runtime"] = SimpleNamespace(**vars(holder["runtime"]))
    if change == "model": holder["runtime"]._model = FakeModel(holder["runtime"]._tokenizer)
    if change == "tokenizer": holder["runtime"]._tokenizer = object()
    if change == "model_tokenizer": holder["runtime"]._model._tokenizer = object()
    if change == "formatter": holder["runtime"]._prompt_formatter = lambda: "other"
    if change == "bytes": paths["weights"].write_bytes(b"other")
    if change == "config": config["mode"] = "other"
    with pytest.raises(ValueError, match="changed"):
        owner.observe()
    with pytest.raises(ValueError, match="permanently invalid"):
        owner.observe()


def test_text_hook_rejects_mutation_inside_real_loader_before_install(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="during loading"):
        _text(tmp_path, monkeypatch, load_change=True)


def test_text_hook_checks_post_inference_boundary(tmp_path, monkeypatch):
    owner, holder, *_ = _text(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="changed"):
        with owner.serving() as runtime:
            runtime._model._tokenizer = object()


def test_text_hook_cannot_be_installed_retrospectively(tmp_path):
    calls = []
    with pytest.raises(ValueError, match="already installed"):
        mo.TextRuntimeOwner.load(resources={}, implementation_files=[], dependency_packages=[], config={},
            runtime_loader=lambda: calls.append("load"), serving_runtime_getter=lambda: object(), install_runtime=lambda r: None)
    assert calls == []


def test_text_owner_failure_is_sticky_after_restoring_same_object(tmp_path, monkeypatch):
    owner, holder, *_ = _text(tmp_path, monkeypatch)
    original = holder["runtime"]
    holder["runtime"] = SimpleNamespace(**vars(original))
    with pytest.raises(ValueError, match="changed"):
        owner.observe()
    holder["runtime"] = original
    with pytest.raises(ValueError, match="permanently invalid"):
        owner.observe()


def test_text_owner_close_never_exports_old_native_proof(tmp_path, monkeypatch):
    owner, *_ = _text(tmp_path, monkeypatch)
    owner.close()
    with pytest.raises(ValueError, match="closed"):
        owner.observe()


@pytest.mark.parametrize("kind", ["text", "visual"])
def test_current_unwired_owners_are_not_identity_or_readiness(kind):
    status = mo.unwired_owner(kind)
    assert status.state == "UNWIRED"
    assert status.functional_qualification == "NOT_RUN" and status.gpu_residency == "NOT_PROVEN"
    with pytest.raises(ValueError):
        remote_models.NativeModelProof.model_validate(status.model_dump())


FAKE_CHILD = '''import argparse,mmap,socket,time
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('-m','--model',dest='model')
p.add_argument('--mmproj')
p.add_argument('--host')
p.add_argument('--port',type=int)
p.add_argument('--control')
p.add_argument('--mode',default='normal')
a=p.parse_args()
files=[];maps={}
for key,path in [('weights',a.model),('mmproj',a.mmproj)]:
 if a.mode=='no-mmproj-map' and key=='mmproj':
  Path(path).read_bytes();continue
 f=open(path,'rb');files.append(f);maps[key]=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ)
s=socket.socket();s.bind((a.host,a.port));s.listen(8)
control=Path(a.control)
while True:
 if control.exists():
  action=control.read_text();control.unlink()
  if action=='unmap-mmproj':maps.pop('mmproj').close()
  if action=='close-listener':s.close()
  if action=='exit':raise SystemExit(0)
 time.sleep(.005)
'''


def _recipe(tmp_path, *, mode="normal", timeout=1.0):
    root = tmp_path.resolve()
    files = {role: root / role for role in ("weights", "tokenizer", "mmproj")}
    for role, path in files.items():
        path.write_bytes((role + "-synthetic-only").encode())
    script = root / "fake_owned_child.py"
    script.write_text(FAKE_CHILD, encoding="utf-8")
    executable = str(Path(sys.executable).resolve())
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    paths = [executable, str(script), *map(str, files.values())]
    recipe = mo.OwnedChildRecipe(executable=executable, arguments=(str(script),
        "-m", str(files["weights"]), "--mmproj", str(files["mmproj"]), "--host", "127.0.0.1",
        "--port", str(port), "--control", str(root / "control"), "--mode", mode),
        resources={key: str(path) for key, path in files.items()}, implementation_files=(str(script),),
        expected_sha256={path: sha256_of(Path(path)) for path in paths}, port=port,
        startup_timeout_s=timeout)
    client = SimpleNamespace(base_url=f"http://127.0.0.1:{port}", is_closed=False)
    holder = {"client": client}
    return recipe, client, holder, files


@pytest.mark.parametrize("change", ["missing_hash", "missing_mmproj", "wrong_weights", "wrong_port",
                                   "duplicate_model", "alternate_host", "relative", "nul"])
def test_owned_recipe_rejects_ambiguous_or_incomplete_inventory(tmp_path, change):
    recipe, *_ = _recipe(tmp_path)
    body = recipe.model_dump()
    if change == "missing_hash": body["expected_sha256"].pop(recipe.resources["tokenizer"])
    if change == "missing_mmproj": body["resources"].pop("mmproj")
    if change == "wrong_weights": body["resources"]["weights"] = recipe.resources["mmproj"]
    if change == "wrong_port": body["port"] += 1 if body["port"] != 65535 else -1
    if change == "duplicate_model": body["arguments"] += ("--model", recipe.resources["weights"])
    if change == "alternate_host": body["arguments"] += ("--host=0.0.0.0",)
    if change == "relative": body["executable"] = "python"
    if change == "nul": body["arguments"] += ("x\0y",)
    with pytest.raises(ValueError):
        mo.OwnedChildRecipe.model_validate(body)


@pytest.mark.parametrize("endpoint", ["http://other.test:18083", "http://localhost:18083", "https://127.0.0.1:18083",
                                      "http://u:p@127.0.0.1:18083", "http://127.0.0.1:18083/prefix"])
def test_owned_client_rejects_sidecar_or_different_endpoint(tmp_path, endpoint):
    recipe, client, holder, *_ = _recipe(tmp_path)
    owner = mo.OwnedChildModelOwner()
    owner.recipe, owner.client, owner.getter = recipe, client, lambda: holder["client"]
    client.base_url = endpoint
    with pytest.raises(ValueError, match="endpoint"):
        owner._client_fence()


LINUX = pytest.mark.skipif(platform.system() != "Linux", reason="NOT_RUN: actual owned child procfs/inotify qualification requires Linux")


@LINUX
def test_actual_owned_child_socket_maps_and_client_qualify_without_inference(tmp_path):
    recipe, client, holder, files = _recipe(tmp_path)
    with mo.OwnedChildModelOwner.start(recipe, inference_client=client,
            serving_client_getter=lambda: holder["client"], environment={}) as owner:
        proof = owner.observe()
        assert proof.model.kind == "visual"
        assert proof.model.resources["mmproj"] == sha256_of(files["mmproj"])
        assert proof.functional_qualification == "NOT_RUN" and proof.gpu_residency == "NOT_PROVEN"
        with owner.serving() as actual:
            assert actual is client
        assert owner.process["pid"] == owner.proc.pid
        assert owner.process["parent_pid"] == os.getpid()
    with pytest.raises(ValueError, match="closed"):
        owner.observe()


@LINUX
@pytest.mark.parametrize("change", ["client_object", "client_url", "client_closed", "weights", "mmproj", "recipe",
                                   "restore_bytes", "unmap-mmproj", "close-listener", "exit"])
def test_actual_owned_child_fences_each_replacement_and_load_time_event(tmp_path, change):
    recipe, client, holder, files = _recipe(tmp_path)
    with mo.OwnedChildModelOwner.start(recipe, inference_client=client,
            serving_client_getter=lambda: holder["client"], environment={}) as owner:
        if change == "client_object": holder["client"] = SimpleNamespace(base_url=client.base_url)
        if change == "client_url": client.base_url = "http://127.0.0.1:1"
        if change == "client_closed": client.is_closed = True
        if change in {"weights", "mmproj"}: files[change].write_bytes(b"modified-synthetic")
        if change == "recipe": owner.recipe.resources["tokenizer"] = str(files["mmproj"])
        if change == "restore_bytes":
            path = files["weights"]
            original, before = path.read_bytes(), path.stat()
            path.write_bytes(b"changed")
            path.write_bytes(original)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        if change in {"unmap-mmproj", "close-listener", "exit"}:
            (tmp_path / "control").write_text(change)
            deadline = time.monotonic() + 1
            while (tmp_path / "control").exists() and time.monotonic() < deadline:
                time.sleep(.005)
        with pytest.raises((ValueError, FileNotFoundError, ProcessLookupError)):
            owner.observe()
        with pytest.raises(ValueError, match="permanently invalid"):
            owner.observe()


@LINUX
def test_mmproj_read_or_digest_without_mapping_is_never_loaded_proof(tmp_path):
    recipe, client, holder, _ = _recipe(tmp_path, mode="no-mmproj-map", timeout=.15)
    with pytest.raises(ValueError, match="load proof unavailable") as caught:
        mo.OwnedChildModelOwner.start(recipe, inference_client=client,
            serving_client_getter=lambda: holder["client"], environment={})
    assert "mmap identity unavailable" in str(caught.value.__cause__)


@LINUX
def test_foreign_ready_listener_and_saved_hash_cannot_qualify_child(tmp_path):
    recipe, client, holder, _ = _recipe(tmp_path, timeout=.15)
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", recipe.port))
        foreign.listen()
        with pytest.raises(ValueError, match="child exited|load proof unavailable"):
            mo.OwnedChildModelOwner.start(recipe, inference_client=client,
                serving_client_getter=lambda: holder["client"], environment={})


@LINUX
def test_owned_file_hash_mismatch_prevents_any_spawn(tmp_path, monkeypatch):
    recipe, client, holder, files = _recipe(tmp_path)
    files["mmproj"].write_bytes(b"changed-before-spawn")
    called = []
    monkeypatch.setattr(mo.subprocess, "Popen", lambda *args, **kwargs: called.append(args))
    with pytest.raises(ValueError, match="bytes differ"):
        mo.OwnedChildModelOwner.start(recipe, inference_client=client,
            serving_client_getter=lambda: holder["client"], environment={})
    assert called == []


@LINUX
def test_owned_hardlink_alias_is_unqualified(tmp_path):
    recipe, client, holder, files = _recipe(tmp_path)
    os.link(files["weights"], tmp_path / "alias")
    with pytest.raises(ValueError, match="hardlink aliases"):
        mo.OwnedChildModelOwner.start(recipe, inference_client=client,
            serving_client_getter=lambda: holder["client"], environment={})
