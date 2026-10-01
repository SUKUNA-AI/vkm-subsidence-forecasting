"""Phase 3 — GLM-OCR over the regions that need recognition (and the shared task plan used by the commit phase).

``plan_page_tasks`` is a pure function of (route, regions, page, config, scenario-B flags); the OCR phase and the
commit phase call it with the same inputs, so the commit finds every result through the call signature of the same
crop without a model call. Crops are rendered deterministically (pixel hash → ``call_signature``), stored as
``OCR_INPUT`` (lossless PNG), sent asynchronously, and every attempt is one immutable ``OCR_RAW`` record plus an
index line in the call cache.
"""
from __future__ import annotations

import asyncio
import io
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from vkm_corpus.layout.regions import Region, overlap_share, regions_from_raw
from vkm_corpus.ocr.prompts import prompt_for
from vkm_corpus.pipeline.config import PipelineConfig

TEXT_FAMILY = "text"


@dataclass
class OcrTaskSpec:
    source_id: str
    page_index: int
    page_id: str
    task: str                         # text | table | formula
    role: str                         # PRIMARY | FORMULA | TABLE | SAMPLE_B | REOCR | FULL_PAGE | EPUB_GIF
    crop_dpi: int
    det_index: int | None = None      # index into the LAYOUT_RAW detections
    bbox_pt: tuple[float, float, float, float] | None = None
    label: str | None = None
    score: float | None = None
    order_rank: int | None = None
    epub_member: str | None = None
    epub_image_order: int | None = None

    def key(self) -> tuple[Any, ...]:
        return (self.source_id, self.page_index, self.role, self.task, self.det_index, self.epub_image_order)


@dataclass
class CropOut:
    spec: OcrTaskSpec
    png: bytes
    png_sha256: str
    pixel_sha256: str
    width: int
    height: int
    mode: str
    ink: float
    crop_box_px: list[int] | None
    call_signature: str = ""
    error: str | None = None
    band_index: int = 0               # tall text/table regions are cut into horizontal bands at blank rows
    band_count: int = 1
    band_hard_cut: bool = False
    spec_index: int = 0               # position of the spec in the page's task list


# ---------------------------------------------------------------------------------------------------- planning
def crop_dpi_for(cfg: PipelineConfig, fmt: str, prep_row: dict[str, Any], task: str = "text") -> int:
    """Crop dpi of a task: the task's dpi, capped for rasters by the scan's own resolution (never upsampled)."""
    c = cfg.ocr_crop
    want = int(c.dpi_by_task.get(task, 200))
    raster = fmt == "DJVU" or prep_row.get("page_class") in ("RASTER_SCAN", "BROKEN_TEXT_LAYER") and \
        (prep_row.get("img_cov") or 0) >= 0.5
    if not raster:
        return want
    native = prep_row.get("native_dpi") or prep_row.get("img_main_dpi") or c.scan_dpi_max
    return int(max(c.scan_dpi_min, min(want, c.scan_dpi_max, round(float(native)))))


def load_regions(store: Any, layout_raw_artifact_id: str | None, width_pt: float, height_pt: float,
                 thresholds: dict[str, float], *, trace=None) -> tuple[dict[str, Any] | None, list[Region]]:
    if not layout_raw_artifact_id:
        return None, []
    raw = store.read_json(layout_raw_artifact_id)
    return raw, regions_from_raw(raw, width_pt, height_pt, thresholds, trace=trace)


def _dedupe_text(regions: list[Region]) -> list[Region]:
    """Text regions fully inside another kept text region are not recognised twice."""
    texts = [r for r in regions if r.family == TEXT_FAMILY]
    out = []
    for r in texts:
        if any(o is not r and o.area > r.area and overlap_share(r.bbox, o.bbox) >= 0.9 for o in texts):
            continue
        out.append(r)
    return out


