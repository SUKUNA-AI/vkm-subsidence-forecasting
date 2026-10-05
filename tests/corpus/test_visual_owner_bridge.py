"""Synthetic protocol/ownership tests only; no llama/model/GPU execution."""
from __future__ import annotations

import contextlib
import json
import os
import platform
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update import model_owner as mo
from vkm_corpus.update import visual_owner_bridge as vb
from vkm_evidence.contracts import canonical_bytes, record_hash

LINUX = pytest.mark.skipif(platform.system() != "Linux",
    reason="NOT_RUN: actual owned child procfs/inotify qualification requires Linux")

FAKE = r'''import argparse,json,os
from http.server import HTTPServer,BaseHTTPRequestHandler
from pathlib import Path
p=argparse.ArgumentParser()
for name in ['model','mmproj','host','port','control','mode']:p.add_argument('--'+name)
a=p.parse_args()
if a.mode=='exit':raise SystemExit(3)
# A synthetic stand-in intentionally does not mmap: the ordinary owner must fail.
Path(a.model).read_bytes();Path(a.mmproj).read_bytes()
control=Path(a.control)
class H(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def reply(self,raw,status=200):
  self.send_response(status);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
 def do_GET(self):
  if self.headers.get('X-VKM-Witness-Authorization')!='Bearer '+os.environ['VKM_OWNED_WITNESS_TOKEN']:
   self.reply(b'{}',401);return
  action=control.read_text() if control.exists() else a.mode
  if self.path.startswith('/__vkm_loaded_witness?nonce='):
   nonce=self.path.split('nonce=')[1]
   data={'schema_version':'vkm-native-loaded-witness/1','upstream_commit':'4da6337767f973e2b4d0797e5b323d77d8565e4a',
    'nonce':nonce,'epoch':1,'capture_before_load':True,'handles':{'model':1,'context':2,'mmproj':3,'vocab':4},
    'paths':{'weights':a.model,'tokenizer':a.model,'mmproj':a.mmproj}}
   data['target_weight_placement']={'schema_version':'vkm-native-target-weight-placement/1',
    'scope':'LLAMA_TARGET_WEIGHT_PLACEMENT','model_handle':1,'context_handle':2,'no_alloc':False,
    'layer_count':28,'total_layer_count':28,'tensors':[{'name':'blk.0.weight','group':'blk.0','aliases':[],'tensor':100,'storage_tensor':100,
    'data':10000,'storage_data':10000,'buffer':200,'base':10000,'buft':300,'device':400,
    'tensor_bytes':64,'storage_bytes':64,'buffer_bytes':128,'host':False,'device_kind':'GPU',
    'device_name':'CUDA0','buffer_name':'CUDA0','view_chain':[100],'view_offsets':[],
    'tensor_type':0,'shape':[16,1,1,1]}]}
   if action=='sleep':self.reply(b'{}',503);return
   if action=='epoch':data['epoch']=2
   if action=='nonce':data['nonce']='f'*64
   if action=='handle':data['handles']['mmproj']=40
   if action=='zero':data['handles']['model']=0
   if action=='path':data['paths']['weights']=a.mmproj
   if action=='capture':data['capture_before_load']=False
   if action=='placement':data['target_weight_placement']['tensors'][0]['device']=401
   if action=='placement-zero':data['target_weight_placement']['tensors'][0]['data']=0
   raw=json.dumps(data).encode()
   if action=='duplicate':raw=b'{"epoch":1,'+raw[1:]
   if action=='oversized':raw=b' '*524289+raw
   self.reply(raw);return
  self.reply(b'{"media_marker":"SYNTHETIC"}')
 def do_POST(self):
  self.rfile.read(int(self.headers.get('Content-Length',0)))
  if a.mode=='drift-in-response':control.write_text('epoch')
  if a.mode=='placement-in-response':control.write_text('placement')
  self.reply(b'[{"embedding":[0.1],"index":0}]')
HTTPServer((a.host,int(a.port)),H).serve_forever()
'''


