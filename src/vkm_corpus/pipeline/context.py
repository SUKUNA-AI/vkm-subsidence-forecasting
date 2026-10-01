"""Run context shared by the orchestrator and its subprocess workers (serialisable configuration, store, cache)."""
from __future__ import annotations

import json
import os
import hashlib
import importlib.metadata
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from vkm_corpus.artifacts.store import ArtifactStore
from vkm_corpus.extract.classify import Thresholds
from vkm_corpus.layout.ppdoclayout import LayoutConfig
from vkm_corpus.ocr.prompts import ModelIdentity, Sampling
from vkm_corpus.pipeline.cache import StageCache
from vkm_corpus.pipeline.config import OcrCrop, PipelineConfig, ScenarioB

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEVANT_PATHS = ("src", "scripts", "infra", "pyproject.toml", "requirements", "setup.cfg", "setup.py")


class ProducerGuardError(RuntimeError):
    """Production prerequisites were not demonstrated; no producer work may start."""


def _git(*args: str) -> str:
    # Ambient Git redirection must never make another repository look like this checkout.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        result = subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True,
                                encoding="utf-8", timeout=120, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProducerGuardError("Git identity could not be verified") from exc
    if result.returncode != 0:
        raise ProducerGuardError("Git identity command failed")
    return result.stdout.strip()


def dependency_identity(cfg: PipelineConfig) -> dict[str, Any]:
    locks = {"requirements/corpus.lock.txt", *cfg.dependency_locks}
    if cfg.use_gpu_layout:
        locks.add("requirements/corpus-layout.lock.txt")
    pins: dict[str, str] = {}
    hashes = {}
    for rel in sorted(locks):
        p = Path(rel)
        if not p.parts or p.is_absolute() or ".." in p.parts or "\\" in rel or ":" in rel or p.parts[0] != "requirements":
            raise ProducerGuardError("dependency lock must be relative to checkout requirements/")
        _git("ls-files", "--error-unmatch", "--", rel)
        path = (REPO_ROOT / p).resolve()
        if not path.is_relative_to(REPO_ROOT.resolve()) or not path.is_file():
            raise ProducerGuardError("dependency lock is absent or outside the checkout")
        data = path.read_bytes()
        hashes[rel] = hashlib.sha256(data).hexdigest()
        for line in data.decode("utf-8").splitlines():
            value = line.split("#", 1)[0].strip()
            if not value:
                continue
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", value)
            if match is None:
                raise ProducerGuardError("production locks require unconditional exact pins")
            name, version = re.sub(r"[-_.]+", "-", match[1]).lower(), match[2]
            if name in pins and pins[name] != version:
                raise ProducerGuardError("dependency locks contain conflicting pins")
            pins[name] = version
    if not pins:
        raise ProducerGuardError("dependency inventory is empty")
    observed = {}
    for name, expected in sorted(pins.items()):
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ProducerGuardError(f"required dependency is missing: {name}") from exc
        if actual != expected:
            raise ProducerGuardError(f"dependency version differs from lock: {name}")
        observed[name] = actual
    return {"lock_sha256": hashes, "installed_versions": observed, "python": platform.python_version(),
            "implementation": platform.python_implementation(), "platform": platform.system(),
            "machine": platform.machine()}