def plan_page_tasks(cfg: PipelineConfig, fmt: str, source_id: str, prep_row: dict[str, Any],
                    vis_row: dict[str, Any] | None, regions: list[Region], *, sample_b: bool = False,
                    reocr: bool = False) -> list[OcrTaskSpec]:
    """Recognition tasks of one page (deterministic order)."""
    idx = prep_row["page_index"]
    route = prep_row.get("route")
    unit = "s" if fmt == "EPUB" else "r" if fmt == "DOCX" else "p"
    page_id = f"{source_id}:{unit}{idx:04d}"
    tasks: list[OcrTaskSpec] = []
    if fmt == "EPUB":
        return tasks  # EPUB formula images are planned by plan_epub_tasks
    if fmt == "DOCX" or route in (None, "FAILED"):
        return tasks
    dpi_of = {t: crop_dpi_for(cfg, fmt, prep_row, t) for t in ("text", "table", "formula")}
    ink = (vis_row or {}).get("ink_ratio") or 0.0
    if route == "EMPTY" and ink >= cfg.classifier.empty_ink:
        route = "OCR_REQUIRED"
    formulas = [r for r in regions if r.family == "formula" and
                (r.label == "display_formula" or cfg.ocr_inline_formulas)]
    tables = [r for r in regions if r.family == "table"]

    def spec(r: Region, task: str, role: str) -> OcrTaskSpec:
        return OcrTaskSpec(source_id=source_id, page_index=idx, page_id=page_id, task=task, role=role,
                           crop_dpi=dpi_of[task],
                           det_index=r.det_index, bbox_pt=r.bbox, label=r.label, score=r.score,
                           order_rank=r.order_rank)

    has_layout = bool((vis_row or {}).get("layout_raw_artifact_id"))
    if route == "OCR_REQUIRED":
        if not has_layout:
            return []  # regions pending: the page stays OCR_REQUIRED until its layout exists
        texts = _dedupe_text(regions)
        tasks += [spec(r, "text", "PRIMARY") for r in texts]
        tasks += [spec(r, "table", "TABLE") for r in tables]
        tasks += [spec(r, "formula", "FORMULA") for r in formulas]
        if not texts and not tables and not formulas:
            w, h = prep_row.get("width_pt"), prep_row.get("height_pt")
            if w and h:
                tasks.append(OcrTaskSpec(source_id=source_id, page_index=idx, page_id=page_id, task="text",
                                         role="FULL_PAGE", crop_dpi=dpi_of["text"],
                                         bbox_pt=(0.0, 0.0, float(w), float(h))))
    elif route in ("NATIVE", "NATIVE_REPAIR", "OCR_OPTIONAL"):
        tasks += [spec(r, "table", "TABLE") for r in tables]
        tasks += [spec(r, "formula", "FORMULA") for r in formulas]
        if route == "OCR_OPTIONAL" and (sample_b or reocr):
            role = "SAMPLE_B" if sample_b else "REOCR"
            tasks += [spec(r, "text", role) for r in _dedupe_text(regions)]
    tasks.sort(key=lambda t: (t.order_rank if t.order_rank is not None else 10**6, t.task, t.det_index or -1))
    return tasks


def plan_epub_tasks(source_id: str, unit_raw: dict[str, Any]) -> list[OcrTaskSpec]:
    idx = unit_raw["index"]
    page_id = f"{source_id}:s{idx:04d}"
    return [OcrTaskSpec(source_id=source_id, page_index=idx, page_id=page_id, task="formula", role="EPUB_GIF",
                        crop_dpi=0, epub_member=im["href"], epub_image_order=im["order"])
            for im in unit_raw.get("images", []) if im.get("is_formula_candidate")]


def task_key(cfg: PipelineConfig, spec: OcrTaskSpec) -> str:
    """Planning key of a task (page, role, task, region, crop profile, model call config) – lets ``run plan`` count
    cached results without rendering. The authoritative cache key stays the pixel-based call signature."""
    import hashlib

    from vkm_corpus.contracts.signatures import config_hash

    parts = [spec.page_id, spec.role, spec.task, str(spec.det_index), str(spec.bbox_pt), str(spec.crop_dpi),
             str(spec.epub_member), str(spec.epub_image_order),
             config_hash({"ocr": cfg.stage_config("OCR"), "regions": cfg.stage_config("REGIONS")})]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------------- crops
