"""Run context shared by the orchestrator and its subprocess workers (serialisable configuration, store, cache)."""
from __future__ import annotations

import json
import os
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


def code_revision() -> tuple[str, bool]:
    try:
        rev = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=60).stdout.strip() or "unknown"
        dirty = bool(subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=no",
                                     "--", "src"], capture_output=True, text=True, timeout=120).stdout.strip())
        return rev, dirty
    except (OSError, subprocess.TimeoutExpired):
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
