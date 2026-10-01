"""Installed code and dependency identities for the two narrow HTTP services.

Profiles are code-owned, not manifest-selected import lists. The current query
encoder uses Rust tokenizers, NumPy and owned llama.cpp processes. HF/Torch
reference runners are separate capabilities and cannot qualify under this
profile. Native model/process/resource leases remain necessary independently.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata as metadata
import importlib.util
import json
import platform
import stat
from dataclasses import dataclass
from pathlib import Path


RETRIEVAL = "RETRIEVAL_LLAMA_TOKENIZERS_V1"
RERANK = "RERANK_HTTP_TOKENIZERS_V1"


@dataclass(frozen=True)
class ServiceProfile:
    modules: tuple[str, ...]
    # Distribution requirement -> actual runtime import (not necessarily the
    # distribution's name). Transitive requirements are read from installed
    # metadata with active PEP 508 markers/extras and checked, never ignored.
    dependencies: tuple[tuple[str, str], ...]
    auto_runtime: tuple[tuple[str, str], ...] = ()


_COMMON = (
    "vkm_corpus", "vkm_corpus.cli", "vkm_corpus.config", "vkm_corpus.logs", "vkm_corpus.versions",
    "vkm_corpus.contracts", "vkm_corpus.contracts.access",
    "vkm_corpus.parquet", "vkm_corpus.parquet.atomic", "vkm_corpus.update",
    "vkm_corpus.update.service_identity", "vkm_corpus.update.native_files",
    "vkm_corpus.update.remote_retrieval", "vkm_evidence", "vkm_evidence.contracts",
    "vkm_world", "vkm_world.core", "vkm_world.core.provenance",
)
_WEB = (("fastapi>=0.141.1,<0.142", "fastapi"), ("uvicorn>=0.40", "uvicorn"),
        ("httpx>=0.28", "httpx"), ("numpy>=2.5.3,<3", "numpy"),
        ("pydantic>=2.13.5,<3", "pydantic"), ("packaging>=26.3,<27", "packaging"),
        ("tokenizers>=0.22,<1", "tokenizers"))
# Uvicorn auto loop/http/websocket choices and HTTPX auto content decoders.
# An installed but broken extension blocks; absence is recorded explicitly.
# These are transports, not optional encoders (which require an explicit profile).
_AUTO = (("httptools", "httptools"), ("uvloop", "uvloop"), ("websockets", "websockets"),
         ("wsproto", "wsproto"), ("brotli", "brotli"), ("brotlicffi", "brotlicffi"),
         ("zstandard", "zstandard"))
PROFILES = {
    RETRIEVAL: ServiceProfile(_COMMON + (
        "vkm_corpus.retrieval_service", "vkm_corpus.retrieval_service.__main__",
        "vkm_corpus.retrieval_service.app", "vkm_corpus.retrieval_service.cli",
        "vkm_corpus.retrieval_service.config", "vkm_corpus.retrieval_service.encoders",
        "vkm_corpus.retrieval_service.metrics", "vkm_corpus.retrieval_service.residency",
        "vkm_corpus.retrieval_service.search", "vkm_corpus.embeddings",
        "vkm_corpus.embeddings.artifacts", "vkm_corpus.embeddings.gpu", "vkm_corpus.embeddings.llama",
        "vkm_corpus.embeddings.pack", "vkm_corpus.embeddings.postprocess",
        "vkm_corpus.embeddings.signature", "vkm_corpus.embeddings.specs", "vkm_corpus.embeddings.tokenize",
        "vkm_corpus.update.remote_pack",
    ), _WEB + (("pyarrow>=25.0.1,<26", "pyarrow.parquet"),), _AUTO),
    RERANK: ServiceProfile(_COMMON + (
        "vkm_corpus.retrieval", "vkm_corpus.retrieval.cli", "vkm_corpus.retrieval.gateway",
        "vkm_corpus.retrieval.backends", "vkm_corpus.retrieval.models", "vkm_corpus.retrieval.pins",
        "vkm_corpus.retrieval.tokens", "vkm_corpus.retrieval.images", "vkm_corpus.retrieval.m0_head",
        "vkm_corpus.update.remote_rerank", "vkm_corpus.update.remote_models",
    ), _WEB + (("pillow>=12.3", "PIL.Image"),), _AUTO),
}


def _profile(profile: str) -> ServiceProfile:
    try:
        return PROFILES[profile]
    except KeyError as exc:
        raise ValueError("unsupported native service capability profile") from exc


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _source_path(module: str) -> Path:
    # find_spec resolves the installed application, including loaded modules;
    # no checkout path, environment revision or caller-supplied directory.
    spec = importlib.util.find_spec(module)
    if spec is None or not spec.origin or not spec.origin.endswith(".py"):
        raise ValueError(f"native service source module unavailable: {module}")
    path = Path(spec.origin).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError(f"indirect native service code: {module}")
    return path


def code_inventory(profile: str) -> dict:
    files, total = {}, 0
    for module in sorted(_profile(profile).modules):
        path = _source_path(module)
        before = path.stat()
        total += before.st_size
        if not stat.S_ISREG(before.st_mode) or total > 16 * 1024 * 1024:
            raise ValueError("native service code inventory exceeds limit")
        data = path.read_bytes()
        after = path.stat()
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(after):
            raise ValueError("native service code changed while hashing")
        files[module] = hashlib.sha256(data).hexdigest()
    return {"schema": "vkm-service-code/1", "profile": profile, "modules": files}


def dependency_inventory(profile: str) -> dict:
    selected = _profile(profile)
    return runtime_dependency_inventory(profile, selected.dependencies, selected.auto_runtime)


def runtime_dependency_inventory(profile: str, dependencies: tuple[tuple[str, str], ...],
                                 auto_runtime: tuple[tuple[str, str], ...] = ()) -> dict:
    """Installed closure for a code-owned capability, never a manifest import list.

    Both callers supply fixed requirements matching their actual service. The
    narrow wrapper still rejects unknown profiles; this shared implementation
    also lets the API qualify its database clients without adding them to the
    retrieval and rerank images.
    """
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    # Import each real capability, catching absent native extensions as well as
    # missing distribution metadata. Never import GPU/reference libraries here.
    for _, module in dependencies:
        importlib.import_module(module)
    pending = [Requirement(req) for req, _ in dependencies]
    auto_presence = {}
    for distribution, module in auto_runtime:
        present = importlib.util.find_spec(module) is not None
        auto_presence[module] = "PRESENT" if present else "ABSENT"
        if present:
            importlib.import_module(module)
            pending.append(Requirement(distribution))
    seen, packages = {}, {}
    environment = default_environment()
    while pending:
        req = pending.pop()
        name = canonicalize_name(req.name)
        dist = metadata.distribution(name)  # missing transitive dependency blocks
        if req.url or not req.specifier.contains(dist.version, prereleases=True):
            raise ValueError(f"native service dependency requirement is not satisfied: {req}")
        extras = set(req.extras) | seen.get(name, set())
        if name in seen and extras == seen[name]:
            continue
        seen[name] = extras
        if len(seen) > 256:
            raise ValueError("native service dependency closure exceeds limit")
        requirements = sorted(dist.requires or [])
        packages[name] = {"version": dist.version, "extras": sorted(extras), "requires": requirements}
        for text in requirements:
            child = Requirement(text)
            if child.marker is None or any(child.marker.evaluate({**environment, "extra": extra})
                                            for extra in ({""} | extras)):
                pending.append(child)
    return {"schema": "vkm-service-dependencies/1", "profile": profile,
        "python": platform.python_version(), "implementation": platform.python_implementation(),
        "system": platform.system(), "machine": platform.machine(), "packages": packages,
        "auto_runtime": auto_presence}


def service_code_identity(profile: str) -> str:
    return _digest(code_inventory(profile))


def service_dependencies_identity(profile: str) -> str:
    return _digest(dependency_inventory(profile))


def require_retrieval_profile(encoders, residency) -> None:
    from vkm_corpus.embeddings.llama import LlamaServerClient
    from vkm_corpus.embeddings.tokenize import SpecTokenizer
    from vkm_corpus.retrieval_service.encoders import QueryEncoder
    from vkm_corpus.retrieval_service.residency import ModelProcess
    if not encoders or not set(encoders) <= {"dense", "late", "visual"}:
        raise ValueError("unsupported retrieval encoder capability")
    for role, encoder in encoders.items():
        model = residency.processes.get(role)
        if (type(encoder) is not QueryEncoder or type(encoder.tok) is not SpecTokenizer
                or type(model) is not ModelProcess or type(encoder.backend) is not LlamaServerClient
                or encoder.backend is not model.client):
            raise ValueError("unsupported retrieval encoder runtime profile")


def require_rerank_profile(resources) -> None:
    from vkm_corpus.retrieval.backends import TextBackend, VisualBackend
    if type(resources.text) is not TextBackend or type(resources.visual) is not VisualBackend:
        raise ValueError("unsupported rerank client runtime profile")