def crop_settings(cfg: PipelineConfig) -> dict[str, Any]:
    return {"pad_pt": cfg.ocr_crop.pad_pt, "mode": cfg.ocr_crop.mode, "band_max_px": dict(cfg.ocr_crop.band_max_px),
            "band_min_tail_px": cfg.ocr_crop.band_min_tail_px}


def call_signature_for(cfg: PipelineConfig, task: str, pixel_sha256: str) -> str:
    from vkm_corpus.contracts.signatures import call_signature

    m = cfg.model
    return call_signature(model_id=m.model_id, model_revision=m.model_revision, weights_sha256=m.weights_sha256,
                          prompt=prompt_for(task), sampling=cfg.sampling_for(task).as_dict(),
                          input_pixel_sha256=pixel_sha256)


def _crop_record(spec: OcrTaskSpec, image: Any, box: list[int] | None) -> CropOut:
    from vkm_corpus.artifacts.render import ink_ratio, png_bytes
    from vkm_corpus.artifacts.store import sha256_hex
    from vkm_corpus.contracts.signatures import pixel_sha256

    png = png_bytes(image)
    return CropOut(spec=spec, png=png, png_sha256=sha256_hex(png),
                   pixel_sha256=pixel_sha256(image.mode, image.width, image.height, image.tobytes()),
                   width=image.width, height=image.height, mode=image.mode, ink=ink_ratio(image), crop_box_px=box)


