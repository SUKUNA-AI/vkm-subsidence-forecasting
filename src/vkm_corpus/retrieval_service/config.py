"""Configuration of the RX580 retrieval service.

A deployment JSON (path in ``VKM_RX580_CONFIG``; an example lives in ``infra/core/rx580/``) describes the resident
models and the search targets; a few environment variables override operational knobs. Secrets: only the optional
bearer token, read from ``VKM_RX580_TOKEN_FILE``. No host paths or addresses are hard-coded here.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

Role = Literal["dense", "late", "visual"]   # visual: the text tower of a page-image model (agent VIS)


class ServiceConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelSlot:
    role: Role
    key: str                                  # vkm_corpus.embeddings.specs key
    gguf: str
    gguf_sha256: str = ""
    quant: str = ""
    tokenizer_dir: str = ""
    tokenizer_sha256: str = ""
    heads: str | None = None                  # npz with gateway-side heads (ColBERT / BGE-M3)
    heads_sha256: str = ""
    url: str | None = None                    # external llama-server (spawn = false)
    port: int = 0
    ctx: int = 4096
    ubatch: int = 2048
    parallel: int = 4
    threads: int = 2
    flash_attn: str = "auto"
    query_max_len: int = 512
    doc_max_len: int = 512
    dimension: int | None = None              # Matryoshka dimension served (None = full)
    query_instruction: str | None = None      # override of the spec query prefix (MODEL_CHOICE)
    max_inflight: int = 0                     # per-model admission (0 = unlimited; never shared between models)
    expected_vram_mib: float | None = None    # measured VRAM of this model+quant (MODEL_MATRIX); residency check
    # gpu: every layer on the RX580 (default); cpu: the slot's llama-server runs on the host CPU (--device none), for a
    # model the GPU cannot host within budget/latency (agent VIS fallback); /health then checks liveness, not VRAM
    placement: Literal["gpu", "cpu"] = "gpu"
    extra_args: tuple[str, ...] = ()

    @property
    def endpoint(self) -> str:
        return self.url or f"http://127.0.0.1:{self.port}"


@dataclass(frozen=True)
class SearchTargets:
    opensearch_url: str | None = None
    text_index: str = "vkm-blocks"
    text_fields: tuple[str, ...] = ("text^1.0", "text.exact^0.5")
    dense_index: str | None = None
    dense_field: str = "vector"
    id_field: str = "id"
    multivector_dir: str | None = None        # late artifact config dir (its packs/CURRENT) or one pack directory
    rrf_k: int = 60
    multivector_check_s: float = 10.0         # how often packs/CURRENT is re-read (hot reload of a new pack)
    multivector_rss_budget_mib: int = 256     # mapped token pages dropped from the RSS after this many MiB read


@dataclass(frozen=True)
class ServiceConfig:
    models: tuple[ModelSlot, ...]
    search: SearchTargets = field(default_factory=SearchTargets)
    spawn: bool = True
    llama_server: str = "/opt/llama/bin/llama-server"
    keepalive_s: float = 2.0
    monitor_s: float = 5.0
    restart_on_exit: bool = True
    token: str | None = None
    log_dir: str | None = None
    parity_receipt: str | None = None          # JSON with the parity verdicts shown in /model-info

    def slot(self, role: Role) -> ModelSlot | None:
        return next((m for m in self.models if m.role == role), None)


def _slot(d: Mapping[str, Any]) -> ModelSlot:
    known = set(ModelSlot.__dataclass_fields__)
    unknown = set(d) - known
    if unknown:
        raise ServiceConfigError(f"unknown model keys: {sorted(unknown)}")
    d = dict(d)
    if "extra_args" in d:
        d["extra_args"] = tuple(d["extra_args"])
    slot = ModelSlot(**d)
    if slot.role not in ("dense", "late", "visual"):
        raise ServiceConfigError(f"role must be dense|late|visual, got {slot.role!r}")
    if slot.placement not in ("gpu", "cpu"):
        raise ServiceConfigError(f"placement must be gpu|cpu, got {slot.placement!r}")
    return slot


def load_config(env: Mapping[str, str] | None = None, *, path: str | Path | None = None) -> ServiceConfig:
    env = os.environ if env is None else env
    path = path or env.get("VKM_RX580_CONFIG", "").strip()
    if not path:
        raise ServiceConfigError("VKM_RX580_CONFIG is not set")
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    models = tuple(_slot(m) for m in raw.get("models", []))
    roles = [m.role for m in models]
    if len(set(roles)) != len(roles):
        raise ServiceConfigError("one model per role (dense, late, visual)")
    s = raw.get("search", {})
    search = SearchTargets(
        opensearch_url=env.get("VKM_OPENSEARCH_URL", "").strip() or s.get("opensearch_url"),
        text_index=s.get("text_index", "vkm-blocks"), text_fields=tuple(s.get("text_fields", ("text^1.0",))),
        dense_index=s.get("dense_index"), dense_field=s.get("dense_field", "vector"), id_field=s.get("id_field", "id"),
        multivector_dir=s.get("multivector_dir"), rrf_k=int(s.get("rrf_k", 60)),
        multivector_check_s=float(s.get("multivector_check_s", 10.0)),
        multivector_rss_budget_mib=int(s.get("multivector_rss_budget_mib", 256)))
    svc = raw.get("service", {})
    token = None
    tf = env.get("VKM_RX580_TOKEN_FILE", "").strip()
    if tf:
        token = Path(tf).read_text(encoding="utf-8").strip() or None
    keep = env.get("VKM_RX580_KEEPALIVE_S", "").strip()
    return ServiceConfig(
        models=models, search=search, spawn=bool(svc.get("spawn", True)),
        llama_server=env.get("VKM_LLAMA_SERVER", "").strip() or svc.get("llama_server", "/opt/llama/bin/llama-server"),
        keepalive_s=float(keep) if keep else float(svc.get("keepalive_s", 2.0)),
        monitor_s=float(svc.get("monitor_s", 5.0)), restart_on_exit=bool(svc.get("restart_on_exit", True)),
        token=token, log_dir=env.get("VKM_RX580_LOG_DIR", "").strip() or svc.get("log_dir"),
        parity_receipt=svc.get("parity_receipt"))