def _recipe(tmp_path, mode="normal", timeout=2.0):
    root = tmp_path.resolve()
    weights, mmproj, script = (root / name for name in ("weights", "mmproj", "child.py"))
    weights.write_bytes(b"SYNTHETIC-GGUF"); mmproj.write_bytes(b"SYNTHETIC-PROJECTOR")
    script.write_text(FAKE, encoding="utf-8")
    executable = str(Path(sys.executable).resolve())
    with socket.socket() as sock: sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    paths = (executable, str(script), str(weights), str(mmproj))
    recipe = vb.HookedChildRecipe(strategy="NATIVE_LOADED_WITNESS_V1", scope="SYNTHETIC", executable=executable,
        arguments=(str(script), "--model", str(weights), "--mmproj", str(mmproj), "--host", "127.0.0.1",
            "--port", str(port), "--control", str(root / "control"), "--mode", mode),
        resources={"weights": str(weights), "tokenizer": str(weights), "mmproj": str(mmproj)},
        implementation_files=(str(script),), expected_sha256={p: sha256_of(Path(p)) for p in paths},
        port=port, startup_timeout_s=timeout)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, follow_redirects=False,
        headers={"Accept-Encoding": "identity"})
    return recipe, client


def _start(recipe, client):
    return vb.OwnedHookChildOwner.start(recipe, inference_client=client,
        serving_client_getter=lambda: client, environment={})


def _witness():
    return dict(schema_version="vkm-native-loaded-witness/1", upstream_commit=vb.UPSTREAM, nonce="a"*64,
        epoch=1, capture_before_load=True, handles={"model":1,"context":2,"mmproj":3,"vocab":4},
        paths={"weights":str(Path.cwd()/"w"), "tokenizer":str(Path.cwd()/"w"), "mmproj":str(Path.cwd()/"p")})


@pytest.mark.parametrize("field,value", [("epoch",True),("epoch","1"),("epoch",0),("capture_before_load",1),
    ("capture_before_load",False),("nonce","b"),("upstream_commit","f"*40),
    ("handles",{"model":"1","context":2,"mmproj":3,"vocab":4}),
    ("handles",{"model":1,"context":2,"mmproj":3}),
    ("paths",{"weights":"relative","tokenizer":"relative","mmproj":"relative"})])
def test_witness_has_strict_live_fields(field, value):
    data=_witness();data[field]=value
    with pytest.raises(ValueError):vb.LoadedWitness.model_validate(data)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}',b'{"x":{"a":1,"a":2}}',b'{"x":NaN}'])
def test_duplicate_or_nonfinite_protocol_is_rejected(raw):
    with pytest.raises(ValueError):vb._json(raw)


def test_hook_recipe_is_explicit_and_embedded_vocab_only(tmp_path):
    recipe, client = _recipe(tmp_path)
    try:
        body=recipe.model_dump();body["schema_version"]="vkm-owned-model-recipe/1"
        with pytest.raises(ValueError):vb.HookedChildRecipe.model_validate(body)
        body=recipe.model_dump();body["resources"]["tokenizer"]=recipe.resources["mmproj"]
        with pytest.raises(ValueError,match="GGUF"):vb.HookedChildRecipe.model_validate(body)
        with pytest.raises(ValueError,match="explicit"):_start(SimpleNamespace(**recipe.model_dump()),client)
        with pytest.raises(ValueError,match="generated"):
            vb.OwnedHookChildOwner.start(recipe,inference_client=client,serving_client_getter=lambda:client,
                environment={vb.WITNESS_ENV:"a"*64})
    finally:client.close()


@LINUX
def test_owned_live_witness_without_mmap_and_sanitized_identity(tmp_path):
    recipe,client=_recipe(tmp_path)
    try:
        with _start(recipe,client) as owner:
            identity=owner.observe()
            assert identity.boundary=="OWNED_NATIVE_LOAD_WITNESS_AND_SOCKET"
            assert identity.functional_qualification=="NOT_RUN" and identity.gpu_residency=="NOT_PROVEN"
            assert identity.model.resources["tokenizer"]==identity.model.resources["weights"]
            assert identity.model.tokenizer_binding=="EMBEDDED_WEIGHTS_VOCAB"
            raw=canonical_bytes(identity)
            assert str(tmp_path).encode() not in raw and owner._witness_token.encode() not in raw
            assert b'"handles"' not in raw and b'"paths"' not in raw
            with owner.serving() as actual:assert actual is client
    finally:client.close()