def band_rows(image: Any, max_h: int, min_gap: int = 4, min_tail: int = 64) -> list[tuple[int, int, bool]]:
    """Horizontal bands of at most ``max_h`` px, cut at the centre of blank pixel rows (deterministic).

    No sliver is left at the bottom: when a cut would leave less than ``min_tail`` px, the last band keeps the rest
    (it may exceed ``max_h`` by less than ``min_tail``). A 3–4 px strip is not a region, and a crop with an aspect
    ratio above 200 is rejected by the GLM-OCR image processor.
    Returns ``(top, bottom, hard_cut)``; ``hard_cut`` is True when no blank run was found in the allowed window."""
    import numpy as np

    h = image.height
    if max_h <= 0 or h <= max_h:
        return [(0, h, False)]
    a = np.asarray(image.convert("L")) < 160
    ink = a.mean(axis=1)
    blank = ink < 0.002
    cuts: list[int] = []
    run_start = None
    for y in range(h + 1):
        b = y < h and bool(blank[y])
        if b and run_start is None:
            run_start = y
        elif not b and run_start is not None:
            if y - run_start >= min_gap:
                cuts.append((run_start + y) // 2)
            run_start = None
    out: list[tuple[int, int, bool]] = []
    start = 0
    while h - start > max_h:
        window = [c for c in cuts if start + max_h // 2 < c <= start + max_h]
        if window:
            cut, hard = window[-1], False
        else:
            cut, hard = start + max_h, True
        if h - cut < min_tail:
            break
        out.append((start, cut, hard))
        start = cut
    out.append((start, h, False))
    return out


_PDF_CACHE: dict[str, Any] = {}


def _cached_pdf(file_path: str) -> Any:
    """Per-process cache of opened PDFs (the crop workers see consecutive pages of the same source)."""
    from vkm_corpus.extract import pdf_native

    doc = _PDF_CACHE.get(file_path)
    if doc is None:
        while len(_PDF_CACHE) >= 2:
            _, old = _PDF_CACHE.popitem()
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        doc = pdf_native.open_pdf(Path(file_path))
        _PDF_CACHE[file_path] = doc
    return doc


def make_page_crops(cfg_dict: dict[str, Any], fmt: str, file_path: str, specs: list[OcrTaskSpec]) -> list[CropOut]:
    """Render the page once per crop dpi and cut every region (runs in a worker process)."""
    from vkm_corpus.artifacts.render import crop_box_px, crop_pt, render_djvu_page, render_pdf_page

    if not specs:
        return []
    out: list[CropOut] = []
    if fmt == "EPUB":
        import zipfile

        from PIL import Image

        with zipfile.ZipFile(file_path) as z:
            for si, s in enumerate(specs):
                try:
                    img = Image.open(io.BytesIO(z.read(s.epub_member)))
                    img.seek(0)
                    if img.mode in ("P", "PA", "LA", "RGBA"):
                        img = img.convert("RGBA")
                        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
                        img = Image.alpha_composite(bg, img)
                    img = img.convert("L")
                    rec = _crop_record(s, img, None)
                    rec.spec_index = si
                    out.append(rec)
                except Exception as exc:  # noqa: BLE001
                    out.append(CropOut(spec=s, png=b"", png_sha256="", pixel_sha256="", width=0, height=0, mode="",
                                       ink=0.0, crop_box_px=None, error=f"{type(exc).__name__}: {exc}"[:300],
                                       spec_index=si))
        return out
    idx = specs[0].page_index
    pad = float(cfg_dict.get("pad_pt", 2.0))
    mode = cfg_dict.get("mode", "L")
    band_max = cfg_dict.get("band_max_px") or {}
    rasters: dict[int, Any] = {}
    errors: dict[int, str] = {}
    doc = None
    try:
        for dpi in sorted({s.crop_dpi for s in specs}):
            try:
                if fmt == "DJVU":
                    rasters[dpi] = render_djvu_page(Path(file_path), idx, dpi, mode, f"crop{dpi}")
                else:
                    if doc is None:
                        doc = _cached_pdf(file_path)
                    rasters[dpi] = render_pdf_page(doc[idx - 1], dpi, mode, f"crop{dpi}")
            except Exception as exc:  # noqa: BLE001
                errors[dpi] = f"RENDER_FAILED {type(exc).__name__}: {exc}"[:300]
    finally:
        pass
    for si, s in enumerate(specs):
        if s.crop_dpi in errors:
            out.append(CropOut(spec=s, png=b"", png_sha256="", pixel_sha256="", width=0, height=0, mode="", ink=0.0,
                               crop_box_px=None, error=errors[s.crop_dpi], spec_index=si))
            continue
        raster = rasters[s.crop_dpi]
        try:
            box = crop_box_px(raster, s.bbox_pt, pad)
            img = crop_pt(raster, s.bbox_pt, pad)
            limit = int(band_max.get(s.task) or 0)
            bands = band_rows(img, limit, min_tail=int(cfg_dict.get("band_min_tail_px") or 64)) if limit else \
                [(0, img.height, False)]
            for i, (top, bottom, hard) in enumerate(bands):
                part = img if len(bands) == 1 else img.crop((0, top, img.width, bottom))
                rec = _crop_record(s, part, [box[0], box[1] + top, box[2], box[1] + bottom] if len(bands) > 1 else box)
                rec.band_index, rec.band_count, rec.band_hard_cut, rec.spec_index = i, len(bands), hard, si
                out.append(rec)
        except Exception as exc:  # noqa: BLE001
            out.append(CropOut(spec=s, png=b"", png_sha256="", pixel_sha256="", width=0, height=0, mode="", ink=0.0,
                               crop_box_px=None, error=f"{type(exc).__name__}: {exc}"[:300], spec_index=si))
    return out


def crop_plan_signature(cfg: PipelineConfig, fmt: str, source_sha256: str, specs: list[OcrTaskSpec]) -> str:
    """Stage signature of the crops of one page: specs, crop settings and renderer versions (not the pixels)."""
    from vkm_corpus.contracts.signatures import config_hash, stage_signature
    from vkm_corpus.pipeline.prepare import library_versions

    try:
        import PIL

        pil = PIL.__version__
    except ImportError:  # pragma: no cover
        pil = "?"
    conf = {"fmt": fmt, "crop": crop_settings(cfg), "renderers": {**library_versions(), "pillow": pil},
            "specs": [asdict(s) for s in specs]}
    return stage_signature(source_sha256=source_sha256, stage="OCR_CROPS", pipeline_version="0.1.0",
                           extractor_id="vkm-pipeline", extractor_version="0.1.0", stage_config_hash=config_hash(conf),
                           page_unit="p", page_index=specs[0].page_index if specs else 0)


def crops_to_manifest(crops: list[CropOut]) -> list[dict[str, Any]]:
    return [{"spec_index": c.spec_index, "band_index": c.band_index, "band_count": c.band_count,
             "band_hard_cut": c.band_hard_cut, "pixel_sha256": c.pixel_sha256, "png_sha256": c.png_sha256,
             "width": c.width, "height": c.height, "mode": c.mode, "ink": round(c.ink, 6),
             "crop_box_px": c.crop_box_px, "error": c.error} for c in crops]


def crops_from_manifest(entries: list[dict[str, Any]], specs: list[OcrTaskSpec]) -> list[CropOut]:
    out = []
    for e in entries:
        out.append(CropOut(spec=specs[e["spec_index"]], png=b"", png_sha256=e["png_sha256"],
                           pixel_sha256=e["pixel_sha256"], width=e["width"], height=e["height"], mode=e["mode"],
                           ink=e["ink"], crop_box_px=e["crop_box_px"], error=e["error"], band_index=e["band_index"],
                           band_count=e["band_count"], band_hard_cut=e["band_hard_cut"], spec_index=e["spec_index"]))
    return out


def manifest_crops(cfg: PipelineConfig, cache: Any, fmt: str, source_sha256: str,
                   specs: list[OcrTaskSpec]) -> list[CropOut] | None:
    """Crops of a page's task list from the recorded ``OCR_CROPS`` manifests, without rendering.

    The OCR phase records one manifest per page and pass: the main tasks, and for a source re-OCR'd by scenario B the
    ``REOCR`` tasks separately. The full list of the assembler/plan is found as a whole or as these two parts (each
    part keeps the relative order of the sorted full list). ``None`` when a part has no manifest."""
    if not specs:
        return []
    hit = cache.get_stage(crop_plan_signature(cfg, fmt, source_sha256, specs))
    if hit is not None:
        return crops_from_manifest(hit["outputs"]["crops"], specs)
    parts = [[i for i, s in enumerate(specs) if s.role != "REOCR"], [i for i, s in enumerate(specs) if s.role == "REOCR"]]
    if not parts[0] or not parts[1]:
        return None
    out: list[CropOut] = []
    for idxs in parts:
        sub = [specs[i] for i in idxs]
        h = cache.get_stage(crop_plan_signature(cfg, fmt, source_sha256, sub))
        if h is None:
            return None
        for c in crops_from_manifest(h["outputs"]["crops"], sub):
            c.spec_index = idxs[c.spec_index]
            c.spec = specs[c.spec_index]
            out.append(c)
    return out


def cached_specs(cfg: PipelineConfig, cache: Any, crops: list[CropOut]) -> set[int]:
    """Indices (in the page's task list) of the tasks whose every band has a successful cached call."""
    ok: dict[int, bool] = {}
    for c in crops:
        hit = not c.error and cache.best_call(call_signature_for(cfg, c.spec.task, c.pixel_sha256)) is not None
        ok[c.spec_index] = ok.get(c.spec_index, True) and hit
    return {i for i, v in ok.items() if v}


def _crop_job(args: tuple[dict[str, Any], str, str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    cfg_dict, fmt, file_path, spec_dicts = args
    specs = [OcrTaskSpec(**{**d, "bbox_pt": tuple(d["bbox_pt"]) if d.get("bbox_pt") else None}) for d in spec_dicts]
    return [{**c.__dict__, "spec": asdict(c.spec)} for c in make_page_crops(cfg_dict, fmt, file_path, specs)]


def _crop_from_dict(d: dict[str, Any]) -> CropOut:
    s = d["spec"]
    spec = OcrTaskSpec(**{**s, "bbox_pt": tuple(s["bbox_pt"]) if s.get("bbox_pt") else None})
    return CropOut(**{**d, "spec": spec})


# ---------------------------------------------------------------------------------------------------- runner
@dataclass
class PageWork:
    source_id: str
    fmt: str
    file_path: str
    page_index: int
    specs: list[OcrTaskSpec]
    source_sha256: str = ""


@dataclass
class OcrStats:
    tasks: int = 0
    cached: int = 0
    called: int = 0
    ok: int = 0
    failed: int = 0
    skipped_budget: int = 0
    crop_errors: int = 0
    stopped: str | None = None
    wall_s: float = 0.0
    latencies_ms: list[int] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    flags: dict[str, int] = field(default_factory=dict)
    pages_done: int = 0


async def run_ocr(cfg: PipelineConfig, store: Any, cache: Any, work: list[PageWork], *, backend: dict[str, Any],
                  max_calls: int | None = None, recall: bool = False, crop_workers: int = 6, log: Any = None,
                  budget_s: float | None = None, stats: OcrStats | None = None, transport: Any = None) -> OcrStats:
    """Send every uncached crop of ``work`` to GLM-OCR (bounded concurrency) and record raw outputs."""
    from vkm_corpus.ocr.client import GlmOcrClient, OcrRequest
    from vkm_corpus.ocr.quality import StopRun, StopWindow, call_flags
    from vkm_corpus.ocr.raw import build_record
    from vkm_corpus.coverage.accounting import record_ocr_event

    stats = stats or OcrStats()
    source_versions = {w.source_id: w.source_sha256 for w in work}
    if any(source_versions[w.source_id] != w.source_sha256 for w in work):
        raise ValueError("ACCOUNTING_MIXED_SOURCE_VERSIONS")

    def event(spec, phase, reason=None, crop=None, outputs=(), attempt=None):
        return record_ocr_event(cfg, cache, spec, source_versions[spec.source_id], phase=phase, reason=reason,
                                crop=crop, outputs=outputs, attempt=attempt)

    for w in work:
        for spec in w.specs:
            event(spec, "PLANNED", "OCR_NOT_STARTED")
    if not cfg.ocr_url:
        for w in work:
            for spec in w.specs:
                event(spec, "NOT_RUN", "MODEL_UNAVAILABLE")
        raise RuntimeError("MODEL_UNAVAILABLE: VKM_OCR_URL is not set")
    window = StopWindow(window=cfg.stop_window)
    t_start = time.perf_counter()
    cfg_dict = crop_settings(cfg)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    budget_left = [max_calls if max_calls is not None else None]

    async with GlmOcrClient(cfg.ocr_url, model=cfg.model.served_model_name, concurrency=cfg.ocr_concurrency,
                            timeout_s=cfg.ocr_timeout_s, transport=transport) as client:
        inflight: set[asyncio.Task] = set()

        async def one(crop: CropOut) -> None:
            spec = crop.spec
            attempt = cache.next_attempt(crop.call_signature)
            event(spec, "RUNNING", "OCR_RESPONSE_NOT_YET_DURABLE", crop, attempt=attempt)
            inp = store.put_bytes(crop.png, "OCR_INPUT", "image/png", source_id=spec.source_id, page_id=spec.page_id,
                                  image_width_px=crop.width, image_height_px=crop.height, image_dpi=spec.crop_dpi or None,
                                  pixel_sha256=crop.pixel_sha256)
            req = OcrRequest(task=spec.task, prompt=prompt_for(spec.task), png=crop.png, png_sha256=crop.png_sha256,
                             pixel_sha256=crop.pixel_sha256, width=crop.width, height=crop.height, mode=crop.mode,
                             sampling=cfg.sampling_for(spec.task))
            resp = await client.recognize(req)
            record = build_record(resp, call_signature=crop.call_signature, attempt=attempt, model=cfg.model,
                                  backend=backend, run_id=cache.run_id, source_id=spec.source_id,
                                  page_id=spec.page_id,
                                  input_ref={"artifact_id": inp.artifact_id, "crop_dpi": spec.crop_dpi,
                                             "crop_box_px": crop.crop_box_px, "bbox_pt": spec.bbox_pt,
                                             "epub_member": spec.epub_member, "band_index": crop.band_index,
                                             "band_count": crop.band_count},
                                  region_ref={"det_index": spec.det_index, "label": spec.label, "score": spec.score,
                                              "role": spec.role})
            flags = call_flags(resp.content, resp.finish_reason, crop.ink) if resp.ok else []
            record["quality_flags"] = flags
            raw = await loop.run_in_executor(None, lambda: store.put_json(
                record, "OCR_RAW", source_id=spec.source_id, page_id=spec.page_id,
                producer_signature=crop.call_signature))
            cache.add_call(call_signature=crop.call_signature, attempt=attempt, status="OK" if resp.ok else "ERROR",
                           raw_artifact_id=raw.artifact_id, input_artifact_id=inp.artifact_id, kind="OCR",
                           source_id=spec.source_id, page_id=spec.page_id,
                           extra={"task": spec.task, "role": spec.role, "task_key": task_key(cfg, spec),
                                  "source_sha256": source_versions[spec.source_id],
                                  "finish_reason": resp.finish_reason,
                                  "latency_ms": resp.latency_ms, "flags": flags,
                                  "error": None if resp.ok else (resp.error or resp.status)[:200]})
            event(spec, "SUCCEEDED" if resp.ok else "FAILED",
                  "OCR_TOKEN_LIMIT" if resp.finish_reason == "length" else "OCR_QUALITY_FLAGS" if flags
                  else None if resp.ok else "OCR_MODEL_ERROR", crop,
                  outputs=(raw.artifact_id[7:], inp.artifact_id[7:]), attempt=attempt)
            stats.called += 1
            if resp.ok:
                stats.ok += 1
                stats.latencies_ms.append(resp.latency_ms)
                u = resp.usage or {}
                stats.prompt_tokens += int(u.get("prompt_tokens") or 0)
                stats.completion_tokens += int(u.get("completion_tokens") or 0)
            else:
                stats.failed += 1
            for f in flags:
                stats.flags[f] = stats.flags.get(f, 0) + 1
            window.add(flags, error=not resp.ok)
            try:
                window.check()
            except StopRun as exc:
                stats.stopped = str(exc)
                stop_event.set()

        async def guarded(crop: CropOut) -> None:
            try:
                await one(crop)
            except Exception as exc:  # noqa: BLE001 - a store/cache failure of one call is counted, not fatal
                stats.failed += 1
                event(crop.spec, "FAILED", "OCR_CALL_OR_PERSISTENCE_FAILED", crop,
                      attempt=cache.next_attempt(crop.call_signature))
                if log is not None:
                    log.error("ocr call failed", extra={"vkm": {"page_id": crop.spec.page_id, "stage": "OCR",
                                                                "error_code": "OCR_FAILED",
                                                                "detail": f"{type(exc).__name__}: {exc}"[:300]}})

        import multiprocessing

        # spawn: the event loop and HTTP client threads of this process must not be forked
        executor = ProcessPoolExecutor(max_workers=max(1, crop_workers),
                                       mp_context=multiprocessing.get_context("spawn"))
        work_iter = iter([w for w in work if w.specs])
        queue: list[asyncio.Future] = []

        def submit_next() -> bool:
            w = next(work_iter, None)
            if w is None:
                return False
            for spec in w.specs:
                event(spec, "RUNNING", "OCR_CROP_IN_PROGRESS")
            queue.append((w, loop.run_in_executor(executor, _crop_job,
                                                  (cfg_dict, w.fmt, w.file_path, [asdict(s) for s in w.specs]))))
            return True

        try:
            for _ in range(max(2, crop_workers * 2)):
                if not submit_next():
                    break
            while queue:
                if stop_event.is_set():
                    break
                w, fut = queue.pop(0)
                try:
                    crops = [_crop_from_dict(d) for d in await fut]
                except Exception:
                    for spec in w.specs:
                        event(spec, "FAILED", "OCR_CROP_WORKER_FAILED")
                    raise
                expected_keys = {s.key() for s in w.specs}
                if any(c.spec.key() not in expected_keys for c in crops):
                    for spec in w.specs:
                        event(spec, "FAILED", "OCR_CROP_WORKER_UNEXPECTED_OUTPUT")
                    raise ValueError("OCR_CROP_WORKER_UNEXPECTED_OUTPUT")
                for spec in w.specs:
                    parts = [c for c in crops if c.spec.key() == spec.key()]
                    if not parts:
                        event(spec, "FAILED", "OCR_CROP_WORKER_OUTPUT_MISSING")
                        raise ValueError("OCR_CROP_WORKER_OUTPUT_MISSING")
                    elif (not 1 <= parts[0].band_count <= 4096 or len({c.band_count for c in parts}) != 1 or
                          sorted(c.band_index for c in parts) != list(range(parts[0].band_count))):
                        event(spec, "FAILED", "OCR_CROP_BANDS_INVALID")
                        raise ValueError("OCR_CROP_BANDS_INVALID")
                submit_next()
                stats.pages_done += 1
                if w.source_sha256 and not any(c.error for c in crops):
                    cache.add_stage(stage_signature=crop_plan_signature(cfg, w.fmt, w.source_sha256, w.specs),
                                    stage="OCR_CROPS", source_id=w.source_id, page_index=w.page_index,
                                    outputs={"crops": crops_to_manifest(crops)})
                for crop in crops:
                    stats.tasks += 1
                    if crop.error:
                        stats.crop_errors += 1
                        reason = "OCR_CROP_SIZE_GUARD" if any(s in crop.error.lower() for s in
                            ("too large", "image size", "aspect ratio", "decompressionbomb")) else "OCR_CROP_FAILED"
                        event(crop.spec, "FAILED", reason, crop)
                        continue
                    crop.call_signature = call_signature_for(cfg, crop.spec.task, crop.pixel_sha256)
                    if not recall and cache.best_call(crop.call_signature) is not None:
                        stats.cached += 1
                        hit = cache.best_call(crop.call_signature)
                        event(crop.spec, "SUCCEEDED", "REUSED_CACHED", crop,
                              outputs=(hit["raw_artifact_id"][7:],), attempt=int(hit["attempt"]))
                        continue
                    if budget_left[0] is not None and budget_left[0] <= 0:
                        stats.skipped_budget += 1
                        event(crop.spec, "NOT_RUN", "OCR_CALL_BUDGET_EXHAUSTED", crop)
                        continue
                    if budget_s is not None and time.perf_counter() - t_start > budget_s:
                        stats.skipped_budget += 1
                        event(crop.spec, "NOT_RUN", "OCR_TIME_BUDGET_EXHAUSTED", crop)
                        continue
                    if stop_event.is_set():
                        break
                    if budget_left[0] is not None:
                        budget_left[0] -= 1
                    while len(inflight) >= cfg.ocr_concurrency * 2:
                        done, _ = await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED)
                        inflight.difference_update(done)
                    t = asyncio.create_task(guarded(crop))
                    inflight.add(t)
                if log is not None and stats.pages_done % 20 == 0:
                    log.info("ocr progress", extra={"vkm": {"stage": "OCR", "status": f"pages={stats.pages_done} "
                                                    f"calls={stats.called} cached={stats.cached}"}})
            if inflight:
                await asyncio.wait(inflight)
        finally:
            if stop_event.is_set():
                for w in work:
                    for spec in w.specs:
                        event(spec, "RUN_STOPPED", "OCR_QUALITY_STOP_WINDOW")
            for _, f in queue:
                f.cancel()
            executor.shutdown(cancel_futures=True)
    stats.wall_s = round(time.perf_counter() - t_start, 2)
    return stats


def summarize(stats: OcrStats) -> dict[str, Any]:
    lat = sorted(stats.latencies_ms)

    def pct(p: float) -> int | None:
        return lat[min(len(lat) - 1, int(p * len(lat)))] if lat else None

    return {"tasks": stats.tasks, "cached": stats.cached, "called": stats.called, "ok": stats.ok,
            "failed": stats.failed, "skipped_budget": stats.skipped_budget, "crop_errors": stats.crop_errors,
            "stopped": stats.stopped, "wall_s": stats.wall_s, "pages_done": stats.pages_done,
            "calls_per_s": round(stats.called / stats.wall_s, 3) if stats.wall_s else None,
            "latency_ms_p50": pct(0.5), "latency_ms_p95": pct(0.95), "prompt_tokens": stats.prompt_tokens,
            "completion_tokens": stats.completion_tokens,
            "completion_tokens_per_s": round(stats.completion_tokens / stats.wall_s, 1) if stats.wall_s else None,
            "flags": stats.flags}


def iter_specs(work: Iterable[PageWork]) -> Iterable[OcrTaskSpec]:
    for w in work:
        yield from w.specs
