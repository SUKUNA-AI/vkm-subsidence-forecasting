"""Actual hash-pinned CareerOps HTTP/runtime code, in memory, with tiny CPU model.

External sources stay out of PUBLIC. The optional integration is honestly NOT_RUN
without VKM_TEXT_OWNER_TEST_SOURCE_ROOT; no socket listener or real model starts.
"""
from __future__ import annotations

import hashlib
import base64
import csv
import difflib
import importlib
import importlib.metadata
import io
import json
import os
import platform
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from vkm_corpus.update import model_owner, native_files, remote_models
from vkm_corpus.update import text_owner_bridge as B
from vkm_corpus.update.remote_models import NativeModelProof
from vkm_corpus.update.remote_retrieval import file_signature
from vkm_corpus.update.runtime import BoundFile
from vkm_evidence.contracts import canonical_bytes, record_hash

PINS = Path(__file__).parents[2] / "infra/edge/careerops-text-owner/source_pins.json"
TOKEN = "synthetic-native-identity-token-" + "a" * 32


class TinyTokenizer:
    model_max_length = 20000
    pad_token, unk_token, pad_token_id = "<pad>", "<unk>", 0
    def __call__(self, *, text, **kwargs):
        return {"input_ids": [list(range(len(text[0])))]}


def tiny_formatter(query, docs, **kwargs):
    return query + "|" + "|".join(docs)


class TinyModel:
    special_tokens = {}
    def __init__(self, tokenizer):
        self._tokenizer, self.calls, self.mutate = tokenizer, [], lambda: None
    def _truncate_texts(self, query, docs, *args):
        return query, docs, list(map(len, docs)), len(query)
    def rerank(self, query, docs, **kwargs):
        self.calls.append((query, docs, kwargs))
        self.mutate()
        return [{"index": i, "relevance_score": 0.5 - i * 0.1} for i in range(kwargs["top_n"])]
    def to(self, device):
        self.calls.append(("to", device))
        return self
    def eval(self): self.calls.append(("eval",))


class MemorySocket:
    def __init__(self, request):
        self.input, self.output = io.BytesIO(request), bytearray()
    def makefile(self, mode, *args):
        assert mode == "rb"
        return self.input
    def sendall(self, data):
        self.output.extend(data)
    def settimeout(self, seconds):
        assert 0 < seconds <= 60


def request(bridge, *, route="/readyz", method="GET", token=None, body=None, headers=None):
    raw = canonical_bytes(body) if body is not None else b""
    fields = ["Host: synthetic", "Connection: close"]
    if token is not None: fields.append("Authorization: Bearer " + token)
    if method == "POST": fields.append("Content-Length: " + str(len(raw)))
    fields += headers or []
    wire = (method + " " + route + " HTTP/1.1\r\n" + "\r\n".join(fields) + "\r\n\r\n").encode("latin1") + raw
    channel = MemorySocket(wire)
    bridge.server.RequestHandlerClass(channel, ("127.0.0.1", 1), bridge.server)
    head, payload = bytes(channel.output).split(b"\r\n\r\n", 1)
    status = int(head.split(b" ")[1])
    return status, json.loads(payload), head


def patched(raw):
    original = raw.decode("utf-8").replace("\r\n", "\n")
    signature = "    def load(cls, config: RerankerRuntimeConfig) -> JinaRerankerRuntime:"
    changed = original.replace(signature, '''    def load(
        cls,
        config: RerankerRuntimeConfig,
        *,
        model_snapshot: str | None = None,
        tokenizer_snapshot: str | None = None,
        local_files_only: bool = False,
    ) -> JinaRerankerRuntime:
        if local_files_only and (not model_snapshot or not tokenizer_snapshot):
            raise ValueError("offline owner requires both immutable snapshots")''')
    changed = changed.replace("            config.model_id,\n            revision=config.tokenizer_revision,",
        "            tokenizer_snapshot or config.model_id,\n            local_files_only=local_files_only,\n            revision=config.tokenizer_revision,")
    changed = changed.replace("            config.model_id,\n            revision=config.model_revision,",
        "            model_snapshot or config.model_id,\n            local_files_only=local_files_only,\n            revision=config.model_revision,")
    assert hashlib.sha256(changed.encode()).hexdigest() == B.NATIVE_SOURCE["runtime"]
    return changed