@LINUX
@pytest.mark.parametrize("mode",["epoch","nonce","zero","path","capture","duplicate","oversized","sleep","placement-zero"])
def test_invalid_first_witness_never_qualifies(tmp_path,mode,monkeypatch):
    recipe,client=_recipe(tmp_path,mode=mode,timeout=.75)
    calls=[]
    original=vb.OwnedHookChildOwner._loaded_resource_fence
    def called(self):
        calls.append(True)
        return original(self)
    monkeypatch.setattr(vb.OwnedHookChildOwner,"_loaded_resource_fence",called)
    try:
        with pytest.raises(ValueError,match="bounded"):_start(recipe,client)
    finally:client.close()
    assert calls, "negative must reach actual listener-owned native witness"


@LINUX
@pytest.mark.parametrize("change",["epoch","handle","sleep","bytes","auth","transport","placement"])
def test_changed_lifetime_or_owned_boundary_closes_permanently(tmp_path,change):
    recipe,client=_recipe(tmp_path)
    try:
        with _start(recipe,client) as owner:
            if change in {"epoch","handle","sleep","placement"}:(tmp_path/"control").write_text(change)
            elif change=="bytes":Path(recipe.resources["mmproj"]).write_bytes(b"changed")
            elif change=="auth":client.headers[vb.WITNESS_HEADER]="Bearer other"
            else:client._transport=httpx.HTTPTransport()
            with pytest.raises(ValueError):owner.observe()
            (tmp_path/"control").write_text("normal")
            with pytest.raises(ValueError,match="permanently invalid"):owner.observe()
    finally:client.close()


@LINUX
def test_foreign_listener_never_receives_private_challenge(tmp_path,monkeypatch):
    recipe,client=_recipe(tmp_path,timeout=.12)
    sent=[]
    monkeypatch.setattr(client,"stream",lambda *a,**kw:sent.append((a,kw)))
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1",recipe.port));foreign.listen(1)
        try:
            with pytest.raises(ValueError):_start(recipe,client)
        finally:client.close()
    assert sent==[]


@LINUX
def test_hook_file_watch_precedes_child_spawn(tmp_path,monkeypatch):
    recipe,client=_recipe(tmp_path)
    spawn=mo.OwnedChildModelOwner._spawn_child
    def mutation(self):
        Path(recipe.resources["weights"]).write_bytes(b"changed-before-Popen")
        return spawn(self)
    monkeypatch.setattr(mo.OwnedChildModelOwner,"_spawn_child",mutation)
    try:
        with pytest.raises(ValueError):_start(recipe,client)
    finally:client.close()


def _bridge(owner,recipe,client):
    b=vb.VisualOwnerBridge()
    b.recipe=SimpleNamespace(child=recipe,max_request_bytes=1024,max_response_bytes=1024,request_timeout_s=1)
    b.owner,b.client,b.token,b.recipe_hash,b.dependencies=owner,client,"t"*32,"a"*64,"b"*64
    b._check=lambda:owner.observe()
    return b


@LINUX
@pytest.mark.parametrize("mode",["drift-in-response","placement-in-response"])
def test_response_buffer_discarded_when_native_epoch_changes_during_inference(tmp_path,mode):
    recipe,client=_recipe(tmp_path,mode=mode)
    try:
        with _start(recipe,client) as owner:
            bridge=_bridge(owner,recipe,client)
            status,body=bridge.dispatch("POST","/embedding",[],b'{"content":"synthetic"}')
            assert status==503 and b"embedding" not in body and b"0.1" not in body
    finally:client.close()


@LINUX
def test_same_owned_client_proxy_routes_and_no_synthetic_native_identity(tmp_path):
    recipe,client=_recipe(tmp_path)
    try:
        with _start(recipe,client) as owner:
            b=_bridge(owner,recipe,client)
            assert b.dispatch("GET","/props",[])[0]==200
            assert b.dispatch("POST","/embedding",[],b'{"content":"synthetic"}')[0]==200
            assert b.dispatch("GET","/identity",[])[0]==401
            assert b.dispatch("GET","/identity",[("Authorization","Bearer "+b.token)])[0]==503
            assert b.dispatch("GET","/placement",[])[0]==401
            assert b.dispatch("GET","/placement",[("Authorization","Bearer "+b.token)])[0]==503
            with pytest.raises(ValueError,match="synthetic"):vb.serve_visual_owner(b,threading_event())
    finally:client.close()


