"""Load-time native model attestation; never infer loaded weights retrospectively.

This helper belongs inside the service that owns the actual loaded model. A
gateway's /readyz claims or nearby files cannot substitute for this boundary.
The service must expose observe() through its authenticated /identity handler.
The existing external CareerOps/llama services do not yet implement this hook.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import platform
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update.remote_retrieval import file_signature
from vkm_evidence.contracts import Sha256, StrictModel, record_hash


class NativeModelProof(StrictModel):
    schema_version: Literal["vkm-loaded-model/1"] = "vkm-loaded-model/1"
    kind: Literal["text", "visual"]
    instance_sha256: Sha256
    code_sha256: Sha256
    dependencies_sha256: Sha256
    config_sha256: Sha256
    resources: dict[str, Sha256]
    tokenizer_binding: Literal["STANDALONE_LOADED", "EMBEDDED_WEIGHTS_VOCAB"] = "STANDALONE_LOADED"

    @model_validator(mode="after")
    def _resources(self):
        required = {"weights", "tokenizer"} | ({"mmproj"} if self.kind == "visual" else set())
        if not required <= self.resources.keys():
            raise ValueError("native loaded model resources incomplete")
        if self.tokenizer_binding == "EMBEDDED_WEIGHTS_VOCAB":
            if self.kind != "visual" or self.resources["tokenizer"] != self.resources["weights"]:
                raise ValueError("embedded native vocabulary must be bound to actual visual weights")
        return self


def native_identity_json(raw):
    """An authenticated identity must have one unambiguous value per field."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate native identity field")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("nonfinite native identity value")

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)
    except (UnicodeDecodeError, RecursionError) as exc:
        raise ValueError("invalid native identity encoding or structure") from exc


def own_process():
    if platform.system() != "Linux":
        raise ValueError("native loaded-model receiver currently qualified only on Linux")
    fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": os.getpid(), "start_ticks": int(fields[19]),
            "executable": list(file_signature(Path("/proc/self/exe").resolve()))}


class LoadedModelLease:
    """Trusted service code calls load() around its real loader, before admission.

    No manifest selects a callable. Config/model paths come from the same
    installed application that calls its loader; getter is the serving object's
    actual accessor. Implementations/resources must remain operator immutable.
    """
    @classmethod
    def load(cls, *, kind, resources, implementation_files, dependency_packages, config,
             loader, serving_getter, install):
        if kind not in {"text", "visual"} or not dependency_packages:
            raise ValueError("explicit model kind and dependency inventory required")
        process = own_process()
        required = {"weights", "tokenizer"} | ({"mmproj"} if kind == "visual" else set())
        if not required <= resources.keys():
            raise ValueError("complete native model resource inventory required")
        from vkm_corpus.update.native_files import NativeFileWatch
        watch = NativeFileWatch([*resources.values(), *implementation_files])
        paths = {str(Path(p).absolute()): file_signature(p)
                 for p in [*resources.values(), *implementation_files]}
        if not implementation_files:
            raise ValueError("native model implementation files required")
        hashes = {p: sha256_of(Path(p)) for p in paths}
        if any(file_signature(p) != sig for p, sig in paths.items()):
            raise ValueError("model resources changed while hashing")
        dependencies = {p: importlib.metadata.version(p) for p in dependency_packages}
        loaded = loader()
        watch.check()
        module = importlib.import_module(type(loaded).__module__)
        module_path = str(Path(module.__file__).absolute()) if getattr(module, "__file__", None) else None
        if module_path not in {str(Path(p).absolute()) for p in implementation_files}:
            raise ValueError("actual loaded model implementation absent from native inventory")
        if any(file_signature(p) != sig for p, sig in paths.items()) or own_process() != process:
            raise ValueError("model resource/process changed during loading")
        install(loaded)
        if serving_getter() is not loaded:
            raise ValueError("attested model is not the actual serving object")
        obj = cls()
        obj.loaded, obj.getter, obj.files, obj.process = loaded, serving_getter, paths, process
        obj.watch = watch
        obj.config, obj.config_sha256 = config, record_hash(config)
        obj.packages, obj.dependencies = tuple(dependency_packages), dependencies
        obj.proof = NativeModelProof(kind=kind, instance_sha256=record_hash(process),
            code_sha256=record_hash({Path(p).name + ':' + str(i): hashes[str(Path(p).absolute())]
                for i, p in enumerate(implementation_files)}),
            dependencies_sha256=record_hash({"python": platform.python_version(), "packages": dependencies}),
            config_sha256=obj.config_sha256,
            resources={name: hashes[str(Path(path).absolute())] for name, path in resources.items()})
        obj.observe()
        return obj

    def observe(self):
        self.watch.check()
        if (self.getter() is not self.loaded or own_process() != self.process
                or any(file_signature(p) != sig for p, sig in self.files.items())
                or record_hash(self.config) != self.config_sha256
                or {p: importlib.metadata.version(p) for p in self.packages} != self.dependencies):
            raise ValueError("native loaded model instance/resources/configuration changed")
        return self.proof.model_copy(deep=True)


async def read_backend_model_identity(backend, kind, token):
    """Uses exactly the HTTP client used for inference, never a sidecar URL."""
    if not token:
        raise ValueError("authenticated native model provider is not configured")
    client = backend.client()
    import httpx
    try:
        response = await client.get("/identity", headers={"Authorization": "Bearer " + token,
            "Cache-Control": "no-cache"}, timeout=5.0)
    except httpx.HTTPError as exc:
        raise ValueError("native model identity transport unavailable") from exc
    if response.status_code != 200 or len(response.content) > 262144:
        raise ValueError("native model identity unavailable")
    proof = NativeModelProof.model_validate(native_identity_json(response.content))
    if proof.kind != kind:
        raise ValueError("native model kind differs from serving route")
    return proof