@pytest.fixture
def case(tmp_path, monkeypatch):
    source = os.environ.get("VKM_TEXT_OWNER_TEST_SOURCE_ROOT")
    if not source:
        pytest.skip("NOT_RUN: actual pinned CareerOps source root was not supplied")
    source = Path(source) / "src/careerops_reranker"
    pins = json.loads(PINS.read_bytes())
    package = tmp_path / "native/careerops_reranker"
    package.mkdir(parents=True)
    for name, expected in pins["original"].items():
        raw = (source / name).read_bytes()
        normalized = raw.replace(b"\r\n", b"\n")
        assert hashlib.sha256(normalized).hexdigest() == expected, "external native source drift: " + name
        if name == "runtime.py":
            original, target = normalized.decode(), patched(raw)
            expected_patch = "".join(difflib.unified_diff(original.splitlines(True), target.splitlines(True),
                fromfile="a/src/careerops_reranker/runtime.py", tofile="b/src/careerops_reranker/runtime.py", n=3))
            assert PINS.with_name("local-load.patch").read_text(encoding="utf-8") == expected_patch
        (package / name).write_text(patched(raw) if name == "runtime.py" else normalized.decode(), encoding="utf-8", newline="\n")
    for name in list(sys.modules):
        if name == "careerops_reranker" or name.startswith("careerops_reranker."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(package.parent))
    importlib.invalidate_caches()
    modules = tuple(importlib.import_module("careerops_reranker." + name) for name in ("config", "runtime", "server"))
    roots = {name: tmp_path / name for name in ("model", "tokenizer", "code")}
    for root in roots.values(): root.mkdir()
    files = {"weights": roots["model"] / "model.safetensors", "tokenizer": roots["tokenizer"] / "tokenizer.json",
        "config": roots["model"] / "config.json", "custom_code": roots["code"] / "tiny_custom.py"}
    for name, path in files.items(): path.write_bytes(("synthetic-" + name).encode())
    runtime_config = modules[0].RerankerRuntimeConfig.from_env({})
    config_file, token_file = tmp_path / "runtime.json", tmp_path / "token"
    config_file.write_bytes(canonical_bytes(runtime_config))
    token_file.write_text(TOKEN, encoding="ascii")
    token_file.chmod(0o600)
    implementations = list(dict.fromkeys([m.__file__ for m in modules] + [str(package / "__init__.py"), __file__,
        str(files["custom_code"]), *B._bridge_implementation_files()]))
    paths = [*map(str, files.values()), *implementations, str(config_file), str(token_file)]
    recipe = B.TextOwnerRecipe(scope="SYNTHETIC", runtime_config=BoundFile(path=str(config_file), sha256=record_hash(runtime_config)),
        model_snapshot=str(roots["model"]), tokenizer_snapshot=str(roots["tokenizer"]), dynamic_modules_cache=str(roots["code"]),
        identity_token=BoundFile(path=str(token_file), sha256=hashlib.sha256(token_file.read_bytes()).hexdigest()),
        resources={name: str(path) for name, path in files.items()}, implementation_files=tuple(implementations),
        expected_sha256={p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},
        python_version=platform.python_version(), dependency_versions={"pydantic": importlib.metadata.version("pydantic")})
    recipe_file = tmp_path / "recipe.json"
    recipe_file.write_bytes(canonical_bytes(recipe))
    ref = BoundFile(path=str(recipe_file), sha256=record_hash(recipe))

    class SyntheticFileWatch:
        def __init__(self, paths):
            self.paths = {str(p): file_signature(p) for p in paths}
            self.closed = False
        def check(self):
            if self.closed or any(file_signature(p) != signature for p, signature in self.paths.items()):
                raise ValueError("synthetic file mutation")
        def close(self): self.closed = True
    monkeypatch.setattr(B, "NativeFileWatch", SyntheticFileWatch)
    monkeypatch.setattr(native_files, "NativeFileWatch", SyntheticFileWatch)
    monkeypatch.setattr(remote_models, "own_process", lambda: {"pid": 1, "start_ticks": 2, "scope": "SYNTHETIC"})
    model = TinyModel(TinyTokenizer())
    identity = modules[1].RerankerRuntimeIdentity(model_id=runtime_config.model_id,
        model_revision=runtime_config.model_revision, model_code_revision=runtime_config.model_code_revision,
        tokenizer_revision=runtime_config.tokenizer_revision, runtime_backend=runtime_config.runtime_backend,
        dtype_or_quantization=runtime_config.dtype_or_quantization, torch_version="SYNTHETIC", transformers_version="SYNTHETIC")
    loaded = modules[1].JinaRerankerRuntime(identity=identity, model=model,
        tokenizer=model._tokenizer, prompt_formatter=tiny_formatter)
    server = SimpleNamespace(runtime=None, server_close=lambda: None)
    loader_calls = []
    def loader():
        loader_calls.append(True)
        return loaded
    value = SimpleNamespace(recipe=recipe, ref=ref, modules=modules, files=files, roots=roots, loaded=loaded,
        config=runtime_config, model=model, server=server, loader=loader, loader_calls=loader_calls)
    def create():
        return B.create_text_owner(value.ref, _modules=modules, _runtime_loader=value.loader,
            _server_factory=(lambda address, runtime: server) if server.runtime is not None else None)
    value.create = create
    yield value