def threading_event():
    import threading
    return threading.Event()


@pytest.mark.parametrize("method,path,body",[("POST","/lora-adapters",b'{}'),("POST","/slots/1",b'{}'),
    ("POST","/v1/chat/completions/control",b'{}'),("GET","/__vkm_loaded_witness",b''),
    ("GET","http://elsewhere/props",b''),("GET","/props?query=x",b'')])
def test_proxy_cannot_forward_mutation_witness_or_an_arbitrary_url(method,path,body):
    b=vb.VisualOwnerBridge()
    assert b.dispatch(method,path,[],body)[0]==404


@pytest.mark.parametrize("body",[b'{"stream":true}',b'{"stream":0}',b'{"a":1,"a":2}',b'[]',b'NaN'])
def test_proxy_refuses_streaming_and_ambiguous_json(body):
    b=vb.VisualOwnerBridge();b.recipe=SimpleNamespace(max_request_bytes=1024)
    assert b.dispatch("POST","/embedding",[],body)[0]==400


def test_proxy_identity_rejects_duplicate_credentials_before_model_access():
    b=vb.VisualOwnerBridge();b.token="x"*32
    assert b.dispatch("GET","/identity",[("Authorization","Bearer "+b.token)]*2)[0]==401


def test_native_http_response_bound_is_enforced_before_parse():
    response=SimpleNamespace(headers={},iter_raw=lambda:iter([b'a'*1024,b'b']))
    with pytest.raises(ValueError,match="bound"):vb._response_bytes(response,1024)
    response=SimpleNamespace(headers={"Content-Encoding":"gzip"})
    with pytest.raises(ValueError,match="encoded"):vb._response_bytes(response,1024)


def _bridge_input(tmp_path):
    recipe,unused=_recipe(tmp_path);unused.close()
    token=tmp_path/"credential";token.write_bytes(b"SYNTHETIC-ONLY-IDENTITY-CREDENTIAL-000000")
    with socket.socket() as sock:sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
    value=vb.VisualOwnerRecipe(schema_version="vkm-visual-owner-bridge/1",child=recipe,environment={},
        identity_token_path=str(token),identity_token_sha256=sha256_of(token),port=port)
    path=tmp_path/"owner.json";path.write_bytes(canonical_bytes(value))
    return path,value


@LINUX
def test_factory_owns_child_and_recipe_token_watch_before_inference(tmp_path,monkeypatch):
    path,recipe=_bridge_input(tmp_path)
    # Synthetic transport fixture omits real installed repo files on DrvFS.
    # Production has no injection switch and requires the fixed actual modules.
    monkeypatch.setattr(vb,"BRIDGE_MODULES",())
    bridge=vb.create_visual_owner(path,sha256_of(path))
    try:
        assert bridge.owner.proc.pid==bridge.owner.pid
        assert bridge.client is bridge.owner.client
        assert bridge.dispatch("GET","/props",[])[0]==200
        token=Path(recipe.identity_token_path);before=token.stat()
        raw=token.read_bytes();token.write_bytes(b"x"*len(raw))
        os.utime(token,ns=(before.st_atime_ns,before.st_mtime_ns))
        assert bridge.dispatch("GET","/props",[])[0]==503
        token.write_bytes(raw)
        assert bridge.dispatch("GET","/props",[])[0]==503
    finally:bridge.close()


@LINUX
def test_factory_refuses_omitted_actual_bridge_code_before_child(tmp_path,monkeypatch):
    path,_=_bridge_input(tmp_path);calls=[]
    monkeypatch.setattr(vb.OwnedHookChildOwner,"start",lambda *a,**kw:calls.append(True))
    with pytest.raises(ValueError,match="implementation absent"):
        vb.create_visual_owner(path,sha256_of(path))
    assert calls==[]


