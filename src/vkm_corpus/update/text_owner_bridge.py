"""Installed entrypoint for the actual CareerOps loader and HTTP handler.

No sidecar, retrospective attach, model download or manifest-selected callable.
Production requires the pinned local-load patch and a complete prepared inventory.
SYNTHETIC factories are memory-handler tests and cannot open a listening server.
"""
from __future__ import annotations

import argparse
import base64
import csv
import email.parser
import hashlib
import hmac
import importlib
import importlib.metadata
import inspect
import io
import json
import logging
import os
import platform
import re
import sys
import threading
import types
import zipfile
from http import HTTPStatus
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update.admission import _ordinary_bytes
from vkm_corpus.update.model_owner import TextRuntimeOwner
from vkm_corpus.update.native_files import NativeFileWatch
from vkm_corpus.update.receiver import operator_token
from vkm_corpus.update.remote_retrieval import file_signature
from vkm_corpus.update.runtime import BoundFile
from vkm_corpus.update.operator_units import strict_json
from vkm_evidence.contracts import Sha256, StrictModel, canonical_bytes, record_hash

LOG = logging.getLogger(__name__)
NATIVE_SOURCE = {
    "config": "841e19e679ca094dcb28ce8eebd56f5b0e2fdc18089b3eee4127d6fc3cd404fa",
    "runtime": "bdaad913ab76d559deb8b6199e72a87533e2b3e551d61404893024cccd80566c",
    "server": "20e904625bcca332d7e5d6bbce23d5dee8f487631c3548bdb2735656f128c525",
}
BRIDGE_MODULES = (
    "vkm_corpus.update.text_owner_bridge", "vkm_corpus.update.model_owner",
    "vkm_corpus.update.remote_models", "vkm_corpus.update.native_files",
    "vkm_corpus.update.receiver", "vkm_corpus.update.remote_retrieval",
    "vkm_corpus.update.runtime", "vkm_corpus.update.operator_units",
    "vkm_corpus.update.admission", "vkm_corpus.parquet.atomic",
    "vkm_evidence.contracts", "vkm_corpus.retrieval.pins",
)


class TextOwnerImagePlan(StrictModel):
    """Exact immutable Python-3.13 image inputs; never an image-ready claim."""
    schema_version: Literal["vkm-text-owner-image-plan/1"] = "vkm-text-owner-image-plan/1"
    status: Literal["PLANNED_NOT_BUILT"] = "PLANNED_NOT_BUILT"
    base_image: str = Field(pattern=r"^[A-Za-z0-9._/:+-]+@sha256:[0-9a-f]{64}$")
    base_verification: BoundFile
    dockerfile: BoundFile
    public_wheel: BoundFile
    native_wheel: BoundFile
    dependency_lock: BoundFile
    dependency_wheels: tuple[BoundFile, ...] = Field(default=(), max_length=512)
    python_version: Literal["3.13.13"] = "3.13.13"
    torch_version: str = Field(pattern=r"^2\.14\.0(?:\+cu132)?$")
    cuda_version: Literal["13.2"] = "13.2"
    transformers_version: Literal["4.57.3"] = "4.57.3"
    pydantic_version: Literal["2.13.5"] = "2.13.5"

    @model_validator(mode="after")
    def _paths(self):
        refs = (self.base_verification, self.dockerfile, self.public_wheel, self.native_wheel,
                self.dependency_lock, *self.dependency_wheels)
        if (any(not Path(ref.path).is_absolute() or ".." in Path(ref.path).parts for ref in refs)
                or len({ref.path for ref in refs}) != len(refs)):
            raise ValueError("direct unique immutable image inputs required")
        return self