def rerank_body(case):
    return {"query": "q", "documents": ["first", "second"], "top_n": 1,
        "token_budget": 1000, "expected_runtime": case.loaded.identity.as_dict()}


@pytest.mark.native_source
def test_actual_native_handler_inference_and_identity_share_loaded_object(case):
    bridge = case.create()
    try:
        assert case.loader_calls == [True] and bridge.server.runtime is case.loaded
        code, result, _ = request(bridge, method="POST", route="/v1/rerank", body=rerank_body(case))
        assert code == 200 and result["results"] == [{"index": 0, "relevance_score": 0.5}]
        assert result["usage"] == {"total_tokens": len("q|first|second")}
        assert case.model.calls == [("q", ["first", "second"], {"top_n": 1, "return_embeddings": False})]
        code, identity, head = request(bridge, route="/identity", token=TOKEN)
        assert code == 503 and b"Cache-Control: no-store" in head
        assert identity == {"schema": "vkm-synthetic-text-owner/1", "status": "SYNTHETIC_UNQUALIFIED", "loaded_model_qualification": "NOT_RUN"}
        with pytest.raises(ValueError): NativeModelProof.model_validate(identity)
        assert TOKEN.encode() not in canonical_bytes(identity)
    finally: bridge.close()


@pytest.mark.parametrize("token", [None, "wrong", "\xff"])
@pytest.mark.native_source
def test_identity_authentication_is_separate_and_bounded(case, token):
    bridge = case.create()
    try:
        code, body, head = request(bridge, route="/identity", token=token)
        assert code == 401 and body == {"error": "unauthorized"} and b"no-store" in head
    finally: bridge.close()


@pytest.mark.native_source
def test_duplicate_identity_credentials_do_not_select_a_last_header(case):
    bridge = case.create()
    try:
        assert request(bridge, route="/identity", token=TOKEN, headers=["Authorization: Bearer wrong"])[0] == 401
    finally: bridge.close()


