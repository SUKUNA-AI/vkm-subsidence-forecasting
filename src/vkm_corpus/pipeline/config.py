"""Pipeline configuration: roots, stage parameters, model identities, budgets.

Roots come from ``vkm_corpus.config.load_settings()``: ``VKM_DATA_ROOT`` with ``VKM_DATA_ROLE=producer`` (the
staging root), ``VKM_RESOURCES_ROOT``, ``VKM_OCR_URL`` and ``VKM_MODELS_DIR`` (``Settings.models_dir``). Only parameters that change a stage's *output* enter that stage's config
hash (``stage_config``); budgets, concurrency and paths never do.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from vkm_corpus.extract.classify import DEFAULT_THRESHOLDS, Thresholds
from vkm_corpus.layout.ppdoclayout import LayoutConfig
from vkm_corpus.layout.regions import DEFAULT_THRESHOLDS as REGION_THRESHOLDS, REGION_RULE
from vkm_corpus.ocr.normalize import OCR_NORMALIZE_RULE
from vkm_corpus.ocr.quality import QUALITY_RULE
from vkm_corpus.ocr.prompts import DEFAULT_MODEL, DEFAULT_SAMPLING, ModelIdentity, Sampling

# semantic extraction generations (H-14): bumped consciously, they enter producer keys and object ids
GENERATIONS: dict[str, int] = {"pymupdf-native": 2, "djvulibre-cli": 1, "epub-xhtml": 2, "docx-xml": 2,
                               "pp-doclayoutv3-hf": 1, "vkm-glm-ocr-client": 1, "pymupdf-find-tables": 1}
EXTRACTOR_VERSIONS: dict[str, str] = {"pymupdf-native": "0.1.0", "djvulibre-cli": "0.1.0", "epub-xhtml": "0.1.0",
                                      "docx-xml": "0.1.0", "pp-doclayoutv3-hf": "0.1.0",
                                      "vkm-glm-ocr-client": "0.1.0", "pymupdf-find-tables": "0.1.0",
                                      "vkm-pipeline": "0.1.0", "libreoffice-docx-pdf": "0.1.0"}


@dataclass
class ScenarioB:
    enabled: bool = True
    share: float = 0.05
    min_pages: int = 1
    cer_threshold: float = 0.10
    letter_share_min: float = 0.60
    reocr: bool = True
    budget_gpu_hours: float = 4.0


@dataclass
class OcrCrop:
    # crop dpi per task: formulas need small glyphs (300); text and tables use 200 dpi, the page dpi of the GLM-OCR SDK
    dpi_by_task: dict[str, int] = field(default_factory=lambda: {"formula": 300, "text": 200, "table": 200})
    scan_dpi_max: int = 300        # scans: never above this …
    scan_dpi_min: int = 150        # … and never below this; never above the scan's own resolution (no upsampling)
    pad_pt: float = 2.0
    mode: str = "L"
    # tall text/table crops are cut into bands of at most this height (px at the crop dpi) at blank rows: bounded
    # output per call (dense numeric tables otherwise exceed the token cap) and bounded KV use on the server
    band_max_px: dict[str, int] = field(default_factory=lambda: {"text": 1100, "table": 600})
    # no sliver bands: a remainder below this height stays in the last band (3-4 px strips were rejected by the
    # model's image processor: aspect ratio > 200)
    band_min_tail_px: int = 64


@dataclass
class PipelineConfig:
    data_root: Path
    resources_root: Path
    models_root: Path | None = None
    ocr_url: str | None = None
    classifier: Thresholds = DEFAULT_THRESHOLDS
    layout: LayoutConfig = field(default_factory=LayoutConfig)
    region_thresholds: dict[str, float] = field(default_factory=lambda: dict(REGION_THRESHOLDS))
    layout_render_dpi: int = 200
    preview_long_side: int = 1024
    preview_quality: int = 85
    ocr_crop: OcrCrop = field(default_factory=OcrCrop)
    sampling: Sampling = DEFAULT_SAMPLING
    # output caps per task: a looping region stops at the cap (finish_reason=length → TRUNCATED, CP-22 window)
    max_tokens_by_task: dict[str, int] = field(default_factory=lambda: {"text": 4096, "table": 6144, "formula": 1024})
    model: ModelIdentity = DEFAULT_MODEL
    ocr_concurrency: int = 32
    ocr_timeout_s: float = 600.0
    max_model_calls: int | None = None
    stop_window: int = 200
    scenario_b: ScenarioB = field(default_factory=ScenarioB)
    workers: int = 8
    source_timeout_s: float = 3600.0
    source_memory_gb: float = 12.0
    docx_render_image: str = "vkm-libreoffice:24.2"
    ocr_inline_formulas: bool = True
    spread_aspect: float = 1.25
    use_gpu_layout: bool = True     # False = explicit no-model configuration (different extractor id)
    normalize_tag: str | None = None  # test knob: changes only the NORMALIZE stage configuration
    profile: str = "exploratory"  # production is fail-closed; no implicit promotion of exploratory receipts
    expected_commit: str | None = None
    dependency_locks: tuple[str, ...] = ()  # additional exact locks, relative to the clean source checkout
    memory_budget_gb: float | None = None  # total concurrent worker reservation, GiB
    memory_reserve_gb: float = 2.0
    min_free_disk_gb: float = 10.0

    def sampling_for(self, task: str) -> Sampling:
        from dataclasses import replace

        cap = self.max_tokens_by_task.get(task)
        return replace(self.sampling, max_tokens=cap) if cap else self.sampling

    def stage_config(self, stage: str) -> dict[str, Any]:
        if stage in ("CLASSIFY",):
            return self.classifier.as_config()
        if stage == "NATIVE_TEXT":
            return {"text_flags": "TEXTFLAGS_DICT-PRESERVE_IMAGES", "raw_schema": "vkm.native_raw.pdf_page/1"}
        if stage == "RENDER":
            return {"dpi": self.layout_render_dpi, "mode": "RGB", "preview_long_side": self.preview_long_side,
                    "preview_quality": self.preview_quality, "spread_aspect": self.spread_aspect}
        if stage == "LAYOUT":
            return {**self.layout.as_config(), "render_dpi": self.layout_render_dpi, "spread_aspect": self.spread_aspect}
        if stage == "REGIONS":
            return {"rule": REGION_RULE, "thresholds": dict(sorted(self.region_thresholds.items()))}
        if stage == "OCR":
            return {"crop": asdict(self.ocr_crop), "sampling": self.sampling.as_dict(), **self.model.as_dict(),
                    "inline_formulas": self.ocr_inline_formulas,
                    "max_tokens_by_task": dict(sorted(self.max_tokens_by_task.items()))}
        if stage == "NORMALIZE":
            out = {"ocr_rule": OCR_NORMALIZE_RULE, "text_rule": "normalize_text_v1", "page_rule": "page_text_v1",
                   "quality_rule": QUALITY_RULE}
            if self.normalize_tag:  # K-07: a normalisation-only change rebuilds rows without model calls
                out["tag"] = self.normalize_tag
            return out
        if stage == "SCENARIO_B":
            return asdict(self.scenario_b)
        return {}


def load_pipeline_config(**overrides: Any) -> PipelineConfig:
    from vkm_corpus.config import load_settings

    s = load_settings()
    if s.data_role != "producer":
        raise RuntimeError("the pipeline writes only to a producer (staging) root: set VKM_DATA_ROLE=producer")
    data_root = s.require_data_root()
    resources = s.require_resources_root()
    cfg = PipelineConfig(data_root=data_root, resources_root=resources, models_root=s.models_dir, ocr_url=s.ocr_url,
                         normalize_tag=os.environ.get("VKM_NORMALIZE_TAG", "").strip() or None)
    # CP-22 window size (CP-32): not part of any stage signature, so changing it never invalidates caches
    window = os.environ.get("VKM_OCR_STOP_WINDOW", "").strip()
    if window:
        cfg.stop_window = int(window)
    for k, v in overrides.items():
        if v is not None:
            setattr(cfg, k, v)
    return cfg