def require_text_owner_image_plan(ref: BoundFile):
    """Validate bound build inputs, without authenticating their supplied authority.

    A base metadata report is only a claim until the independent image verifier
    qualifies its producer and actual immutable image; this function runs neither.
    """
    if not Path(ref.path).is_absolute() or ".." in Path(ref.path).parts:
        raise ValueError("direct immutable image plan required")
    raw = _ordinary_bytes(Path(ref.path), 65536)
    if hashlib.sha256(raw).hexdigest() != ref.sha256:
        raise ValueError("immutable text owner image plan changed")
    plan = TextOwnerImagePlan.model_validate(strict_json(raw))
    for bound in (plan.base_verification, plan.dockerfile, plan.public_wheel, plan.native_wheel, plan.dependency_lock, *plan.dependency_wheels):
        file_signature(bound.path)
        if Path(bound.path).stat().st_nlink != 1 or sha256_of(Path(bound.path)) != bound.sha256:
            raise ValueError("immutable text owner image input changed")
    base = strict_json(_ordinary_bytes(Path(plan.base_verification.path), 65536))
    expected = {"schema": "vkm-text-owner-base-image/1", "status": "BASE_METADATA_VERIFIED_NOT_MODEL_QUALIFIED",
        "image_reference": plan.base_image, "python_implementation": "CPython", "python_version": plan.python_version,
        "torch_version": plan.torch_version, "cuda_version": plan.cuda_version,
        "transformers_version": plan.transformers_version, "pydantic_version": plan.pydantic_version}
    if base != expected:
        raise ValueError("base image cannot support the exact PUBLIC/native owner environment")
    if (Path(plan.public_wheel.path).name != "vkm_world-0.1.0-py3-none-any.whl"
            or Path(plan.native_wheel.path).name != "careerops_reranker_native-0.1.0-py3-none-any.whl"):
        raise ValueError("valid exact scoped wheel filenames required")
    _native_wheel(Path(plan.native_wheel.path))
    return plan


def _native_wheel(path):
    """No entire CareerOps environment or unpinned native code enters the image."""
    prefix = "careerops_reranker_native-0.1.0.dist-info/"
    sources = {"careerops_reranker/" + key + ".py": value for key, value in NATIVE_SOURCE.items()}
    sources["careerops_reranker/__init__.py"] = "2d1246b790407c0f10effbbbb4fc226b3c8bd40e0ba86fbd680ad6ede82e75b4"
    required = {*sources, *(prefix + name for name in ("METADATA", "WHEEL", "RECORD", "licenses/LICENSE"))}
    with zipfile.ZipFile(path) as wheel:
        files = wheel.infolist()
        if (len(files) != len(required) or {info.filename for info in files} != required
                or sum(info.file_size for info in files) > 2 * 1024 * 1024
                or any(info.file_size > 512 * 1024 for info in files)):
            raise ValueError("scoped native wheel inventory differs")
        if any(hashlib.sha256(wheel.read(name).replace(b"\r\n", b"\n")).hexdigest() != digest
               for name, digest in sources.items()):
            raise ValueError("scoped native wheel contains an unqualified implementation")
        metadata = email.parser.BytesParser().parsebytes(wheel.read(prefix + "METADATA"))
        if (metadata.get("Name") != "careerops-reranker-native" or metadata.get("Version") != "0.1.0"
                or set(part.strip() for part in (metadata.get("Requires-Python") or "").split(",")) != {">=3.13", "<3.14"}
                or set(metadata.get_all("Requires-Dist", [])) != {"pydantic==2.13.5", "torch==2.14.0", "transformers==4.57.3"}):
            raise ValueError("scoped native wheel dependency contract differs")
        if hashlib.sha256(wheel.read(prefix + "licenses/LICENSE").replace(b"\r\n", b"\n")).hexdigest() != "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4":
            raise ValueError("native source licence was not retained")
        wheel_metadata = email.parser.BytesParser().parsebytes(wheel.read(prefix + "WHEEL"))
        if (wheel_metadata.get("Wheel-Version") != "1.0" or wheel_metadata.get("Root-Is-Purelib") != "true"
                or wheel_metadata.get_all("Tag") != ["py3-none-any"]):
            raise ValueError("scoped native wheel format differs")
        rows = list(csv.reader(io.StringIO(wheel.read(prefix + "RECORD").decode("utf-8"))))
        if (len(rows) != len(required) or any(len(row) != 3 for row in rows)
                or {row[0] for row in rows} != required):
            raise ValueError("scoped native wheel RECORD inventory differs")
        for name, digest, size in rows:
            if name == prefix + "RECORD":
                if digest or size:
                    raise ValueError("wheel RECORD cannot hash itself")
                continue
            raw = wheel.read(name)
            expected = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
            if digest != expected or size != str(len(raw)):
                raise ValueError("scoped native wheel RECORD identity differs")