@pytest.mark.parametrize("change", ["runtime", "model", "tokenizer", "formatter", "identity", "weights", "config_file", "recipe_file", "token", "runtime_method", "model_method", "tokenizer_method", "native_handler_method"])
@pytest.mark.native_source
def test_all_actual_native_health_and_inference_routes_fail_closed_after_replacement(case, change):
    bridge = case.create()
    body = rerank_body(case)
    if change == "runtime": bridge.server.runtime = SimpleNamespace(**vars(case.loaded))
    if change == "model": case.loaded._model = TinyModel(case.loaded._tokenizer)
    if change == "tokenizer": case.loaded._tokenizer = TinyTokenizer()
    if change == "formatter": case.loaded._prompt_formatter = lambda *args, **kwargs: "different"
    if change == "identity": case.loaded.identity = SimpleNamespace(as_dict=lambda: {})
    if change == "weights": case.files["weights"].write_bytes(b"other")
    if change == "config_file": Path(case.recipe.runtime_config.path).write_bytes(b"{}")
    if change == "recipe_file": Path(case.ref.path).write_bytes(b"{}")
    if change == "token": Path(case.recipe.identity_token.path).write_text("other")
    if change == "runtime_method": case.loaded.rerank = lambda **kwargs: {}
    if change == "model_method": case.model.rerank = lambda *args, **kwargs: []
    if change == "tokenizer_method": case.loaded._tokenizer.__call__ = lambda **kwargs: {}
    if change == "native_handler_method": case.modules[2]._RerankerHandler.do_POST = lambda *args: None
    try:
        for route, method in (("/healthz", "GET"), ("/readyz", "GET"), ("/identity", "GET"), ("/v1/rerank", "POST")):
            code, value, _ = request(bridge, route=route, method=method, token=TOKEN, body=body if method == "POST" else None)
            assert code == 503 and value == {"error": "model_owner_unavailable"}
        assert not case.model.calls
    finally: bridge.close()


@pytest.mark.native_source
def test_inference_mutation_is_detected_before_any_success_response(case):
    bridge = case.create()
    case.model.mutate = lambda: case.files["weights"].write_bytes(b"mutated-during-inference")
    try:
        code, value, _ = request(bridge, method="POST", route="/v1/rerank", body=rerank_body(case))
        assert code == 503 and value == {"error": "model_owner_unavailable"}
        assert request(bridge, route="/readyz")[0] == 503
    finally: bridge.close()


@pytest.mark.parametrize("change", ["missing_hash", "unlisted_file", "missing_shard", "wrong_python", "wrong_dependency", "wrong_source", "already_loaded", "missing_bridge", "missing_model_owner", "missing_helper"])
@pytest.mark.native_source
def test_complete_source_snapshot_environment_is_required_before_loading(case, change):
    c = case
    if change == "missing_hash":
        value = c.recipe.model_dump(mode="json")
        value["expected_sha256"].pop(c.recipe.resources["weights"])
        Path(c.ref.path).write_bytes(canonical_bytes(value))
        c.ref = BoundFile(path=c.ref.path, sha256=record_hash(value))
    if change == "unlisted_file": (c.roots["model"] / "unlisted.txt").write_text("unlisted")
    if change == "missing_shard": (c.roots["model"] / "model.safetensors.index.json").write_bytes(canonical_bytes({"weight_map": {"layer": "absent.safetensors"}}))
    if change in {"wrong_python", "wrong_dependency"}:
        value = c.recipe.model_dump(mode="json")
        if change == "wrong_python": value["python_version"] = "3.13.999"
        else: value["dependency_versions"]["pydantic"] = "0.0.0"
        Path(c.ref.path).write_bytes(canonical_bytes(value))
        c.ref = BoundFile(path=c.ref.path, sha256=record_hash(value))
    if change == "wrong_source": Path(c.modules[2].__file__).write_text("unsupported-server")
    if change == "already_loaded": c.server.runtime = c.loaded
    if change in {"missing_bridge", "missing_model_owner", "missing_helper"}:
        module = B if change == "missing_bridge" else model_owner if change == "missing_model_owner" else remote_models
        value = c.recipe.model_dump(mode="json")
        value["implementation_files"].remove(module.__file__)
        value["expected_sha256"].pop(module.__file__)
        Path(c.ref.path).write_bytes(canonical_bytes(value))
        c.ref = BoundFile(path=c.ref.path, sha256=record_hash(value))
    with pytest.raises(ValueError): c.create()
    assert not c.loader_calls