def test_factory_production_requires_actual_python_executable_before_child(tmp_path,monkeypatch):
    """Pre-spawn production branch only: no production child or model is run."""
    import importlib
    from vkm_corpus.retrieval.pins import VISUAL
    path,recipe=_bridge_input(tmp_path)
    native=tmp_path/"llama-server";native.write_bytes(b"SYNTHETIC-NONEXECUTED-NATIVE-FILE")
    modules=tuple(str(Path(importlib.import_module(name).__file__).absolute()) for name in vb.BRIDGE_MODULES)
    child=recipe.child.model_dump()
    child.update(scope="VISUAL_OWNER_PRODUCTION",executable=str(native),implementation_files=modules,
        arguments=("-m",recipe.child.resources["weights"],"--mmproj",recipe.child.resources["mmproj"],
            "--host","127.0.0.1","--port",str(recipe.child.port),"--embeddings","--pooling","last"))
    child["expected_sha256"]={p:sha256_of(Path(p)) for p in (str(native),*modules,*recipe.child.resources.values())}
    child["expected_sha256"][recipe.child.resources["weights"]]=VISUAL["weights_sha256"]
    child["expected_sha256"][recipe.child.resources["mmproj"]]=VISUAL["mmproj_sha256"]
    # This recipe reaches the parent closure branch. Startup has no injection
    # switch; hashes of synthetic model bytes would fail if start were invoked.
    production=vb.VisualOwnerRecipe.model_validate(recipe.model_dump()|{"child":child})
    path.write_bytes(canonical_bytes(production))
    calls=[]
    monkeypatch.setattr(vb,"NativeFileWatch",lambda *a:SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(vb.OwnedHookChildOwner,"start",lambda *a,**kw:calls.append(True))
    with pytest.raises(ValueError,match="Python executable absent"):
        vb.create_visual_owner(path,sha256_of(path))
    assert calls==[]


def test_wrong_recipe_bytes_rejected_before_watch_or_spawn(tmp_path,monkeypatch):
    path,_=_bridge_input(tmp_path);calls=[]
    monkeypatch.setattr(vb,"NativeFileWatch",lambda *a:calls.append(True))
    with pytest.raises(ValueError,match="recipe changed"):vb.create_visual_owner(path,"0"*64)
    assert calls==[]


@pytest.mark.parametrize("env",[{"VKM_OWNED_WITNESS_TOKEN":"external"},{"LLAMA_ARG_MODEL":"other"},
    {"LD_PRELOAD":"other"},{"PYTHONPATH":"other"},{"AIP_HTTP_PORT":"9000"}])
def test_native_profile_rejects_environment_routing_and_loader_overrides(tmp_path,env):
    _,recipe=_bridge_input(tmp_path)
    with pytest.raises(ValueError,match="environment"):
        vb.VisualOwnerRecipe.model_validate(recipe.model_dump()|{"environment":env})


@pytest.mark.parametrize("maps",[
    b'001-002 r-xp 0 00:00 1 /library.so (deleted)\n', b'001-002 rw-p 0 00:00 0 [heap]\n'])
def test_native_implementation_mapping_is_observed_not_declared(monkeypatch,maps):
    monkeypatch.setattr(vb,"_read",lambda *a:maps)
    with pytest.raises(ValueError):vb._mapped_implementation(123)


def test_native_implementation_mapping_contains_executable_and_shared_libraries(monkeypatch):
    monkeypatch.setattr(vb,"_read",lambda *a:b'001-002 r-xp 0 00:00 1 /app/llama-server\n'
        b'002-003 r--p 0 00:00 2 /lib/libggml.so\n003-004 r--p 0 00:00 3 /models/weights.gguf\n')
    assert vb._mapped_implementation(123)=={"/app/llama-server","/lib/libggml.so"}


# --- Bounded source-owned startup diagnostics (closed stage/fence/class only) ---

from vkm_corpus.update import owner_diagnostics as od  # noqa: E402


def _diagnosed_start(tmp_path, mode, timeout=.75):
    recipe, client = _recipe(tmp_path, mode=mode, timeout=timeout)
    diag = od.StartupDiagnostics(kind="visual", identity={"recipe_sha256": "a"*64})
    try:
        with pytest.raises(ValueError) as caught:
            vb.OwnedHookChildOwner.start(recipe, inference_client=client, serving_client_getter=lambda: client,
                environment={}, diagnostics=diag)
        diag.fail(caught.value)
    finally:
        client.close()
    value = diag.public()
    assert od.validate_public(value)
    raw = json.dumps(diag.private())
    assert str(tmp_path) not in raw and "SYNTHETIC-GGUF" not in raw and "Bearer" not in raw
    return value


@LINUX
@pytest.mark.parametrize("mode,step,cause", [
    ("sleep", "WITNESS_STATUS", "ValueError"), ("epoch", "WITNESS_BINDING", "ValueError"),
    ("nonce", "WITNESS_BINDING", "ValueError"), ("duplicate", "WITNESS_SCHEMA", "ValueError"),
    ("oversized", "WITNESS_BODY", "ValueError"), ("placement-zero", "WITNESS_SCHEMA", "ValidationError"),
    ("capture", "WITNESS_SCHEMA", "ValidationError")])
def test_load_proof_deadline_names_the_exact_failing_fence(tmp_path, mode, step, cause):
    value = _diagnosed_start(tmp_path, mode)
    assert value["terminal_stage"] == "NATIVE_LOAD_PROOF" and value["outcome"] == "LOAD_PROOF_DEADLINE"
    assert value["fence_step"] == step and value["child"] == {"spawned": True, "alive_at_terminal": True,
                                                              "exit_code": None}
    assert any(f[0] == step and f[1] == cause for f in value["fence_failures"])
    stages = [s[0] for s in value["stages"]]
    assert stages == ["FILE_INVENTORY", "SPAWN", "NATIVE_LOAD_PROOF"]
    if mode == "sleep":
        assert value["witness_http_status"] and value["witness_http_status"][0][0] == 503


@LINUX
def test_exited_child_is_reported_immediately_with_exit_code(tmp_path):
    import time
    began = time.monotonic()
    value = _diagnosed_start(tmp_path, "exit", timeout=30)
    assert time.monotonic() - began < 10
    assert value["outcome"] == "CHILD_EXITED_DURING_LOAD" and value["child"]["exit_code"] == 3
    assert value["child"]["alive_at_terminal"] is False


@LINUX
def test_foreign_listener_is_localized_without_sending_the_challenge(tmp_path, monkeypatch):
    recipe, client = _recipe(tmp_path, timeout=.3)
    sent = []
    monkeypatch.setattr(client, "stream", lambda *a, **kw: sent.append(True))
    diag = od.StartupDiagnostics(kind="visual")
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", recipe.port)); foreign.listen(1)
        try:
            with pytest.raises(ValueError):
                vb.OwnedHookChildOwner.start(recipe, inference_client=client, serving_client_getter=lambda: client,
                    environment={}, diagnostics=diag)
        finally:
            client.close()
    assert sent == []
    assert {f[0] for f in diag.public()["fence_failures"]} <= {"LISTENER", "CHILD_PROCESS"}


@LINUX
def test_cleanup_failure_does_not_mask_the_primary_startup_cause(tmp_path, monkeypatch):
    original = mo.OwnedChildModelOwner.close
    def broken_close(self):
        original(self)
        raise RuntimeError("SENTINEL secondary " + str(tmp_path))
    monkeypatch.setattr(mo.OwnedChildModelOwner, "close", broken_close)
    recipe, client = _recipe(tmp_path, mode="sleep", timeout=.3)
    diag = od.StartupDiagnostics(kind="visual")
    try:
        with pytest.raises(ValueError, match="bounded native child load proof unavailable"):
            vb.OwnedHookChildOwner.start(recipe, inference_client=client, serving_client_getter=lambda: client,
                environment={}, diagnostics=diag)
    finally:
        client.close()
    assert diag.public()["secondary"] == [["OWNER_CLOSE", "RuntimeError"]]


def test_unexpected_mapped_library_basenames_reach_only_the_private_record(tmp_path, monkeypatch):
    recipe, client = _recipe(tmp_path, timeout=.3)
    production = recipe.model_copy(update={"scope": "VISUAL_OWNER_PRODUCTION"})
    owner = vb.OwnedHookChildOwner.__new__(vb.OwnedHookChildOwner)
    owner.recipe, owner.pid, owner._diagnostics = production, 1, od.StartupDiagnostics(kind="visual")
    monkeypatch.setattr(vb, "_mapped_implementation",
                        lambda pid: {recipe.executable, str(tmp_path / "private" / "libextra.so.9")})
    with pytest.raises(ValueError, match="absent from pre-spawn inventory"):
        owner._loaded_resource_fence()
    client.close()
    assert owner._fence_step == "MAPPED_IMPLEMENTATION"
    assert owner._diagnostics.private()["unexpected_mapped_basenames"] == ["libextra.so.9"]
    assert str(tmp_path) not in json.dumps(owner._diagnostics.public())


def test_bridge_main_prints_one_closed_line_and_private_receipt(tmp_path, monkeypatch, capsys):
    secret = "SENTINEL-" + str(tmp_path)
    def fail(*a, diagnostics=None, **kw):
        diagnostics.enter("CREDENTIAL_READ")
        raise PermissionError(13, secret)
    monkeypatch.setattr(vb, "create_visual_owner", fail)
    out = tmp_path / "diag"; out.mkdir()
    assert vb.main(["--recipe", str(tmp_path / "r.json"), "--recipe-sha256", "c"*64,
                    "--diagnostics-dir", str(out.resolve())]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("visual_owner_startup_unavailable {")
    value = json.loads(err[0].split(" ", 1)[1])
    assert od.validate_public(value) and value["cause_code"] == "CREDENTIAL_READ/FAILED/-/PermissionError"
    private = list(out.iterdir())
    assert len(private) == 1 and "SENTINEL" not in private[0].read_text() and "SENTINEL" not in err[0]


@LINUX
def test_fatal_unretried_fence_error_is_attributed_to_its_own_step(tmp_path, monkeypatch):
    calls = []
    def fence(self):
        calls.append(True)
        if len(calls) <= 2:
            self._fence_step, self._witness_status = "WITNESS_STATUS", 503
            raise ValueError("loaded native witness unavailable")
        self._fence_step = "MAPPED_IMPLEMENTATION"
        raise PermissionError(13, "SENTINEL " + str(tmp_path))
    monkeypatch.setattr(vb.OwnedHookChildOwner, "_loaded_resource_fence", fence)
    recipe, client = _recipe(tmp_path, timeout=30)
    diag = od.StartupDiagnostics(kind="visual")
    try:
        with pytest.raises(PermissionError) as caught:
            vb.OwnedHookChildOwner.start(recipe, inference_client=client, serving_client_getter=lambda: client,
                environment={}, diagnostics=diag)
        diag.fail(caught.value)
    finally:
        client.close()
    value = diag.public()
    assert value["cause_code"] == "NATIVE_LOAD_PROOF/FAILED/MAPPED_IMPLEMENTATION/PermissionError"
    assert ["MAPPED_IMPLEMENTATION", "PermissionError"] == [f[:2] for f in value["fence_failures"]
                                                         if f[0] == "MAPPED_IMPLEMENTATION"][0]
    assert "SENTINEL" not in json.dumps(value)


@LINUX
def test_cancel_request_stops_load_loop_even_when_signal_is_swallowed(tmp_path):
    import time
    recipe, client = _recipe(tmp_path, mode="sleep", timeout=30)
    diag = od.StartupDiagnostics(kind="visual")
    calls = []
    def cancel():
        calls.append(True)
        return len(calls) > 4
    began = time.monotonic()
    try:
        with pytest.raises(InterruptedError) as caught:
            vb.OwnedHookChildOwner.start(recipe, inference_client=client, serving_client_getter=lambda: client,
                environment={}, diagnostics=diag, cancel=cancel)
        diag.fail(caught.value)
    finally:
        client.close()
    assert time.monotonic() - began < 10
    assert diag.public()["cause_code"] == "NATIVE_LOAD_PROOF/FAILED/UNSET/InterruptedError"


@LINUX
def test_owner_config_identity_excludes_spawn_time_witness_credential(tmp_path):
    # The plan's expected identity is computed before any model load from the
    # recipe and its configured environment; the per-lifetime witness token that
    # the hook adds at spawn must not enter config_sha256 (else no match ever).
    recipe, client = _recipe(tmp_path)
    environment = {"LANG": "C.UTF-8"}
    try:
        owner = vb.OwnedHookChildOwner.start(recipe, inference_client=client, serving_client_getter=lambda: client,
            environment=environment)
        with owner:
            expected = record_hash({"recipe": record_hash(recipe), "environment": environment})
            assert owner.proof.config_sha256 == expected
            assert owner.observe().model.config_sha256 == expected
            assert vb.WITNESS_ENV in owner.environment and vb.WITNESS_ENV not in owner.configured_environment
            with_token = record_hash({"recipe": record_hash(recipe), "environment": owner.environment})
            assert with_token != expected
    finally:
        client.close()