class TextOwnerRecipe(StrictModel):
    schema_version: Literal["vkm-text-owner-recipe/1"] = "vkm-text-owner-recipe/1"
    scope: Literal["TEXT_OWNER_PRODUCTION", "SYNTHETIC"]
    runtime_config: BoundFile
    model_snapshot: str
    tokenizer_snapshot: str
    dynamic_modules_cache: str
    identity_token: BoundFile
    resources: dict[str, str]
    implementation_files: tuple[str, ...] = Field(min_length=1, max_length=512)
    expected_sha256: dict[str, Sha256]
    python_version: str = Field(pattern=r"^3\.13\.[0-9]+$")
    dependency_versions: dict[str, str] = Field(min_length=1, max_length=128)
    max_clients: int = Field(default=8, ge=1, le=32)
    request_timeout_s: float = Field(default=20.0, gt=0, le=60)

    @model_validator(mode="after")
    def _bounded(self):
        paths = {*self.resources.values(), *self.implementation_files,
                 self.runtime_config.path, self.identity_token.path}
        if (not {"weights", "tokenizer"} <= self.resources.keys() or not 2 <= len(self.resources) <= 512
                or any(not Path(p).is_absolute() or ".." in Path(p).parts for p in
                       (*paths, self.model_snapshot, self.tokenizer_snapshot, self.dynamic_modules_cache))
                or set(self.expected_sha256) != paths or len(paths) > 1024
                or len(set(self.implementation_files)) != len(self.implementation_files)
                or self.expected_sha256[self.runtime_config.path] != self.runtime_config.sha256
                or self.expected_sha256[self.identity_token.path] != self.identity_token.sha256):
            raise ValueError("exact complete bounded text-owner inventory required")
        if self.scope == "TEXT_OWNER_PRODUCTION" and (not {"torch", "transformers", "pydantic"} <= self.dependency_versions.keys()
                or "python_executable" not in self.resources):
            raise ValueError("production native model dependencies and Python executable must be explicit")
        if self.scope == "TEXT_OWNER_PRODUCTION" and (self.python_version != "3.13.13"
                or not re.fullmatch(r"2\.14\.0(?:\+cu132)?", self.dependency_versions["torch"])
                or self.dependency_versions["transformers"] != "4.57.3"
                or self.dependency_versions["pydantic"] != "2.13.5"):
            raise ValueError("production text owner requires the exact qualified 3.13 image plan")
        return self


def _source_hash(path):
    return hashlib.sha256(_ordinary_bytes(Path(path), 2 * 1024 * 1024).replace(b"\r\n", b"\n")).hexdigest()


def _native_modules(recipe, injected=None):
    modules = injected or tuple(importlib.import_module("careerops_reranker." + key)
                                for key in ("config", "runtime", "server"))
    if len(modules) != 3:
        raise ValueError("actual CareerOps module set required")
    inventory = set(recipe.implementation_files)
    for key, module in zip(("config", "runtime", "server"), modules, strict=True):
        if (getattr(module, "__file__", None) not in inventory
                or _source_hash(module.__file__) != NATIVE_SOURCE[key]):
            raise ValueError("actual CareerOps source differs from the pinned patched implementation")
    if not {"model_snapshot", "tokenizer_snapshot", "local_files_only"} <= inspect.signature(modules[1].JinaRerankerRuntime.load).parameters.keys():
        raise ValueError("actual CareerOps offline load patch is absent")
    return modules