@pytest.mark.native_source
def test_close_permanently_denies_previous_readiness_or_identity(case):
    bridge = case.create()
    bridge.close()
    assert request(bridge, route="/readyz")[0] == 503
    assert request(bridge, route="/identity", token=TOKEN)[0] == 503


@pytest.mark.native_source
def test_native_request_schema_runtime_identity_and_budget_behavior_stay_intact(case):
    bridge = case.create()
    try:
        body = rerank_body(case)
        body["token_budget"] = 1
        assert request(bridge, method="POST", route="/v1/rerank", body=body)[0] == 413
        body = rerank_body(case)
        body["expected_runtime"]["model_revision"] = "different"
        assert request(bridge, method="POST", route="/v1/rerank", body=body)[0] == 409
        body = rerank_body(case)
        body["invented"] = 1
        assert request(bridge, method="POST", route="/v1/rerank", body=body)[0] == 400
        assert not case.model.calls
    finally: bridge.close()


@pytest.mark.native_source
def test_actual_patched_native_loader_keeps_original_model_settings_and_uses_only_local_snapshots(case, monkeypatch):
    c, calls = case, []
    def tokenizer(path, **kwargs):
        calls.append(("tokenizer", path, kwargs))
        return c.model._tokenizer
    def model(path, **kwargs):
        calls.append(("model", path, kwargs))
        return c.model
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True), float16="SYNTHETIC_FLOAT16"))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=tokenizer), AutoModel=SimpleNamespace(from_pretrained=model)))
    monkeypatch.setattr(c.modules[1], "_package_version", lambda name: "SYNTHETIC")
    monkeypatch.setattr(sys.modules[__name__], "format_docs_prompts_func", tiny_formatter, raising=False)
    c.loader = lambda: c.modules[1].JinaRerankerRuntime.load(c.config, model_snapshot=c.recipe.model_snapshot,
        tokenizer_snapshot=c.recipe.tokenizer_snapshot, local_files_only=True)
    bridge = c.create()
    try:
        revision = c.config.model_revision
        assert calls == [("tokenizer", c.recipe.tokenizer_snapshot, {"local_files_only": True,
            "revision": revision, "trust_remote_code": True}),
            ("model", c.recipe.model_snapshot, {"local_files_only": True, "revision": revision,
             "code_revision": revision, "dtype": "SYNTHETIC_FLOAT16", "trust_remote_code": True})]
        assert c.model.calls == [("to", c.config.device), ("eval",)]
        assert bridge.server.runtime._model is c.model
    finally: bridge.close()


@pytest.mark.native_source
def test_http_request_logging_never_echoes_credentials_in_url_or_headers(case, caplog):
    bridge = case.create()
    try:
        with caplog.at_level("INFO"):
            assert request(bridge, route="/identity?token=" + TOKEN, token=TOKEN)[0] == 404
        assert TOKEN not in caplog.text and "/identity?token=" not in caplog.text
    finally: bridge.close()