def producer_identity(cfg: PipelineConfig) -> dict[str, Any]:
    if cfg.profile not in ("exploratory", "production"):
        raise ProducerGuardError("unknown producer profile")
    if cfg.profile != "production":
        return {"profile": "exploratory", "verified": False}
    if not cfg.expected_commit or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", cfg.expected_commit):
        raise ProducerGuardError("production requires an exact full expected_commit")
    rev, dirty = code_revision(strict=True)
    if rev != cfg.expected_commit or dirty:
        raise ProducerGuardError("production requires the expected commit and a clean relevant checkout")
    config = config_to_json(cfg)
    for key in ("data_root", "resources_root", "models_root", "ocr_url"):
        config.pop(key, None)
    body = {"profile": "production", "verified": True, "code_revision": rev, "code_dirty": False,
            "dependencies": dependency_identity(cfg), "config": config}
    body = json.loads(json.dumps(body, sort_keys=True, ensure_ascii=False))  # stable across worker JSON round trips
    body["identity_sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                                        ensure_ascii=False).encode("utf-8")).hexdigest()
    return body


def resource_guard(cfg: PipelineConfig) -> dict[str, Any]:
    """Reserve aggregate CPU worker memory and require a real enforcement mechanism before execution."""
    if cfg.profile != "production":
        return {"verified": False, "effective_workers": max(1, cfg.workers)}
    import math
    values = (cfg.memory_budget_gb, cfg.memory_reserve_gb, cfg.source_memory_gb, cfg.min_free_disk_gb)
    if (any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in values)
            or cfg.memory_budget_gb <= 0 or cfg.memory_reserve_gb < 0 or cfg.source_memory_gb <= 0
            or cfg.min_free_disk_gb <= 0
            or any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (cfg.workers, cfg.ocr_concurrency))):
        raise ProducerGuardError("production requires finite positive memory/disk budgets and concurrency")
    if platform.system() != "Linux":
        raise ProducerGuardError("production memory enforcement is currently qualified only on Linux")
    try:
        import resource
        hard = resource.getrlimit(resource.RLIMIT_AS)[1]
        if hard != resource.RLIM_INFINITY and cfg.source_memory_gb * 1024 ** 3 > hard:
            raise ProducerGuardError("worker budget exceeds the inherited memory hard limit")
        available = available_memory_bytes()
    except (ImportError, OSError, ValueError, KeyError, AttributeError) as exc:
        raise ProducerGuardError("memory enforcement or available-memory observation is unavailable") from exc
    budget = int(cfg.memory_budget_gb * 1024 ** 3)
    if budget > available:
        raise ProducerGuardError("configured aggregate memory reservation exceeds available memory")
    workers = min(cfg.workers, int((cfg.memory_budget_gb - cfg.memory_reserve_gb) // cfg.source_memory_gb))
    if workers < 1:
        raise ProducerGuardError("memory budget cannot reserve even one worker plus coordinator")
    base = Path(cfg.data_root).resolve()
    while not base.exists() and base.parent != base:
        base = base.parent
    free = shutil.disk_usage(base).free
    if free < cfg.min_free_disk_gb * 1024 ** 3:
        raise ProducerGuardError("free disk is below the configured production reserve")
    return {"verified": True, "effective_workers": workers, "memory_budget_bytes": budget,
            "worker_memory_bytes": int(cfg.source_memory_gb * 1024 ** 3), "available_memory_bytes": available,
            "disk_free_bytes": free, "enforcement": "RLIMIT_AS_child_before_exec"}


def available_memory_bytes() -> int:
    """Host availability capped by cgroup limits; /proc/meminfo alone overestimates container memory."""
    mem = dict((line.split(':', 1)[0], int(line.split()[1]) * 1024)
               for line in Path('/proc/meminfo').read_text(encoding='ascii').splitlines())
    available = mem["MemAvailable"]
    for row in Path('/proc/self/cgroup').read_text(encoding='ascii').splitlines():
        hierarchy, controllers, relative = row.split(':', 2)
        if hierarchy == "0" and not controllers:
            base = Path('/sys/fs/cgroup')
            member = base / relative.lstrip('/')
            # Some cgroup namespaces expose a mount-relative root; always include the mount's own limits.
            nodes = [base]
            if member.is_relative_to(base) and '..' not in member.parts:
                nodes.extend(p for p in (member, *member.parents) if p != base and p.is_relative_to(base))
            for node in nodes:
                if (node / 'memory.max').is_file():
                    limit = (node / 'memory.max').read_text(encoding='ascii').strip()
                    if limit != 'max':
                        used = int((node / 'memory.current').read_text(encoding='ascii'))
                        available = min(available, max(0, int(limit) - used))
        elif 'memory' in controllers.split(','):
            node = Path('/sys/fs/cgroup/memory') / relative.lstrip('/')
            if not (node / 'memory.limit_in_bytes').is_file():
                node = Path('/sys/fs/cgroup/memory')
            limit = int((node / 'memory.limit_in_bytes').read_text(encoding='ascii'))
            used = int((node / 'memory.usage_in_bytes').read_text(encoding='ascii'))
            available = min(available, max(0, limit - used))
    return available


def verify_approved_sources(cfg: PipelineConfig, sources: list[Any], plan: dict[str, Any]) -> None:
    """Recheck the approved byte identity, not merely whichever identity a changed registry now declares."""
    if cfg.profile != "production":
        return
    from vkm_corpus.registry.sources import fresh_source_identity

    if plan.get("producer_identity") != producer_identity(cfg):
        raise ProducerGuardError("producer identity differs from the approved plan")
    approved = {row["source_id"]: row.get("source_identity") for row in plan.get("sources", [])}
    for source in sources:
        if source.lifecycle != "ACTIVE":
            raise ProducerGuardError("worker selection changed lifecycle after planning")
        observed = fresh_source_identity(cfg.resources_root, source.canonical_path, source.sha256, source.size_bytes)
        if approved.get(source.source_id) != observed:
            raise ProducerGuardError("worker source differs from the approved plan")


def config_to_json(cfg: PipelineConfig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields(cfg):
        v = getattr(cfg, f.name)
        if isinstance(v, Path):
            out[f.name] = str(v)
        elif hasattr(v, "__dataclass_fields__"):
            out[f.name] = asdict(v)
        else:
            out[f.name] = v
    return out


def _known(cls: Any, values: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in values.items() if k in names}


def config_from_json(data: dict[str, Any]) -> PipelineConfig:
    d = _known(PipelineConfig, dict(data))
    for k in ("data_root", "resources_root", "models_root"):
        if d.get(k):
            d[k] = Path(d[k])
    d["classifier"] = Thresholds(**_known(Thresholds, d["classifier"]))
    d["layout"] = LayoutConfig(**_known(LayoutConfig, d["layout"]))
    d["ocr_crop"] = OcrCrop(**_known(OcrCrop, d["ocr_crop"]))
    d["sampling"] = Sampling(**_known(Sampling, d["sampling"]))
    extra = d["model"].pop("extra", {}) if isinstance(d["model"], dict) else {}
    d["model"] = ModelIdentity(**d["model"], extra=extra)
    d["scenario_b"] = ScenarioB(**_known(ScenarioB, d["scenario_b"]))
    return PipelineConfig(**d)


def run_dir(cfg: PipelineConfig, run_id: str) -> Path:
    p = Path(cfg.data_root) / "tmp" / f"run={run_id}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def open_store(cfg: PipelineConfig, run_id: str, name: str) -> ArtifactStore:
    return ArtifactStore(Path(cfg.data_root) / "artifacts",
                         index_path=Path(cfg.data_root) / "cache" / "artifacts" / f"run={run_id}" / f"{name}.jsonl")


def open_cache(cfg: PipelineConfig, run_id: str, name: str) -> StageCache:
    return StageCache(Path(cfg.data_root), run_id, writer_name=name)


def code_revision(*, strict: bool = False) -> tuple[str, bool]:
    try:
        rev = _git("rev-parse", "--verify", "HEAD^{commit}")
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", rev):
            raise ProducerGuardError("Git returned an invalid full commit")
        dirty = bool(_git("status", "--porcelain", "--untracked-files=all", "--", *RELEVANT_PATHS))
        ignored = _git("ls-files", "--others", "--ignored", "--exclude-standard", "--", *RELEVANT_PATHS)
        executable = {".py", ".pyc", ".sh", ".ps1", ".bat", ".cmd", ".so", ".pyd", ".dll", ".exe"}
        dirty = dirty or any(Path(p).suffix.lower() in executable for p in ignored.splitlines())
        return rev, dirty
    except ProducerGuardError:
        if strict:
            raise
        return "unknown", True


def logical_argv(argv: list[str], cfg: PipelineConfig) -> str:
    """argv with the data/resources/models roots replaced by logical names (no machine paths in the canon)."""
    subs = [(str(cfg.data_root), "DATA:"), (str(cfg.resources_root), "PRIVATE:")]
    if cfg.models_root:
        subs.append((str(cfg.models_root), "MODELS:"))
    out = []
    for a in argv:
        for real, logical in subs:
            a = a.replace(real, logical)
        out.append(a)
    return " ".join(out)


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1, default=str), encoding="utf-8",
                   newline="\n")
    os.replace(tmp, path)