def _bridge_implementation_files():
    """Code selects its own imported PUBLIC helper closure, never the recipe.

    Follow actual imported vkm modules/classes/functions, including provenance
    validators. Unrelated modules elsewhere in sys.modules are not included.
    """
    pending, visited, paths = list(BRIDGE_MODULES), set(), set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        if len(visited) > 256:
            raise ValueError("PUBLIC text owner helper closure exceeds bound")
        module = importlib.import_module(name)
        path = getattr(module, "__file__", None)
        if path is None or not Path(path).is_absolute() or Path(path).suffix != ".py":
            raise ValueError("ordinary installed PUBLIC text helper required")
        paths.add(path)
        for value in vars(module).values():
            target = value.__name__ if isinstance(value, types.ModuleType) else getattr(value, "__module__", "")
            if isinstance(target, str) and target.startswith(("vkm_corpus.", "vkm_evidence.", "vkm_world.")):
                pending.append(target)
    return tuple(sorted(paths))


def _require_bridge_implementation(recipe):
    required = set(_bridge_implementation_files())
    if not required <= set(recipe.implementation_files) or not required <= recipe.expected_sha256.keys():
        raise ValueError("code-owned PUBLIC text-owner helper inventory is incomplete")
    if recipe.scope == "TEXT_OWNER_PRODUCTION" and recipe.resources["python_executable"] != str(Path(sys.executable).resolve()):
        raise ValueError("production Python executable differs from the bound actual process")


class _MemoryNativeServer:
    """Source-owned memory-handler test state; owns no transport or listener."""
    def __init__(self):
        self.runtime = None
        self.closed = False

    def server_close(self):
        self.closed = True


def _inventory(recipe):
    """No unmanifested configs, shard files, custom code or mutable cache links."""
    declared = set(recipe.expected_sha256)
    discovered = set()
    for value in (recipe.model_snapshot, recipe.tokenizer_snapshot, recipe.dynamic_modules_cache):
        root = Path(value)
        if not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
            raise ValueError("direct immutable prepared text snapshot required")
        for directory, directories, files in os.walk(root, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in directories):
                raise ValueError("snapshot directory alias is not immutable")
            for name in files:
                path = Path(directory) / name
                signature = file_signature(path)
                if path.stat().st_nlink != 1 or signature[2] <= 0 and path.name != "__init__.py":
                    raise ValueError("snapshot contains empty or shared resource")
                discovered.add(str(path))
                if len(discovered) > 1024:
                    raise ValueError("text snapshot inventory exceeds bound")
                if name.endswith(".index.json"):
                    index = strict_json(_ordinary_bytes(path, 2 * 1024 * 1024))
                    mapping = index.get("weight_map") if isinstance(index, dict) else None
                    if not isinstance(mapping, dict) or not mapping:
                        raise ValueError("invalid model shard index")
                    for shard in mapping.values():
                        if (not isinstance(shard, str) or Path(shard).is_absolute() or ".." in Path(shard).parts
                                or str(path.parent / shard) not in declared or not (path.parent / shard).is_file()):
                            raise ValueError("model shard missing from exact immutable inventory")
    if not discovered <= declared:
        raise ValueError("snapshot contains an unmanifested file")
    if (recipe.resources["weights"] != str(Path(recipe.model_snapshot) / "model.safetensors")
            or recipe.resources["tokenizer"] != str(Path(recipe.tokenizer_snapshot) / "tokenizer.json")
            or not any(Path(p).is_relative_to(Path(recipe.dynamic_modules_cache)) and Path(p).suffix == ".py"
                       for p in recipe.implementation_files)):
        raise ValueError("pinned weights/tokenizer and prepared custom implementation required")
    for path, wanted in recipe.expected_sha256.items():
        file_signature(path)
        if Path(path).stat().st_nlink != 1 or sha256_of(Path(path)) != wanted:
            raise ValueError("text owner resource identity differs")


class TextOwnerBridge:
    def _check(self):
        if self.closed or self.invalid:
            raise ValueError("text owner bridge is closed")
        try:
            self.watch.check()
            if (sha256_of(Path(self.recipe_ref.path)) != self.recipe_ref.sha256
                    or record_hash(self.config) != self.config_sha
                    or self.server.runtime is not self.owner.runtime
                    or self.server.RequestHandlerClass is not self.handler
                    or self.server.runtime.identity is not self.identity
                    or record_hash(self.identity.as_dict()) != self.identity_sha
                    or any(_callable_key(getattr(target, name)) != original
                           for target, name, original in self.callables)):
                raise ValueError("actual text owner config/runtime/handler/method changed")
            self.owner.observe()
        except (AttributeError, OSError, TypeError, ValueError):
            self.invalid = True
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.server.server_close()
            finally:
                self.owner.close()
                self.watch.close()