@pytest.mark.native_source
def test_synthetic_injection_cannot_open_a_socket_listener_or_enter_production(case):
    factory_calls = []
    with pytest.raises(ValueError, match="cannot open"):
        B.create_text_owner(case.ref, _runtime_loader=case.loader,
            _server_factory=lambda *args: factory_calls.append(args))
    assert not factory_calls
    bridge = case.create()
    try:
        assert type(bridge.server) is B._MemoryNativeServer
        assert not hasattr(bridge.server, "socket") and not hasattr(bridge.server, "serve_forever")
        assert request(bridge, route="/identity", token=TOKEN)[0] == 503
    finally: bridge.close()
    case.loader_calls.clear()
    value = case.recipe.model_dump(mode="json")
    value["scope"] = "TEXT_OWNER_PRODUCTION"
    value["python_version"] = "3.13.13"
    value["dependency_versions"].update(torch="2.14.0", transformers="4.57.3", pydantic="2.13.5")
    executable = str(Path(sys.executable).resolve())
    value["resources"]["python_executable"] = executable
    value["expected_sha256"][executable] = hashlib.sha256(Path(executable).read_bytes()).hexdigest()
    Path(case.ref.path).write_bytes(canonical_bytes(value))
    ref = BoundFile(path=case.ref.path, sha256=record_hash(value))
    with pytest.raises(ValueError, match="cannot open"):
        B.create_text_owner(ref, _runtime_loader=case.loader, _server_factory=lambda address, runtime: case.server)
    assert not case.loader_calls


@pytest.mark.native_source
def test_runtime_recipe_binds_exact_json_bytes_even_when_not_canonical(case):
    raw = json.dumps(case.recipe.model_dump(mode="json"), indent=2).encode()
    Path(case.ref.path).write_bytes(raw)
    case.ref = BoundFile(path=case.ref.path, sha256=hashlib.sha256(raw).hexdigest())
    bridge = case.create()
    try:
        assert request(bridge)[0] == 200
        Path(case.ref.path).write_bytes(raw + b"\n")
        assert request(bridge)[0] == 503
    finally: bridge.close()


def _zip_wheel(case, tmp_path, *, change=None):
    """A tiny ZIP consumer fixture, not a package build or install qualification."""
    prefix = "careerops_reranker_native-0.1.0.dist-info/"
    package = Path(case.modules[0].__file__).parent
    entries = {"careerops_reranker/" + name: (package / name).read_bytes()
               for name in ("__init__.py", "config.py", "runtime.py", "server.py")}
    source = Path(os.environ["VKM_TEXT_OWNER_TEST_SOURCE_ROOT"])
    entries[prefix + "licenses/LICENSE"] = (source / "LICENSE").read_bytes()
    entries[prefix + "METADATA"] = ("Metadata-Version: 2.4\nName: careerops-reranker-native\nVersion: 0.1.0\n"
        "Requires-Python: <3.14,>=3.13\nRequires-Dist: pydantic==2.13.5\nRequires-Dist: torch==2.14.0\n"
        "Requires-Dist: transformers==4.57.3\n\n").encode()
    entries[prefix + "WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
    if change == "extra_module": entries["careerops/runnable_extra.py"] = b"print('unexpected')"
    if change == "source": entries["careerops_reranker/server.py"] = b"other implementation"
    if change == "license": entries[prefix + "licenses/LICENSE"] = b"Apache License fake"
    if change == "python": entries[prefix + "METADATA"] = entries[prefix + "METADATA"].replace(b">=3.13", b">=3.12")
    if change == "dependency": entries[prefix + "METADATA"] = entries[prefix + "METADATA"].replace(b"torch==2.14.0", b"torch>=2.14.0")
    if change == "tag": entries[prefix + "WHEEL"] = entries[prefix + "WHEEL"].replace(b"py3-none-any", b"cp312-none-any")
    rows = [(name, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("="), str(len(raw)))
            for name, raw in entries.items()]
    rows.append((prefix + "RECORD", "", ""))
    if change == "record_hash": rows[0] = (rows[0][0], "sha256=unrelated", rows[0][2])
    if change == "record_missing": rows.pop(0)
    record = io.StringIO(newline="")
    csv.writer(record).writerows(rows)
    entries[prefix + "RECORD"] = record.getvalue().encode()
    path = tmp_path / "careerops_reranker_native-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        for name, raw in entries.items(): wheel.writestr(name, raw)
    return path


@pytest.mark.native_source
def test_scoped_native_wheel_consumer_binds_actual_sources_license_python_deps_and_record(case, tmp_path):
    B._native_wheel(_zip_wheel(case, tmp_path))


@pytest.mark.parametrize("change", ["extra_module", "source", "license", "python", "dependency", "tag", "record_hash", "record_missing"])
@pytest.mark.native_source
def test_scoped_native_wheel_consumer_rejects_unqualified_zip_fixture(case, tmp_path, change):
    with pytest.raises(ValueError): B._native_wheel(_zip_wheel(case, tmp_path, change=change))


def _image_plan(tmp_path, monkeypatch):
    """No Docker/wheel build: independent syntax/raw-input unit qualification."""
    image = "registry.example.invalid/text-owner@sha256:" + "a" * 64
    base = {"schema": "vkm-text-owner-base-image/1", "status": "BASE_METADATA_VERIFIED_NOT_MODEL_QUALIFIED",
        "image_reference": image, "python_implementation": "CPython", "python_version": "3.13.13",
        "torch_version": "2.14.0", "cuda_version": "13.2", "transformers_version": "4.57.3", "pydantic_version": "2.13.5"}
    def bound(name, raw):
        path = tmp_path / name
        path.write_bytes(raw)
        return BoundFile(path=str(path), sha256=hashlib.sha256(raw).hexdigest())
    plan = B.TextOwnerImagePlan(base_image=image, base_verification=bound("base.json", canonical_bytes(base)),
        dockerfile=bound("Dockerfile", b"synthetic-file"), public_wheel=bound("vkm_world-0.1.0-py3-none-any.whl", b"synthetic-public"),
        native_wheel=bound("careerops_reranker_native-0.1.0-py3-none-any.whl", b"synthetic-native"),
        dependency_lock=bound("dependencies.lock.txt", b"synthetic-lock"), torch_version="2.14.0")
    # Native ZIP contents are independently exercised above against actual source.
    # This injected consumer isolates metadata/file identity, never deployment.
    monkeypatch.setattr(B, "_native_wheel", lambda path: None)
    raw = json.dumps(plan.model_dump(mode="json"), indent=2).encode()
    ref = bound("image-plan.json", raw)
    return plan, ref, base, raw


def test_image_plan_binds_exact_raw_json_without_mistaking_metadata_for_ready(tmp_path, monkeypatch):
    plan, ref, _, raw = _image_plan(tmp_path, monkeypatch)
    assert ref.sha256 != record_hash(plan)
    assert B.require_text_owner_image_plan(ref) == plan
    assert plan.status == "PLANNED_NOT_BUILT"
    Path(ref.path).write_bytes(raw + b"\n")
    with pytest.raises(ValueError, match="changed"): B.require_text_owner_image_plan(ref)


@pytest.mark.parametrize("change", ["python312", "image", "claim_ready", "input_mutated", "bad_filename"])
def test_image_input_gate_rejects_wrong_base_and_changed_build_bytes(tmp_path, monkeypatch, change):
    plan, ref, base, _ = _image_plan(tmp_path, monkeypatch)
    values = plan.model_dump(mode="json")
    if change == "python312": base["python_version"] = "3.12.9"
    if change == "image": base["image_reference"] = "registry.example.invalid/wrong@sha256:" + "b" * 64
    if change == "claim_ready": base["status"] = "READY"
    if change in {"python312", "image", "claim_ready"}:
        raw = canonical_bytes(base)
        Path(plan.base_verification.path).write_bytes(raw)
        values["base_verification"]["sha256"] = hashlib.sha256(raw).hexdigest()
    if change == "input_mutated": Path(plan.dockerfile.path).write_bytes(b"changed")
    if change == "bad_filename":
        new = tmp_path / "careerops-0.1.0-py3-none-any.whl"
        new.write_bytes(b"synthetic-native")
        values["native_wheel"]["path"] = str(new)
    raw = canonical_bytes(values)
    Path(ref.path).write_bytes(raw)
    ref = BoundFile(path=ref.path, sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError): B.require_text_owner_image_plan(ref)