def create_text_owner(recipe_ref: BoundFile, *, _runtime_loader=None, _server_factory=None, _modules=None):
    if not Path(recipe_ref.path).is_absolute() or ".." in Path(recipe_ref.path).parts:
        raise ValueError("direct immutable text recipe required")
    raw = _ordinary_bytes(Path(recipe_ref.path), 2 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != recipe_ref.sha256:
        raise ValueError("immutable text owner recipe changed")
    recipe = TextOwnerRecipe.model_validate(strict_json(raw))
    synthetic = recipe.scope == "SYNTHETIC"
    if (_server_factory is not None or not synthetic and any(x is not None for x in (_runtime_loader, _modules))):
        raise ValueError("synthetic injection cannot open or qualify a production server")
    if platform.python_version() != recipe.python_version or any(
            importlib.metadata.version(name) != version for name, version in recipe.dependency_versions.items()):
        raise ValueError("text owner Python/dependencies differ from the approved environment")
    if not synthetic and (platform.system() != "Linux" or
            os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("TRANSFORMERS_OFFLINE") != "1"
            or os.environ.get("HF_MODULES_CACHE") != recipe.dynamic_modules_cache):
        raise ValueError("production text owner requires Linux and prepared offline custom-code cache")
    obj = TextOwnerBridge()
    obj.recipe, obj.recipe_ref, obj.closed, obj.invalid = recipe, recipe_ref, False, False
    obj.watch = NativeFileWatch([recipe_ref.path, *recipe.expected_sha256])
    server, owner = None, None
    try:
        _require_bridge_implementation(recipe)
        _inventory(recipe)
        config_module, runtime_module, native = _native_modules(recipe, _modules)
        config = config_module.RerankerRuntimeConfig.model_validate(strict_json(_ordinary_bytes(Path(recipe.runtime_config.path), 65536)))
        from vkm_corpus.retrieval.pins import TEXT
        if (config.model_id != TEXT["model_id"] or any(getattr(config, field) != TEXT["model_revision"] for field in
                ("model_revision", "model_code_revision", "tokenizer_revision"))
                or config.runtime_backend != "transformers-cuda" or config.dtype_or_quantization != "float16"
                or not config.device.startswith("cuda")):
            raise ValueError("text bridge cannot change model/revision/backend/dtype/device policy")
        if not synthetic and any(recipe.expected_sha256[recipe.resources[role]] != TEXT[role + "_sha256"]
                                 for role in ("weights", "tokenizer")):
            raise ValueError("production text bridge cannot replace the pinned original model inputs")
        obj.config, obj.config_sha = config, record_hash(config)
        obj.token = operator_token(Path(recipe.identity_token.path))
        class BoundedNativeServer(native._RerankerServer):
            def __init__(self, address, runtime):
                self._client_slots = threading.BoundedSemaphore(recipe.max_clients)
                super().__init__(address, runtime)

            def process_request(self, channel, address):
                if not self._client_slots.acquire(blocking=False):
                    self.shutdown_request(channel)
                    return
                try:
                    super().process_request(channel, address)
                except BaseException:
                    self._client_slots.release()
                    raise

            def process_request_thread(self, channel, address):
                try:
                    super().process_request_thread(channel, address)
                finally:
                    self._client_slots.release()

        server = _MemoryNativeServer() if synthetic else BoundedNativeServer((config.host, config.port), None)
        if server.runtime is not None:
            raise ValueError("bridge cannot attach to an already loaded serving runtime")
        obj.server = server
        loader = _runtime_loader or (lambda: runtime_module.JinaRerankerRuntime.load(config,
            model_snapshot=recipe.model_snapshot, tokenizer_snapshot=recipe.tokenizer_snapshot, local_files_only=True))
        owner = TextRuntimeOwner.load(resources={name: Path(path) for name, path in recipe.resources.items()},
            implementation_files=recipe.implementation_files, dependency_packages=tuple(recipe.dependency_versions),
            config={"recipe": recipe.model_dump(mode="json"), "runtime": config.model_dump(mode="json")},
            runtime_loader=loader, serving_runtime_getter=lambda: server.runtime,
            install_runtime=lambda value: setattr(server, "runtime", value))
        if type(server.runtime) is not runtime_module.JinaRerankerRuntime:
            raise ValueError("actual CareerOps runtime must own the HTTP inference path")
        obj.owner = owner
        obj.identity, obj.identity_sha = server.runtime.identity, record_hash(server.runtime.identity.as_dict())
        obj.callables = tuple((target, name, _callable_key(getattr(target, name)))
            for target, names in ((server.runtime, ("rerank", "exact_total_tokens", "_prompt_token_count")),
                                  (server.runtime._model, ("rerank", "_truncate_texts")),
                                  (server.runtime._tokenizer, ("__call__",)),
                                  (native._RerankerHandler, ("do_GET", "do_POST")))
            for name in names)

        class QualifiedHandler(native._RerankerHandler):
            timeout = recipe.request_timeout_s

            def log_message(self, format, *args):
                # Native request-line logging could echo URL bearer strings.
                LOG.info("text_owner_http_request")

            def _write_json(self, status, payload):
                self._pending_reply = (status, payload)

            def _emit(self, status, payload):
                raw = canonical_bytes(payload)
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def _run(self, method):
                self._pending_reply = None
                try:
                    obj._check()
                    with owner.serving():
                        method()
                    obj._check()
                    if self._pending_reply is None:
                        raise ValueError("native handler did not produce a bounded JSON response")
                    self._emit(*self._pending_reply)
                except (AttributeError, OSError, TypeError, ValueError):
                    self._emit(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "model_owner_unavailable"})

            def do_GET(self):
                if self.path == "/identity":
                    auth = self.headers.get_all("Authorization") or []
                    authorized = (len(auth) == 1 and len(auth[0]) <= 512
                        and hmac.compare_digest(auth[0].encode("utf-8"), ("Bearer " + obj.token).encode("ascii")))
                    if not authorized:
                        self._emit(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    if synthetic:
                        self._run(lambda: self._write_json(HTTPStatus.SERVICE_UNAVAILABLE,
                            {"schema": "vkm-synthetic-text-owner/1", "status": "SYNTHETIC_UNQUALIFIED",
                             "loaded_model_qualification": "NOT_RUN"}))
                        return
                    self._run(lambda: self._write_json(HTTPStatus.OK, owner.observe().model_dump(mode="json")))
                    return
                self._run(lambda: super(QualifiedHandler, self).do_GET())

            def do_POST(self):
                self._run(lambda: super(QualifiedHandler, self).do_POST())

        obj.handler = QualifiedHandler
        server.RequestHandlerClass = QualifiedHandler
        obj._check()
        return obj
    except BaseException:
        if server is not None: server.server_close()
        if owner is not None: owner.close()
        obj.watch.close()
        raise


def _callable_key(value):
    """Bound methods are recreated by Python; compare their owner and function."""
    return (id(getattr(value, "__self__", None)), id(getattr(value, "__func__", value)))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Actual pinned CareerOps text-owner entrypoint")
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--recipe-sha256", required=True)
    args = parser.parse_args(argv)
    bridge = None
    try:
        bridge = create_text_owner(BoundFile(path=args.recipe, sha256=args.recipe_sha256))
        if bridge.recipe.scope != "TEXT_OWNER_PRODUCTION":
            raise ValueError("synthetic owner cannot serve")
        native = importlib.import_module("careerops_reranker.server")
        stop = threading.Event()
        native._install_signal_handlers(stop)
        bridge.server.timeout = 0.5
        while not stop.is_set(): bridge.server.handle_request()
        return 0
    except Exception:
        LOG.error("text_owner_startup_unavailable")
        return 2
    finally:
        if bridge is not None: bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
